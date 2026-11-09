"""Onsite replacement, independent reverse custody, factory disposition and warranty parts."""
from copy import deepcopy
from types import SimpleNamespace
from decimal import Decimal, InvalidOperation
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from account.models import UserOrganization, Organization
from account.services.auth_service import get_active_identity
from account.services.role_delegation_service import identity_has_role, vacant_role_manager_openids
from lifecycle.models import (ReplacementFlow,ReplacementLeg,ReplacementMaterialReturn,ReplacementTurnoverStock,
    AfterSalesApplication,DeviceState,OrganizationDevice,LifecycleEvent,QRCode)
from lifecycle.services import replacement_flow_service as B
from lifecycle.services.aftersales_service import AfterSalesService as S, AfterSalesError

ACTIONS={'UPGRADE_REPLACEMENT_PROTOCOL','CONFIRM_REPLACEMENT_SERVICE','FACTORY_PICKUP','RECOVERY_DECIDE','RECOVERY_REPAIR_COMPLETE',
         'RECOVERY_DISASSEMBLY_COMPLETE','SEND_WARRANTY_PART'}
RECOVERY_ACTIONS = {
    'RECOVERY_DECIDE', 'RECOVERY_REPAIR_COMPLETE',
    'RECOVERY_DISASSEMBLY_COMPLETE', 'SEND_WARRANTY_PART',
}


def transport_recovery_ready(app, flow):
    """Recovery can continue after a confirmed direct factory replacement."""
    transport = (app.escalation or {}).get('factory_transport') or {}
    return bool(
        flow and flow.mode == 'FACTORY' and flow.protocol_version == 6
        and flow.swapped_at and app.status == 'CLOSED'
        and transport.get('kind') == 'REPLACEMENT'
        and transport.get('route') == 'DIRECT'
        and transport.get('state') == 'DELIVERED'
        and transport.get('factory_received_at')
        and flow.recovery.get('picked_up_at')
    )


def logistics(user,root):
    i=get_active_identity(user)
    return bool(i and i.organization_id==root and identity_has_role(i,'factory_logistics'))


def factory(user,app):return S._is_factory_aftersales(app,user)


def _role_task(app,roles,title,key,user,need_action=True):
    ids=list(UserOrganization.objects.filter(organization_id=app.factory_organization_id,is_active=True,
        user__is_active=True,role__is_active=True,role__code__in=roles).exclude(user__wx_openid='').values_list('user__wx_openid',flat=True))
    if need_action:
        ids.extend(vacant_role_manager_openids(app.factory_organization, roles))
    S._publish_task(app,step='replacement-'+key,task_type='AFTERSALES_REPLACEMENT_RECOVERY',title=title,
        summary=S._summary(app),recipients_openids=ids,actor_openid=user.wx_openid or '',need_action=need_action,
        mark_workitem_done_immediately=not need_action)


def _offline(data):
    if data.get('confirmed') is not True:B.error('请先线下核对实物，再明确确认本次交接')
    # Optional trace details; no forced courier, scan or document for offline handover.
    return {'confirmation_basis':'OFFLINE_CHECK_ONLINE_CONFIRM','note':str(data.get('note') or data.get('receipt_note') or '').strip(),
        'company':str(data.get('logistics_company') or '').strip(),'tracking_no':str(data.get('tracking_no') or '').strip()}


def can_send(leg,user):
    app=leg.flow.application
    if app.status=='CANCELLED' or leg.direction=='USER' or leg.status!='PENDING' or not B._previous_done(leg):return False
    sender=B._handoff_user(leg, user, leg.from_organization)
    if leg.direction=='SUPPLY' and leg.sequence==1:
        from sales.services.replacement_supply_service import can_dispatch
        sender=can_dispatch(leg,user)
    if leg.from_organization_id==app.customer_organization_id:sender=user.pk==app.applicant_id
    target=leg.to_organization
    root=target.get_factory_root()
    return bool(sender and target.is_active and root and root.pk==app.factory_organization_id
        and (leg.direction!='SUPPLY' or leg.flow.swapped_at)
        and (leg.direction!='OLD' or leg.flow.mode=='FACTORY' or leg.flow.swapped_at))


