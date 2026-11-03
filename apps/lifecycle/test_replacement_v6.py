from contextlib import ExitStack
from unittest.mock import patch
from django.test import TestCase,RequestFactory
from account.models import Organization,OrganizationType,OrganizationRelation
from account.services.auth_service import issue_access_token
from lifecycle.models import (AfterSalesApplication,ReplacementFlow,ReplacementLeg,ReplacementMaterialReturn,
    ReplacementTurnoverStock,ReplacementInventoryEntry,DeviceState,OrganizationDevice,AfterSalesMedia,ActivationRecord,LifecycleEvent)
from lifecycle.services.aftersales_service import AfterSalesService as S,AfterSalesError
from lifecycle.services.repair_escalation_service import execute as call_api
from lifecycle.services import replacement_v6_service as V
from lifecycle.services import replacement_flow_service as B
from lifecycle.api.views import ReplacementMaterialReturnAPI,ReplacementRecoveredStockAPI
import lifecycle.test_replacement_flow as base

class ReplacementV6Tests(TestCase):
    user=classmethod(base.ReplacementFlowTests.user.__func__)
    stock=base.ReplacementFlowTests.stock
    proof=base.ReplacementFlowTests.proof
    normal_shipment=base.ReplacementFlowTests.normal_shipment
    send=base.ReplacementFlowTests.send
    receive=base.ReplacementFlowTests.receive
    prepare_supply=base.ReplacementFlowTests.prepare_supply
    @classmethod
    def setUpTestData(cls):
        base.ReplacementFlowTests.setUpTestData.__func__(cls)
        cls.logistics,_=cls.user('recovery-logistics',cls.factory,'factory_logistics')
        cls.supplier=Organization.objects.create(code='RECOVERY-SUPPLIER',name='配套厂家',organization_type=OrganizationType.objects.get(code='SUPPLIER'),factory_root=cls.factory)
        cls.supplier_user,_=cls.user('recovery-supplier',cls.supplier,'supplier_owner')
        cls.other_supplier=Organization.objects.create(code='RECOVERY-OTHER',name='另一配套厂家',organization_type=cls.supplier.organization_type,factory_root=cls.factory)
        cls.other_supplier_user,_=cls.user('recovery-other-supplier',cls.other_supplier,'supplier_owner')
        from product.models import BOMItem
        from supply_chain.models import Material,MaterialSpec
        material=Material.objects.create(code='RECOVERY-MAT',name='控制器')
        spec=MaterialSpec.objects.create(code='RECOVERY-SPEC',name='控制器A',material=material)
        cls.bom_item=BOMItem.objects.create(bom=cls.batch.bom,material=material,material_spec=spec,supplier=cls.supplier,quantity=2)
        cls.case.bom_snapshot=S._snapshot(cls.device)['bom'];cls.case.save()
    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        for name in ['_publish_task','_publish_applicant_notice','_complete_tasks','_complete_factory_workorder','_send_factory_close_task','_publish_supplier_quality_notices']:
            mock=self.stack.enter_context(patch.object(S,name))
            if name=='_publish_task':self.notifications=mock
        self.stack.enter_context(patch('lifecycle.services.replacement_flow_service.enforce_device_operation',return_value={'allowed':True}))
    def call(self,action,user=None,**data):
        actor=user or self.sales
        leg=ReplacementLeg.objects.filter(pk=data.get('leg_id')).first()
        if action=='SEND_REPLACEMENT_LEG' and leg and leg.direction=='SUPPLY' and leg.sequence==1:
            from sales.models import OrderPlanShipment
            shipment=OrderPlanShipment.objects.filter(pk=data.get('shipment_id') or 0).first()
            self.prepare_supply(leg,shipment.order_plan if shipment else None);actor=self.warehouse;self.proof(actor)
            data.update(handover_method='SHIPMENT',note='补发',logistics_company='顺丰',tracking_no='SUPPLY-SF')
        if action=='SEND_REPLACEMENT_LEG' and ReplacementLeg.objects.filter(pk=data.get('leg_id'),direction='OLD').exists():
            image=AfterSalesMedia.objects.create(application=self.case,purpose='REPAIR_RETURN_SHIPMENT_DOCUMENT',media_type='IMAGE',file='old.jpg',uploaded_by=actor)
            doc=AfterSalesMedia.objects.create(application=self.case,purpose='REPAIR_RETURN_SHIPMENT_DOCUMENT',media_type='IMAGE',file='waybill.jpg',uploaded_by=actor)
            data.update(device_image_ids=[image.pk],logistics_document_ids=[doc.pk])
        return call_api(application_id=self.case.pk,user=actor,data={'action':action,**data})
    def approve(self,mode='LOCAL'):
        self.call('DECIDE',self.af,decision='REPLACEMENT',replacement_flow_version=3,execution_mode=mode,receiving_address='厂家地址')
        return ReplacementFlow.objects.get(application=self.case)
    def swap(self,method='VEHICLE_PLATE'):
        ActivationRecord.objects.filter(device=self.device).update(activation_method=method)
        flow=self.approve();new=self.stock(self.merchant)
        self.call('CONFIRM_REPLACEMENT_SERVICE',service_mode='HOME')
        self.call('SCAN_SWAP',qr_text='NEW-LIFECYCLE')
        flow.refresh_from_db();return flow,new
    def pickup(self,flow):
        leg=flow.legs.get(direction='OLD')
        self.call('SEND_REPLACEMENT_LEG',leg_id=leg.pk,confirmed=True)
        self.call('FACTORY_PICKUP',self.logistics,confirmed=True)
        flow.refresh_from_db();return leg
    def repair_proof(self):
        AfterSalesMedia.objects.create(application=self.case,purpose='REPAIR_EVIDENCE',media_type='IMAGE',file='repair.jpg',uploaded_by=self.af)

    def test_onsite_swap_requires_only_service_mode_and_one_scan(self):
        flow=self.approve();new=self.stock(self.merchant)
        with self.assertRaises(AfterSalesError):self.call('SCAN_SWAP',qr_text='NEW-LIFECYCLE')
        self.call('CONFIRM_REPLACEMENT_SERVICE',service_mode='SHOP')
        self.call('SCAN_SWAP',qr_text='NEW-LIFECYCLE')
        flow.refresh_from_db();self.case.refresh_from_db()
        self.assertIsNotNone(flow.swapped_at)
        leg=flow.legs.get(direction='USER');self.assertEqual(leg.status,'RECEIVED');self.assertEqual(leg.received_by_id,self.sales.pk)
        self.assertFalse(leg.logistics['customer_online_signature']);self.assertIsNone(self.case.applicant_confirmed_at)
        self.assertEqual(self.case.escalation['delivery_basis'],'ONSITE_OPERATOR_SCAN')
        self.assertEqual(ActivationRecord.objects.get(device=new).activation_time,self.original_time)
        self.assertEqual(ReplacementInventoryEntry.objects.filter(kind='LOCAL_OUT').count(),1)
        with self.assertRaises(AfterSalesError):self.call('SCAN_SWAP',qr_text='NEW-LIFECYCLE')

    def test_device_code_swap_leaves_user_to_scan_physical_code(self):
        flow,new=self.swap('DEVICE_CODE')
        self.assertIsNotNone(flow.swapped_at);self.assertFalse(ActivationRecord.objects.filter(device=new).exists())
        self.assertEqual(DeviceState.objects.get(device=new).activation_status,'NOT_ACTIVATED')

    def test_customer_cannot_be_forced_into_second_signature(self):
        flow,new=self.swap();leg=flow.legs.get(direction='USER')
        with self.assertRaises(AfterSalesError):self.call('RECEIVE_REPLACEMENT_LEG',self.customer_owner,leg_id=leg.pk,confirmed=True)

    def test_return_notification_targets_factory_aftersales_and_logistics(self):
        flow,_=self.swap();leg=flow.legs.get(direction='OLD')
        self.call('SEND_REPLACEMENT_LEG',leg_id=leg.pk,confirmed=True)
        matches=[c.kwargs for c in self.notifications.call_args_list if c.kwargs.get('step')=='replacement-'+str(leg.pk)+'-receive']
        self.assertTrue(matches);self.assertIn(self.af.wx_openid,matches[-1]['recipients_openids']);self.assertIn(self.logistics.wx_openid,matches[-1]['recipients_openids'])
        with self.assertRaises(AfterSalesError):self.call('RECEIVE_REPLACEMENT_LEG',self.af,leg_id=leg.pk,confirmed=True)
        with self.assertRaises(AfterSalesError):self.call('FACTORY_PICKUP',self.af,confirmed=True)
        self.call('FACTORY_PICKUP',self.logistics,confirmed=True)
        self.assertEqual(ReplacementFlow.objects.get(pk=flow.pk).recovery['state'],'WAIT_DECISION')

    def test_delayed_intermediate_return_needs_proofs_but_no_scan(self):
        OrganizationRelation.objects.create(source_organization=self.merchant,target_organization=self.other_merchant,relation_type='DISTRIBUTION')
        operator,_=self.user('onsite-lower',self.other_merchant,'merchant_electrician')
        self.case.escalation.update(source_org_id=self.other_merchant.pk,source_user_id=operator.pk,source_route='ORIGINAL_INSTALLER',source_assignee={})
        self.case.assigned_user=operator;self.case.assigned_organization=self.other_merchant;self.case.save()
        flow=self.approve();self.stock(self.other_merchant)
        self.call('CONFIRM_REPLACEMENT_SERVICE',operator,service_mode='HOME');self.call('SCAN_SWAP',operator,qr_text='NEW-LIFECYCLE')
        first=flow.legs.get(direction='OLD',sequence=1);second=flow.legs.get(direction='OLD',sequence=2)
        self.call('SEND_REPLACEMENT_LEG',operator,leg_id=first.pk,confirmed=True)
        self.call('RECEIVE_REPLACEMENT_LEG',self.owner,leg_id=first.pk,confirmed=True)
        second.refresh_from_db();self.assertEqual(second.status,'PENDING')
        self.assertEqual(OrganizationDevice.objects.get(device=self.device).organization_id,self.merchant.pk)
        self.call('SEND_REPLACEMENT_LEG',self.owner,leg_id=second.pk,confirmed=True)
        second.refresh_from_db();self.assertEqual(second.status,'SENT')

    def test_factory_must_pick_up_before_diagnosis_or_closing(self):
        flow,_=self.swap()
        with self.assertRaises(AfterSalesError):self.call('RECOVERY_DECIDE',self.af,decision='REPAIR',note='检修')
        with self.assertRaises(AfterSalesError):S.factory_close(application_id=self.case.pk,user=self.af,data={'final_fault_type':'OTHER','final_solution_type':'DEVICE_REPLACEMENT','final_solution_note':'结束'})
        self.pickup(flow);self.call('RECOVERY_DECIDE',self.af,decision='REPAIR',note='检修')
        self.assertEqual(ReplacementFlow.objects.get(pk=flow.pk).recovery['state'],'REPAIRING')

    def test_repaired_device_enters_real_finished_stock_and_existing_order_pool(self):
        flow,_=self.swap();self.pickup(flow);self.call('RECOVERY_DECIDE',self.af,decision='REPAIR',note='可修复')
        self.repair_proof();self.call('RECOVERY_REPAIR_COMPLETE',self.af,stock_target='FINISHED',note='维修测试通过')
        from sales.models import FactoryFinishedGoodsInventoryItem
        stock=FactoryFinishedGoodsInventoryItem.objects.get(replacement_flow=flow)
        self.assertEqual(stock.stock_status,'PENDING_REISSUE');self.assertIsNone(stock.source_order_plan_id)
        from sales.services.order_plan_reissue_service import get_return_inventory_options
        options=get_return_inventory_options(factory_organization=self.factory,merchant_organization=self.merchant,product_model=self.product)
        self.assertEqual(options['available_count'],1)
        self.assertEqual(ReplacementFlow.objects.get(pk=flow.pk).recovery['state'],'CLOSED')
        S.factory_close(application_id=self.case.pk,user=self.af,data={'final_fault_type':'OTHER','final_solution_type':'DEVICE_REPLACEMENT','final_solution_note':'旧机已维修入库'})
        self.assertEqual(AfterSalesApplication.objects.get(pk=self.case.pk).status,'CLOSED')
        self.assertTrue(flow.legs.filter(direction='SUPPLY',status='PENDING').exists())

    def test_turnover_pool_does_not_leak_into_normal_finished_stock(self):
        flow,_=self.swap();self.pickup(flow);self.call('RECOVERY_DECIDE',self.af,decision='REPAIR',note='转周转机')
        self.repair_proof();self.call('RECOVERY_REPAIR_COMPLETE',self.af,stock_target='TURNOVER',note='维修测试通过')
        self.assertEqual(ReplacementTurnoverStock.objects.get(flow=flow).status,'AVAILABLE')
        from sales.services.order_plan_reissue_service import get_return_inventory_options
        self.assertEqual(get_return_inventory_options(factory_organization=self.factory,merchant_organization=self.merchant,product_model=self.product)['available_count'],0)
        request=RequestFactory().get('/stock',HTTP_AUTHORIZATION='Bearer '+issue_access_token(self.af))
        result=ReplacementRecoveredStockAPI.as_view()(request)
        self.assertEqual(result.status_code,200);self.assertEqual(result.data['data']['items'][0]['kind'],'周转机库')

    def test_disassembly_real_parts_notify_then_supplier_signs_own_return(self):
        flow,_=self.swap();self.pickup(flow);self.call('RECOVERY_DECIDE',self.af,decision='DISASSEMBLE',note='确认拆机')
        self.repair_proof();self.call('RECOVERY_DISASSEMBLY_COMPLETE',self.af,parts=[{'bom_item_id':self.bom_item.pk,'quantity':'1'}],note='实际拆下控制器')
        part=ReplacementMaterialReturn.objects.get(flow=flow);self.assertEqual(str(part.quantity),'1.000');self.assertEqual(part.supplier_id,self.supplier.pk)
        self.assertEqual(DeviceState.objects.get(device=self.device).status,'SCRAPPED')
        self.proof(self.af);self.call('SEND_WARRANTY_PART',self.af,part_id=part.pk,handover_method='SHIPMENT',logistics_company='顺丰',tracking_no='PART-RETURN',note='物流退料')
        with self.assertRaises(AfterSalesError):V.receive_material(part_id=part.pk,user=self.other_supplier_user,data={'confirmed':True})
        data=V.receive_material(part_id=part.pk,user=self.supplier_user,data={'confirmed':True})
        self.assertEqual(data['status'],'RECEIVED');self.assertNotIn('customer',data);self.assertNotIn('phone',data)
        with self.assertRaises(AfterSalesError):V.receive_material(part_id=part.pk,user=self.supplier_user,data={'confirmed':True})

    def test_invalid_disassembly_quantity_rolls_back_all_rows(self):
        flow,_=self.swap();self.pickup(flow);self.call('RECOVERY_DECIDE',self.af,decision='DISASSEMBLE',note='拆机')
        self.repair_proof()
        for quantity in ['0','-1','NaN','3','0.0001']:
            with self.assertRaises(AfterSalesError):self.call('RECOVERY_DISASSEMBLY_COMPLETE',self.af,parts=[{'bom_item_id':self.bom_item.pk,'quantity':quantity}])
        self.assertFalse(ReplacementMaterialReturn.objects.exists())
        self.assertEqual(ReplacementFlow.objects.get(pk=flow.pk).recovery['state'],'DISASSEMBLING')

    def test_direct_factory_replacement_has_no_intermediate_channel_or_stock_book(self):
        OrganizationRelation.objects.create(source_organization=self.merchant,target_organization=self.other_merchant,relation_type='DISTRIBUTION')
        operator,_=self.user('factory-direct-lower',self.other_merchant,'merchant_electrician')
        self.case.escalation.update(source_org_id=self.other_merchant.pk,source_user_id=operator.pk,source_route='ORIGINAL_INSTALLER',source_assignee={})
        self.case.assigned_user=operator;self.case.assigned_organization=self.other_merchant;self.case.save()
        flow=self.approve('FACTORY');self.assertEqual(flow.chain,[self.factory.pk,self.merchant.pk,self.other_merchant.pk])
        self.assertEqual(flow.legs.count(),1);old=flow.legs.get();self.assertEqual(old.from_organization_id,self.other_merchant.pk);self.assertEqual(old.to_organization_id,self.factory.pk)
        self.call('SEND_REPLACEMENT_LEG',operator,leg_id=old.pk,confirmed=True);self.call('FACTORY_PICKUP',self.logistics,confirmed=True)
        self.stock(self.factory);self.proof(self.af)
        self.call('SCAN_SWAP',self.af,qr_text='NEW-LIFECYCLE',handover_method='SHIPMENT',logistics_company='顺丰',tracking_no='FACTORY-SWAP',note='厂家换新')
        self.assertFalse(ReplacementInventoryEntry.objects.filter(flow=flow).exists());self.assertFalse(flow.legs.filter(direction='SUPPLY').exists())

    def test_supplier_limited_api_rejects_other_supplier(self):
        flow,_=self.swap();part=ReplacementMaterialReturn.objects.create(flow=flow,bom_item=self.bom_item,supplier=self.supplier,quantity=1,snapshot={'material_name':'控制器','device_sn':self.device.sn})
        request=RequestFactory().get('/part',HTTP_AUTHORIZATION='Bearer '+issue_access_token(self.other_supplier_user))
        result=ReplacementMaterialReturnAPI.as_view()(request,part_id=part.pk)
        self.assertNotEqual(result.data['code'],0)
        request=RequestFactory().get('/part',HTTP_AUTHORIZATION='Bearer '+issue_access_token(self.supplier_user))
        result=ReplacementMaterialReturnAPI.as_view()(request,part_id=part.pk)
        self.assertEqual(result.data['code'],0);self.assertNotIn('application_id',result.data['data'])

    def test_direct_factory_repair_pickup_does_not_reassign_repair_to_logistics(self):
        self.case.escalation={};self.case.execution_stage='REPAIRING';self.case.save()
        AfterSalesMedia.objects.create(application=self.case,purpose='REPAIR_EVIDENCE',media_type='IMAGE',file='test.jpg',uploaded_by=self.sales)
        self.call('REQUEST',requested='FACTORY_REPAIR',holder='EXECUTOR',reason='不能现场维修')
        self.call('DECIDE',self.af,decision='FACTORY_REPAIR',receiving_address='厂家售后维修部')
        proof=AfterSalesMedia.objects.create(application=self.case,purpose='REPAIR_RETURN_SHIPMENT_DOCUMENT',media_type='IMAGE',file='handover.jpg',uploaded_by=self.sales)
        self.call('FACTORY_TRANSPORT_SEND',direction='inbound',index=0,document_ids=[proof.pk],logistics_company='顺丰',tracking_no='FACTORY-RETURN')
        with self.assertRaises(AfterSalesError):self.call('FACTORY_TRANSPORT_RECEIVE',self.af,direction='inbound',index=0,confirmed=True)
        self.call('FACTORY_TRANSPORT_RECEIVE',self.logistics,direction='inbound',index=0,confirmed=True)
        self.case.refresh_from_db();self.assertEqual(self.case.assigned_user_id,self.af.pk);self.assertEqual(self.case.execution_stage,'REPAIRING')
        self.assertFalse(ReplacementFlow.objects.filter(application=self.case).exists());self.assertFalse(ReplacementInventoryEntry.objects.exists())

    def test_every_downstream_plan_queries_pending_supply_without_changing_its_money(self):
        from lifecycle.models import GoodsTransfer
        from lifecycle.models.goods_transfer_settlement import GoodsTransferSettlement,GoodsTransferLogisticsDocument
        OrganizationRelation.objects.create(source_organization=self.merchant,target_organization=self.other_merchant,relation_type='DISTRIBUTION')
        operator,_=self.user('lower-supply',self.other_merchant,'merchant_electrician')
        self.case.escalation.update(source_org_id=self.other_merchant.pk,source_user_id=operator.pk,source_route='ORIGINAL_INSTALLER',source_assignee={})
        self.case.assigned_user=operator;self.case.assigned_organization=self.other_merchant;self.case.save()
        flow=self.approve();self.stock(self.other_merchant);self.call('CONFIRM_REPLACEMENT_SERVICE',operator,service_mode='SHOP');self.call('SCAN_SWAP',operator,qr_text='NEW-LIFECYCLE')
        self.assertEqual(len(V.pending_for_route(self.merchant.pk,self.other_merchant.pk)),1)
        # First supply hop actually moves a different new device to the direct merchant.
        d=self.stock(self.factory,'CHANNEL-NEW');self.proof(self.af);leg1=flow.legs.get(direction='SUPPLY',sequence=1)
        self.call('SEND_REPLACEMENT_LEG',self.af,leg_id=leg1.pk,qr_text='CHANNEL-NEW',handover_method='IN_PERSON',note='首段补发')
        self.proof(self.owner);self.call('RECEIVE_REPLACEMENT_LEG',self.owner,leg_id=leg1.pk,confirmed=True,receipt_note='签收')
        transfer=GoodsTransfer.objects.create(flow_no='SHARED-CHANNEL',flow_type='SHIPMENT',client_request_id='SHARED-CHANNEL',
            instruction_organization=self.merchant,from_organization=self.merchant,to_organization=self.other_merchant,
            target_account=operator,initiated_by=self.owner,status='PENDING_RECEIPT')
        settlement=GoodsTransferSettlement.objects.create(transfer=transfer,receivable_amount=500)
        GoodsTransferLogisticsDocument.objects.create(settlement=settlement,sequence_no=1,client_request_id='SHARED-DOC',document_file='test.pdf',file_type='PDF',file_size=10,uploaded_by=self.owner)
        from lifecycle.services.goods_transfer_service import serialize_goods_transfer
        detail=serialize_goods_transfer(transfer,self.owner_i)
        self.assertEqual(len(detail['pending_replacement_demands']),1)
        leg2=flow.legs.get(direction='SUPPLY',sequence=2)
        self.call('SEND_REPLACEMENT_LEG',self.owner,leg_id=leg2.pk,qr_text='CHANNEL-NEW',shipment_id='G:'+str(transfer.pk))
        self.assertEqual(ReplacementLeg.objects.get(pk=leg2.pk).logistics['shared_goods_transfer_id'],transfer.pk)
        settlement.refresh_from_db();self.assertEqual(settlement.receivable_amount,500);self.assertEqual(transfer.items.count(),0)

    def test_onsite_replacement_final_closure_is_blocked_until_recovery_done(self):
        flow,_=self.swap();app=AfterSalesApplication.objects.get(pk=self.case.pk)
        self.assertFalse(S._viewer_actions(app,self.af,S._identity(self.af))['can_factory_close'])
        self.pickup(flow);self.call('RECOVERY_DECIDE',self.af,decision='REPAIR',note='修复')
        self.repair_proof();self.call('RECOVERY_REPAIR_COMPLETE',self.af,stock_target='TURNOVER',note='检修合格')
        app.refresh_from_db();self.assertTrue(S._viewer_actions(app,self.af,S._identity(self.af))['can_factory_close'])

    def test_unexecuted_v5_case_can_switch_without_inventing_delivery(self):
        self.call('DECIDE',self.af,decision='REPLACEMENT',replacement_flow_version=2,execution_mode='LOCAL',receiving_address='厂家')
        flow=ReplacementFlow.objects.get(application=self.case);self.assertEqual(flow.protocol_version,5)
        self.call('UPGRADE_REPLACEMENT_PROTOCOL')
        flow.refresh_from_db();self.assertEqual(flow.protocol_version,6);self.assertIsNone(flow.swapped_at)
        self.assertFalse(flow.legs.exclude(status='PENDING').exists())
        self.assertFalse(ReplacementInventoryEntry.objects.exists())

    def test_existing_physical_v5_delivery_cannot_be_automatically_converted(self):
        self.call('DECIDE',self.af,decision='REPLACEMENT',replacement_flow_version=2,execution_mode='LOCAL',receiving_address='厂家')
        self.stock(self.merchant);self.proof(self.sales)
        self.call('SCAN_SWAP',qr_text='NEW-LIFECYCLE',handover_method='IN_PERSON',note='旧版已交付')
        with self.assertRaises(AfterSalesError):self.call('UPGRADE_REPLACEMENT_PROTOCOL')
        self.assertEqual(ReplacementFlow.objects.get(application=self.case).protocol_version,5)

    def test_old_return_requires_two_distinct_scoped_proofs_without_scanning(self):
        flow,_=self.swap();leg=flow.legs.get(direction='OLD')
        def submit(**data):return call_api(application_id=self.case.pk,user=self.sales,data={'action':'SEND_REPLACEMENT_LEG','leg_id':leg.pk,**data})
        with self.assertRaises(AfterSalesError):submit()
        image=AfterSalesMedia.objects.create(application=self.case,purpose='REPAIR_RETURN_SHIPMENT_DOCUMENT',media_type='IMAGE',file='old.jpg',uploaded_by=self.sales)
        doc=AfterSalesMedia.objects.create(application=self.case,purpose='REPAIR_RETURN_SHIPMENT_DOCUMENT',media_type='IMAGE',file='waybill.jpg',uploaded_by=self.sales)
        other=AfterSalesMedia.objects.create(application=self.case,purpose='REPAIR_RETURN_SHIPMENT_DOCUMENT',media_type='IMAGE',file='other.jpg',uploaded_by=self.af)
        with self.assertRaises(AfterSalesError):submit(device_image_ids=[image.pk],logistics_document_ids=[image.pk])
        with self.assertRaises(AfterSalesError):submit(device_image_ids=[image.pk],logistics_document_ids=[other.pk])
        with self.assertRaises(AfterSalesError):submit(device_image_ids=[image.pk],logistics_document_ids=[999999])
        leg.refresh_from_db();self.assertEqual(leg.status,'PENDING')
        with patch.object(B,'_scan',side_effect=AssertionError('old return must not scan')):
            submit(device_image_ids=[image.pk],logistics_document_ids=[doc.pk])
        leg.refresh_from_db();self.assertEqual(leg.status,'SENT')
        self.assertEqual(leg.logistics['device_image_ids'],[image.pk])
        self.assertEqual(leg.logistics['logistics_document_ids'],[doc.pk])
        with self.assertRaises(AfterSalesError):submit(device_image_ids=[image.pk],logistics_document_ids=[doc.pk])
