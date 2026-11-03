from contextlib import ExitStack
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from django.test import TestCase
from django.utils import timezone
from account.models import OrganizationRelation
from lifecycle.models import (Device, DeviceState, OrganizationDevice, QRCode, ActivationRecord, CustomerDeviceBinding,
    CustomerVehicle, DriverVehicleBinding, PhysicalDeviceCode, AfterSalesMedia, ReplacementFlow, ReplacementLeg,
    ReplacementInventoryEntry, InstallationRecord, VehicleDeviceAssignment, AfterSalesApplication)
from lifecycle.services.aftersales_service import AfterSalesService as S, AfterSalesError
from lifecycle.services.customer_service import CustomerDeviceService as C, CustomerFlowStateError, CustomerFlowPermissionError
from lifecycle.services.repair_escalation_service import execute as api_execute
from lifecycle.services.replacement_flow_service import execute, approve, pending_for_plan, statistics, describe
from lifecycle.services.aftersales_access import visible_cases
import lifecycle.test_aftersales_access_basis as fixtures

class ReplacementFlowTests(TestCase):
    user=classmethod(fixtures.AfterSalesAccessBasisTests.user.__func__)
    @classmethod
    def setUpTestData(cls):
        fixtures.AfterSalesAccessBasisTests.setUpTestData.__func__(cls)
        cls.relation=OrganizationRelation.objects.create(source_organization=cls.factory,target_organization=cls.merchant,relation_type='DISTRIBUTION')
        cls.warehouse,_=cls.user('supply-warehouse',cls.factory,'factory_stock')
        cls.logistics,_=cls.user('supply-logistics',cls.factory,'factory_logistics')
        cls.factory_sales,_=cls.user('supply-sales',cls.factory,'factory_sales')
        cls.case.execution_stage='PENDING_ESCALATION'
        cls.case.escalation={'requested':'REPLACEMENT','holder':'EXECUTOR','source_user_id':cls.sales.pk,
            'source_org_id':cls.merchant.pk,'source_route':'SERVICE_STATION','source_assignee':deepcopy(cls.case.assignee_snapshot)}
        cls.case.save()
        cls.original_time=timezone.now()-timedelta(days=200)
        old=ActivationRecord.objects.create(device=cls.device,organization=cls.customer,operator=cls.customer_owner,
            activation_method='VEHICLE_PLATE',vehicle_no='渝A12345',vehicle_info={'type':'truck'})
        ActivationRecord.objects.filter(pk=old.pk).update(activation_time=cls.original_time)
        DeviceState.objects.create(device=cls.device,current_organization=cls.customer,status='ACTIVATED',install_status='INSTALLED',activation_status='ACTIVATED')
        cls.vehicle=CustomerVehicle.objects.create(customer_organization=cls.customer,vehicle_no='渝A12345',normalized_vehicle_no='渝A12345')
        cls.binding=CustomerDeviceBinding.objects.create(device=cls.device,customer_organization=cls.customer,
            source_organization=cls.merchant,vehicle=cls.vehicle,vehicle_no='渝A12345',customer_type='FLEET')
        InstallationRecord.objects.create(device=cls.device,organization=cls.merchant,operator=cls.installer)
        VehicleDeviceAssignment.objects.create(device=cls.device,customer_organization=cls.customer,vehicle=cls.vehicle,source_binding=cls.binding)
        QRCode.objects.create(code='OLD-LIFECYCLE',device=cls.device,status='ACTIVE')

    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        for name in ('_publish_task','_complete_tasks','_complete_factory_workorder','_send_factory_close_task','_publish_applicant_notice','_publish_supplier_quality_notices'):
            self.stack.enter_context(patch.object(S,name))
        self.stack.enter_context(patch('lifecycle.services.replacement_flow_service.enforce_device_operation',return_value={'allowed':True}))
        self.stack.enter_context(patch('lifecycle.services.customer_service.enforce_device_operation',return_value={'allowed':True}))

    def stock(self,org,code='NEW-LIFECYCLE'):
        d=Device.objects.create(sn=code,product_model=self.product,batch=self.batch)
        DeviceState.objects.create(device=d,current_organization=org,status='IN_STOCK')
        OrganizationDevice.objects.create(device=d,organization=org,status='IN_STOCK')
        QRCode.objects.create(code=code,device=d,status='ACTIVE');return d

    def proof(self,user):
        AfterSalesMedia.objects.create(application=self.case,purpose='REPAIR_RETURN_SHIPMENT_DOCUMENT',media_type='IMAGE',file='test.jpg',uploaded_by=user)

    def approve(self,mode='LOCAL'):
        result=api_execute(application_id=self.case.pk,user=self.af,data={'action':'DECIDE','decision':'REPLACEMENT',
            'replacement_flow_version':2,'execution_mode':mode,'receiving_address':'厂家地址'})
        self.case.refresh_from_db();return ReplacementFlow.objects.get(application=self.case)

    def call(self,action,user,**kwargs):
        leg=ReplacementLeg.objects.filter(pk=kwargs.get('leg_id')).first()
        if action=='SEND_REPLACEMENT_LEG' and leg and leg.direction=='SUPPLY' and leg.sequence==1:
            from sales.models import OrderPlanShipment
            shipment=OrderPlanShipment.objects.filter(pk=kwargs.get('shipment_id') or 0).first()
            self.prepare_supply(leg,shipment.order_plan if shipment else None)
            user=self.warehouse;self.proof(user)
            kwargs.update(handover_method='SHIPMENT',note='补发',logistics_company='顺丰',tracking_no='SUPPLY-SF')
        if action=='SCAN_SWAP' and user==self.af and kwargs.get('handover_method')=='IN_PERSON':
            kwargs.update(handover_method='SHIPMENT',logistics_company='顺丰',tracking_no='FACTORY-NEW')
        return api_execute(application_id=self.case.pk,user=user,data={'action':action,**kwargs})

    def prepare_supply(self,leg,normal_order=None):
        from sales.models import ReplacementSupplyPlan,ReplacementSupplyPlanItem
        if ReplacementSupplyPlanItem.objects.filter(leg=leg,status='RESERVED').exists():return
        for old in leg.flow.legs.filter(direction='OLD').order_by('sequence'):
            if old.status=='PENDING':
                from account.models import UserOrganization
                actor=UserOrganization.objects.filter(organization=old.from_organization,is_active=True).select_related('user').first().user
                self.send(old,actor,'OLD-LIFECYCLE');old.refresh_from_db()
            if old.status=='SENT':self.receive(old,self.logistics if old.to_organization_id==self.factory.pk else self.owner)
        plan=ReplacementSupplyPlan.objects.create(factory=self.factory,merchant=leg.to_organization,normal_order=normal_order,created_by=self.factory_sales,client_request_id='fixture-'+str(leg.pk))
        ReplacementSupplyPlanItem.objects.create(plan=plan,leg=leg)

    def swap(self,method='VEHICLE_PLATE'):
        ActivationRecord.objects.filter(device=self.device).update(activation_method=method)
        flow=self.approve();new=self.stock(self.merchant);self.proof(self.sales)
        self.call('SCAN_SWAP',self.sales,qr_text='NEW-LIFECYCLE',handover_method='IN_PERSON',note='现场换新')
        leg=flow.legs.get(direction='USER');self.proof(self.customer_owner)
        self.call('RECEIVE_REPLACEMENT_LEG',self.customer_owner,leg_id=leg.pk,confirmed=True,receipt_note='新机签收',qr_text='NEW-LIFECYCLE')
        flow.refresh_from_db();return flow,new

    def send(self,leg,user,qr):
        self.proof(user)
        evidence={}
        if leg.direction=='OLD':
            image=AfterSalesMedia.objects.create(application=self.case,purpose='REPAIR_RETURN_SHIPMENT_DOCUMENT',media_type='IMAGE',file='old.jpg',uploaded_by=user)
            doc=AfterSalesMedia.objects.create(application=self.case,purpose='REPAIR_RETURN_SHIPMENT_DOCUMENT',media_type='IMAGE',file='waybill.jpg',uploaded_by=user)
            evidence={'device_image_ids':[image.pk],'logistics_document_ids':[doc.pk]}
        return self.call('SEND_REPLACEMENT_LEG',user,leg_id=leg.pk,qr_text=qr,
            handover_method='SHIPMENT',note='按链路发出',logistics_company='顺丰',tracking_no='SF-'+str(leg.pk),**evidence)

    def receive(self,leg,user):
        if leg.direction=='OLD' and leg.to_organization_id==self.factory.pk:user=self.logistics
        self.proof(user)
        return self.call('RECEIVE_REPLACEMENT_LEG',user,leg_id=leg.pk,confirmed=True,receipt_note='核对设备及单据已签收')

    def test_local_scan_is_real_stock_out_not_sales(self):
        flow=self.approve();new=self.stock(self.merchant);self.proof(self.sales)
        self.call('SCAN_SWAP',self.sales,qr_text='NEW-LIFECYCLE',handover_method='IN_PERSON',note='现场换新')
        self.assertEqual(OrganizationDevice.objects.get(device=new).status,'TRANSFERRED')
        self.assertEqual(ReplacementInventoryEntry.objects.get(kind='LOCAL_OUT').quantity,-1)
        self.assertEqual(ActivationRecord.objects.filter(device=new).count(),0)
        self.assertIsNone(ReplacementFlow.objects.get(pk=flow.pk).swapped_at)
        with self.assertRaises(AfterSalesError):self.call('SCAN_SWAP',self.sales,qr_text='NEW-LIFECYCLE')

    def test_plate_auto_activates_new_with_first_time_and_stops_old(self):
        flow,new=self.swap()
        act=ActivationRecord.objects.get(device=new)
        self.assertEqual(act.activation_time,self.original_time);self.assertEqual(act.vehicle_no,'渝A12345')
        self.assertEqual(act.status,'ACTIVATED');self.assertEqual(DeviceState.objects.get(device=new).activation_status,'ACTIVATED')
        self.assertEqual(CustomerDeviceBinding.objects.get(device=self.device).status,'INACTIVE')
        self.assertEqual(ActivationRecord.objects.get(device=self.device).status,'CANCELLED')
        self.assertFalse(VehicleDeviceAssignment.objects.filter(device=self.device,is_current=True).exists())
        self.assertEqual(VehicleDeviceAssignment.objects.get(device=new,is_current=True).vehicle_id,self.vehicle.pk)
        # Actual API draft: new device can immediately start another after-sales case.
        result,_created=S.save_draft(device_id=new.pk,user=self.customer_owner,data={'fault_description':'下一次故障'})
        self.assertEqual(result['device']['sn'],new.sn)
        with self.assertRaises((AfterSalesError,CustomerFlowStateError,CustomerFlowPermissionError)):S.save_draft(device_id=self.device.pk,user=self.customer_owner,data={})

    def test_device_code_requires_actual_rescan_and_inherits_original_time(self):
        flow,new=self.swap('DEVICE_CODE')
        self.assertFalse(ActivationRecord.objects.filter(device=new).exists())
        self.assertEqual(DeviceState.objects.get(device=new).activation_status,'NOT_ACTIVATED')
        activation=C.device_result(device_id=new.pk,user=self.customer_owner)['activation']
        self.assertEqual(activation['required_activation_method'],'DEVICE_CODE')
        self.assertEqual(activation['activated_at'],'')
        self.assertEqual(activation['inherited_first_activation_at'],self.original_time.isoformat())
        with self.assertRaises(AfterSalesError):S.save_draft(device_id=new.pk,user=self.customer_owner,data={})
        with self.assertRaises(CustomerFlowStateError):C.activate(device_id=new.pk,user=self.customer_owner,activation_method='VEHICLE_PLATE',vehicle_no='渝A12345')
        PhysicalDeviceCode.objects.create(code='PHYSICAL-NEW',factory_root=self.factory)
        C.activate(device_id=new.pk,user=self.customer_owner,activation_method='DEVICE_CODE',physical_device_code='PHYSICAL-NEW')
        act=ActivationRecord.objects.get(device=new)
        self.assertEqual(act.activation_time,self.original_time)
        self.assertEqual(act.physical_device_code_snapshot,'PHYSICAL-NEW')
        self.assertEqual(S.context(device_id=new.pk,user=self.customer_owner)['activation']['activated_at'],self.original_time.isoformat())

    def test_pending_activation_cannot_use_packaging_code(self):
        flow,new=self.swap('DEVICE_CODE')
        with self.assertRaises(CustomerFlowStateError):C.activate(device_id=new.pk,user=self.customer_owner,activation_method='DEVICE_CODE',physical_device_code='NEW-LIFECYCLE')
        self.assertFalse(ActivationRecord.objects.filter(device=new).exists())

    def test_unstocked_wrong_role_and_manual_sn_are_rejected(self):
        self.approve();new=self.stock(self.merchant);self.proof(self.sales)
        for user,qr in [(self.plain,'NEW-LIFECYCLE'),(self.sales,'NO-LIFECYCLE')]:
            with self.assertRaises(AfterSalesError):self.call('SCAN_SWAP',user,qr_text=qr,handover_method='IN_PERSON',note='换新')
        OrganizationDevice.objects.filter(device=new).update(status='TRANSFERRED')
        with self.assertRaises(AfterSalesError):self.call('SCAN_SWAP',self.sales,qr_text='NEW-LIFECYCLE',handover_method='IN_PERSON',note='换新')

    def test_user_wrong_qr_and_unconfirmed_receipt_do_not_replace(self):
        self.approve();self.stock(self.merchant);self.proof(self.sales)
        self.call('SCAN_SWAP',self.sales,qr_text='NEW-LIFECYCLE',handover_method='IN_PERSON',note='换新')
        leg=ReplacementLeg.objects.get(direction='USER');self.proof(self.customer_owner)
        for payload in ({'confirmed':False,'qr_text':'NEW-LIFECYCLE'},{'confirmed':True,'qr_text':'OLD-LIFECYCLE'}):
            with self.assertRaises(AfterSalesError):self.call('RECEIVE_REPLACEMENT_LEG',self.customer_owner,leg_id=leg.pk,receipt_note='签收',**payload)
        self.assertEqual(ActivationRecord.objects.get(device=self.device).status,'ACTIVATED')

    def test_old_return_and_supply_follow_chain_without_finance(self):
        flow,new=self.swap()
        oldleg=flow.legs.get(direction='OLD');self.send(oldleg,self.sales,'OLD-LIFECYCLE');self.receive(oldleg,self.af)
        self.assertEqual(OrganizationDevice.objects.get(device=self.device).status,'RETURNED_TO_FACTORY')
        self.assertFalse(ReplacementInventoryEntry.objects.filter(organization=self.factory).exists())
        supply=self.stock(self.factory,'SUPPLY-CODE');supplyleg=flow.legs.get(direction='SUPPLY')
        self.send(supplyleg,self.af,'SUPPLY-CODE');self.receive(supplyleg,self.owner)
        self.assertEqual(OrganizationDevice.objects.get(device=supply).organization_id,self.merchant.pk)
        self.assertEqual(OrganizationDevice.objects.get(device=supply).status,'IN_STOCK')
        self.assertEqual(ReplacementInventoryEntry.objects.get(kind='SUPPLY_IN').quantity,1)
        self.assertTrue(describe(AfterSalesApplication.objects.get(pk=self.case.pk),self.sales)['supply_complete'])
        from sales.models import OrderPlanFinance,OrderPlanShipment
        from lifecycle.models import GoodsTransferAccountCase
        self.assertFalse(OrderPlanFinance.objects.exists());self.assertFalse(GoodsTransferAccountCase.objects.exists());self.assertFalse(OrderPlanShipment.objects.exists())

    def test_repeated_signature_is_blocked_and_stock_fact_unique(self):
        flow,new=self.swap();leg=flow.legs.get(direction='OLD');self.send(leg,self.sales,'OLD-LIFECYCLE');self.receive(leg,self.af)
        with self.assertRaises(AfterSalesError):self.receive(leg,self.af)
        self.assertEqual(ReplacementInventoryEntry.objects.filter(leg=leg,kind='OLD_OUT').count(),1)

    def test_multihop_cannot_ship_ahead_or_direct(self):
        OrganizationRelation.objects.create(source_organization=self.merchant,target_organization=self.other_merchant,relation_type='DISTRIBUTION')
        other,identity=self.user('lower-op',self.other_merchant,'merchant_electrician')
        es=self.case.escalation;es.update(source_org_id=self.other_merchant.pk,source_user_id=other.pk,source_route='ORIGINAL_INSTALLER',source_assignee={})
        self.case.assigned_user=other;self.case.assigned_organization=self.other_merchant;self.case.escalation=es;self.case.save()
        flow=self.approve();self.assertEqual(flow.chain,[self.factory.pk,self.merchant.pk,self.other_merchant.pk])
        oldsecond=flow.legs.get(direction='OLD',sequence=2)
        with self.assertRaises(AfterSalesError):self.send(oldsecond,self.owner,'OLD-LIFECYCLE')
        self.assertEqual(oldsecond.from_organization_id,self.merchant.pk)
        self.assertEqual(flow.legs.filter(direction='SUPPLY').count(),2)

    def test_ambiguous_channel_is_not_guessed(self):
        OrganizationRelation.objects.create(source_organization=self.other_merchant,target_organization=self.merchant,relation_type='DISTRIBUTION')
        with self.assertRaises(AfterSalesError):self.approve()
        self.assertFalse(ReplacementFlow.objects.exists())

    def test_factory_swap_requires_returns_and_has_no_stock_ledger(self):
        flow=self.approve('FACTORY');new=self.stock(self.factory)
        with self.assertRaises(AfterSalesError):self.call('SCAN_SWAP',self.af,qr_text='NEW-LIFECYCLE')
        oldleg=flow.legs.get(direction='OLD');self.send(oldleg,self.sales,'OLD-LIFECYCLE');self.receive(oldleg,self.af)
        self.proof(self.af);self.call('SCAN_SWAP',self.af,qr_text='NEW-LIFECYCLE',handover_method='IN_PERSON',note='厂家换新交付')
        leg=flow.legs.get(direction='USER');self.proof(self.customer_owner)
        self.call('RECEIVE_REPLACEMENT_LEG',self.customer_owner,leg_id=leg.pk,confirmed=True,receipt_note='新机签收',qr_text='NEW-LIFECYCLE')
        self.assertFalse(ReplacementInventoryEntry.objects.filter(organization=self.factory).exists())
        self.assertFalse(flow.legs.filter(direction='SUPPLY').exists())

    def test_pending_chain_demands_on_new_order_detail_source(self):
        flow,new=self.swap()
        plan=SimpleNamespace(factory_organization_id=self.factory.pk,merchant_organization_id=self.merchant.pk)
        self.assertEqual(pending_for_plan(plan),[])
        oldleg=flow.legs.get(direction='OLD');self.send(oldleg,self.sales,'OLD-LIFECYCLE');self.receive(oldleg,self.logistics)
        demands=pending_for_plan(plan);self.assertEqual(len(demands),1);self.assertEqual(demands[0]['application_id'],self.case.pk)
        self.stock(self.factory,'SUPPLY');leg=flow.legs.get(direction='SUPPLY');self.send(leg,self.af,'SUPPLY')
        self.assertEqual(pending_for_plan(plan),[])

    def test_invalid_shipment_link_cannot_change_any_sales_or_stock(self):
        flow,new=self.swap();d=self.stock(self.factory,'SUPPLY');leg=flow.legs.get(direction='SUPPLY')
        with self.assertRaises(AfterSalesError):self.call('SEND_REPLACEMENT_LEG',self.af,leg_id=leg.pk,qr_text='SUPPLY',shipment_id=999999)
        self.assertEqual(OrganizationDevice.objects.get(device=d).status,'IN_STOCK')
        self.assertIsNone(ReplacementLeg.objects.get(pk=leg.pk).device_id)

    def test_driver_vehicle_access_survives_swap_and_revocation_is_enforced(self):
        DriverVehicleBinding.objects.create(driver=self.driver,vehicle=self.vehicle,is_active=True)
        flow,new=self.swap()
        self.assertTrue(visible_cases(self.driver).filter(pk=self.case.pk).exists())
        C._owned_device_context(device_id=new.pk,user=self.driver)
        DriverVehicleBinding.objects.filter(driver=self.driver).update(is_active=False)
        self.assertFalse(visible_cases(self.driver).filter(pk=self.case.pk).exists())
        with self.assertRaises(Exception):C._owned_device_context(device_id=new.pk,user=self.driver)

    def test_statistical_replacement_is_not_repair_or_settlement(self):
        flow,new=self.swap()
        result=statistics(self.af);self.assertEqual(result['replacement_completed_count'],1)
        self.assertEqual(result['replacement_pending_supply_count'],1)
        self.assertEqual(result['replacement_open_return_count'],1)
        from lifecycle.services.repair_basis_service import summary
        self.assertEqual(summary(self.af)['repair_completed_visits'],0)

    def test_legacy_dispatch_endpoints_cannot_bypass_v2(self):
        self.approve()
        for action in ['DELIVER_REPLACEMENT','CONFIRM_REPLACEMENT','HANDOVER']:
            with self.assertRaises(AfterSalesError):self.call(action,self.af)

    def normal_shipment(self):
        from product.models import Brand
        from channel.models import MerchantChannelAssignment
        from sales.models import OrderPlan,OrderPlanFinance,OrderPlanShipment,OrderPlanLogisticsDocument
        brand=Brand.objects.get(code='BASIS-BRAND')
        channel,_=MerchantChannelAssignment.objects.get_or_create(relation=self.relation,defaults={'brand':brand,'region':'重庆','assigned_by':self.af})
        plan=OrderPlan.objects.create(plan_code='NORMAL-SALES',client_request_id='NORMAL-SALES',factory_organization=self.factory,
            merchant_organization=self.merchant,merchant_relation=self.relation,channel_assignment=channel,brand=brand,
            region_snapshot='重庆',created_by=self.af,total_quantity=10,status='SHIPPED')
        OrderPlanFinance.objects.create(order_plan=plan,receivable_amount=10000)
        document=OrderPlanLogisticsDocument.objects.create(order_plan=plan,sequence_no=1,client_request_id='DOC',
            document_file='test.pdf',file_type='PDF',file_size=10,uploaded_by=self.af)
        shipment=OrderPlanShipment.objects.create(order_plan=plan,sequence_no=1,client_request_id='NORMAL-SHIP',status='SHIPPED',
            logistics_company='顺丰',tracking_no='NORMAL-SF',logistics_document=document)
        return plan,shipment

    def test_shared_sales_shipment_copies_logistics_but_never_sales_quantity_or_receivable(self):
        flow,new=self.swap();supply=self.stock(self.factory,'SUPPLY')
        plan,shipment=self.normal_shipment();leg=flow.legs.get(direction='SUPPLY')
        self.call('SEND_REPLACEMENT_LEG',self.af,leg_id=leg.pk,qr_text='SUPPLY',shipment_id=shipment.pk)
        leg.refresh_from_db();self.assertEqual(leg.logistics['tracking_no'],'NORMAL-SF');self.assertEqual(leg.shipment_id,shipment.pk)
        self.assertEqual(leg.status,'SENT')
        plan.refresh_from_db();self.assertEqual(plan.total_quantity,10);self.assertEqual(plan.finance.receivable_amount,10000)
        from sales.models import OrderPlanShipmentDevice
        self.assertFalse(OrderPlanShipmentDevice.objects.filter(device=supply).exists())
        self.receive(leg,self.owner)
        self.assertEqual(OrganizationDevice.objects.get(device=supply).organization_id,self.merchant.pk)
        self.assertEqual(plan.finance.receivable_amount,10000)

    def test_cancelled_normal_shipment_cannot_be_used_as_carrier(self):
        flow,new=self.swap();self.stock(self.factory,'SUPPLY');plan,shipment=self.normal_shipment()
        shipment.status='CANCELLED';shipment.save(update_fields=['status'])
        leg=flow.legs.get(direction='SUPPLY')
        with self.assertRaises(AfterSalesError):self.call('SEND_REPLACEMENT_LEG',self.af,leg_id=leg.pk,qr_text='SUPPLY',shipment_id=shipment.pk)
        self.assertEqual(ReplacementLeg.objects.get(pk=leg.pk).status,'PENDING')

    def test_second_swap_still_uses_very_first_activation_time(self):
        flow,firstnew=self.swap()
        case=AfterSalesApplication.objects.create(case_no='SECOND-SWAP',device=firstnew,customer_organization=self.customer,
            factory_organization=self.factory,applicant=self.customer_owner,assigned_user=self.sales,assigned_organization=self.merchant,
            dispatch_route='SERVICE_STATION',status='PROCESSING',execution_stage='PENDING_ESCALATION',
            escalation={'requested':'REPLACEMENT','holder':'EXECUTOR','source_user_id':self.sales.pk,'source_org_id':self.merchant.pk,
                        'source_route':'SERVICE_STATION','source_assignee':{'service_station_application_id':self.station.pk}})
        self.case=case;flow=self.approve();nextnew=self.stock(self.merchant,'NEW-TWICE');self.proof(self.sales)
        self.call('SCAN_SWAP',self.sales,qr_text='NEW-TWICE',handover_method='IN_PERSON',note='第二次换新')
        leg=flow.legs.get(direction='USER');self.proof(self.customer_owner)
        self.call('RECEIVE_REPLACEMENT_LEG',self.customer_owner,leg_id=leg.pk,confirmed=True,receipt_note='第二次签收',qr_text='NEW-TWICE')
        self.assertEqual(ActivationRecord.objects.get(device=nextnew).activation_time,self.original_time)
        self.assertEqual(ActivationRecord.objects.get(device=firstnew).status,'CANCELLED')
        from data_statistics.services import get_statistics_identity
        from data_statistics.services.aftersales_service import build_aftersales_statistics
        metrics=build_aftersales_statistics(get_statistics_identity(self.af))['metrics']
        self.assertEqual(metrics['activated_exposure_device_count'],3)
        self.assertLessEqual(metrics['aftersales_fault_rate'],100)

    def test_replacement_receive_failure_rolls_back_binding_and_activation(self):
        flow=self.approve();new=self.stock(self.merchant);self.proof(self.sales)
        self.call('SCAN_SWAP',self.sales,qr_text='NEW-LIFECYCLE',handover_method='IN_PERSON',note='换新')
        leg=flow.legs.get(direction='USER');self.proof(self.customer_owner)
        with patch('lifecycle.services.replacement_flow_service.enforce_device_operation',side_effect=AfterSalesError('质量锁定')):
            with self.assertRaises(AfterSalesError):self.call('RECEIVE_REPLACEMENT_LEG',self.customer_owner,leg_id=leg.pk,confirmed=True,receipt_note='签收',qr_text='NEW-LIFECYCLE')
        self.assertEqual(CustomerDeviceBinding.objects.get(device=self.device).status,'ACTIVE')
        self.assertEqual(ActivationRecord.objects.get(device=self.device).status,'ACTIVATED')
        self.assertFalse(CustomerDeviceBinding.objects.filter(device=new).exists())
        self.assertEqual(ReplacementLeg.objects.get(pk=leg.pk).status,'SENT')

    def test_order_creation_result_includes_chain_replacement_demand(self):
        flow,new=self.swap();plan,shipment=self.normal_shipment()
        old=flow.legs.get(direction='OLD');self.send(old,self.sales,'OLD-LIFECYCLE');self.receive(old,self.logistics)
        from sales.services.order_plan_material_flow_service import _creation_result
        result=_creation_result(plan)
        self.assertEqual(result['pending_replacement_demands'][0]['application_id'],self.case.pk)
        self.assertEqual(result['plan']['total_quantity'],10)

    def test_factory_original_repairer_can_apply_and_replace_without_inventory_entries(self):
        from lifecycle.models import AfterSalesEvent
        self.case.escalation.update(source_org_id=self.factory.pk,source_user_id=self.af.pk,source_route='FACTORY_AFTERSALES',source_assignee={})
        self.case.assigned_user=self.af;self.case.assigned_organization=self.factory;self.case.dispatch_route='FACTORY_AFTERSALES';self.case.save()
        AfterSalesEvent.objects.create(application=self.case,event_type='REPAIRER_RECEIPT_CONFIRMED')
        flow=self.approve('FACTORY');self.assertEqual(flow.chain,[self.factory.pk]);self.assertFalse(flow.legs.filter(direction='OLD').exists())
        new=self.stock(self.factory);self.proof(self.af)
        self.call('SCAN_SWAP',self.af,qr_text='NEW-LIFECYCLE',handover_method='IN_PERSON',note='厂家换新')
        self.proof(self.customer_owner);leg=flow.legs.get(direction='USER')
        self.call('RECEIVE_REPLACEMENT_LEG',self.customer_owner,leg_id=leg.pk,qr_text='NEW-LIFECYCLE',confirmed=True,receipt_note='签收')
        self.assertFalse(ReplacementInventoryEntry.objects.filter(flow=flow).exists())
        self.assertEqual(DeviceState.objects.get(device=self.device).status,'RETURNED_TO_FACTORY')

    def test_complete_multihop_return_and_supply_and_replenished_stock_can_be_used(self):
        OrganizationRelation.objects.create(source_organization=self.merchant,target_organization=self.other_merchant,relation_type='DISTRIBUTION')
        operator,_=self.user('lower-replacement',self.other_merchant,'merchant_electrician')
        self.case.escalation.update(source_org_id=self.other_merchant.pk,source_user_id=operator.pk,source_route='ORIGINAL_INSTALLER',source_assignee={})
        self.case.assigned_user=operator;self.case.assigned_organization=self.other_merchant;self.case.dispatch_route='ORIGINAL_INSTALLER';self.case.save()
        flow=self.approve();new=self.stock(self.other_merchant);self.proof(operator)
        self.call('SCAN_SWAP',operator,qr_text='NEW-LIFECYCLE',handover_method='IN_PERSON',note='装车组织换新')
        leg=flow.legs.get(direction='USER');self.proof(self.customer_owner)
        self.call('RECEIVE_REPLACEMENT_LEG',self.customer_owner,leg_id=leg.pk,qr_text='NEW-LIFECYCLE',confirmed=True,receipt_note='签收')
        old1=flow.legs.get(direction='OLD',sequence=1);old2=flow.legs.get(direction='OLD',sequence=2)
        self.send(old1,operator,'OLD-LIFECYCLE');self.receive(old1,self.owner)
        self.send(old2,self.owner,'OLD-LIFECYCLE');self.receive(old2,self.af)
        supply=self.stock(self.factory,'CHANNEL-SUPPLY')
        sup1=flow.legs.get(direction='SUPPLY',sequence=1);sup2=flow.legs.get(direction='SUPPLY',sequence=2)
        self.send(sup1,self.af,'CHANNEL-SUPPLY');self.receive(sup1,self.owner)
        self.send(sup2,self.owner,'CHANNEL-SUPPLY');self.receive(sup2,operator)
        self.assertFalse(flow.legs.exclude(status='RECEIVED').exists())
        self.assertEqual(OrganizationDevice.objects.get(device=supply).organization_id,self.other_merchant.pk)
        from lifecycle.services.replacement_flow_service import _new_stock
        device,_,_=_new_stock(AfterSalesApplication.objects.get(pk=self.case.pk),self.other_merchant.pk,'CHANNEL-SUPPLY',operator)
        self.assertEqual(device.pk,supply.pk)

    def test_unenabled_lifecycle_code_is_not_consumed(self):
        self.approve();new=self.stock(self.merchant)
        QRCode.objects.filter(device=new).update(status='CREATED');self.proof(self.sales)
        with self.assertRaises(AfterSalesError):self.call('SCAN_SWAP',self.sales,qr_text='NEW-LIFECYCLE',handover_method='IN_PERSON',note='换新')
        self.assertEqual(OrganizationDevice.objects.get(device=new).status,'IN_STOCK')

    def test_personal_customer_reuses_own_current_version_reads_without_fabricating_reads(self):
        from product.models import ProductDocument
        from lifecycle.models import ProductDocumentRead
        CustomerDeviceBinding.objects.filter(device=self.device).update(customer_type='PERSONAL')
        for kind in ['USER_MANUAL','DAILY_MAINTENANCE','TROUBLESHOOTING_GUIDE']:
            doc=ProductDocument.objects.create(product_model=self.product,document_type=kind,title=kind,content='说明')
            ProductDocumentRead.objects.create(user=self.customer_owner,device=self.device,document=doc,document_version=1)
        flow,new=self.swap('DEVICE_CODE')
        PhysicalDeviceCode.objects.create(code='PERSONAL-NEW',factory_root=self.factory)
        C.activate(device_id=new.pk,user=self.customer_owner,activation_method='DEVICE_CODE',physical_device_code='PERSONAL-NEW')
        self.assertEqual(ActivationRecord.objects.get(device=new).activation_time,self.original_time)
        self.assertFalse(ProductDocumentRead.objects.filter(device=new).exists())

    def test_new_request_cannot_downgrade_to_legacy_by_omitting_frontend_flag(self):
        self.case.escalation={};self.case.execution_stage='REPAIRING';self.case.save()
        AfterSalesMedia.objects.create(application=self.case,purpose='REPAIR_EVIDENCE',media_type='IMAGE',file='test.jpg',uploaded_by=self.sales)
        self.call('REQUEST',self.sales,requested='REPLACEMENT',holder='EXECUTOR',reason='无法维修')
        self.call('DECIDE',self.af,decision='REPLACEMENT',receiving_address='厂家地址')
        self.assertTrue(ReplacementFlow.objects.filter(application=self.case).exists())
        with self.assertRaises(AfterSalesError):self.call('DELIVER_REPLACEMENT',self.af,new_device_sn='ANY')
