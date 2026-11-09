"""Replacement custody, non-financial stock facts and inherited activation.
QR codes identify immutable physical devices; user bindings move, QR history does not.
"""
from copy import deepcopy
from django.db import transaction
from django.utils import timezone
from django.db.models import Q
from account.models import Organization, OrganizationRelation, UserOrganization
from account.services.auth_service import get_active_identity
from account.services.role_delegation_service import (
    identity_has_role, identity_can_perform_shared_operation,
    shared_operation_recipient_role_codes, vacant_role_manager_openids,
)
from lifecycle.models import (AfterSalesApplication, ReplacementFlow, ReplacementLeg, ReplacementInventoryEntry,
    Device, DeviceState, OrganizationDevice, QRCode, ActivationRecord, InstallationRecord, CustomerDeviceBinding,
    LifecycleEvent, AfterSalesMedia, DriverVehicleBinding, VehicleDeviceAssignment)
from lifecycle.services.scan_service import ScanService, QRCodeResolveError
from quality_trace.services import enforce_device_operation

V2_ACTIONS={'CONFIRM_FACTORY_OLD_RECEIPT','SCAN_SWAP','SEND_REPLACEMENT_LEG','RECEIVE_REPLACEMENT_LEG','START_REPLACEMENT_FLOW'}
ORG_ROLES={'merchant_owner','merchant_sales','merchant_stock','merchant_electrician','service_electrician',
    'factory_aftersales','factory_stock','factory_logistics','factory_admin'}


def error(message):
    from lifecycle.services.aftersales_service import AfterSalesError
    raise AfterSalesError(message)


def chain_for(app, org_id):
    """Only a unique active recorded channel is accepted; never invent a direct shipment."""
    root=app.factory_organization_id;path=[org_id];seen={org_id}
    while path[-1]!=root:
        parents=list(OrganizationRelation.objects.filter(target_organization_id=path[-1],is_active=True,
            relation_type__in=['DISTRIBUTION','SERVICE'],source_organization__is_active=True,
            target_organization__is_active=True).values_list('source_organization_id',flat=True).distinct())
        parents=[p for p in parents if p==root or Organization.objects.filter(pk=p,factory_root_id=root).exists()]
        if len(parents)!=1 or parents[0] in seen or len(path)>20:
            error('换新组织缺少唯一有效的厂家渠道链路，请先核对组织关系，不能越级直发')
        seen.add(parents[0]);path.append(parents[0])
    return list(reversed(path))


def _org_user(user, org_id):
    i=get_active_identity(user)
    return bool(i and user.is_active and i.organization_id==org_id and
        (i.role.is_owner_role or i.role.code in ORG_ROLES or identity_can_perform_shared_operation(i,'goods_transfer_receipt')
         or identity_has_role(i,'factory_aftersales')))


def _order_supply_merchant(leg, organization):
    if leg.direction != 'SUPPLY' or organization.organization_type.code != 'MERCHANT':
        return False
    return leg.flow.legs.filter(
        direction='SUPPLY', sequence=1,
        supply_plan_items__status__in=('RESERVED', 'SENT'),
        supply_plan_items__plan__status='OPEN',
        supply_plan_items__plan__normal_order__isnull=False,
        supply_plan_items__plan__factory_id=leg.flow.application.factory_organization_id,
    ).exists()


def _handoff_user(leg, user, organization):
    if not _order_supply_merchant(leg, organization):
        return _org_user(user, organization.pk)
    identity = get_active_identity(user)
    return bool(
        identity and user.is_active and identity.organization_id == organization.pk
        and identity_can_perform_shared_operation(identity, 'goods_transfer_receipt')
    )


def _event(app, device, action, user, **data):
    i=get_active_identity(user)
    return LifecycleEvent.objects.create(device=device,event_type='AFTERSALE_TRANSFER',operator=user,
        organization=i.organization,data={'application_id':app.pk,'replacement_action':action,**data})