def can_receive(leg,user):
    app=leg.flow.application
    if app.status=='CANCELLED' or leg.status!='SENT':return False
    if leg.direction=='OLD' and leg.to_organization_id==app.factory_organization_id:return logistics(user,app.factory_organization_id)
    if leg.direction=='USER':return user.pk==app.applicant_id
    return B._handoff_user(leg, user, leg.to_organization)


@transaction.atomic
def approve(app,user,data):
    # Trusted server protocol. Existing saved v5 rows are not rewritten.
    return B.approve(app,user,{**data,'_trusted_protocol':6})


def _pickup_ready(flow):
    last=flow.legs.filter(direction='OLD').order_by('-sequence').first()
    return (last and last.status=='SENT') or (not last and flow.mode=='FACTORY')


def _finish_recovery(flow,user,data):
    recovery=deepcopy(flow.recovery);recovery.update(state='CLOSED',closed_at=timezone.now().isoformat(),closed_by=user.pk,**data)
    flow.recovery=recovery;flow.save(update_fields=['recovery'])
    S._complete_tasks(flow.application,steps=['replacement-recovery'],actor_openid=user.wx_openid or '')
    if flow.swapped_at and flow.application.status != 'CLOSED':
        S._send_factory_close_task(flow.application,actor_openid=user.wx_openid or '',title='旧机处置已完成，待最终归因结案')


def _supplier_notice(row,user,action=False):
    ids=list(UserOrganization.objects.filter(organization=row.supplier,is_active=True,user__is_active=True,
        role__is_active=True).filter(Q(role__is_owner_role=True)|Q(role__code='supplier_sales')).exclude(user__wx_openid='').values_list('user__wx_openid',flat=True))
    app=row.flow.application
    S._publish_task(app,step='replacement-part-'+str(row.pk),task_type='AFTERSALES_REPLACEMENT_WARRANTY',
        title=('售后三包退料待签收' if action else '拆机物料拟按三包退回，请知悉'),summary=row.snapshot['material_name']+' × '+str(row.quantity),
        recipients_openids=ids,actor_openid=user.wx_openid or '',need_action=action,
        mark_workitem_done_immediately=not action,payload={'inbox_group_key':'replacement_warranty:'+str(row.pk),'inbox_group_kind':'REPLACEMENT_WARRANTY','inbox_group_label':'三包退料', 'replacement_material_return_id':row.pk,
          'link':'/pages/aftersale/material_return/material_return?id='+str(row.pk)})
    # Work-item link must point to the supplier-limited endpoint/page, never the customer case.
    from workflow.models import WorkItem
    WorkItem.objects.filter(uniq_key=S._task_key(app,'replacement-part-'+str(row.pk))).update(
        link='/pages/aftersale/material_return/material_return?id='+str(row.pk))


def _trace(app,item):
    from lifecycle.services.factory_return_service import _resolve_material_trace
    trace=_resolve_material_trace(SimpleNamespace(source_production_batch=app.device.batch),item)
    return trace


def _notify_disassembly(flow,user):
    suppliers={trace['supplier'].pk for item in flow.application.device.batch.bom.items.all() for trace in [_trace(flow.application,item)] if trace.get('supplier')}
    for supplier in suppliers:
        if not Organization.objects.filter(pk=supplier,is_active=True,factory_root_id=flow.application.factory_organization_id).exists():continue
        ids=list(UserOrganization.objects.filter(organization_id=supplier,is_active=True,user__is_active=True,
            role__is_active=True).filter(Q(role__is_owner_role=True)|Q(role__code='supplier_sales')).exclude(user__wx_openid='').values_list('user__wx_openid',flat=True))
        S._publish_task(flow.application,step='replacement-disassembly-notice-'+str(supplier),task_type='AFTERSALES_REPLACEMENT_DISASSEMBLY_NOTICE',
            title='厂家正在拆机研判，待确认三包退料',summary='设备 '+flow.application.device.sn+'；实际退料数量及单据确认后另行通知',
            recipients_openids=ids,actor_openid=user.wx_openid or '',need_action=False,mark_workitem_done_immediately=True,
            payload={'notice_only':True})
        from workflow.models import WorkItem
        WorkItem.objects.filter(uniq_key=S._task_key(flow.application,'replacement-disassembly-notice-'+str(supplier))).update(link='/pages/personal/task_list/task_list')


