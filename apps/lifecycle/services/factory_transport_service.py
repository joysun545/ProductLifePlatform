"""Factory service custody. Frozen routes; no sales, channel stock or credit facts."""
from copy import deepcopy
from django.db import transaction
from django.utils import timezone
from account.models import UserOrganization,User
from account.services.auth_service import get_active_identity
from lifecycle.models import AfterSalesApplication,ReplacementFlow,ReplacementLeg,OrganizationDevice,DeviceState,LifecycleEvent,Device
from lifecycle.services import replacement_flow_service as B
from lifecycle.services.aftersales_service import AfterSalesService as S

KEY='factory_transport'
ACTIONS={'START_FACTORY_TRANSPORT','FACTORY_TRANSPORT_SEND','FACTORY_TRANSPORT_RECEIVE','FACTORY_TRANSPORT_PICKUP','FACTORY_TRANSPORT_PREPARE_SWAP','FACTORY_TRANSPORT_FINALIZE'}

def get(app):return (app.escalation or {}).get(KEY)

def logistics(user,app):
    from lifecycle.services.replacement_v6_service import logistics as allowed
    return allowed(user,app.factory_organization_id)

def actor(app,user,org,*,factory=False):
    if org==app.factory_organization_id:return logistics(user,app)
    if org==app.customer_organization_id:return S._is_applicant(app,user) and S.can_view_application(application=app,user=user)
    return B._org_user(user,org)

def can_start(app,user):
    if get(app) or app.status in {'CANCELLED','CLOSED'} or not S._is_factory_aftersales(app,user):return False
    flow=ReplacementFlow.objects.filter(application=app).first()
    if flow and (flow.mode!='FACTORY' or flow.swapped_at or flow.legs.filter(direction='USER').exists()):return False
    if app.dispatch_route!='FACTORY_AFTERSALES':return False
    if S._has_event(app,'RETURN_SHIPMENT_CONFIRMED') or app.applicant_confirmed_at:return False
    return app.execution_stage in {'AWAITING_FACTORY_HANDOVER','AWAITING_FACTORY_RECEIPT','AWAITING_APPLICANT_SHIPMENT','AWAITING_REPAIR_RECEIPT','AWAITING_REPLACEMENT','REPAIRING','AWAITING_RETURN_SHIPMENT','FINAL_DIAGNOSIS'}


def _save(app,t):
    app.escalation={**app.escalation,KEY:t};app.save()

def _event(app,name,user,**payload):
    S._event(app,name,user=user,payload=payload)
    i=get_active_identity(user)
    device=app.replacement_device if payload.get('direction')=='outbound' and app.replacement_device_id else app.device
    LifecycleEvent.objects.create(device=device,event_type='AFTERSALE_TRANSFER',operator=user,organization=i.organization if i else None,data={'application_id':app.pk,'factory_transport_action':name,**payload})

def _notify(app,t,direction,index,user,receive=False):
    rows=t[direction];leg=rows[index];org=leg['to'] if receive else leg['from']
    if org==app.factory_organization_id:
        ids=list(UserOrganization.objects.filter(organization_id=org,is_active=True,user__is_active=True,role__is_active=True,role__code='factory_logistics').exclude(user__wx_openid='').values_list('user__wx_openid',flat=True))
    elif org==app.customer_organization_id:ids=[app.applicant.wx_openid]
    else:
        ids=list(UserOrganization.objects.filter(organization_id=org,is_active=True,user__is_active=True,role__is_active=True,role__code__in=['merchant_owner','merchant_sales','merchant_stock','merchant_electrician','service_electrician']).exclude(user__wx_openid='').values_list('user__wx_openid',flat=True))
    S._publish_task(app,step=f'factory-transport-{direction}-{index}-'+('receive' if receive else 'send'),task_type='AFTERSALES_FACTORY_TRANSPORT',title=('返厂设备待接收' if direction=='inbound' else '厂家处理完毕：待原路交付') if receive else ('设备待返厂寄送' if direction=='inbound' else '维修/换新返件待物流发出'),summary=S._summary(app),recipients_openids=ids,actor_openid=user.wx_openid or '')