@transaction.atomic
def _notify(app, org_id, title, key, actor, *, leg=None):
    from lifecycle.services.aftersales_service import AfterSalesService as S
    if org_id==app.customer_organization_id:
        recipients=[app.applicant.wx_openid]
    else:
        recipients=list(UserOrganization.objects.filter(organization_id=org_id,is_active=True,user__is_active=True,
            role__is_active=True).filter(Q(role__is_owner_role=True)|Q(role__code__in=ORG_ROLES)).exclude(user__wx_openid='').values_list('user__wx_openid',flat=True))
    v6=ReplacementFlow.objects.filter(application=app,protocol_version__gte=6).first()
    if v6 and org_id==app.factory_organization_id and ('-receive' in key or key=='factory-pickup'):
        recipients=list(UserOrganization.objects.filter(organization_id=org_id,is_active=True,user__is_active=True,
            role__is_active=True,role__code__in=['factory_aftersales','factory_logistics']).exclude(user__wx_openid='').values_list('user__wx_openid',flat=True))
        recipients.extend(vacant_role_manager_openids(
            app.factory_organization, {"factory_aftersales", "factory_logistics"},
        ))
    payload = None
    if leg is not None and leg.direction == 'SUPPLY':
        organization = leg.to_organization if org_id == leg.to_organization_id else leg.from_organization
        if _order_supply_merchant(leg, organization):
            roles = shared_operation_recipient_role_codes(org_id, 'goods_transfer_receipt')
            recipients = list(UserOrganization.objects.filter(
                organization_id=org_id, is_active=True, user__is_active=True,
                role__is_active=True, role__code__in=roles,
            ).exclude(user__wx_openid='').values_list('user__wx_openid', flat=True))
            recipients.extend(vacant_role_manager_openids(organization, roles))
        payload = {'replacement_leg_id': leg.pk, 'root_organization_id': app.factory_organization_id}
    from lifecycle.services.replacement_task_scope import notification_scope, split_recipients, split_scan_recipients
    scope = notification_scope(app, org_id, key, leg)
    if scope is None:
        S._publish_task(app,step='replacement-'+key,task_type='AFTERSALES_REPLACEMENT_CHANNEL',title=title,
            summary=S._summary(app),recipients_openids=recipients,actor_openid=actor.wx_openid or '',payload=payload)
        return
    # Keep the original action key so the real handoff still completes it.
    # Other original recipients retain a separate knowledge copy in this case.
    if scope['action'] == 'scan':
        action_recipients, notice_recipients = split_scan_recipients(app, scope['flow'], recipients)
        payload = {**(payload or {}), 'replacement_flow_id': scope['flow'].pk,
                   'execution_organization_id': scope['flow'].execution_organization_id}
    else:
        recipients.extend(vacant_role_manager_openids(app.factory_organization, {scope['role']}))
        action_recipients, notice_recipients = split_recipients(app, scope['role'], recipients)
    payload = {**(payload or {}), 'root_organization_id': app.factory_organization_id}
    if scope['leg'] is not None:
        payload['replacement_leg_id'] = scope['leg'].pk
    if action_recipients:
        S._publish_task(app,step='replacement-'+key,task_type='AFTERSALES_REPLACEMENT_CHANNEL',title=title,
            summary=S._summary(app),recipients_openids=action_recipients,actor_openid=actor.wx_openid or '',payload=payload,
            need_action=True,mark_workitem_done_immediately=False)
    if notice_recipients:
        notice_title = '现场换新已获批准，服务人员待办理' if scope['action'] == 'scan' else title
        S._publish_task(app,step='replacement-'+key+'-notice',task_type='AFTERSALES_REPLACEMENT_CHANNEL',title=notice_title,
            summary=S._summary(app),recipients_openids=notice_recipients,actor_openid=actor.wx_openid or '',payload=payload,
            need_action=False,mark_workitem_done_immediately=True)


def _finish(app, leg, step, actor):
    from lifecycle.services.aftersales_service import AfterSalesService as S
    S._complete_tasks(app,steps=['replacement-'+str(leg.pk)+'-'+step],actor_openid=actor.wx_openid or '')


def _stock(flow, leg, org_id, device, kind, quantity, user):
    # Factory swap/return is statistical only. No financial ledger is imported here.
    if flow.mode=='FACTORY' or org_id==flow.application.factory_organization_id or Organization.objects.filter(pk=org_id,organization_type__code='CUSTOMER').exists():return
    ReplacementInventoryEntry.objects.get_or_create(leg=leg,organization_id=org_id,kind=kind,
        defaults={'flow':flow,'device':device,'quantity':quantity,'actor':user})


def _proof(app, purpose, user, after):
    qs=app.media.filter(purpose=purpose,uploaded_by=user,created_at__gte=after)
    ids=list(qs.values_list('pk',flat=True))
    if not ids:error('请上传本次交接的图片、物流单据或签收凭证')
    return ids


def _old_return_evidence(flow,leg,user,data):
    from lifecycle.services import aftersales_evidence_service as E
    # Explicit attachment IDs bind this submission to two distinct kinds of proof.
    def ids(name):
        raw=data.get(name)
        if not isinstance(raw,list) or not raw or any(type(x) is not int or x<=0 for x in raw):
            error('请分别上传本次退旧的物流单和设备图片')
        return set(raw)
    images=ids('device_image_ids');documents=ids('logistics_document_ids')
    if images & documents:error('物流单和设备图片须分别上传，不能重复使用同一附件')
    if len(images | documents)>E.LIMIT:error('本段物流单和设备图片合计最多9个')
    after=flow.created_at
    previous=flow.legs.filter(direction='OLD',sequence__lt=leg.sequence).order_by('-sequence').first()
    if previous and previous.received_at:after=max(after,previous.received_at)
    rows=E.require_ids(flow.application,user,sorted(images),key=E.old_scope(leg),kind='DEVICE',after=after)
    rows+=E.require_ids(flow.application,user,sorted(documents),key=E.old_scope(leg),kind='LOGISTICS',after=after)
    if any(x.media_type!='IMAGE' for x in rows if x.pk in images):error('设备凭证必须为图片')
    if any(x.media_type not in {'IMAGE','DOCUMENT'} for x in rows if x.pk in documents):error('物流单须为图片或单据')
    return {'method':'SHIPMENT','confirmation_basis':'RETURN_DOCUMENT_AND_DEVICE_IMAGE',
        'device_image_ids':sorted(images),'logistics_document_ids':sorted(documents),'evidence_ids':sorted(images|documents),
        'note':str(data.get('note') or '').strip(),'company':str(data.get('logistics_company') or '').strip(),
        'tracking_no':str(data.get('tracking_no') or '').strip()}


def _logistics(app, user, data, after):
    method=data.get('handover_method','SHIPMENT')
    identity=get_active_identity(user)
    if identity and identity.organization_id==app.factory_organization_id and method!='SHIPMENT':error('厂家发出固定使用物流寄送')
    if method not in {'SHIPMENT','IN_PERSON'}:error('请选择物流或现场交接')
    info={'method':method,'note':str(data.get('note') or '').strip()}
    if not info['note']:error('请填写交接说明')
    info['evidence_ids']=_proof(app,'REPAIR_RETURN_SHIPMENT_DOCUMENT',user,after)
    if method=='SHIPMENT':
        info.update(company=str(data.get('logistics_company') or '').strip(),tracking_no=str(data.get('tracking_no') or '').strip())
        if not info['company'] or not info['tracking_no']:error('请填写物流公司及单号')
    return info