@transaction.atomic
def execute(*,application_id,user,data):
    app=AfterSalesApplication.objects.select_for_update().select_related('device__batch__bom','factory_organization','applicant','assigned_user').get(pk=application_id)
    if not S.can_view_application(application=app,user=user):B.error('无权处理该售后单')
    if data.get('action')=='UPGRADE_REPLACEMENT_PROTOCOL':
        flow=ReplacementFlow.objects.select_for_update().filter(application=app,protocol_version=5).first()
        if not flow or app.status=='CANCELLED' or not (factory(user,app) or (S._is_assignee(app,user) and B._org_user(user,flow.execution_organization_id))):B.error('当前不能切换换新处理方式')
        if flow.swapped_at or app.replacement_device_id or flow.inventory_entries.exists() or flow.legs.exclude(status='PENDING').exists():B.error('已有实物交接记录，不能自动转换或补造历史')
        superseded=list(flow.legs.values_list('pk',flat=True))
        for leg in flow.legs.all():B._finish(app,leg,'send',user)
        flow.legs.all().delete()
        origin=app.escalation['source_org_id']
        path=list(reversed(flow.chain))
        if flow.mode=='FACTORY':
            holder=app.customer_organization_id if app.escalation.get('holder')=='APPLICANT' else origin
            path=[holder,app.factory_organization_id] if holder!=app.factory_organization_id else [holder]
        for seq,(source,target) in enumerate(zip(path,path[1:]),1):ReplacementLeg.objects.create(flow=flow,direction='OLD',sequence=seq,from_organization_id=source,to_organization_id=target,device=app.device)
        if flow.mode=='LOCAL':
            for seq,(source,target) in enumerate(zip(flow.chain,flow.chain[1:]),1):ReplacementLeg.objects.create(flow=flow,direction='SUPPLY',sequence=seq,from_organization_id=source,to_organization_id=target)
        flow.protocol_version=6;flow.recovery={'state':'WAIT_PICKUP'};flow.save(update_fields=['protocol_version','recovery'])
        S._event(app,'REPLACEMENT_UNEXECUTED_PLAN_REPLACED',user=user,payload={'superseded_pending_leg_ids':superseded})
        if flow.mode=='FACTORY':
            first=flow.legs.filter(direction='OLD').first()
            B._notify(app,first.from_organization_id if first else app.factory_organization_id,'旧机返厂交接待处理',str(first.pk)+'-send' if first else 'factory-pickup',user)
        return S.serialize(app)
    flow=ReplacementFlow.objects.select_for_update().filter(application=app,protocol_version=6).first()
    direct_transport = (app.escalation or {}).get('factory_transport')
    if direct_transport and (
        data.get('action') not in RECOVERY_ACTIONS or not transport_recovery_ready(app, flow)
    ):
        B.error('直接返厂旧机须完成厂家物流收货和新机签收后才能处置')
    if not flow or app.status=='CANCELLED':B.error('当前售后流程不能执行此操作')
    data={key:value for key,value in data.items() if not key.startswith('_')}
    action=data.get('action');now=timezone.now()
    if action=='SCAN_SWAP' and flow.mode=='FACTORY':return B.execute(application_id=app.pk,user=user,data=data)
    if action=='CONFIRM_REPLACEMENT_SERVICE':
        if flow.mode!='LOCAL' or flow.swapped_at or app.replacement_device_id or not S._is_assignee(app,user) or not B._org_user(user,flow.execution_organization_id):B.error('当前不能确认现场服务')
        if data.get('service_mode') not in {'HOME','SHOP'}:B.error('请选择上门服务或店内服务')
        flow.service_mode=data['service_mode'];flow.service_confirmed_at=now;flow.save(update_fields=['service_mode','service_confirmed_at'])
    elif action=='SCAN_SWAP' and flow.mode=='LOCAL':
        if not flow.service_confirmed_at:B.error('请先确认上门服务或店内服务')
        if flow.swapped_at or app.replacement_device_id or not S._is_assignee(app,user) or not B._org_user(user,flow.execution_organization_id):B.error('仅当前服务操作人可扫码交付，不能重复换新')
        # Local service is a face-to-face handoff. No user signature is fabricated.
        recipient=get_active_identity(app.applicant)
        if not recipient or recipient.organization_id!=app.customer_organization_id or not S.can_view_application(application=app,user=app.applicant):B.error('用户授权已改变，请先核对')
        new,record,state=B._new_stock(app,flow.execution_organization_id,data.get('qr_text'),user)
        leg=ReplacementLeg.objects.create(flow=flow,direction='USER',sequence=1,from_organization=flow.execution_organization,
            to_organization=app.customer_organization,device=new,status='RECEIVED',sent_by=user,sent_at=now,received_by=user,received_at=now,
            receipt_note='服务操作人扫码确认当面交付',logistics={'confirmation_basis':'ONSITE_OPERATOR_SCAN','service_mode':flow.service_mode,'customer_online_signature':False})
        app.replacement_device=new;es=deepcopy(app.escalation);es.update(new_device_sn=new.sn,new_device_id=new.pk,replacement_delivered_at=now.isoformat(),holder='EXECUTOR')
        app.escalation=es;app.save()
        B._stock(flow,leg,flow.execution_organization_id,new,'LOCAL_OUT',-1,user)
        B._apply_swap(flow,leg,user,onsite=True)
        S._publish_applicant_notice(app,topic='onsite-replacement-delivered',title='新机已由服务操作人扫码交付',summary='新设备 '+new.sn,actor_openid=user.wx_openid or '')
    elif action in {'SEND_REPLACEMENT_LEG','RECEIVE_REPLACEMENT_LEG','FACTORY_PICKUP'}:
        if action=='FACTORY_PICKUP':
            if not logistics(user,app.factory_organization_id) or flow.recovery.get('state')!='WAIT_PICKUP' or not _pickup_ready(flow):B.error('仅厂家物流经理可以确认本次取货')
            leg=flow.legs.filter(direction='OLD').order_by('-sequence').first()
        else:
            try:leg=flow.legs.select_for_update().select_related('flow__application','device','to_organization','from_organization').get(pk=int(data.get('leg_id') or 0))
            except (ValueError,ReplacementLeg.DoesNotExist):B.error('交接段不存在')
        if leg and leg.direction!='OLD':
            return _supply_or_legacy(flow,leg,user,data)
        if action=='SEND_REPLACEMENT_LEG':
            if not can_send(leg,user):B.error('请先完成前段接收，不允许越级退回')
            info=B._old_return_evidence(flow,leg,user,data)
            leg.status='SENT';leg.sent_by=user;leg.sent_at=now;leg.logistics=info;leg.save()
            B._stock(flow,leg,leg.from_organization_id,leg.device,'OLD_OUT',-1,user)
            B._event(app,leg.device,'OLD_DEVICE_RETURN_SENT',user,leg_id=leg.pk,**info)
            B._finish(app,leg,'send',user)
            B._notify(app,leg.to_organization_id,'旧机待线下核对接收'+('（物流取货）' if leg.to_organization_id==app.factory_organization_id else ''),str(leg.pk)+'-receive',user)
        else:
            if leg and not can_receive(leg,user):B.error('仅本段接收方可以确认；到厂由厂家物流经理取货确认')
            info=_offline(data)
            if leg:
                leg.status='RECEIVED';leg.received_by=user;leg.received_at=now;leg.receipt_note=info['note'] or '已线下核对实物';leg.save()
                B._stock_state(leg.device,leg.to_organization_id,'RETURNED_TO_FACTORY' if leg.to_organization_id==app.factory_organization_id else 'RETURNED',B._event(app,leg.device,'OLD_DEVICE_RETURN_RECEIVED',user,leg_id=leg.pk,**info))
                B._stock(flow,leg,leg.to_organization_id,leg.device,'OLD_IN',1,user);B._finish(app,leg,'receive',user)
            root_pickup=not leg or leg.to_organization_id==app.factory_organization_id
            if root_pickup:
                if not logistics(user,app.factory_organization_id):B.error('请由厂家物流经理确认取货')
                recovery=deepcopy(flow.recovery);recovery.update(state='WAIT_DECISION',picked_up_by=user.pk,picked_up_at=now.isoformat(),pickup_note=info['note'])
                flow.recovery=recovery;flow.save(update_fields=['recovery'])
                B._event(app,app.device,'FACTORY_LOGISTICS_PICKUP',user,**recovery)
                _role_task(app,{'factory_aftersales'},'旧机已取回：请研判维修或拆机','recovery',user)
                from sales.services.replacement_supply_service import returned
                returned(flow,user)
            else:
                nxt=flow.legs.filter(direction='OLD',sequence=leg.sequence+1).first()
                if nxt:B._notify(app,nxt.from_organization_id,'旧机已接收暂存，可择时退回上级',str(nxt.pk)+'-send',user)
    elif action=='RECOVERY_DECIDE':
        if not factory(user,app) or flow.recovery.get('state')!='WAIT_DECISION':B.error('请先由厂家物流取货确认，再由售后经理研判')
        decision=data.get('decision');note=str(data.get('note') or '').strip()
        if decision not in {'REPAIR','DISASSEMBLE'} or not note:B.error('请选择维修或拆机并填写研判说明')
        if direct_transport:
            event = B._event(app, app.device, 'FACTORY_RECOVERY_CUSTODY_CONFIRMED', user)
            B._stock_state(app.device, app.factory_organization_id, 'RETURNED_TO_FACTORY', event)
        flow.recovery={**flow.recovery,'state':'REPAIRING' if decision=='REPAIR' else 'DISASSEMBLING','decision':decision,
            'diagnosed_by':user.pk,'diagnosed_at':now.isoformat(),'diagnosis_note':note};flow.save(update_fields=['recovery'])
        if decision=='DISASSEMBLE':_notify_disassembly(flow,user)
    elif action=='RECOVERY_REPAIR_COMPLETE':
        if not factory(user,app) or flow.recovery.get('state')!='REPAIRING':B.error('当前旧机不在维修处置阶段')
        target=data.get('stock_target');note=str(data.get('note') or '').strip()
        if target not in {'FINISHED','TURNOVER'} or not note:B.error('请填写修复说明并选择成品库或周转机库')
        B._proof(app,'REPAIR_EVIDENCE',user,flow.recovery['diagnosed_at'])
        if target=='FINISHED':
            from sales.models import FactoryFinishedGoodsInventoryItem
            FactoryFinishedGoodsInventoryItem.objects.create(factory_organization=app.factory_organization,device=app.device,
                replacement_flow=flow,source_product_model=app.device.product_model,source_production_batch=app.device.batch,
                source_bom=app.device.batch.bom,stored_by=user,stock_status='PENDING_REISSUE')
            status='IN_STOCK'
        else:
            ReplacementTurnoverStock.objects.create(flow=flow,device=app.device,factory=app.factory_organization,stored_by=user)
            status='RETURNED_TO_FACTORY' # distinct pool; normal finished-stock selection cannot consume it
        event=B._event(app,app.device,'RECOVERED_MACHINE_STORED',user,stock_target=target,note=note)
        B._stock_state(app.device,app.factory_organization_id,status,event)
        _finish_recovery(flow,user,{'stock_target':target,'repair_note':note})
    elif action=='RECOVERY_DISASSEMBLY_COMPLETE':
        if not factory(user,app) or flow.recovery.get('state')!='DISASSEMBLING':B.error('当前旧机不在拆机处置阶段')
        selections=data.get('parts')
        if not isinstance(selections,list) or not selections:B.error('请填写实际拆下并退三包的物料数量')
        B._proof(app,'REPAIR_EVIDENCE',user,flow.recovery['diagnosed_at'])
        seen=set()
        for row in selections:
            if not isinstance(row,dict):B.error('拆机物料格式无效')
            try:item=app.device.batch.bom.items.select_related('material','material_spec','supplier').get(pk=int(row.get('bom_item_id')));qty=Decimal(str(row.get('quantity')))
            except (ValueError,TypeError,InvalidOperation,app.device.batch.bom.items.model.DoesNotExist):B.error('请选择原BOM物料并填写实际数量')
            if item.pk in seen or not qty.is_finite() or qty<=0 or qty>Decimal(str(item.quantity)) or qty.as_tuple().exponent < -3:B.error('实际退料数量无效、重复或超过原BOM数量')
            trace=_trace(app,item);supplier=trace.get('supplier')
            if not supplier or not supplier.is_active or not supplier.get_factory_root() or supplier.get_factory_root().pk!=app.factory_organization_id:B.error('该物料缺少有效原供应商，不能猜测退料对象')
            seen.add(item.pk)
            record=ReplacementMaterialReturn.objects.create(flow=flow,bom_item=item,supplier=supplier,quantity=qty,
                snapshot={'device_sn':app.device.sn,'bom_id':app.device.batch.bom_id,'material_name':item.material.name,
                    'specification':item.material_spec.name if item.material_spec else '', 'purpose':'AFTERSALES_WARRANTY_RETURN',
                    'source_activation_at':flow.first_activation_at.isoformat(),'source_production_batch':app.device.batch.batch_no,
                    'supplier_trace_source':trace['trace_source_name'],'material_batch':trace.get('material_batch') or '', 'settlement':'NOT_APPLICABLE'})
            _supplier_notice(record,user)
        DeviceState.objects.filter(device=app.device).update(status='SCRAPPED')
        B._event(app,app.device,'RECOVERY_DISASSEMBLY_COMPLETED',user,returned_bom_items=list(seen))
        _finish_recovery(flow,user,{'disassembly_note':str(data.get('note') or '').strip()})
    elif action=='SEND_WARRANTY_PART':
        if not factory(user,app):B.error('仅厂家售后经理可确认三包物料发出')
        row=flow.material_returns.select_for_update().filter(pk=data.get('part_id'),status='NOTIFIED').first()
        if not row:B.error('退料记录不存在或已发出')
        info=B._logistics(app,user,data,flow.created_at)
        row.logistics=info;row.status='SENT';row.sent_by=user;row.sent_at=now;row.save();_supplier_notice(row,user,True)
    else:B.error('当前新版流程不支持此操作')
    S._event(app,'REPLACEMENT_V6_'+str(action),user=user,payload={'flow_id':flow.pk})
    return S.serialize(app)


