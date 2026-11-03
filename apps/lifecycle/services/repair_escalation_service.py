"""A repairer cannot repair: factory approval, custody handover and replacement evidence.
Replacement delivery uses existing stock/installation workflows; this endpoint never
fabricates a dispatch, activation, installation, inventory movement or repair fact.
"""
from copy import deepcopy
from django.db import transaction
from django.utils import timezone
from account.models import User
from lifecycle.models import AfterSalesApplication, AfterSalesMedia, Device, OrganizationDevice, LifecycleEvent, CustomerDeviceBinding, InstallationRecord, ActivationRecord

PENDING = 'PENDING_ESCALATION'
HANDOVER = 'AWAITING_FACTORY_HANDOVER'
RECEIPT = 'AWAITING_FACTORY_RECEIPT'
REPLACE = 'AWAITING_REPLACEMENT'
CONFIRM = 'AWAITING_REPLACEMENT_CONFIRM'
ACTIVE = {'CONTACT_INSTALLER', 'REPAIRING'}
ROUTES = {'SERVICE_STATION', 'ORIGINAL_INSTALLER', 'MERCHANT_ELECTRICIAN', 'FACTORY_AFTERSALES'}


def actions(service, app, user):
    factory = service._is_factory_aftersales(app, user)
    stage = app.execution_stage
    es = app.escalation or {}
    identity = service._identity(user)
    same_org = bool(identity and identity.organization_id == app.assigned_organization_id)
    result = {
        'can_escalation_request': stage in ACTIVE and app.dispatch_route in ROUTES
            and not es and same_org and not app.repair_completed_at and app.status == 'PROCESSING' and service._is_assignee(app, user),
        'can_escalation_decide': factory and stage == PENDING,
        'can_escalation_handover': stage == HANDOVER and holder_allowed(service, app, user),
        'can_escalation_receipt': factory and stage == RECEIPT,
        'can_replacement_deliver': factory and stage == REPLACE,
        'can_replacement_confirm': service._is_applicant(app, user) and stage == CONFIRM,
    }

    if es.get('requires_replacement_flow_v6') and es.get('decision')=='FACTORY_REPAIR':
        from account.services.role_delegation_service import identity_has_role
        result['can_escalation_receipt']=bool(identity and identity.organization_id==app.factory_organization_id
            and identity_has_role(identity,'factory_logistics') and stage==RECEIPT)
    if es.get('flow_version') == 2:
        for key in ('can_escalation_handover','can_escalation_receipt','can_replacement_deliver','can_replacement_confirm'):
            result[key]=False
    return result


def holder_allowed(service, app, user):
    es = app.escalation or {}
    if es.get('holder') == 'APPLICANT':
        return service._is_applicant(app, user)
    # Require the actual authenticated submitting repairer; reassignment does not
    # silently authorize a different person to certify physical handover.
    identity = service._identity(user)
    return bool(identity and identity.organization_id == es.get('source_org_id')
                and user.pk == es.get('source_user_id'))


def media_allowed(service, app, user, purpose):
    if (app.escalation or {}).get('flow_version') == 2:
        from lifecycle.services.replacement_flow_service import describe
        flow=describe(app,user)
        v6_recovery = bool(flow and flow.get('protocol_version')==6 and (flow.get('can_complete_repair') or flow.get('can_complete_disassembly')))
        if purpose == 'REPAIR_EVIDENCE' and v6_recovery:return True
        return purpose == 'REPAIR_RETURN_SHIPMENT_DOCUMENT' and bool(flow and
            (flow['can_scan_swap'] or any(l['can_send'] or l['can_receive'] for l in flow['legs']) or any(p.get('can_send') for p in flow.get('material_returns',[]))))
    a = actions(service, app, user)
    return bool(
        purpose == 'REPAIR_EVIDENCE' and a['can_escalation_request']
        or purpose == 'APPLICANT_SHIPMENT_DOCUMENT' and a['can_escalation_handover']
        or purpose == 'REPAIR_RETURN_SHIPMENT_DOCUMENT' and a['can_replacement_deliver']
    )