def begin(app,user,kind='REPAIR',route='DIRECT'):
    if get(app):return
    if route not in {'DIRECT','CHAIN'} or kind not in {'REPAIR','REPLACEMENT'}:B.error('返厂路线无效')
    if kind=='REPLACEMENT' and route!='DIRECT':B.error('直接返厂换新须使用直寄物流，现场库存换新继续原补发流程')
    es=app.escalation or {};root=app.factory_organization_id;customer=app.customer_organization_id
    source=customer if not es.get('source_org_id') or es.get('holder')=='APPLICANT' else es['source_org_id']
    if route=='CHAIN':
        origin=es.get('source_org_id')
        if not origin or origin in {customer,root}:B.error('逐级返厂维修需要原接修组织的有效渠道链路')
        path=list(reversed(B.chain_for(app,origin)))
        if source==customer:path=[customer]+path
        outbound=list(reversed(path))
        if outbound[-1]!=customer:outbound.append(customer)
    else:
        path=[source,root] if source!=root else [root]
        outbound=[root,customer]
    existing_flow=ReplacementFlow.objects.filter(application=app,mode='FACTORY').first()
    previous_old=existing_flow.legs.filter(direction='OLD').order_by('-sequence').first() if existing_flow else None
    legacy_custody=bool(es.get('factory_received_at') or S._has_event(app,'REPAIRER_RECEIPT_CONFIRMED') or (previous_old and previous_old.status=='RECEIVED'))
    if legacy_custody:
        if route!='DIRECT':B.error('历史直寄维修不能补造逐级交接事实')
        path=[root]
    now=timezone.now().isoformat()
    def legs(nodes):return [{'from':a,'to':b,'status':'PENDING','ready_at':now} for a,b in zip(nodes,nodes[1:])]
    t={'version':1,'kind':kind,'route':route,'state':'INBOUND','created_at':now,'participants':list(dict.fromkeys(path+outbound)),
        'inbound':legs(path),'outbound':legs(outbound),'attribution':None,'prepared_device_id':None,'legacy_custody':legacy_custody,'repair_completed':bool(kind=='REPAIR' and app.repair_completed_at)}
    # Existing unreceived direct handover may be adopted without changing its facts.
    flow=ReplacementFlow.objects.filter(application=app,mode='FACTORY').first()
    if flow and len(t['inbound'])==1:
        old=flow.legs.filter(direction='OLD').first()
        if old and old.status=='SENT':
            if old.from_organization_id!=t['inbound'][0]['from'] or old.to_organization_id!=root:B.error('历史返厂路线不一致')
            t['inbound'][0].update(status='SENT',sent_by=old.sent_by_id,sent_at=old.sent_at.isoformat(),logistics=old.logistics)
    elif es.get('handed_over_at') and len(t['inbound'])==1:
        t['inbound'][0].update(status='SENT',sent_by=es.get('handed_over_by'),sent_at=es['handed_over_at'],logistics=es.get('handover',{}))
    app.status='PROCESSING';app.execution_stage='AWAITING_FACTORY_RECEIPT';_save(app,t)
    S._complete_tasks(app,steps=['factory-handover','direct-repair-pickup','applicant-shipment','repair-receipt','replacement-scan'],actor_openid=user.wx_openid or '')
    S._complete_factory_workorder(app,actor_openid=user.wx_openid or '')
    _event(app,'FACTORY_TRANSPORT_STARTED',user,kind=kind,route=route,path=path,return_path=outbound)
    if t['inbound']:_notify(app,t,'inbound',0,user,receive=t['inbound'][0]['status']=='SENT')
    else:
        # Physical receipt still requires an explicit logistics acknowledgement.
        S._publish_task(app,step='factory-transport-pickup',task_type='AFTERSALES_FACTORY_TRANSPORT',title='请厂家物流核验已在厂设备',summary=S._summary(app),recipients_openids=list(UserOrganization.objects.filter(organization_id=root,is_active=True,role__code='factory_logistics',user__is_active=True).values_list('user__wx_openid',flat=True)),actor_openid=user.wx_openid or '')