def _supply_or_legacy(flow,leg,user,data):
    """All channel levels may share a real outgoing shipment; never its sales items/amounts."""
    carrier=str(data.get('shipment_id') or '')
    if leg.direction=='SUPPLY' and data.get('action')=='SEND_REPLACEMENT_LEG' and carrier.startswith('G:'):
        if not can_send(leg,user):B.error('前段未接收或不是当前发出方')
        from lifecycle.models import GoodsTransfer
        from lifecycle.models.goods_transfer_settlement import GoodsTransferLogisticsDocument
        try:carrier_id=int(carrier[2:])
        except ValueError:B.error('下发计划标识无效')
        transfer=GoodsTransfer.objects.select_for_update().filter(pk=carrier_id,flow_type='SHIPMENT',status='PENDING_RECEIPT',
            from_organization_id=leg.from_organization_id,to_organization_id=leg.to_organization_id).first()
        if not transfer:B.error('只能关联本组织向本段下级已发出的真实发货计划')
        docs=list(GoodsTransferLogisticsDocument.objects.filter(settlement__transfer=transfer).values_list('pk',flat=True))
        if not docs:B.error('该下发计划尚无物流凭证')
        payload={**data,'shipment_id':None,'_shared_goods_transfer':transfer.pk,'_shared_document_ids':docs,'_shared_transfer_no':transfer.flow_no}
        return B.execute(application_id=flow.application_id,user=user,data=payload)
    return B.execute(application_id=flow.application_id,user=user,data=data)