def _text(data, key, label):
    from lifecycle.services.aftersales_service import AfterSalesError
    value = str(data.get(key) or '').strip()
    if not value or len(value) > 2000:
        raise AfterSalesError(label+'为必填，且不能超过2000字')
    return value


def _new_media(service, app, purpose, after, message, user):
    from lifecycle.services.aftersales_service import AfterSalesError
    if not app.media.filter(purpose=purpose, created_at__gte=after, uploaded_by=user).exists():
        raise AfterSalesError(message)


def _holder_task(service, app, actor):
    es = app.escalation
    uid = app.applicant_id if es['holder'] == 'APPLICANT' else es['source_user_id']
    holder = User.objects.filter(pk=uid, is_active=True).first()
    if not holder or not holder.wx_openid:
        from lifecycle.services.aftersales_service import AfterSalesError
        raise AfterSalesError('设备交接人账号不可用，请先恢复该账号后确认处理')
    service._publish_task(app, step='factory-handover', task_type='AFTERSALES_FACTORY_HANDOVER',
        title=('返厂维修：请安排原设备返厂' if es.get('decision')=='FACTORY_REPAIR' else '请交接原设备至厂家'), summary=service._summary(app)+'｜'+es['receiving_address'],
        recipients_openids=[holder.wx_openid], actor_openid=actor.wx_openid or '')


