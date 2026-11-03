from unittest.mock import patch
from django.test import TestCase
from django.db import IntegrityError,transaction
from sales.models import ReplacementSupplyPlan,ReplacementSupplyPlanItem,OrderPlan,OrderPlanShipmentDevice,OrderPlanFinance
from sales.services import replacement_supply_service as R
from sales.services.order_plan_material_flow_service import OrderPlanMaterialFlowError,create_material_flow_order_plan
from lifecycle.models import ReplacementLeg,ReplacementFlow,AfterSalesMedia,OrganizationDevice
from lifecycle.services.repair_escalation_service import execute as execute
from lifecycle.services.aftersales_service import AfterSalesError
from lifecycle.services import replacement_flow_service as B
from lifecycle.test_replacement_v6 import ReplacementV6Tests as Fixture
from sales.test_v2_fixture import V2OrderPlanFixture
from workflow.services.workflow_service import publish_workitem as real_publish_workitem

class ReplacementSupplyTests(TestCase):
    setUpTestData=classmethod(Fixture.setUpTestData.__func__)
    setUp=Fixture.setUp
    user=Fixture.user
    stock=Fixture.stock
    proof=Fixture.proof
    approve=Fixture.approve
    call=Fixture.call
    swap=Fixture.swap
    pickup=Fixture.pickup
    normal_shipment=Fixture.normal_shipment
    send=Fixture.send
    receive=Fixture.receive
    prepare_supply=Fixture.prepare_supply
    fixture_setup=Fixture.setUp
    def setUp(self):
        self.fixture_setup()
        from channel.models import MerchantChannelAssignment
        from product.models import Brand
        MerchantChannelAssignment.objects.create(relation=self.relation,brand=Brand.objects.get(code='BASIS-BRAND'),region='重庆',assigned_by=self.factory_sales)
        from account.models import InvitationRecord,RoleInvitationRule,UserOrganization
        from django.utils import timezone
        from datetime import timedelta
        sales_role=UserOrganization.objects.get(user=self.factory_sales,is_active=True).role
        owner_role=UserOrganization.objects.get(user=self.owner,is_active=True).role
        rule,_=RoleInvitationRule.objects.get_or_create(inviter_role=sales_role,target_role=owner_role,scope='cross_organization',relation_type='DISTRIBUTION')
        invitation=InvitationRecord.objects.create(inviter=self.factory_sales,source_organization=self.factory,inviter_role=sales_role,rule=rule,target_role=owner_role,target_organization=self.merchant,invitee=self.owner,scope='cross_organization',relation_type='DISTRIBUTION',status=InvitationRecord.Status.ACCEPTED,expires_at=timezone.now()+timedelta(days=1),accepted_at=timezone.now())
        self.relation.created_by_invitation=invitation;self.relation.save(update_fields=['created_by_invitation'])
        self.work=self.stack.enter_context(patch('workflow.services.workflow_service.publish_workitem'))
    def raw(self,user,**data):return execute(application_id=self.case.pk,user=user,data=data)
    def make(self,key='standalone'):
        result=R.create(self.factory_sales,self.merchant.pk,key)
        return ReplacementSupplyPlan.objects.get(pk=result['id'])
    def test_receipt_not_attribution_unlocks_two_awareness_notices(self):
        flow,_=self.swap();self.assertEqual(R.candidates(self.factory.pk),[])
        with self.assertRaises(OrderPlanMaterialFlowError):self.make()
        self.pickup(flow)
        self.assertEqual(flow.recovery['state'],'WAIT_DECISION')
        self.assertEqual(len(R.candidates(self.factory.pk)),1)
        call=self.work.call_args.kwargs
        self.assertFalse(call['need_action'])
        from sales.services.order_plan_material_flow_service import _role_openids
        self.assertEqual(set(call['recipients_openids']),set(_role_openids(self.factory,{'factory_sales','factory_stock'})))
        self.assertTrue(any(c.kwargs.get('step')=='replacement-recovery' for c in self.notifications.call_args_list))
    def test_zero_new_order_plan_has_no_finance_production_or_sales_rows(self):
        flow,_=self.swap();self.pickup(flow)
        before=(OrderPlan.objects.count(),OrderPlanFinance.objects.count(),OrderPlanShipmentDevice.objects.count())
        plan=self.make();data=R.serialize(plan,self.warehouse)
        self.assertEqual(data['normal_sale_quantity'],0);self.assertEqual(data['replacement_quantity'],1)
        self.assertIsNone(plan.normal_order_id)
        self.assertEqual(before,(OrderPlan.objects.count(),OrderPlanFinance.objects.count(),OrderPlanShipmentDevice.objects.count()))
        self.assertEqual(R.create(self.factory_sales,self.merchant.pk,'standalone')['id'],plan.pk)
        with self.assertRaises(OrderPlanMaterialFlowError):self.make('second')
        self.assertEqual(ReplacementSupplyPlanItem.objects.filter(status='RESERVED').count(),1)
    def test_aftersales_and_sales_cannot_ship_but_stock_ships_before_diagnosis(self):
        flow,_=self.swap();self.pickup(flow);plan=self.make();leg=flow.legs.get(direction='SUPPLY');new=self.stock(self.factory,'REPLENISH')
        for user in [self.af,self.factory_sales]:
            with self.assertRaises(AfterSalesError):self.raw(user,action='SEND_REPLACEMENT_LEG',leg_id=leg.pk,qr_text='REPLENISH')
        self.proof(self.warehouse)
        self.raw(self.warehouse,action='SEND_REPLACEMENT_LEG',leg_id=leg.pk,qr_text='REPLENISH',note='补发',logistics_company='顺丰',tracking_no='SUPPLY-1')
        flow.refresh_from_db();leg.refresh_from_db();plan.items.get().refresh_from_db()
        self.assertEqual(flow.recovery['state'],'WAIT_DECISION');self.assertEqual(leg.status,'SENT')
        self.assertEqual(plan.items.get().status,'SENT');self.assertFalse(OrderPlanShipmentDevice.objects.filter(device=new).exists())
        with self.assertRaises(OrderPlanMaterialFlowError):R.cancel(plan,self.factory_sales)
    def test_unplanned_stock_send_and_factory_in_person_are_rejected(self):
        flow,_=self.swap();self.pickup(flow);leg=flow.legs.get(direction='SUPPLY');new=self.stock(self.factory,'REPLENISH');self.proof(self.warehouse)
        payload={'action':'SEND_REPLACEMENT_LEG','leg_id':leg.pk,'qr_text':'REPLENISH','note':'补发','logistics_company':'顺丰','tracking_no':'S1'}
        with self.assertRaises(AfterSalesError):self.raw(self.warehouse,**payload)
        self.make()
        with self.assertRaises(AfterSalesError):self.raw(self.warehouse,**payload,handover_method='IN_PERSON')
        self.assertEqual(OrganizationDevice.objects.get(device=new).status,'IN_STOCK')
    def test_normal_order_auto_attach_is_idempotent_and_preserves_receivable(self):
        flow,_=self.swap();self.pickup(flow);order,shipment=self.normal_shipment()
        with transaction.atomic():plan=R.auto_attach(order,self.factory_sales)
        self.assertEqual(plan.items.count(),1)
        with transaction.atomic():self.assertIsNone(R.auto_attach(order,self.factory_sales))
        order.refresh_from_db();self.assertEqual(order.total_quantity,10);self.assertEqual(order.finance.receivable_amount,10000)
        self.assertEqual(R.overview(self.warehouse,order_id=order.pk)['plan']['id'],plan.pk)
        with self.assertRaises(OrderPlanMaterialFlowError):self.make()
    def test_cancel_releases_demand_without_deleting_history(self):
        flow,_=self.swap();self.pickup(flow);plan=self.make()
        R.cancel(plan,self.factory_sales)
        plan.refresh_from_db()
        history=R.serialize(plan,self.warehouse)
        self.assertEqual(history['replacement_quantity'],1)
        self.assertEqual(history['items'][0]['status'],'CANCELLED')
        self.assertFalse(history['items'][0]['can_ship'])
        self.assertEqual(plan.items.get().status,'CANCELLED');self.assertEqual(len(R.candidates(self.factory.pk)),1)
        second=self.make('new');self.assertNotEqual(second.pk,plan.pk)
        with self.assertRaises(IntegrityError),transaction.atomic():ReplacementSupplyPlanItem.objects.create(plan=second,leg=second.items.get().leg)
    def test_normal_order_cancellation_releases_only_unshipped_allocations(self):
        flow,_=self.swap();self.pickup(flow);order,_=self.normal_shipment()
        with transaction.atomic():plan=R.auto_attach(order,self.factory_sales);R.release_normal(order)
        self.assertEqual(plan.items.get().status,'CANCELLED');self.assertEqual(len(R.candidates(self.factory.pk)),1)
    def test_cross_factory_and_merchant_roles_cannot_read_or_create(self):
        flow,_=self.swap();self.pickup(flow);plan=self.make()
        with self.assertRaises(OrderPlanMaterialFlowError):R.overview(self.owner)
        with self.assertRaises(OrderPlanMaterialFlowError):R.create(self.af,self.merchant.pk,'bad')
        other,_=self.user('another-sales',self.other_factory,'factory_sales')
        with self.assertRaises(OrderPlanMaterialFlowError):R.overview(other,plan.pk)
    def test_plan_carrier_must_match_exact_normal_order(self):
        flow,_=self.swap();self.pickup(flow);order,shipment=self.normal_shipment()
        with transaction.atomic():R.auto_attach(order,self.factory_sales)
        leg=flow.legs.get(direction='SUPPLY');self.stock(self.factory,'REPLENISH');self.proof(self.warehouse)
        payload={'action':'SEND_REPLACEMENT_LEG','leg_id':leg.pk,'qr_text':'REPLENISH'}
        with self.assertRaises(AfterSalesError):self.raw(self.warehouse,**payload,shipment_id=999999)
        self.raw(self.warehouse,**payload,shipment_id=shipment.pk)
        leg.refresh_from_db();self.assertEqual(leg.shipment_id,shipment.pk)
        self.assertEqual(leg.logistics['tracking_no'],'NORMAL-SF')

    def test_real_receipt_notice_persists_once_for_sales_and_stock(self):
        from workflow.models import WorkItem
        original=real_publish_workitem
        flow,_=self.swap()
        with patch('workflow.services.workflow_service.publish_workitem',wraps=original):
            self.pickup(flow);R.returned(flow,self.logistics)
        rows=WorkItem.objects.filter(type='REPLACEMENT_SUPPLY_NOTICE')
        self.assertEqual(rows.count(),1)
        work=rows.get();self.assertEqual(work.link,R.LINK)
        self.assertIn(self.factory_sales.wx_openid,set(work.user_tasks.values_list('to_openid',flat=True)))
        self.assertIn(self.warehouse.wx_openid,set(work.user_tasks.values_list('to_openid',flat=True)))
        self.assertFalse(work.user_tasks.filter(need_action=True).exists())

    def test_legacy_receipt_needs_explicit_logistics_ack_without_rewriting_history(self):
        flow,_=self.swap();flow.protocol_version=5;flow.recovery={};flow.save()
        old=flow.legs.get(direction='OLD');old.status='RECEIVED';old.received_by=self.af;old.save()
        self.assertEqual(R.candidates(self.factory.pk),[])
        with self.assertRaises(AfterSalesError):self.raw(self.af,action='CONFIRM_FACTORY_OLD_RECEIPT',confirmed=True)
        before=list(flow.inventory_entries.values_list('pk',flat=True))
        self.raw(self.logistics,action='CONFIRM_FACTORY_OLD_RECEIPT',confirmed=True)
        old.refresh_from_db();self.assertEqual(old.received_by_id,self.af.pk)
        self.assertEqual(before,list(flow.inventory_entries.values_list('pk',flat=True)))
        self.assertEqual(len(R.candidates(self.factory.pk)),1)
        with self.assertRaises(AfterSalesError):self.raw(self.logistics,action='CONFIRM_FACTORY_OLD_RECEIPT',confirmed=True)

class ReplacementOrderCreationHookTests(V2OrderPlanFixture):
    def test_normal_creation_triggers_auto_attach_once_not_on_retry(self):
        payload=dict(user=self.sales_user,merchant_id=self.merchant.pk,client_request_id='supply-hook',items=[{'category_id':self.generator_category.pk,'product_model_id':self.generator.pk,'quantity':1,'source_bom_id':self.generator.boms.get(version='active-v2').pk}])
        with patch.object(R,'auto_attach') as attach:
            first,created=create_material_flow_order_plan(**payload)
            self.assertTrue(created);self.assertEqual(attach.call_count,1)
            second,created=create_material_flow_order_plan(**payload)
            self.assertFalse(created);self.assertEqual(attach.call_count,1)
            self.assertEqual(first['plan']['id'],second['plan']['id'])

# Avoid collecting imported fixture test methods a second time.
del Fixture