def decorate(app,user,flow,result):
    recovery=flow.recovery or {};last=flow.legs.filter(direction='OLD').order_by('-sequence').first()
    recovery={**recovery,'state_name':{'WAIT_PICKUP':'待厂家物流取货','WAIT_DECISION':'待售后经理研判','REPAIRING':'维修中','DISASSEMBLING':'拆机中','CLOSED':'已完成处置'}.get(recovery.get('state'),'待回收')}
    result.update(protocol_version=6,service_mode=flow.service_mode,service_confirmed=bool(flow.service_confirmed_at),recovery=recovery,
        can_confirm_service=flow.mode=='LOCAL' and not app.replacement_device_id and S._is_assignee(app,user) and B._org_user(user,flow.execution_organization_id),
        can_pickup=recovery.get('state')=='WAIT_PICKUP' and logistics(user,app.factory_organization_id) and bool(_pickup_ready(flow)),
        can_diagnose=factory(user,app) and recovery.get('state')=='WAIT_DECISION',
        can_complete_repair=factory(user,app) and recovery.get('state')=='REPAIRING',
        can_complete_disassembly=factory(user,app) and recovery.get('state')=='DISASSEMBLING',
        material_returns=[{'id':p.pk,'material':p.snapshot.get('material_name'),'quantity':str(p.quantity),'status':p.status,
            'supplier':p.supplier.name,'can_send':factory(user,app) and p.status=='NOTIFIED'} for p in flow.material_returns.select_related('supplier')])
    result['needs_logistics_controls']=(flow.mode=='FACTORY' and result['can_scan_swap']) or any(l.direction in {'SUPPLY','USER'} and (can_send(l,user) or can_receive(l,user)) for l in flow.legs.select_related('flow__application','to_organization')) or any(p['can_send'] for p in result['material_returns'])
    factory_legs={l.pk for l in flow.legs.filter(direction='OLD',to_organization_id=app.factory_organization_id)}
    for row in result['legs']:row['is_factory_arrival']=row['id'] in factory_legs
    if flow.mode=='LOCAL':result['can_scan_swap']=result['can_scan_swap'] and bool(flow.service_confirmed_at)
    result['can_operate']=result['can_operate'] or any(result[k] for k in ['can_confirm_service','can_pickup','can_diagnose','can_complete_repair','can_complete_disassembly']) or any(r['can_send'] for r in result['material_returns'])
    from lifecycle.models import GoodsTransfer
    for leg in flow.legs.select_related('flow__application','to_organization'):
        if leg.direction=='SUPPLY' and leg.sequence>1 and can_send(leg,user):
            result['shared_shipments'] += [{'id':'G:'+str(t.pk),'label':t.flow_no+' / '+t.from_organization.name+' → '+t.to_organization.name}
                for t in GoodsTransfer.objects.select_related('from_organization','to_organization').filter(flow_type='SHIPMENT',status='PENDING_RECEIPT',
                    from_organization_id=leg.from_organization_id,to_organization_id=leg.to_organization_id).order_by('-pk')[:30]]
    return result


