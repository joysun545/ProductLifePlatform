"""Zero-sale replacement plans: no production, sales items or receivables."""
from django.db import transaction
from django.db.models import Exists, OuterRef
from account.services.auth_service import get_active_identity
from account.services.role_delegation_service import identity_has_role
from account.models import Organization,OrganizationRelation
from lifecycle.models import ReplacementLeg
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
    payload={'inbox_group_key':'replacement-supply:'+key,'inbox_group_kind':'replacement_supply','inbox_group_label':'售后换新补发'}
    if plan:payload['replacement_supply_plan_id']=plan.pk
    if flow:payload['application_id']=flow.application_id
    return publish_workitem(domain='SALES',type='REPLACEMENT_SUPPLY_PLAN' if plan else 'REPLACEMENT_SUPPLY_NOTICE',biz_id=key,biz_ref='sales.ReplacementSupplyPlan:'+str(plan.pk) if plan else 'lifecycle.ReplacementFlow:'+str(flow.pk),
        uniq_key='replacement-supply:'+key,title=title,summary='正常销售数量与应收不变；无需等待售后归因结案。',link=LINK+('?id='+str(plan.pk) if plan else ''),
        recipients_openids=O._role_openids(factory,{'factory_sales','factory_stock'}),need_action=action,action_mode='EACH_ACK',payload=payload,actor_openid=actor.wx_openid or '')

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

def serialize(plan,user):
    i=_view_identity(user)
    if i.organization_id!=plan.factory_id:raise O.OrderPlanMaterialFlowError('无权查看其他厂家的补发计划')
    items=list(plan.items.select_related('leg__flow__application__device__product_model','leg__flow__execution_organization'))
    states=[x.leg.status for x in items]
    display_status=plan.status if plan.status=='CANCELLED' else ('RECEIVED' if states and all(x=='RECEIVED' for x in states) else ('SHIPPED' if states and all(x in {'SENT','RECEIVED'} for x in states) else ('PART_SHIPPED' if any(x in {'SENT','RECEIVED'} for x in states) else 'OPEN')))
    return {'id':plan.pk,'code':'RP-'+str(plan.pk),'merchant':plan.merchant.name,'merchant_id':plan.merchant_id,'normal_order_id':plan.normal_order_id,
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
