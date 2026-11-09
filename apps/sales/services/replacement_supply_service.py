"""Zero-sale replacement plans: no production, sales items or receivables."""
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from account.services.auth_service import get_active_identity
from account.services.role_delegation_service import (
    identity_can_perform_shared_operation,
    identity_has_role,
    vacant_role_manager_openids,
)
from account.models import Organization,OrganizationRelation
from lifecycle.models import GoodsTransfer, ReplacementLeg
from sales.models import ReplacementSupplyPlan,ReplacementSupplyPlanItem
from sales.services import order_plan_material_flow_service as O

LINK='/pages/sales/replacement_supply/replacement_supply'

def stock(user,factory):
    i=get_active_identity(user)
    return bool(i and user.is_active and i.organization_id==factory and identity_has_role(i,'factory_stock'))

def ready(flow):
    # A received old-machine leg is the physical prerequisite, never fault attribution.
    last=flow.legs.filter(direction='OLD').order_by('-sequence').first()
    return bool(flow.mode=='LOCAL' and flow.swapped_at and last and last.status=='RECEIVED'
        and flow.recovery.get('picked_up_at'))

def candidates(factory,merchant=None):
    active=ReplacementSupplyPlanItem.objects.filter(leg_id=OuterRef('pk'),status__in=['RESERVED','SENT'])
    rows=ReplacementLeg.objects.filter(direction='SUPPLY',sequence=1,status='PENDING',from_organization_id=factory,
        flow__mode='LOCAL',flow__swapped_at__isnull=False).exclude(flow__application__status='CANCELLED').annotate(reserved=Exists(active)).filter(reserved=False)
    if merchant:rows=rows.filter(to_organization_id=merchant)
    return [l for l in rows.select_related('flow__application__device__product_model','to_organization') if ready(l.flow)]

def _view_identity(user):
    i=get_active_identity(user)
    if not i or not user.is_active or not any(identity_has_role(i,c) for c in ['factory_sales','factory_stock','factory_aftersales']):
        raise O.OrderPlanMaterialFlowError('仅厂家销售、成品库管或售后岗位可查看补发计划')
    return i

def _notify(factory,title,key,actor,*,plan=None,flow=None,action=False):
    from workflow.services.workflow_service import publish_workitem
    payload={'inbox_group_key':'replacement-supply:'+key,'inbox_group_kind':'replacement_supply','inbox_group_label':'售后换新补发',
        'root_organization_id':factory.pk}
    if plan:payload['replacement_supply_plan_id']=plan.pk
    if flow:payload['application_id']=flow.application_id
    recipients=O._role_openids(factory,{'factory_sales','factory_stock'})
    if plan and plan.normal_order_id:
        recipients.extend(vacant_role_manager_openids(factory, {'factory_stock'}))
    return publish_workitem(domain='SALES',type='REPLACEMENT_SUPPLY_PLAN' if plan else 'REPLACEMENT_SUPPLY_NOTICE',biz_id=key,biz_ref='sales.ReplacementSupplyPlan:'+str(plan.pk) if plan else 'lifecycle.ReplacementFlow:'+str(flow.pk),
        uniq_key='replacement-supply:'+key,title=title,summary='正常销售数量与应收不变；无需等待售后归因结案。',link=LINK+('?id='+str(plan.pk) if plan else ''),
        recipients_openids=list(dict.fromkeys(recipients)),need_action=action,action_mode='EACH_ACK',payload=payload,actor_openid=actor.wx_openid or '')

def returned(flow,user):
    if ready(flow) and flow.legs.filter(direction='SUPPLY',sequence=1,status='PENDING').exists():
        _notify(flow.application.factory_organization,'旧机已到厂：换新机待安排补发','return:'+str(flow.pk),user,flow=flow)

def _attach(plan,legs):
    for leg in legs:
        ReplacementSupplyPlanItem.objects.create(plan=plan,leg=leg)