def _scan(raw):
    try:code=ScanService.extract_code(str(raw or ''))
    except QRCodeResolveError as exc:error(str(exc))
    qr=QRCode.objects.select_for_update().select_related('device__batch__manufacturer').filter(code=code,status='ACTIVE').first()
    if not qr or not qr.device_id:error('请扫描有效的新设备生命周期二维码，不能用设备编码或手输SN代替')
    return qr.device


def _new_stock(app, org_id, raw, user):
    device=_scan(raw)
    # Stable device lock shared with normal stock/dispatch operations.
    device=Device.objects.select_for_update().select_related('batch__manufacturer').get(pk=device.pk)
    if device.pk==app.device_id or device.product_model_id!=app.device.product_model_id or device.batch.manufacturer.get_factory_root().pk!=app.factory_organization_id:
        error('请选择本厂家同型号的另一台设备')
    record=OrganizationDevice.objects.select_for_update().filter(device=device,organization_id=org_id,status='IN_STOCK').first()
    state=DeviceState.objects.select_for_update().filter(device=device).first()
    if not record or not state or state.status not in {'IN_STOCK','PRODUCED','CREATED'} or state.activation_status=='ACTIVATED':error('该设备不是本组织可调用的未激活库存')
    if (ActivationRecord.objects.filter(device=device).exists() or InstallationRecord.objects.filter(device=device).exists()
        or CustomerDeviceBinding.objects.filter(device=device).exists() or AfterSalesApplication.objects.filter(replacement_device=device).exists()
        or ReplacementLeg.objects.filter(direction='SUPPLY',device=device).exclude(status='RECEIVED').exists()):error('该设备已有在用、换新或补发记录，不能重复调用')
    from sales.models import OrderPlanShipmentDevice, OrderPlanReturnInventoryAllocation
    if OrderPlanShipmentDevice.objects.filter(device=device,shipment__status__in=['PLANNED','AUTHORIZED','OUTBOUNDED','SHIPPED']).exists():
        error('该设备已被正常销售批次占用，不能调用换新')
    if OrderPlanReturnInventoryAllocation.objects.filter(source_device=device,status__in=['RESERVED','READY_TO_SHIP']).exists():
        error('该设备已被订单预占，不能调用换新')
    enforce_device_operation(device_id=device.pk,operation='SHIPMENT',user=user,context={'source':'AFTERSALES_REPLACEMENT','application_id':app.pk})
    return device,record,state


def _previous_done(leg):
    return not leg.flow.legs.filter(direction=leg.direction,sequence__lt=leg.sequence).exclude(status='RECEIVED').exists()


def can_send(leg,user):
    if leg.flow.protocol_version>=6:
        from lifecycle.services.replacement_v6_service import can_send as v6_can_send
        return v6_can_send(leg,user)
    flow=leg.flow
    if leg.direction=='USER' or flow.application.status=='CANCELLED':return False
    if not Organization.objects.filter(pk=leg.to_organization_id,is_active=True).filter(Q(pk=flow.application.factory_organization_id)|Q(factory_root_id=flow.application.factory_organization_id)).exists():return False
    sender = _handoff_user(leg, user, leg.from_organization)
    if leg.direction=='SUPPLY' and leg.sequence==1:
        from sales.services.replacement_supply_service import can_dispatch
        sender=can_dispatch(leg,user)
    if leg.from_organization_id==flow.application.customer_organization_id: sender=user.pk==flow.application.applicant_id
    return (leg.status=='PENDING' and sender and _previous_done(leg)
        and (leg.direction!='SUPPLY' or flow.swapped_at is not None)
        and (leg.direction!='OLD' or flow.mode=='FACTORY' or flow.swapped_at is not None))


def can_receive(leg,user):
    if leg.flow.protocol_version>=6:
        from lifecycle.services.replacement_v6_service import can_receive as v6_can_receive
        return v6_can_receive(leg,user)
    app=leg.flow.application
    if app.status=='CANCELLED':return False
    if leg.direction=='USER':
        return leg.status=='SENT' and user.pk==app.applicant_id
    if leg.direction=='OLD' and leg.to_organization_id==app.factory_organization_id:
        from lifecycle.services.replacement_v6_service import logistics
        return leg.status=='SENT' and logistics(user,app.factory_organization_id)
    return leg.status=='SENT' and _handoff_user(leg, user, leg.to_organization)