def pending_for_route(source,target):
    rows=ReplacementLeg.objects.filter(direction='SUPPLY',status='PENDING',from_organization_id=source,to_organization_id=target,
        flow__swapped_at__isnull=False).exclude(flow__application__status='CANCELLED').select_related('flow__application__device__product_model','flow__execution_organization')
    return [{'application_id':r.flow.application_id,'case_no':r.flow.application.case_no,'organization':r.flow.execution_organization.name,
        'model':r.flow.application.device.product_model.name,'leg_id':r.pk,'quantity':1} for r in rows]


def material_detail(row,user):
    i=get_active_identity(user)
    if not i or not i.user.is_active or i.organization_id!=row.supplier_id or not i.organization.get_factory_root() or i.organization.get_factory_root().pk!=row.flow.application.factory_organization_id or not (i.role.is_owner_role or i.role.code=='supplier_sales'):B.error('仅原配套供应商可查看自己的三包退料')
    root = row.flow.application.factory_organization
    return {'id':row.pk,'root_organization':{'id':root.pk,'name':root.name},
        'supplier':row.supplier.name,'device_sn':row.snapshot.get('device_sn'),
        'material':row.snapshot.get('material_name'),'specification':row.snapshot.get('specification'),'quantity':str(row.quantity),
        'status':row.status,'logistics':{k:v for k,v in row.logistics.items() if k not in {'evidence_ids'}},
        'purpose':'售后三包退料，无关销售结算','can_receive':row.status=='SENT'}


@transaction.atomic
def receive_material(*,part_id,user,data):
    try:row=ReplacementMaterialReturn.objects.select_for_update().select_related('flow__application','supplier').get(pk=part_id)
    except ReplacementMaterialReturn.DoesNotExist:B.error('退料记录不存在')
    material_detail(row,user)
    if row.status!='SENT' or data.get('confirmed') is not True:B.error('请实际收料核对后确认，不能重复签收')
    row.status='RECEIVED';row.received_by=user;row.received_at=timezone.now();row.save()
    S._complete_tasks(row.flow.application,steps=['replacement-part-'+str(row.pk)],actor_openid=user.wx_openid or '')
    S._event(row.flow.application,'REPLACEMENT_WARRANTY_PART_RECEIVED',user=user,payload={'part_id':row.pk,'quantity':str(row.quantity)})
    return material_detail(row,user)