@transaction.atomic
def execute(*, application_id, user, data):
    from lifecycle.services.aftersales_service import AfterSalesService as S, AfterSalesError
    app = AfterSalesApplication.objects.select_for_update().select_related(
        'device__batch__manufacturer', 'assigned_user', 'assigned_organization', 'factory_organization', 'applicant'
    ).filter(pk=application_id).first()
    if not app:
        raise AfterSalesError('售后申请不存在')
    # Existing tenant/device scopes also enforce revoked driver access.
    if not S.can_view_application(application=app, user=user):
        raise AfterSalesError('无权处理该售后申请')
    action = str(data.get('action') or '')
    from lifecycle.services import factory_transport_service as F
    if action in F.ACTIONS:return F.execute(application_id,user,data)
    if F.get(app) and action not in {'REQUEST','DECIDE'}:raise AfterSalesError('本单已使用厂家物流返件流程，请刷新后按交接段操作')
    permissions = actions(S, app, user)
    mapping = {'REQUEST':'can_escalation_request', 'DECIDE':'can_escalation_decide',
               'HANDOVER':'can_escalation_handover', 'RECEIVE':'can_escalation_receipt',
               'DELIVER_REPLACEMENT':'can_replacement_deliver', 'CONFIRM_REPLACEMENT':'can_replacement_confirm'}
    from lifecycle.services.replacement_flow_service import V2_ACTIONS
    from lifecycle.services.replacement_v6_service import ACTIONS as V6_ACTIONS
    V2_ACTIONS = V2_ACTIONS | V6_ACTIONS
    if action not in V2_ACTIONS and (action not in mapping or not permissions[mapping[action]]):
        raise AfterSalesError('当前账号或售后阶段不允许此操作，请刷新页面')
    from lifecycle.services.replacement_flow_service import execute as replacement_execute, approve
    if action in V2_ACTIONS:
        from lifecycle.models import ReplacementFlow
        if action=='UPGRADE_REPLACEMENT_PROTOCOL' or ReplacementFlow.objects.filter(application=app,protocol_version__gte=6).exists():
            from lifecycle.services.replacement_v6_service import execute as v6_execute
            return v6_execute(application_id=application_id,user=user,data=data)
        return replacement_execute(application_id=application_id,user=user,data=data)
    if action == 'DECIDE' and data.get('decision') == 'REPLACEMENT' and (data.get('replacement_flow_version') in {2,3} or (app.escalation or {}).get('requires_replacement_flow_v2')):
        if data.get('replacement_flow_version')==3 or (app.escalation or {}).get('requires_replacement_flow_v6'):
            from lifecycle.services.replacement_v6_service import approve as v6_approve
            return v6_approve(app,user,data)
        return approve(app,user,data)
    if (app.escalation or {}).get('flow_version') == 2:
        error_message = '新版换新须使用工作流内的扫码、逐段发出和签收操作'
        raise AfterSalesError(error_message)
    identity = S._identity(user)
    now = timezone.now()
    es = deepcopy(app.escalation or {})
    event = {}
    if action == 'REQUEST':
        requested = data.get('requested')
        if requested not in {'REPLACEMENT', 'FACTORY_REPAIR'}:
            raise AfterSalesError('请选择售后换新或返厂维修')
        if data.get('factory_return_route','DIRECT') not in {'DIRECT','CHAIN'}:raise AfterSalesError('请选择直接寄厂或逐级返厂维修')
        holder = data.get('holder')
        if holder not in {'APPLICANT', 'EXECUTOR'}:
            raise AfterSalesError('请选择原设备实际持有人')
        reason = _text(data, 'reason', '无法维修原因')
        S._require_media(app, AfterSalesMedia.Purpose.REPAIR_EVIDENCE, '请先上传无法维修的现场图片或视频')
        # Never set repair_completed_by/at or replacement quantities here.
        es = {'requested':requested, 'reason':reason, 'holder':holder, 'requires_replacement_flow_v2':True, 'requires_replacement_flow_v6':True, 'requires_factory_transport':True,'factory_return_route':data.get('factory_return_route','DIRECT'),
              'source_user_id':user.pk, 'source_org_id':identity.organization_id,
              'source_route':app.dispatch_route, 'source_assignee':deepcopy(app.assignee_snapshot),
              'source_station_id':(app.assignee_snapshot or {}).get('service_station_application_id'),
              'requested_at':now.isoformat(), 'evidence_ids':list(app.media.filter(purpose='REPAIR_EVIDENCE').values_list('pk',flat=True))}
        app.execution_stage = PENDING
        app.escalation = es
        app.save()
        S._complete_assignee_workorder(app, actor_openid=user.wx_openid or '')
        S._complete_tasks(app, steps=['repair-execution'], actor_openid=user.wx_openid or '')
        S._publish_factory_workorder(app, topic='unable-repair-request', title='无法维修：待确认换新或返厂',
            summary=S._summary(app)+'｜'+reason, actor_openid=user.wx_openid or '', need_action=True)
        event = {'requested':requested,'reason':reason,'holder':holder}
    elif action == 'DECIDE':
        decision = data.get('decision')
        allowed = {'FACTORY_REPAIR','REPLACEMENT'} if es['requested']=='REPLACEMENT' else {'FACTORY_REPAIR'}
        if decision not in allowed:
            raise AfterSalesError('只能同意申请的换新或转返厂维修，不设置驳回')
        address = _text(data, 'receiving_address', '厂家接收地址、联系人和电话')
        es.update(decision=decision, decided_by=user.pk, decided_at=now.isoformat(), receiving_address=address,
                  decision_note=str(data.get('note') or '').strip())
        app.dispatch_route = 'FACTORY_AFTERSALES'
        app.assigned_user = user
        app.assigned_organization = app.factory_organization
        app.assignee_snapshot = S._contact_snapshot(user=user, organization=app.factory_organization,
            role_code=identity.role.code, role_name=identity.role.name, fallback_address=address)
        app.execution_stage = HANDOVER
        app.status = 'PROCESSING'
        app.escalation = es
        app.save()
        S._complete_factory_workorder(app, actor_openid=user.wx_openid or '')
        if decision=='FACTORY_REPAIR' and es.get('requires_factory_transport'):
            from lifecycle.services.factory_transport_service import begin
            begin(app,user,'REPAIR',es.get('factory_return_route','DIRECT'))
            S._event(app,'FACTORY_REPAIR_TRANSPORT_APPROVED',user=user,payload={'route':es.get('factory_return_route','DIRECT')})
            return S.serialize(app)
        _holder_task(S, app, user)
        S._publish_applicant_notice(app, topic='unable-repair-approved', title='厂家已确认'+('售后换新' if decision=='REPLACEMENT' else '返厂维修'),
            summary=S._summary(app)+'｜请按实际持有情况交接原设备', actor_openid=user.wx_openid or '')
        event = {'decision':decision,'receiving_address':address}
    elif action == 'HANDOVER':
        method = data.get('handover_method')
        if method not in {'SHIPMENT','IN_PERSON'}:
            raise AfterSalesError('请选择寄送或现场交接')
        info = {'method':method, 'note':_text(data, 'handover_note', '交接说明')}
        if method=='SHIPMENT':
            info.update(S._logistics_payload(data))
        _new_media(S, app, 'APPLICANT_SHIPMENT_DOCUMENT', es['decided_at'], '请上传本次寄送单据或现场交接凭证', user)
        es.update(handover=info, handed_over_at=now.isoformat(), handed_over_by=user.pk)
        app.execution_stage = RECEIPT
        app.escalation=es;app.save()
        S._complete_tasks(app, steps=['factory-handover'], actor_openid=user.wx_openid or '')
        S._publish_factory_workorder(app, topic='escalation-receipt', title='原设备返厂待确认收件',
            summary=S._summary(app), actor_openid=user.wx_openid or '', need_action=True)
        if es.get('requires_replacement_flow_v6') and es.get('decision')=='FACTORY_REPAIR':
            from account.models import UserOrganization
            recipients=list(UserOrganization.objects.filter(organization=app.factory_organization,is_active=True,user__is_active=True,
                role__is_active=True,role__code__in=['factory_aftersales','factory_logistics']).exclude(user__wx_openid='').values_list('user__wx_openid',flat=True))
            S._publish_task(app,step='direct-repair-pickup',task_type='AFTERSALES_DIRECT_REPAIR_PICKUP',title='返厂维修：待厂家物流取货确认',
                summary=S._summary(app),recipients_openids=recipients,actor_openid=user.wx_openid or '')
        event=info
    elif action == 'RECEIVE':
        es.update(factory_received_at=now.isoformat(), factory_received_by=user.pk,
                  receipt_note=_text(data, 'receipt_note', '厂家收件说明'))
        direct_repair=es.get('requires_replacement_flow_v6') and es.get('decision')=='FACTORY_REPAIR'
        if direct_repair:
            if data.get('confirmed') is not True:raise AfterSalesError('请实际取货核对后确认')
            # Logistics records custody; the assigned aftersales manager keeps the repair task.
            S._complete_tasks(app,steps=['direct-repair-pickup'],actor_openid=user.wx_openid or '')
        else:
            app.assigned_user=user
            app.assignee_snapshot=S._contact_snapshot(user=user, organization=app.factory_organization,
                role_code=identity.role.code, role_name=identity.role.name, fallback_address=es['receiving_address'])
        app.execution_stage = REPLACE if es['decision']=='REPLACEMENT' else 'REPAIRING'
        app.escalation=es;app.save()
        S._complete_factory_workorder(app, actor_openid=user.wx_openid or '')
        if es['decision']=='REPLACEMENT':
            S._publish_factory_workorder(app, topic='replacement-delivery', title='售后换新：待交付新设备',
                summary=S._summary(app), actor_openid=user.wx_openid or '', need_action=True)
        else:
            S._publish_assignee_workorder(app, topic='factory-repair-required', title='返厂设备待您维修',
                summary=S._summary(app), actor_openid=user.wx_openid or '', need_action=True)
        event={'receipt_note':es['receipt_note']}
    elif action == 'DELIVER_REPLACEMENT':
        sn=_text(data, 'new_device_sn', '换新设备SN')
        device=Device.objects.select_for_update().select_related('batch__manufacturer').filter(sn=sn).first()
        if not device or device.pk==app.device_id or device.product_model_id!=app.device.product_model_id or device.batch.manufacturer.get_factory_root().pk!=app.factory_organization_id:
            raise AfterSalesError('换新设备必须是本厂家同型号的另一台有效设备')
        # This is delivery evidence, not a hidden bypass around stock/installation.
        record=OrganizationDevice.objects.select_for_update().filter(device=device,organization=app.customer_organization).first()
        if not record:
            raise AfterSalesError('请先通过现有设备流转流程将新设备交付本用户组织，再登记换新')
        if (CustomerDeviceBinding.objects.filter(device=device, status='ACTIVE', accepted_at__lt=es['requested_at']).exists()
                or InstallationRecord.objects.filter(device=device, status='INSTALLED', created_at__lt=es['requested_at']).exists()
                or ActivationRecord.objects.filter(device=device, status='ACTIVATED', created_at__lt=es['requested_at']).exists()):
            raise AfterSalesError('该设备在本次换新申请前已有在用记录，不能作为新设备交付')
        if AfterSalesApplication.objects.filter(replacement_device=device).exclude(pk=app.pk).exists():
            raise AfterSalesError('该设备已经用于另一张换新单')
        if AfterSalesApplication.objects.filter(device=device).exclude(status__in=['CLOSED','CANCELLED','DRAFT']).exists():
            raise AfterSalesError('换新设备存在未完结售后')
        _new_media(S, app, 'REPAIR_RETURN_SHIPMENT_DOCUMENT', es['factory_received_at'], '请上传本次新设备交付凭证', user)
        es.update(new_device_sn=sn,new_device_id=device.pk, replacement_delivered_at=now.isoformat(),
                  replacement_delivered_by=user.pk, delivery_note=_text(data,'delivery_note','换新交付说明'),
                  old_device_disposition='RETURNED_FOR_REPLACEMENT')
        app.replacement_device=device
        app.execution_stage=CONFIRM
        # No 7-day automatic confirmation for replacement SN verification.
        app.applicant_confirm_due_at=None
        app.escalation=es;app.save()
        S._complete_factory_workorder(app, actor_openid=user.wx_openid or '')
        S._publish_applicant_workorder(app,topic='replacement-confirm',title='请核对换新设备SN并确认收货',
            summary=S._summary(app)+'｜新设备SN：'+sn,actor_openid=user.wx_openid or '',need_action=True)
        event={'old_device_sn':app.device.sn,'new_device_sn':sn,'delivery_note':es['delivery_note']}
    elif action == 'CONFIRM_REPLACEMENT':
        if data.get('confirmed') is not True or str(data.get('new_device_sn') or '').strip()!=es['new_device_sn']:
            raise AfterSalesError('请核对实际收到的新设备SN并明确确认收货')
        if not OrganizationDevice.objects.filter(device_id=es['new_device_id'],organization=app.customer_organization).exists():
            raise AfterSalesError('新设备当前归属已改变，请联系厂家核对')
        es.update(replacement_confirmed_at=now.isoformat(),replacement_confirmed_by=user.pk)
        app.applicant_confirmed_at=now;app.applicant_confirm_mode='MANUAL'
        app.status='PENDING_FACTORY_CLOSE';app.execution_stage='FINAL_DIAGNOSIS'
        app.escalation=es;app.save()
        S._complete_applicant_workorder(app,actor_openid=user.wx_openid or '')
        S._send_factory_close_task(app,actor_openid=user.wx_openid or '',title='换新收货已确认，待厂家归因结案')
        event={'old_device_sn':app.device.sn,'new_device_sn':es['new_device_sn']}
    S._event(app,'REPAIR_ESCALATION_'+action,user=user,identity=identity,payload=event)
    if action in {'DELIVER_REPLACEMENT','CONFIRM_REPLACEMENT'}:
        for device_id in (app.device_id,app.replacement_device_id):
            LifecycleEvent.objects.create(device_id=device_id,event_type='AFTERSALE_TRANSFER',operator=user,
                organization=app.factory_organization,data={'application_id':app.pk,'action':action,**event})
    return S.serialize(app)