def _factory_received(app,t,user):
    now=timezone.now().isoformat();t.update(state='FACTORY_RECEIVED',factory_received_at=now,factory_received_by=user.pk)
    es=app.escalation
    if not es.get('factory_received_at'):es.update(factory_received_at=now,factory_received_by=user.pk)
    # Mirror the direct factory replacement inbound leg, preserving its actual actors.
    flow=ReplacementFlow.objects.filter(application=app,mode='FACTORY').first()
    if flow:
        incoming=t['inbound'][-1] if t['inbound'] else None
        for leg in flow.legs.filter(direction='OLD',status__in=['PENDING','SENT']):
            if not incoming or leg.from_organization_id!=incoming['from'] or leg.to_organization_id!=incoming['to']:B.error('旧版返厂段与当前物流路线不一致')
            leg.status='RECEIVED';leg.sent_by_id=incoming['sent_by'];leg.sent_at=incoming['sent_at'];leg.received_by=user;leg.received_at=timezone.now();leg.logistics=incoming['logistics'];leg.receipt_note='厂家物流核对收到';leg.save()
        flow.recovery={**flow.recovery,'picked_up_at':flow.recovery.get('picked_up_at') or now,'picked_up_by':flow.recovery.get('picked_up_by') or user.pk,'state':'WAIT_DECISION'};flow.save(update_fields=['recovery'])
    app.execution_stage='AWAITING_REPLACEMENT' if t['kind']=='REPLACEMENT' else ('FINAL_DIAGNOSIS' if t.get('repair_completed') else 'REPAIRING');_save(app,t)
    S._publish_assignee_workorder(app,topic='factory-service-received',title='厂家物流已收货，请办理'+('换新及归因' if t['kind']=='REPLACEMENT' else '维修及归因'),summary=S._summary(app),actor_openid=user.wx_openid or '',need_action=True)

def record_repair(app,user):
    t=deepcopy(get(app));t['repair_completed']=True
    app.status='PROCESSING';app.execution_stage='FINAL_DIAGNOSIS';_save(app,t)
    _event(app,'FACTORY_SERVICE_REPAIR_COMPLETED',user,repair_summary=app.repair_summary,repair_materials=app.repair_materials)
    S._event(app,'REPAIR_COMPLETED',user=user,payload={'repair_summary':app.repair_summary,'repair_materials':app.repair_materials,'actual_processed_at':app.repair_completed_at.isoformat(),'factory_transport_version':1})
    LifecycleEvent.objects.create(device=app.device,event_type='REPAIR',operator=user,organization=app.factory_organization,data={'application_id':app.pk,'case_no':app.case_no,'repair_summary':app.repair_summary,'repair_materials':app.repair_materials,'actual_processed_at':app.repair_completed_at.isoformat()})
    S._complete_tasks(app,steps=['repair-execution'],actor_openid=user.wx_openid or '')
    S._publish_assignee_workorder(app,topic='factory-service-attribution',title='维修已完成，请归因后交物流返件',summary=S._summary(app),actor_openid=user.wx_openid or '',need_action=True)

def media_allowed(app,user,purpose):
    t=get(app)
    if not t:return False
    if purpose=='REPAIR_EVIDENCE':return t['state']=='FACTORY_RECEIVED' and S._is_assignee(app,user) and S._is_factory_aftersales(app,user)
    if purpose!='REPAIR_RETURN_SHIPMENT_DOCUMENT':return False
    for direction in ['inbound','outbound']:
        if direction=='outbound' and not t.get('attribution'):continue
        for n,leg in enumerate(t[direction]):
            if leg['status']=='PENDING' and (n==0 or t[direction][n-1]['status']=='RECEIVED') and actor(app,user,leg['from']):return True
    return False