@transaction.atomic
def approve(app,user,data):
    from lifecycle.services.aftersales_service import AfterSalesService as S, AfterSalesError
    if not S._is_factory_aftersales(app,user):error('仅厂家售后负责人或获授权人员可确认换新')
    es=deepcopy(app.escalation or {})
    if es.get('requested')!='REPLACEMENT' or app.execution_stage!='PENDING_ESCALATION':error('当前无待确认的换新申请')
    mode=data.get('execution_mode') or ('FACTORY' if es['source_org_id']==app.factory_organization_id else 'LOCAL')
    if mode not in {'LOCAL','FACTORY'}:error('请选择组织库存换新或厂家换新')
    org_id=es['source_org_id'] if mode=='LOCAL' else app.factory_organization_id
    if mode=='LOCAL' and org_id==app.factory_organization_id:error('厂家换新请选择厂家方式')
    origin=es['source_org_id']
    protocol=int(data.get('_trusted_protocol',5))
    if protocol>=6 and mode=='FACTORY':
        # Preserve the old channel history, but never use it to create direct-return channel obligations.
        try:chain=chain_for(app,origin)
        except AfterSalesError:chain=[]
    else:chain=chain_for(app,origin)
    old=ActivationRecord.objects.select_for_update().filter(device=app.device,status='ACTIVATED').first()
    if not old or old.activation_method not in {'VEHICLE_PLATE','DEVICE_CODE'}:error('原设备无有效的首次售后激活事实，不能自动承接，请先核对')
    snap={'vehicle_no':old.vehicle_no,'vehicle_info':old.vehicle_info,'location':old.location,
          'physical_device_code':old.physical_device_code_snapshot}
    flow=ReplacementFlow.objects.create(application=app,protocol_version=protocol,recovery={'state':'WAIT_PICKUP'} if protocol>=6 else {},mode=mode,execution_organization_id=org_id,chain=chain,
        activation_method=old.activation_method,first_activation_at=old.activation_time,activation_snapshot=snap)
    # Start where the physical old device is held, then every channel hop to factory.
    oldpath=list(reversed(chain))
    if protocol>=6:
        if mode=='LOCAL':
            es['holder']='EXECUTOR'
        else:
            holder=app.customer_organization_id if es['holder']=='APPLICANT' else origin
            oldpath=[holder,app.factory_organization_id] if holder!=app.factory_organization_id else [holder]
    elif es['holder']=='APPLICANT':oldpath=[app.customer_organization_id]+oldpath
    if mode=='FACTORY' and len(oldpath)==1:
        if not S._has_event(app,'REPAIRER_RECEIPT_CONFIRMED') and not es.get('factory_received_at'):
            error('请先完成厂家收件确认，再办理厂家直接换新')
        OrganizationDevice.objects.filter(device=app.device).update(organization=app.factory_organization,status='RETURNED_TO_FACTORY')
        DeviceState.objects.filter(device=app.device).update(current_organization=app.factory_organization,status='RETURNED_TO_FACTORY')
    for seq,(source,target) in enumerate(zip(oldpath,oldpath[1:]),1):
        ReplacementLeg.objects.create(flow=flow,direction='OLD',sequence=seq,from_organization_id=source,to_organization_id=target,device=app.device)
    if mode=='LOCAL':
        for seq,(source,target) in enumerate(zip(chain,chain[1:]),1):
            ReplacementLeg.objects.create(flow=flow,direction='SUPPLY',sequence=seq,from_organization_id=source,to_organization_id=target)
    es.update(flow_version=2,decision='REPLACEMENT',execution_mode=mode,decided_at=timezone.now().isoformat(),decided_by=user.pk,
              receiving_address=str(data.get('receiving_address') or '').strip())
    app.escalation=es;app.status='PROCESSING';app.execution_stage='AWAITING_REPLACEMENT'
    app.assigned_organization_id=org_id
    if mode=='FACTORY':
        app.assigned_user=user;app.dispatch_route='FACTORY_AFTERSALES'
        identity=get_active_identity(user)
        app.assignee_snapshot=S._contact_snapshot(user=user,organization=app.factory_organization,
            role_code=identity.role.code,role_name=identity.role.name,fallback_address=es['receiving_address'])
    else:
        app.assigned_user_id=es['source_user_id'];app.dispatch_route=es['source_route'];app.assignee_snapshot=es['source_assignee']
    app.save()
    S._complete_factory_workorder(app,actor_openid=user.wx_openid or '')
    S._event(app,'REPLACEMENT_FLOW_APPROVED',user=user,payload={'mode':mode,'chain':chain})
    if mode=='FACTORY' and (es.get('requires_factory_transport') or data.get('factory_transport_version')==1):
        from lifecycle.services.factory_transport_service import begin
        begin(app,user,'REPLACEMENT','DIRECT')
        return S.serialize(app)
    _notify(app,org_id,'售后换新：请扫描库存新机生命周期码','scan',user)
    if protocol>=6 and mode=='FACTORY' and not flow.legs.filter(direction='OLD').exists():
        _notify(app,app.factory_organization_id,'旧机已在厂家，待物流核验取货','factory-pickup',user)
    if mode=='FACTORY':
        first=flow.legs.filter(direction='OLD').first()
        if first:_notify(app,first.from_organization_id,'旧机按链路返厂：待发出',str(first.pk)+'-send',user)
    return S.serialize(app)


def _stock_state(device,org_id,status,event):
    OrganizationDevice.objects.filter(device=device).update(organization_id=org_id,status=status,last_event=event,received_at=timezone.now())
    DeviceState.objects.filter(device=device).update(current_organization_id=org_id,status=('RETURNED_TO_FACTORY' if status=='RETURNED_TO_FACTORY' else ('REPAIRING' if status=='RETURNED' else status)),last_event=event,last_event_time=timezone.now())