def auto_attach(order,user):
    # Called inside normal creation's transaction, including retry. No allocation on reads.
    list(Organization.objects.select_for_update().filter(pk=order.factory_organization_id))
    legs=candidates(order.factory_organization_id,order.merchant_organization_id)
    if not legs:return None
    plan=ReplacementSupplyPlan.objects.select_for_update().filter(normal_order=order,status='OPEN').first()
    if not plan:
        if ReplacementSupplyPlan.objects.filter(normal_order=order).exists():return None
        plan=ReplacementSupplyPlan.objects.create(factory_id=order.factory_organization_id,merchant_id=order.merchant_organization_id,normal_order=order,created_by=user,client_request_id='order:'+str(order.pk))
    _attach(plan,legs)
    _notify(order.factory_organization,'订单已自动关联待补发换新机','order:'+str(order.pk),user,plan=plan)
    return plan

@transaction.atomic
def create(user,merchant_id,key):
    i=O._sales_identity(user)
    key=str(key or '').strip()
    if not key or len(key)>64 or key.startswith('order:'):raise O.OrderPlanMaterialFlowError('补发计划提交标识无效')
    list(Organization.objects.select_for_update().filter(pk=i.organization_id))
    existing=ReplacementSupplyPlan.objects.filter(created_by=user,client_request_id=key).first()
    if existing:
        if str(existing.merchant_id)!=str(merchant_id):raise O.OrderPlanMaterialFlowError('重复请求的商家不一致')
        return serialize(existing,user)
    try:relation=O._get_direct_relation(user,i,merchant_id)
    except Exception as e:raise O.OrderPlanMaterialFlowError(str(e))
    legs=candidates(i.organization_id,relation.target_organization_id)
    if not legs:raise O.OrderPlanMaterialFlowError('该直属商家链路没有已收旧机且未被计划占用的待补发需求')
    plan=ReplacementSupplyPlan.objects.create(factory_id=i.organization_id,merchant=relation.target_organization,created_by=user,client_request_id=key)
    _attach(plan,legs);_notify(i.organization,'独立换新补发计划已生成（零新订单）','standalone:'+str(plan.pk),user,plan=plan)
    return serialize(plan,user)

@transaction.atomic
def cancel(plan,user):
    i=O._sales_identity(user)
    list(Organization.objects.select_for_update().filter(pk=i.organization_id))
    plan=ReplacementSupplyPlan.objects.select_for_update().get(pk=plan.pk)
    if i.organization_id!=plan.factory_id or plan.created_by_id!=user.pk:raise O.OrderPlanMaterialFlowError('仅创建该计划的厂家销售经理可取消')
    if plan.normal_order_id:raise O.OrderPlanMaterialFlowError('随单补发请通过原订单取消')
    if plan.items.filter(leg__status__in=['SENT','RECEIVED']).exists():raise O.OrderPlanMaterialFlowError('已有实物发货，不能取消')
    plan.items.filter(status='RESERVED').update(status='CANCELLED');plan.status='CANCELLED';plan.save(update_fields=['status'])
    return serialize(plan,user)

def release_normal(order):
    list(Organization.objects.select_for_update().filter(pk=order.factory_organization_id))
    plan=ReplacementSupplyPlan.objects.select_for_update().filter(normal_order=order).first()
    if not plan:return
    if plan.items.exclude(status='CANCELLED').filter(leg__status__in=['SENT','RECEIVED']).exists():raise O.OrderPlanMaterialFlowError('随单补发已有实物发货，不能取消原订单')
    plan.items.filter(status='RESERVED').update(status='CANCELLED');plan.status='CANCELLED';plan.save(update_fields=['status'])

def allocation(leg):return ReplacementSupplyPlanItem.objects.select_related('plan__normal_order').filter(leg=leg,status='RESERVED',plan__status='OPEN').first()

def can_dispatch(leg,user):return bool(stock(user,leg.from_organization_id) and ready(leg.flow) and allocation(leg))