def _proof(app,user,data,after):
    ids=data.get('document_ids')
    if not isinstance(ids,list) or not ids or any(type(x) is not int for x in ids):B.error('请上传本段物流凭证')
    records=list(app.media.filter(pk__in=ids,uploaded_by=user,purpose='REPAIR_RETURN_SHIPMENT_DOCUMENT',created_at__gte=after))
    if {x.pk for x in records}!=set(ids):B.error('本段物流凭证无效、已过期或不属于当前账号')
    return sorted(set(ids))

def _delivered(app,t,user):
    t['state']='DELIVERED';t['delivered_at']=timezone.now().isoformat();_save(app,t)
    if t['kind']=='REPLACEMENT':
        flow=ReplacementFlow.objects.get(application=app,mode='FACTORY');leg=flow.legs.get(direction='USER')
        leg.status='RECEIVED';leg.received_by=user;leg.received_at=timezone.now();leg.receipt_note='用户明确确认收到物流换新设备';leg.save()
        B._apply_swap(flow,leg,user)
        app.refresh_from_db()
    # Attribution was explicitly signed by aftersales before dispatch. Delivery now closes it.
    if t['kind']=='REPAIR':
        app.applicant_confirmed_at=timezone.now();app.applicant_confirm_mode='MANUAL';app.applicant_confirm_due_at=None;app.save()
    original=User.objects.get(pk=t['attribution']['by'])
    values=t['attribution']['data']
    S._close_application(app,user=user,identity=get_active_identity(user),data=values,event_type='FACTORY_TRANSPORT_DELIVERED_AND_CLOSED')
    app.closed_by=original;app.save(update_fields=['closed_by','updated_at'])

def _leg(t,data):
    direction=data.get('direction')
    if direction not in {'inbound','outbound'}:B.error('返厂交接方向无效')
    index=data.get('index')
    if type(index) is not int or index<0 or index>=len(t[direction]):B.error('返厂交接段无效')
    return direction,index,t[direction][index]