def _apply_swap(flow,leg,user,onsite=False):
    app=flow.application;new=leg.device;now=timezone.now()
    # Snapshot old user/vehicle relations. Historical installation/activation rows stay intact.
    binding=CustomerDeviceBinding.objects.select_for_update().filter(device=app.device,customer_organization=app.customer_organization,status='ACTIVE').first()
    if not binding:error('原设备用户绑定已改变，请核对后再签收换新')
    oldact=ActivationRecord.objects.select_for_update().filter(device=app.device,status='ACTIVATED').first()
    if not oldact or oldact.activation_time!=flow.first_activation_at:error('原设备激活事实已改变，请先核对')
    if OrganizationDevice.objects.get(device=new).organization_id!=flow.execution_organization_id:error('新机交付归属已改变')
    event=_event(app,new,'REPLACEMENT_EFFECTIVE',user,old_device_id=app.device_id,old_sn=app.device.sn,new_sn=new.sn,
                 first_activation_at=flow.first_activation_at.isoformat(),activation_method=flow.activation_method)
    new_binding=CustomerDeviceBinding.objects.create(device=new,customer_organization=app.customer_organization,
        source_organization=flow.execution_organization,accepted_by=user,acceptance_mode='LEGACY',
        customer_type=binding.customer_type,vehicle_no=binding.vehicle_no,vehicle=binding.vehicle)
    CustomerDeviceBinding.objects.filter(pk=binding.pk).update(status='INACTIVE')
    VehicleDeviceAssignment.objects.filter(device=app.device,is_current=True).update(is_current=False,ended_at=now)
    if binding.vehicle_id:
        VehicleDeviceAssignment.objects.create(device=new,customer_organization=app.customer_organization,
            vehicle=binding.vehicle,source_binding=new_binding,assigned_by=user,reason='REINSTALL',
            started_at=now,metadata={'replacement_application_id':app.pk,'old_device_id':app.device_id})
    oldinstall=InstallationRecord.objects.filter(device=app.device).first()
    InstallationRecord.objects.create(device=new,organization=flow.execution_organization,operator=app.assigned_user,
        installed_at=now,location=(oldinstall.location if oldinstall else ''),remark='售后换新承接原装车关系，原SN:'+app.device.sn)
    InstallationRecord.objects.filter(device=app.device).update(status='CANCELLED')
    ActivationRecord.objects.filter(pk=oldact.pk).update(status='CANCELLED')
    returned = (flow.mode=='FACTORY' or flow.legs.filter(direction='OLD').exists()) and not flow.legs.filter(direction='OLD').exclude(status='RECEIVED').exists()
    DeviceState.objects.filter(device=app.device).update(status='RETURNED_TO_FACTORY' if returned else 'REPAIRING',activation_status='NOT_ACTIVATED',install_status='NOT_INSTALLED')
    if flow.mode=='LOCAL' and (app.escalation or {}).get('holder')=='EXECUTOR':
        oldevent=_event(app,app.device,'OLD_DEVICE_ACCEPTED_ON_SWAP',user,execution_org=flow.execution_organization_id)
        _stock_state(app.device,flow.execution_organization_id,'RETURNED',oldevent)
        _stock(flow,leg,flow.execution_organization_id,app.device,'LOCAL_OLD_IN',1,user)
    _event(app,app.device,'REPLACED_BY_NEW_DEVICE',user,new_device_id=new.pk,new_sn=new.sn)
    plate=flow.activation_method=='VEHICLE_PLATE'
    if plate:enforce_device_operation(device_id=new.pk,operation='ACTIVATION',user=user,context={'source':'REPLACEMENT_INHERITED_PLATE'})
    if plate:
        activation_event=LifecycleEvent.objects.create(device=new,event_type='ACTIVATE',operator=user,organization=app.customer_organization,
            data={'activation_method':'VEHICLE_PLATE','inherited_first_activation_at':flow.first_activation_at.isoformat(),
                  'replacement_application_id':app.pk,'actual_replacement_at':now.isoformat()})
        act=ActivationRecord.objects.create(device=new,organization=app.customer_organization,operator=user,
            activation_method='VEHICLE_PLATE',vehicle_no=binding.vehicle_no or oldact.vehicle_no,vehicle_info=deepcopy(oldact.vehicle_info),
            location=oldact.location,lifecycle_event=activation_event,remark='换新自动承接首次售后激活时间；实际替换时间见事件')
        ActivationRecord.objects.filter(pk=act.pk).update(activation_time=flow.first_activation_at)
    _stock_state(new,app.customer_organization_id,'SOLD' if plate else 'INSTALLED',event)
    DeviceState.objects.filter(device=new).update(install_status='INSTALLED',activation_status='ACTIVATED' if plate else 'NOT_ACTIVATED',status='ACTIVATED' if plate else 'INSTALLED')
    flow.swapped_at=now;flow.save(update_fields=['swapped_at'])
    es=deepcopy(app.escalation);es.update(replacement_confirmed_at=now.isoformat(),replacement_confirmed_by=user.pk,
        activation_method=flow.activation_method,first_activation_at=flow.first_activation_at.isoformat(),
        needs_device_code_activation=not plate)
    if onsite:
        es.pop('replacement_confirmed_at',None);es.pop('replacement_confirmed_by',None)
        es.update(delivery_completed_at=now.isoformat(),delivery_basis='ONSITE_OPERATOR_SCAN',delivery_completed_by=user.pk)
    app.escalation=es;app.applicant_confirmed_at=None if onsite else now;app.applicant_confirm_mode='' if onsite else 'MANUAL';app.applicant_confirm_due_at=None
    app.status='PENDING_FACTORY_CLOSE';app.execution_stage='FINAL_DIAGNOSIS';app.save()
    from lifecycle.services.aftersales_service import AfterSalesService as S
    if flow.protocol_version<6 or flow.recovery.get('state')=='CLOSED':
        S._send_factory_close_task(app,actor_openid=user.wx_openid or '',title='用户换机已完成，旧机处置已满足，待归因结案')
    _finish(app,leg,'receive',user)
    S._complete_tasks(app,steps=['replacement-scan'],actor_openid=user.wx_openid or '')
    for direction in ('OLD',):
        first=flow.legs.filter(direction=direction).first()
        if first and first.status=='PENDING':_notify(app,first.from_organization_id,'换新'+('旧机返厂' if direction=='OLD' else '待补发')+'：待发出',str(first.pk)+'-send',user)