def validate_dispatch(leg,user,data):
    from lifecycle.services.replacement_flow_service import error
    list(Organization.objects.select_for_update().filter(pk=leg.from_organization_id))
    if not can_dispatch(leg,user):error('仅成品库管可按已安排的补发计划出库；须先由厂家物流确认收旧机')
    item=allocation(leg)
    if item.plan.normal_order_id:
        from sales.models import OrderPlanShipment
        try:shipment_id=int(data.get('shipment_id') or 0)
        except (ValueError,TypeError):error('正常批次标识无效')
        shipment=OrderPlanShipment.objects.filter(pk=shipment_id,order_plan_id=item.plan.normal_order_id,status='SHIPPED').first()
        if not shipment:error('随单补发须关联本计划已发出的正常批次；单独补发请另建补发计划')
    elif data.get('shipment_id'):error('独立补发不允许关联其他订单物流')
    if data.get('handover_method','SHIPMENT')!='SHIPMENT':error('厂家补发固定使用物流寄送')
    return item


def _merchant_can_handle(identity):
    return (
        identity.organization.organization_type.code == 'MERCHANT'
        and identity_can_perform_shared_operation(identity, 'goods_transfer_receipt')
    )


def _supply_items(**filters):
    """Only the order's actual first-hop allocation can anchor a supply card."""
    return ReplacementSupplyPlanItem.objects.filter(
        plan__status='OPEN', status__in=('RESERVED', 'SENT'),
        leg__direction='SUPPLY', leg__sequence=1, leg__flow__mode='LOCAL',
        leg__flow__swapped_at__isnull=False,
        **filters,
    ).select_related(
        'plan', 'leg__from_organization', 'leg__to_organization', 'leg__device',
        'leg__flow__execution_organization', 'leg__flow__application__device__product_model',
    ).order_by('pk')


def _visible_supply_cases(identity, legs):
    from lifecycle.services.aftersales_access import visible_cases

    case_ids = {leg.flow.application_id for leg in legs}
    if not case_ids:
        return set()
    return set(visible_cases(identity.user, identity).filter(pk__in=case_ids).values_list('pk', flat=True))


def _node_leg(leg):
    return {
        'id': leg.pk, 'from_name': leg.from_organization.name,
        'to_name': leg.to_organization.name, 'status': leg.status,
        'status_name': leg.get_status_display(),
    }


def _supply_card(item, leg, identity, *, shipment_id, shipment_sequence_no=None,
                 carrier_label='', can_attach_first_leg=False):
    from lifecycle.services.replacement_flow_service import can_receive, can_send

    organization_id = identity.organization_id
    node_legs = list(leg.flow.legs.filter(direction='SUPPLY').filter(
        Q(from_organization_id=organization_id) | Q(to_organization_id=organization_id),
    ).select_related('flow__application', 'from_organization', 'to_organization'))
    actions = []
    if not (leg.flow.application.escalation or {}).get('factory_transport'):
        for node in node_legs:
            if node.to_organization_id == organization_id and can_receive(node, identity.user):
                actions.append({'leg_id': node.pk, 'code': 'RECEIVE_REPLACEMENT_LEG', 'label': '确认补发机签收'})
            if (
                node.from_organization_id == organization_id
                and (node.sequence > 1 or can_attach_first_leg)
                and can_send(node, identity.user)
            ):
                actions.append({'leg_id': node.pk, 'code': 'SEND_REPLACEMENT_LEG', 'label': '确认转发补发机' if node.sequence > 1 else '办理换新补发'})
    app = leg.flow.application
    return {
        **_node_leg(leg), 'id': item.pk, 'leg_id': leg.pk, 'plan_id': item.plan_id,
        'application_id': app.pk, 'case_no': app.case_no,
        'model': app.device.product_model.name, 'quantity': 1,
        'device_sn': leg.device.sn if leg.device_id else '',
        'organization': leg.flow.execution_organization.name,
        'shipment_id': shipment_id, 'shipment_sequence_no': shipment_sequence_no,
        'carrier_label': carrier_label,
        'node_legs': [_node_leg(node) for node in node_legs], 'actions': actions,
        'link': f'/pages/aftersale/detail/detail?application_id={app.pk}',
    }