@transaction.atomic
def execute(application_id,user,data):
    app=AfterSalesApplication.objects.select_for_update().select_related('device__batch__manufacturer','factory_organization','applicant','assigned_user').get(pk=application_id)
    if not S.can_view_application(application=app,user=user):B.error('无权查看该返厂单')
    if data.get('action')=='START_FACTORY_TRANSPORT':
        if not can_start(app,user):B.error('仅厂家售后可接入尚未发出返件的历史单')
        begin(app,user,'REPLACEMENT' if ReplacementFlow.objects.filter(application=app,mode='FACTORY').exists() else 'REPAIR','DIRECT')
        return S.serialize(app)
    t=deepcopy(get(app))
    if not t or app.status in {'CANCELLED','CLOSED'}:B.error('该单不能办理返厂交接')
    action=data.get('action');now=timezone.now();iso=now.isoformat()
    if action=='FACTORY_TRANSPORT_PICKUP':
        if t['inbound'] or t['state']!='INBOUND' or not logistics(user,app) or data.get('confirmed') is not True:B.error('仅厂家物流可明确确认取货，不能重复确认')
        _factory_received(app,t,user)
        S._complete_tasks(app,steps=['factory-transport-pickup'],actor_openid=user.wx_openid or '')
    elif action=='FACTORY_TRANSPORT_PREPARE_SWAP':
        if t['state']!='FACTORY_RECEIVED' or t['kind']!='REPLACEMENT' or t.get('prepared_device_id') or not S._is_factory_aftersales(app,user) or not S._is_assignee(app,user):B.error('仅当前厂家售后可在物流收货后扫描准备换新')
        new,record,state=B._new_stock(app,app.factory_organization_id,data.get('qr_text'),user)
        # Reserve for this service case, not a sale or an outbound booking.
        ev=B._event(app,new,'FACTORY_REPLACEMENT_RESERVED',user,transport_version=1)
        from sales.models import FactoryFinishedGoodsInventoryItem
        pool=FactoryFinishedGoodsInventoryItem.objects.select_for_update().filter(device=new).first()
        if pool:
            if pool.stock_status!='PENDING_REISSUE':B.error('该成品库存已被其他计划占用')
            pool.stock_status='READY_TO_SHIP';pool.save(update_fields=['stock_status'])
        record.status='TRANSFERRED';record.last_event=ev;record.save(update_fields=['status','last_event','updated_at'])
        app.replacement_device=new;t['prepared_device_id']=new.pk
        flow=ReplacementFlow.objects.get(application=app,mode='FACTORY')
        ReplacementLeg.objects.create(flow=flow,direction='USER',sequence=1,from_organization=app.factory_organization,to_organization=app.customer_organization,device=new,status='PENDING')
        app.execution_stage='FINAL_DIAGNOSIS';_save(app,t)
    elif action=='FACTORY_TRANSPORT_FINALIZE':
        if t['state']!='FACTORY_RECEIVED' or not S._is_assignee(app,user) or not S._is_factory_aftersales(app,user):B.error('仅当前厂家售后可在收货后归因')
        if t['kind']=='REPAIR' and not t.get('repair_completed'):B.error('请先完成厂家实际维修并上传记录')
        if t['kind']=='REPLACEMENT' and not t.get('prepared_device_id'):B.error('请先扫描准备换新设备')
        fault,materials,solution,note=S._validate_final_data(app,data)
        if t['kind']=='REPLACEMENT' and solution!='DEVICE_REPLACEMENT':B.error('直接返厂换新请选择整机换新方案')
        if t['kind']=='REPAIR' and solution=='DEVICE_REPLACEMENT':B.error('当前是原机返厂维修，不能填为整机换新')
        final={key:deepcopy(data.get(key)) for key in ['final_fault_type','final_fault_materials','final_fault_causes','final_solution_type','final_solution_note']}
        t['attribution']={'by':user.pk,'at':iso,'data':final};t['state']='READY_TO_RETURN';t['outbound'][0]['ready_at']=iso
        app.final_fault_type=fault;app.final_fault_materials=materials;app.final_solution_type=solution;app.final_solution_note=note
        app.status='PROCESSING';app.execution_stage='AWAITING_RETURN_SHIPMENT';_save(app,t)
        S._complete_assignee_workorder(app,actor_openid=user.wx_openid or '')
        _notify(app,t,'outbound',0,user)
    elif action in {'FACTORY_TRANSPORT_SEND','FACTORY_TRANSPORT_RECEIVE'}:
        direction,index,leg=_leg(t,data)
        if direction=='outbound' and not t.get('attribution'):B.error('请先由厂家售后完成处理归因')
        if index and t[direction][index-1]['status']!='RECEIVED':B.error('前段尚未接收，禁止越级交接')
        if action=='FACTORY_TRANSPORT_SEND':
            if leg['status']!='PENDING' or not actor(app,user,leg['from']):B.error('仅本段发送岗位可发出，不能重复发送')
            info=S._logistics_payload(data);info['evidence_ids']=_proof(app,user,data,leg['ready_at'])
            leg.update(status='SENT',sent_by=user.pk,sent_at=iso,logistics=info)
            if direction=='outbound':
                t['state']='OUTBOUND'
                if t['kind']=='REPLACEMENT' and index==0:
                    Device.objects.select_for_update().get(pk=t['prepared_device_id'])
                    if not OrganizationDevice.objects.select_for_update().filter(device_id=t['prepared_device_id'],organization_id=app.factory_organization_id,status='TRANSFERRED').exists():B.error('准备换新的设备当前归属或预留状态已改变，请核对')
                    B.enforce_device_operation(device_id=t['prepared_device_id'],operation='SHIPMENT',user=user,context={'source':'AFTERSALES_REPLACEMENT','application_id':app.pk})
                    flow=ReplacementFlow.objects.get(application=app,mode='FACTORY');record=flow.legs.get(direction='USER')
                    from sales.models import FactoryFinishedGoodsInventoryItem
                    FactoryFinishedGoodsInventoryItem.objects.filter(device_id=t['prepared_device_id'],stock_status='READY_TO_SHIP').update(stock_status='REISSUED')
                    DeviceState.objects.filter(device_id=t['prepared_device_id']).update(status='SHIPPED')
                    record.status='SENT';record.sent_by=user;record.sent_at=now;record.logistics=info;record.save()
            _notify(app,t,direction,index,user,receive=True)
        else:
            if leg['status']!='SENT' or data.get('confirmed') is not True or not actor(app,user,leg['to']):B.error('仅本段接收岗位可明确确认收到，不能重复接收')
            leg.update(status='RECEIVED',received_by=user.pk,received_at=iso)
            if index+1<len(t[direction]):t[direction][index+1]['ready_at']=iso;_notify(app,t,direction,index+1,user)
            elif direction=='inbound':_factory_received(app,t,user)
            else:
                _save(app,t);_event(app,'FACTORY_TRANSPORT_RECEIVED',user,direction=direction,index=index,from_org=leg['from'],to_org=leg['to'])
                S._complete_tasks(app,steps=[f'factory-transport-{direction}-{index}-receive'],actor_openid=user.wx_openid or '')
                _delivered(app,t,user);return S.serialize(app)
        S._complete_tasks(app,steps=[f'factory-transport-{direction}-{index}-'+('send' if action=='FACTORY_TRANSPORT_SEND' else 'receive')],actor_openid=user.wx_openid or '')
        _save(app,t)
    else:B.error('未知返厂交接操作')
    trace={}
    if action in {'FACTORY_TRANSPORT_SEND','FACTORY_TRANSPORT_RECEIVE'}:trace={'from_org':leg['from'],'to_org':leg['to']}
    _event(app,action,user,direction=data.get('direction'),index=data.get('index'),**trace)
    return S.serialize(app)