@transaction.atomic
def execute(*,application_id,user,data):
    from lifecycle.services.aftersales_service import AfterSalesService as S
    app=AfterSalesApplication.objects.select_for_update().select_related('device__batch__manufacturer','factory_organization','applicant','assigned_user').get(pk=application_id)
    if not S.can_view_application(application=app,user=user):error('无权查看该售后单')
    if app.status=='CANCELLED':error('已取消售后单不能办理换新交接')
    if (app.escalation or {}).get('factory_transport'):error('本单由厂家物流返件流程办理，禁止旧版扫码发货或签收入口')
    action=data.get('action')
    if action=='START_REPLACEMENT_FLOW':error('旧版本已审批工单不能自动重建物流事实，请使用新申请或先核对历史交接')
    flow=ReplacementFlow.objects.select_for_update().filter(application=app).first()
    if not flow:error('该单未启用新换新流程')
    now=timezone.now()
    if action=='CONFIRM_FACTORY_OLD_RECEIPT':
        from lifecycle.services.replacement_v6_service import logistics
        last=flow.legs.filter(direction='OLD').order_by('-sequence').first()
        if flow.protocol_version!=5 or flow.mode!='LOCAL' or not last or last.status!='RECEIVED' or flow.recovery.get('picked_up_at') or data.get('confirmed') is not True or not logistics(user,app.factory_organization_id):
            error('仅厂家物流可对历史已签收旧机明确补充当前收货确认，不能重复确认')
        flow.recovery={**flow.recovery,'picked_up_at':now.isoformat(),'picked_up_by':user.pk};flow.save(update_fields=['recovery'])
        S._event(app,'FACTORY_OLD_RECEIPT_CONFIRMED',user=user,payload={'flow_id':flow.pk,'confirmation_basis':'CURRENT_LOGISTICS_ACK_OF_LEGACY_RECEIPT'})
        from sales.services.replacement_supply_service import returned
        returned(flow,user)
        S._send_factory_close_task(app,actor_openid=user.wx_openid or '',title='旧机已由厂家物流确认收到，请处理归因；补发独立安排')
        return S.serialize(app)
    if action=='SCAN_SWAP':
        receiver=get_active_identity(app.applicant)
        if not receiver or receiver.organization_id!=app.customer_organization_id or receiver.role.code not in {'customer_owner','driver'} or not S.can_view_application(application=app,user=app.applicant):
            error('原售后申请人当前无权签收本用户设备，请先核对用户岗位或车辆授权')
        if flow.swapped_at or app.replacement_device_id or not S._is_assignee(app,user) or not _org_user(user,flow.execution_organization_id):error('仅当前换新操作人可扫描调用库存新机，不能重复换机')
        if flow.mode=='FACTORY' and flow.legs.filter(direction='OLD').exclude(status='RECEIVED').exists():error('厂家换新须先完成旧机逐段返厂签收')
        new,record,state=_new_stock(app,flow.execution_organization_id,data.get('qr_text'),user)
        info=_logistics(app,user,data,flow.created_at)
        leg=ReplacementLeg.objects.create(flow=flow,direction='USER',sequence=1,from_organization=flow.execution_organization,
            to_organization=app.customer_organization,device=new,status='SENT',logistics=info,sent_by=user,sent_at=now)
        app.replacement_device=new;es=deepcopy(app.escalation);es.update(new_device_sn=new.sn,new_device_id=new.pk,replacement_delivered_at=now.isoformat())
        app.escalation=es;app.execution_stage='AWAITING_REPLACEMENT_CONFIRM';app.save()
        event=_event(app,new,'LOCAL_SWAP_SEND' if flow.mode=='LOCAL' else 'FACTORY_SWAP_SEND',user,leg_id=leg.pk,**info)
        record.status='TRANSFERRED';record.last_event=event;record.save(update_fields=['status','last_event','updated_at'])
        state.status='SHIPPED';state.last_event=event;state.save(update_fields=['status','last_event','updated_at'])
        if flow.mode=='LOCAL':_stock(flow,leg,flow.execution_organization_id,new,'LOCAL_OUT',-1,user)
        _notify(app,app.customer_organization_id,'请签收换新设备，核对新生命周期码',str(leg.pk)+'-receive',user)
    else:
        try: leg_id=int(data.get('leg_id') or 0)
        except (TypeError,ValueError):error('交接段标识无效')
        leg=flow.legs.select_for_update().select_related('flow__application','device','from_organization','to_organization').filter(pk=leg_id).first()
        if not leg:error('交接段不存在')
        if action=='SEND_REPLACEMENT_LEG':
            if not can_send(leg,user):error('当前账号或前段签收状态不允许发出，禁止越级发货')
            if leg.direction=='SUPPLY' and leg.sequence==1:
                from sales.services.replacement_supply_service import validate_dispatch
                validate_dispatch(leg,user,data)
                device,record,state=_new_stock(app,leg.from_organization_id,data.get('qr_text'),user)
                flow.legs.filter(direction='SUPPLY').update(device=device);leg.device=device
            else:
                device=Device.objects.select_for_update().get(pk=leg.device_id)
                if leg.direction!='OLD' and _scan(data.get('qr_text')).pk!=device.pk:error('扫描设备与本段交接设备不一致')
            if leg.direction=='SUPPLY' and leg.sequence>1:
                enforce_device_operation(device_id=leg.device_id,operation='SHIPMENT',user=user,context={'source':'REPLACEMENT_CHANNEL'})
                if not OrganizationDevice.objects.select_for_update().filter(device=leg.device,organization=leg.from_organization,status='IN_STOCK').exists():error('补发设备不在本组织可用库存')
            shipment_id=data.get('shipment_id')
            if leg.direction=='OLD':
                info=_old_return_evidence(flow,leg,user,data)
            elif shipment_id:
                if leg.direction!='SUPPLY' or leg.sequence!=1:error('仅厂家补发首段可随订单批次发货')
                try: shipment_id=int(shipment_id)
                except (TypeError,ValueError):error('批次标识无效')
                from sales.models import OrderPlanShipment, OrderPlanShipmentDevice
                shipment=OrderPlanShipment.objects.select_for_update().select_related('order_plan').filter(pk=shipment_id,
                    order_plan__factory_organization_id=leg.from_organization_id,order_plan__merchant_organization_id=leg.to_organization_id,
                    status='SHIPPED').first()
                if not shipment or not shipment.logistics_company or not shipment.tracking_no or not shipment.logistics_document_id:error('正常批次尚未发货或缺少完整物流单据')
                if OrderPlanShipmentDevice.objects.filter(device=leg.device).exists():error('补发新机已进入销售发货明细，不能重复记为销售')
                leg.shipment_id=shipment.pk
                info={'method':'SHIPMENT','company':shipment.logistics_company,'tracking_no':shipment.tracking_no,
                      'sales_logistics_document_id':shipment.logistics_document_id,'shared_shipment_id':shipment.pk,'note':'随正常批次补发，非销售设备'}
            elif data.get('_shared_goods_transfer') and flow.protocol_version>=6:
                info={'method':'SHIPMENT','shared_goods_transfer_id':data['_shared_goods_transfer'],
                    'shared_document_ids':data['_shared_document_ids'],'shared_goods_flow_no':data.get('_shared_transfer_no',''),'note':'随本段下发计划补发，不计销售数量/应收'}
            else:info=_logistics(app,user,data,flow.created_at)
            if leg.direction=='SUPPLY' and leg.sequence==1:
                from sales.models import ReplacementSupplyPlanItem
                ReplacementSupplyPlanItem.objects.filter(leg=leg,status='RESERVED').update(status='SENT')
            leg.logistics=info;leg.status='SENT';leg.sent_by=user;leg.sent_at=now;leg.save()
            event=_event(app,leg.device,'CHANNEL_'+leg.direction+'_SEND',user,leg_id=leg.pk,
                from_org=leg.from_organization_id,to_org=leg.to_organization_id,**info)
            if leg.direction=='SUPPLY':
                _stock(flow,leg,leg.from_organization_id,leg.device,'SUPPLY_OUT',-1,user)
                OrganizationDevice.objects.filter(device=leg.device).update(status='TRANSFERRED',last_event=event,delivered_at=now)
                DeviceState.objects.filter(device=leg.device).update(status='SHIPPED',last_event=event)
            else:
                _stock(flow,leg,leg.from_organization_id,leg.device,'OLD_OUT',-1,user)
                DeviceState.objects.filter(device=leg.device).update(status='REPAIRING',last_event=event)
            _finish(app,leg,'send',user)
            _notify(app,leg.to_organization_id,'换新交接待签收：'+leg.device.sn,str(leg.pk)+'-receive',user,leg=leg)
        elif action=='RECEIVE_REPLACEMENT_LEG':
            if not can_receive(leg,user) or data.get('confirmed') is not True:error('仅接收方可以明确签收，不能重复签收')
            note=str(data.get('receipt_note') or '').strip()
            if not note:error('请填写签收核对说明')
            evidence=_proof(app,'REPAIR_RETURN_SHIPMENT_DOCUMENT',user,leg.sent_at)
            if leg.direction=='USER':
                if _scan(data.get('qr_text')).pk!=leg.device_id:error('请核对收到的新机生命周期二维码')
                _apply_swap(flow,leg,user)
            else:
                if leg.sequence>1 and not _previous_done(leg):error('前段未签收')
                event=_event(app,leg.device,'CHANNEL_'+leg.direction+'_RECEIVE',user,leg_id=leg.pk,
                    from_org=leg.from_organization_id,to_org=leg.to_organization_id,evidence_ids=evidence)
                _stock_state(leg.device,leg.to_organization_id,'IN_STOCK' if leg.direction=='SUPPLY' else
                    ('RETURNED_TO_FACTORY' if leg.to_organization_id==app.factory_organization_id else 'RETURNED'),event)
                _stock(flow,leg,leg.to_organization_id,leg.device,'SUPPLY_IN' if leg.direction=='SUPPLY' else 'OLD_IN',1,user)
                _finish(app,leg,'receive',user)
            leg.status='RECEIVED';leg.received_by=user;leg.received_at=now;leg.receipt_note=note
            leg.logistics={**leg.logistics,'receipt_evidence_ids':evidence};leg.save()
            if leg.direction=='OLD' and leg.to_organization_id==app.factory_organization_id:
                flow.recovery={**flow.recovery,'picked_up_at':now.isoformat(),'picked_up_by':user.pk};flow.save(update_fields=['recovery'])
                from sales.services.replacement_supply_service import returned
                returned(flow,user)
                S._send_factory_close_task(app,actor_openid=user.wx_openid or '',title='旧机已返厂，请完成售后处理归因；补发独立安排')
            nextleg=flow.legs.filter(direction=leg.direction,sequence=leg.sequence+1).first()
            if nextleg:_notify(app,nextleg.from_organization_id,'换新下一段待发出',str(nextleg.pk)+'-send',user,leg=nextleg)
            elif leg.direction=='OLD' and flow.mode=='FACTORY':_notify(app,flow.execution_organization_id,'旧机已返厂签收，请扫描新机换新','scan',user)
        else:error('未知换新操作')
    S._event(app,'REPLACEMENT_FLOW_'+str(action),user=user,payload={'flow_id':flow.pk,'leg_id':leg.pk})
    return S.serialize(app)