def order_supply_items(plan, identity, shipments):
    """Keep replacement custody and actions out of the sales shipment rows."""
    organization_id = identity.organization_id
    if organization_id == plan.factory_organization_id:
        if not identity_has_role(identity, {'factory_sales', 'factory_stock', 'factory_logistics', 'factory_aftersales'}):
            return []
    elif organization_id != plan.merchant_organization_id or not _merchant_can_handle(identity):
        return []
    items = list(_supply_items(
        plan__normal_order=plan, plan__factory_id=plan.factory_organization_id,
        plan__merchant_id=plan.merchant_organization_id,
        leg__from_organization_id=plan.factory_organization_id,
        leg__to_organization_id=plan.merchant_organization_id,
        leg__flow__application__factory_organization_id=plan.factory_organization_id,
    ))
    visible = _visible_supply_cases(identity, (item.leg for item in items))
    carriers = {row['id']: row for row in shipments if row['status'] in {'SHIPPED', 'RECEIVED'}}
    can_attach = any(
        row['status'] == 'SHIPPED' and row['logistics_document']
        and row['logistics_company'] and row['tracking_no']
        for row in carriers.values()
    )
    cards = []
    for item in items:
        leg = item.leg
        if leg.flow.application_id not in visible:
            continue
        carrier = carriers.get(leg.shipment_id)
        if (
            item.status == 'RESERVED' and leg.status == 'PENDING'
            and not leg.shipment_id and leg.flow.application.status != 'CANCELLED'
        ):
            cards.append(_supply_card(item, leg, identity, shipment_id=None, can_attach_first_leg=can_attach))
        elif item.status == 'SENT' and leg.status in {'SENT', 'RECEIVED'} and carrier:
            cards.append(_supply_card(
                item, leg, identity, shipment_id=leg.shipment_id,
                shipment_sequence_no=carrier['sequence_no'], carrier_label=carrier['title'],
            ))
    return cards


def transfer_supply_items(transfer, identity):
    """A downstream detail projects only its exact persisted carrier link."""
    if (
        not identity or not _merchant_can_handle(identity)
        or identity.organization_id not in {transfer.from_organization_id, transfer.to_organization_id}
        or transfer.flow_type != GoodsTransfer.FlowType.SHIPMENT
    ):
        return []
    legs = list(ReplacementLeg.objects.filter(
        direction='SUPPLY', sequence__gt=1, status__in=('SENT', 'RECEIVED'),
        from_organization_id=transfer.from_organization_id,
        to_organization_id=transfer.to_organization_id,
        logistics__shared_goods_transfer_id=transfer.pk,
    ).select_related('from_organization', 'to_organization', 'device',
                     'flow__execution_organization', 'flow__application__device__product_model'))
    visible = _visible_supply_cases(identity, legs)
    items_by_flow = {item.leg.flow_id: item for item in _supply_items(
        status='SENT', leg__flow_id__in={leg.flow_id for leg in legs},
        plan__factory_id=identity.organization.factory_root_id,
    )}
    return [
        _supply_card(items_by_flow[leg.flow_id], leg, identity, shipment_id=f'G:{transfer.pk}', carrier_label=transfer.flow_no)
        for leg in legs if leg.flow.application_id in visible and leg.flow_id in items_by_flow
    ]


def transfer_supply_candidates(transfer, identity):
    """Keep actionable proposals separate from already attached downstream cargo."""
    if (
        not identity or not _merchant_can_handle(identity)
        or identity.organization_id != transfer.from_organization_id
        or transfer.flow_type != GoodsTransfer.FlowType.SHIPMENT
        or transfer.status != GoodsTransfer.Status.PENDING_RECEIPT
    ):
        return []
    from lifecycle.models.goods_transfer_settlement import GoodsTransferLogisticsDocument
    from lifecycle.services.replacement_flow_service import can_send
    from lifecycle.services.replacement_v6_service import pending_for_route

    candidates = pending_for_route(transfer.from_organization_id, transfer.to_organization_id)
    if not candidates or not GoodsTransferLogisticsDocument.objects.filter(settlement__transfer=transfer).exists():
        return []
    legs = list(ReplacementLeg.objects.filter(pk__in=[row['leg_id'] for row in candidates]).select_related(
        'flow__application', 'to_organization',
    ))
    visible = _visible_supply_cases(identity, legs)
    allocated_flows = set(_supply_items(
        status='SENT', leg__flow_id__in={leg.flow_id for leg in legs},
        plan__factory_id=identity.organization.factory_root_id,
    ).values_list('leg__flow_id', flat=True))
    eligible = {
        leg.pk for leg in legs if leg.flow_id in allocated_flows
        and leg.flow.application_id in visible and can_send(leg, identity.user)
    }
    return [
        {**row, 'link': f"/pages/aftersale/detail/detail?application_id={row['application_id']}"}
        for row in candidates if row['leg_id'] in eligible
    ]