def describe(app,user):
    t=get(app)
    if not t:return None
    from account.models import Organization
    names=dict(Organization.objects.filter(pk__in=t['participants']).values_list('pk','name'))
    rows=[];active=app.status not in {'CANCELLED','CLOSED'}
    for direction in ['inbound','outbound']:
        for n,leg in enumerate(t[direction]):
            enabled=active and (direction=='inbound' or bool(t.get('attribution'))) and (n==0 or t[direction][n-1]['status']=='RECEIVED')
            rows.append({**leg,'key':direction+'-'+str(n),'direction':direction,'index':n,'from_name':names.get(leg['from'],''),'to_name':names.get(leg['to'],''),
                'can_send':enabled and leg['status']=='PENDING' and actor(app,user,leg['from']),
                'can_receive':enabled and leg['status']=='SENT' and actor(app,user,leg['to'])})
    af=active and S._is_factory_aftersales(app,user) and S._is_assignee(app,user) and t['state']=='FACTORY_RECEIVED'
    return {'kind':t['kind'],'route':t['route'],'state':t['state'],'legs':rows,'prepared_device_id':t.get('prepared_device_id'),'device_sn':app.replacement_device.sn if t.get('prepared_device_id') else app.device.sn,
        'can_prepare':af and t['kind']=='REPLACEMENT' and not t.get('prepared_device_id'),
        'can_finalize':af and bool(t.get('repair_completed') or t.get('prepared_device_id')),
        'can_pickup':active and not t['inbound'] and t['state']=='INBOUND' and logistics(user,app)}