def describe(app,user):
    from lifecycle.services import aftersales_evidence_service as E
    flow=ReplacementFlow.objects.filter(application=app).first()
    if not flow:return None
    from lifecycle.services.aftersales_service import AfterSalesService as S
    legs=list(flow.legs.select_related('flow__application','from_organization','to_organization','device'))
    can_scan = app.status!='CANCELLED' and not app.replacement_device_id and S._is_assignee(app,user) and _org_user(user,flow.execution_organization_id) and (flow.mode!='FACTORY' or all(l.status=='RECEIVED' for l in legs if l.direction=='OLD'))
    from lifecycle.services.replacement_v6_service import logistics
    final_old=next((l for l in reversed(legs) if l.direction=='OLD'),None)
    can_confirm_legacy=bool(flow.protocol_version==5 and flow.mode=='LOCAL' and app.status!='CANCELLED' and final_old and final_old.status=='RECEIVED' and not flow.recovery.get('picked_up_at') and logistics(user,app.factory_organization_id))
    shared=[];allocated=None
    if any(l.direction=='SUPPLY' and l.sequence==1 and can_send(l,user) for l in legs):
        from sales.models import OrderPlanShipment
        first=next(l for l in legs if l.direction=='SUPPLY' and l.sequence==1)
        from sales.services.replacement_supply_service import allocation
        allocated=allocation(first)
        normal_id=allocated.plan.normal_order_id if allocated else None
        shared=[{'id':s.pk,'label':s.order_plan.plan_code+' / 第'+str(s.sequence_no)+'批 / '+s.logistics_company+' '+s.tracking_no}
            for s in OrderPlanShipment.objects.select_related('order_plan').filter(status='SHIPPED',order_plan_id=normal_id,
                order_plan__factory_organization_id=first.from_organization_id,order_plan__merchant_organization_id=first.to_organization_id,
                logistics_document__isnull=False).exclude(order_plan__status='CANCELLED').order_by('-pk')[:30] if s.logistics_company and s.tracking_no]
    can_upgrade = flow.protocol_version==5 and not flow.swapped_at and not app.replacement_device_id and not flow.inventory_entries.exists() and all(l.status=='PENDING' for l in legs) and (S._is_factory_aftersales(app,user) or (S._is_assignee(app,user) and _org_user(user,flow.execution_organization_id)))
    result = {'can_upgrade_protocol':can_upgrade, 'shared_shipments':shared, 'can_operate':can_scan or any(can_send(l,user) or can_receive(l,user) for l in legs), 'id':flow.pk,'mode':flow.mode,'chain':flow.chain,'first_activation_at':flow.first_activation_at.isoformat(),
        'activation_method':flow.activation_method,'swapped':bool(flow.swapped_at),
        'can_scan_swap':can_scan,
        'can_confirm_factory_old':can_confirm_legacy,
        'supply_plan_id':allocated.plan_id if allocated else None,
        'supply_normal_order_id':allocated.plan.normal_order_id if allocated else None,
        'factory_logistics_only':bool(get_active_identity(user) and get_active_identity(user).organization_id==app.factory_organization_id),
        'needs_legacy_logistics':can_scan or any((l.direction!='OLD' and can_send(l,user)) or can_receive(l,user) for l in legs),
        'can_send_old':any(l.direction=='OLD' and can_send(l,user) for l in legs),
        'old_return_complete':all(l.status=='RECEIVED' for l in legs if l.direction=='OLD'),
        'supply_complete':all(l.status=='RECEIVED' for l in legs if l.direction=='SUPPLY'),
        'legs':[{'id':l.pk,**(E.describe(app,user,E.old_scope(l)) if l.direction=='OLD' else {}),'direction':l.direction,'sequence':l.sequence,'status':l.status,
            'from_name':l.from_organization.name,'to_name':l.to_organization.name,'device_sn':l.device.sn if l.device else '',
            'logistics':l.logistics,'received_at':l.received_at.isoformat() if l.received_at else '',
            'can_send':can_send(l,user),'can_receive':can_receive(l,user)} for l in legs]}

    if (app.escalation or {}).get('factory_transport'):
        result.update(can_scan_swap=False,can_operate=False,needs_legacy_logistics=False,can_upgrade_protocol=False,can_confirm_factory_old=False,can_send_old=False)
        for row in result['legs']:row.update(can_send=False,can_receive=False)
        from lifecycle.services.replacement_v6_service import decorate, transport_recovery_ready
        if transport_recovery_ready(app, flow):
            result = decorate(app, user, flow, result)
            result['needs_logistics_controls'] = any(row['can_send'] for row in result['material_returns'])
        return result
    if flow.protocol_version>=6:
        from lifecycle.services.replacement_v6_service import decorate
        return decorate(app,user,flow,result)
    return result