def serialize(plan,user):
    i=_view_identity(user)
    if i.organization_id!=plan.factory_id:raise O.OrderPlanMaterialFlowError('无权查看其他厂家的补发计划')
    items=list(plan.items.select_related('leg__flow__application__device__product_model','leg__flow__execution_organization'))
    states=[x.leg.status for x in items]
    display_status=plan.status if plan.status=='CANCELLED' else ('RECEIVED' if states and all(x=='RECEIVED' for x in states) else ('SHIPPED' if states and all(x in {'SENT','RECEIVED'} for x in states) else ('PART_SHIPPED' if any(x in {'SENT','RECEIVED'} for x in states) else 'OPEN')))
    return {'id':plan.pk,'root_organization':{'id':plan.factory_id,'name':i.organization.name},
        'code':'RP-'+str(plan.pk),'merchant':plan.merchant.name,'merchant_id':plan.merchant_id,'normal_order_id':plan.normal_order_id,
        'normal_sale_quantity':0,'replacement_quantity':len(items),'status':display_status,'can_cancel':identity_has_role(i,'factory_sales') and plan.created_by_id==user.pk and not plan.normal_order_id and plan.status=='OPEN' and all(x.leg.status=='PENDING' for x in items),
        'items':[{'leg_id':x.leg_id,'application_id':x.leg.flow.application_id,'case_no':x.leg.flow.application.case_no,'model':x.leg.flow.application.device.product_model.name,
            'destination':x.leg.flow.execution_organization.name,'status':'CANCELLED' if x.status=='CANCELLED' else x.leg.status,'can_ship':x.status=='RESERVED' and plan.status=='OPEN' and can_dispatch(x.leg,user)} for x in items]}

def overview(user,plan_id=None,order_id=None):
    i=_view_identity(user)
    if order_id:
        try:order_id=int(order_id)
        except (ValueError,TypeError):raise O.OrderPlanMaterialFlowError('订单标识无效')
        p=ReplacementSupplyPlan.objects.filter(normal_order_id=order_id,factory_id=i.organization_id).first()
        return {'plan':serialize(p,user)} if p else {'plan':None}
    if plan_id:
        p=ReplacementSupplyPlan.objects.filter(pk=plan_id,factory_id=i.organization_id).first()
        if not p:raise O.OrderPlanMaterialFlowError('补发计划不存在')
        return {'plan':serialize(p,user)}
    # Only the sales manager's assigned direct merchants may be selected to create.
    merchant_ids=[]
    if identity_has_role(i,'factory_sales'):
        from channel.models import MerchantChannelAssignment
        merchant_ids=list(MerchantChannelAssignment.objects.filter(assigned_by=user,is_active=True,relation__source_organization=i.organization,relation__is_active=True).values_list('relation__target_organization_id',flat=True))
    pending=candidates(i.organization_id)
    merchants={l.to_organization_id:l.to_organization.name for l in pending if l.to_organization_id in merchant_ids}
    return {'can_create':identity_has_role(i,'factory_sales'),'merchants':[{'id':k,'name':v} for k,v in merchants.items()],
        'pending_count':len(pending),'plans':[serialize(p,user) for p in ReplacementSupplyPlan.objects.filter(factory_id=i.organization_id).order_by('-pk')[:100]]}