def pending_for_plan(plan):
    from sales.services.replacement_supply_service import candidates
    from sales.models import ReplacementSupplyPlanItem
    available=candidates(plan.factory_organization_id,plan.merchant_organization_id)
    reserved=ReplacementSupplyPlanItem.objects.filter(plan__normal_order_id=getattr(plan,'pk',0) or 0,status='RESERVED').select_related('leg__flow__application__device__product_model','leg__flow__execution_organization')
    legs={l.pk:l for l in available}
    for item in reserved:legs[item.leg_id]=item.leg
    return [{'application_id':l.flow.application_id,'case_no':l.flow.application.case_no,'leg_id':l.pk,
        'organization':l.flow.execution_organization.name,'model':l.flow.application.device.product_model.name,
        'quantity':1,'link':'/pages/aftersale/detail/detail?application_id='+str(l.flow.application_id)} for l in legs.values()]


def statistics(user):
    from lifecycle.services.aftersales_access import visible_cases
    flows=ReplacementFlow.objects.filter(application_id__in=visible_cases(user).values('pk'))
    return {'replacement_completed_count':flows.filter(swapped_at__isnull=False).count(),
        'replacement_waiting_count':flows.filter(swapped_at__isnull=True).count(),
        'replacement_pending_supply_count':ReplacementLeg.objects.filter(flow__in=flows,flow__swapped_at__isnull=False,
            direction='SUPPLY',sequence=1,status='PENDING').count(),
        'replacement_open_return_count':ReplacementLeg.objects.filter(flow__in=flows,direction='OLD').exclude(status='RECEIVED').values('flow_id').distinct().count()}
