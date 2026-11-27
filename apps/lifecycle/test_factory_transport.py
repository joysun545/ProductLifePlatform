from contextlib import ExitStack
from tempfile import TemporaryDirectory
from unittest.mock import patch
import uuid
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone
from account.models import OrganizationRelation, UserOrganization
from account.services.auth_service import issue_access_token
from lifecycle.models import AfterSalesMedia,ReplacementFlow,ReplacementLeg,ReplacementInventoryEntry,OrganizationDevice,ActivationRecord,AfterSalesEvent
from lifecycle.services import factory_transport_service as F
from lifecycle.services import replacement_flow_service as B
from lifecycle.services.aftersales_service import AfterSalesService as S,AfterSalesError
from lifecycle.services.repair_escalation_service import execute as api
from sales.models import ReplacementSupplyPlan,OrderPlan,OrderPlanFinance
import lifecycle.test_replacement_v6 as fixture

class FactoryTransportTests(TestCase):
    user=classmethod(fixture.ReplacementV6Tests.user.__func__)
    stock=fixture.ReplacementV6Tests.stock
    setUpTestData=classmethod(fixture.ReplacementV6Tests.setUpTestData.__func__)
    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        media_root=self.stack.enter_context(TemporaryDirectory())
        self.stack.enter_context(patch.object(AfterSalesMedia._meta.get_field('file'),
            'storage',FileSystemStorage(location=media_root)))
        self.notices={}
        for name in ['_publish_task','_publish_factory_workorder','_publish_assignee_workorder','_publish_applicant_workorder','_publish_applicant_notice','_publish_factory_aftersales_notice','_complete_tasks','_complete_assignee_workorder','_complete_factory_workorder','_complete_applicant_workorder','_send_factory_close_task','_publish_supplier_quality_notices']:
            self.notices[name]=self.stack.enter_context(patch.object(S,name))
        self.stack.enter_context(patch('lifecycle.services.replacement_flow_service.enforce_device_operation',return_value={'allowed':True}))
    def refresh(self):self.case.refresh_from_db();return F.get(self.case)
    def call(self,action,user=None,**data):return api(application_id=self.case.pk,user=user or self.af,data={'action':action,**data})
    def proof(self,user,direction=None,index=None,*,foreign_uploader=False):
        """Upload a real current-scope file before testing the business action.

        Invalid send actors/early legs must fail in send, rather than making
        their upload fail first and accidentally satisfying assertRaises.
        Such calls use a legal sender/current leg, or a previously uploaded
        real file when no new sending leg is ready.
        """
        t=self.refresh()
        ready=[]
        for candidate_direction in ['inbound','outbound']:
            if candidate_direction=='outbound' and not t.get('attribution'):continue
            for candidate_index,leg in enumerate(t[candidate_direction]):
                if leg['status']!='PENDING' or (candidate_index and t[candidate_direction][candidate_index-1]['status']!='RECEIVED'):continue
                if leg['from']==self.case.customer_organization_id:sender=self.case.applicant
                elif leg['from']==self.factory.pk:sender=self.logistics
                elif F.actor(self.case,user,leg['from']):sender=user
                else:
                    sender=next((identity.user for identity in UserOrganization.objects.filter(
                        organization_id=leg['from'],is_active=True,user__is_active=True,
                        role__is_active=True).select_related('user') if F.actor(self.case,identity.user,leg['from'])),None)
                if sender:ready.append((candidate_direction,candidate_index,sender))
        selected=next((row for row in ready if row[:2]==(direction,index)),None) or next(iter(ready),None)
        if selected is None:
            existing=next((row for row in self.case.media.filter(
                purpose='REPAIR_RETURN_SHIPMENT_DOCUMENT',evidence_scope_key__isnull=False)
                if row.file and row.file.storage.exists(row.file.name)),None)
            self.assertIsNotNone(existing,'negative send requires an already uploaded real proof')
            return existing
        selected_direction,selected_index,sender=selected
        response=self.client.post(f'/pldp/api/lifecycle/after-sales/applications/{self.case.pk}/media/',{
            'purpose':'REPAIR_RETURN_SHIPMENT_DOCUMENT','media_type':'IMAGE',
            'file':SimpleUploadedFile('waybill.jpg',b'\xff\xd8\xff\xe0transport-test',content_type='image/jpeg'),
            'evidence_action':'FACTORY_TRANSPORT_SEND','transport_id':t['transport_id'],
            'direction':selected_direction,'index':selected_index,'evidence_kind':'LOGISTICS',
            'client_upload_id':str(uuid.uuid4()),
        },HTTP_AUTHORIZATION='Bearer '+issue_access_token(sender))
        self.assertEqual(response.status_code,201,response.json())
        result=AfterSalesMedia.objects.get(pk=response.json()['data']['id'])
        if foreign_uploader:
            # Negative authorship fault only: the file/scope were created by
            # the real API, then its uploader is made foreign to the sender.
            # This row is never used for a successful business transition.
            result.uploaded_by=user;result.save(update_fields=['uploaded_by'])
        return result
    def begin(self,kind='REPAIR',route='DIRECT',source=None):
        self.case.dispatch_route='FACTORY_AFTERSALES';self.case.assigned_user=self.af;self.case.assigned_organization=self.factory
        self.case.escalation.update(decision='FACTORY_REPAIR' if kind=='REPAIR' else 'REPLACEMENT',holder='APPLICANT' if source is None else 'EXECUTOR')
        if source:self.case.escalation['source_org_id']=source.pk
        self.case.save()
        if kind=='REPLACEMENT':
            # Create the approved direct replacement flow using the real approval path.
            self.case.execution_stage='PENDING_ESCALATION';self.case.escalation['requested']='REPLACEMENT';self.case.save()
            self.call('DECIDE',decision='REPLACEMENT',replacement_flow_version=3,execution_mode='FACTORY',receiving_address='厂家收件地址',factory_transport_version=1)
        else:F.begin(self.case,self.af,kind,route)
        return self.refresh()
    def test_factory_triage_starts_direct_transport_before_applicant_reads_detail(self):
        self.factory.address = "厂家收件地址"
        self.factory.save(update_fields=["address"])
        self.case.status = "SUBMITTED"
        self.case.execution_stage = "NONE"
        self.case.dispatch_route = ""
        self.case.escalation = {}
        self.case.save()
        url = f"/pldp/api/lifecycle/after-sales/applications/{self.case.pk}/"
        dispatch = self.client.post(
            url + "triage/",
            {
                "preliminary_fault_type": "INSTALLATION_OR_WIRING",
                "diagnosis_note": "依据设备视频检查线路。",
                "repair_plan": "直接返厂检查。",
                "dispatch_route": "FACTORY_AFTERSALES",
                "assigned_user_id": self.af.pk,
                "factory_transport_version": 1,
            },
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + issue_access_token(self.af),
        )
        self.assertEqual(dispatch.status_code, 200, dispatch.json())
        self.assertEqual(self.refresh()["route"], "DIRECT")
        self.assertEqual(self.case.assigned_user_id, self.af.pk)
        detail = self.client.get(
            url, HTTP_AUTHORIZATION="Bearer " + issue_access_token(self.case.applicant)
        )
        self.assertEqual(detail.status_code, 200, detail.json())
        self.assertEqual(detail.json()["data"]["factory_transport"]["state"], "INBOUND")

    def send(self,direction,index,user):
        proof=self.proof(user,direction,index)
        return self.call('FACTORY_TRANSPORT_SEND',user,direction=direction,index=index,document_ids=[proof.pk],logistics_company='顺丰',tracking_no='SF-'+direction+str(index))
    def receive(self,direction,index,user):return self.call('FACTORY_TRANSPORT_RECEIVE',user,direction=direction,index=index,confirmed=True)
    def pickup(self):self.send('inbound',0,self.customer_owner);self.receive('inbound',0,self.logistics)
    def repair(self):
        AfterSalesMedia.objects.create(application=self.case,purpose='REPAIR_EVIDENCE',media_type='IMAGE',file='repair.jpg',uploaded_by=self.af)
        return S.repair_complete(application_id=self.case.pk,user=self.af,data={'repair_summary':'厂家实际修复','repair_material_replaced':False,'actual_processed_at':timezone.now().isoformat()})
    def final(self,swap=False):return self.call('FACTORY_TRANSPORT_FINALIZE',final_fault_type='OTHER',final_solution_type='DEVICE_REPLACEMENT' if swap else 'OTHER',final_solution_note='厂家完成处理归因')

    def test_direct_repair_full_route_and_attribution_precedes_logistics_send(self):
        self.begin();self.pickup();self.repair();self.final()
        with self.assertRaises(AfterSalesError):self.send('outbound',0,self.af)
        with self.assertRaises(AfterSalesError):self.send('outbound',0,self.warehouse)
        self.send('outbound',0,self.logistics)
        self.assertNotEqual(self.refresh()['state'],'DELIVERED')
        self.receive('outbound',0,self.customer_owner)
        self.assertEqual(self.refresh()['state'],'DELIVERED');self.assertEqual(self.case.status,'CLOSED')
        self.assertEqual(self.case.closed_by_id,self.af.pk);self.assertIsNotNone(self.case.applicant_confirmed_at)
        self.assertEqual(ReplacementInventoryEntry.objects.count(),0)
        self.assertEqual(ReplacementSupplyPlan.objects.count(),0);self.assertEqual(OrderPlan.objects.count(),0);self.assertEqual(OrderPlanFinance.objects.count(),0)
    def test_factory_receipt_only_logistics_and_cannot_skip_actual_repair(self):
        self.begin();self.send('inbound',0,self.customer_owner)
        with self.assertRaises(AfterSalesError):self.receive('inbound',0,self.af)
        with self.assertRaises(AfterSalesError):self.repair()
        self.receive('inbound',0,self.logistics)
        with self.assertRaises(AfterSalesError):self.final()
        with self.assertRaises(AfterSalesError):self.send('outbound',0,self.logistics)
        self.assertEqual(self.refresh()['state'],'FACTORY_RECEIVED')
    def test_chain_repair_returns_same_channel_without_stock_or_credit(self):
        OrganizationRelation.objects.create(source_organization=self.merchant,target_organization=self.other_merchant,relation_type='DISTRIBUTION')
        lower,_=self.user('factory-chain-lower',self.other_merchant,'merchant_electrician')
        t=self.begin(route='CHAIN',source=self.other_merchant)
        self.assertEqual([(x['from'],x['to']) for x in t['inbound']],[(self.other_merchant.pk,self.merchant.pk),(self.merchant.pk,self.factory.pk)])
        with self.assertRaises(AfterSalesError):self.send('inbound',1,self.owner)
        self.send('inbound',0,lower);self.receive('inbound',0,self.owner)
        self.assertEqual(self.refresh()['inbound'][1]['status'],'PENDING')
        self.send('inbound',1,self.owner);self.receive('inbound',1,self.logistics);self.repair();self.final()
        self.send('outbound',0,self.logistics);self.receive('outbound',0,self.owner)
        self.send('outbound',1,self.owner);self.receive('outbound',1,lower)
        self.send('outbound',2,lower);self.receive('outbound',2,self.customer_owner)
        self.assertEqual(self.refresh()['state'],'DELIVERED');self.assertEqual(ReplacementInventoryEntry.objects.count(),0)
        self.assertEqual(ReplacementSupplyPlan.objects.count(),0)
    def test_direct_factory_swap_separates_prepare_from_send_and_inherits_activation(self):
        original=ActivationRecord.objects.get(device=self.device).activation_time
        self.begin('REPLACEMENT');self.pickup();new=self.stock(self.factory,'FACTORY-NEW')
        self.call('FACTORY_TRANSPORT_PREPARE_SWAP',qr_text='FACTORY-NEW')
        flow=ReplacementFlow.objects.get(application=self.case);self.assertIsNone(flow.swapped_at)
        self.assertEqual(flow.legs.get(direction='USER').status,'PENDING')
        with self.assertRaises(AfterSalesError):self.send('outbound',0,self.logistics)
        self.final(True);self.send('outbound',0,self.logistics)
        from lifecycle.models import DeviceState,LifecycleEvent
        self.assertEqual(DeviceState.objects.get(device=new).status,'SHIPPED')
        self.assertTrue(LifecycleEvent.objects.filter(device=new,data__factory_transport_action='FACTORY_TRANSPORT_SEND').exists())
        with self.assertRaises(AfterSalesError):self.receive('outbound',0,self.af)
        self.receive('outbound',0,self.customer_owner)
        flow.refresh_from_db();self.assertIsNotNone(flow.swapped_at)
        self.assertEqual(ActivationRecord.objects.get(device=new).activation_time,original)
        self.assertEqual(self.refresh()['state'],'DELIVERED');self.assertEqual(self.case.status,'CLOSED')
        self.assertEqual(ReplacementInventoryEntry.objects.count(),0);self.assertFalse(flow.legs.filter(direction='SUPPLY').exists())
    def test_no_factory_sales_or_stock_task_and_no_early_logistics_dispatch(self):
        self.begin();self.pickup()
        recipients=[openid for call in self.notices['_publish_task'].call_args_list for openid in call.kwargs.get('recipients_openids',[])]
        self.assertNotIn(self.factory_sales.wx_openid,recipients);self.assertNotIn(self.warehouse.wx_openid,recipients)
        self.assertTrue(self.notices['_publish_assignee_workorder'].called)
        self.assertFalse(any('outbound' in str(c.kwargs.get('step','')) for c in self.notices['_publish_task'].call_args_list))
    def test_stale_legacy_endpoints_cannot_bypass_transport(self):
        self.begin();self.pickup()
        for action in ['HANDOVER','RECEIVE','SCAN_SWAP','SEND_REPLACEMENT_LEG','DELIVER_REPLACEMENT']:
            with self.assertRaises(AfterSalesError):self.call(action)
        for method in [S.repair_receipt,S.return_shipment,S.factory_close]:
            with self.assertRaises(AfterSalesError):method(application_id=self.case.pk,user=self.af,data={})
    def test_invalid_and_foreign_proofs_or_duplicate_receipt_roll_back(self):
        self.begin();proof=self.proof(self.af,foreign_uploader=True)
        with self.assertRaises(AfterSalesError):self.call('FACTORY_TRANSPORT_SEND',self.customer_owner,direction='inbound',index=0,document_ids=[proof.pk],logistics_company='顺丰',tracking_no='S1')
        self.assertEqual(self.refresh()['inbound'][0]['status'],'PENDING')
        self.pickup()
        with self.assertRaises(AfterSalesError):self.receive('inbound',0,self.logistics)
        self.assertEqual(self.refresh()['state'],'FACTORY_RECEIVED')
    def test_revoked_driver_and_other_tenant_cannot_operate(self):
        self.begin()
        with self.assertRaises(AfterSalesError):self.send('inbound',0,self.driver)
        other,_=self.user('other-root-logistics',self.other_factory,'factory_logistics')
        with self.assertRaises(AfterSalesError):self.receive('inbound',0,other)
    def test_server_marks_new_unable_repair_request_and_cannot_downgrade(self):
        self.case.escalation={};self.case.execution_stage='REPAIRING';self.case.dispatch_route='SERVICE_STATION';self.case.assigned_user=self.sales;self.case.assigned_organization=self.merchant;self.case.save()
        AfterSalesMedia.objects.create(application=self.case,purpose='REPAIR_EVIDENCE',media_type='IMAGE',file='cannot.jpg',uploaded_by=self.sales)
        self.call('REQUEST',self.sales,requested='FACTORY_REPAIR',holder='EXECUTOR',reason='无法现场维修',factory_return_route='CHAIN')
        self.call('DECIDE',decision='FACTORY_REPAIR',receiving_address='厂家地址',factory_transport_version=0)
        self.assertEqual(self.refresh()['route'],'CHAIN')
    def test_device_code_factory_swap_keeps_first_activation_and_needs_rescan(self):
        ActivationRecord.objects.filter(device=self.device).update(activation_method='DEVICE_CODE')
        old=ActivationRecord.objects.get(device=self.device).activation_time
        self.begin('REPLACEMENT');self.pickup();new=self.stock(self.factory,'FACTORY-NEW')
        self.call('FACTORY_TRANSPORT_PREPARE_SWAP',qr_text='FACTORY-NEW');self.final(True)
        self.send('outbound',0,self.logistics);self.receive('outbound',0,self.customer_owner)
        from lifecycle.models import CustomerDeviceBinding
        binding=CustomerDeviceBinding.objects.get(device=new)
        from lifecycle.models import DeviceState
        self.assertEqual(DeviceState.objects.get(device=new).activation_status,'NOT_ACTIVATED')
        self.assertFalse(ActivationRecord.objects.filter(device=new,status='ACTIVATED').exists())
        flow=ReplacementFlow.objects.get(application=self.case);self.assertEqual(flow.first_activation_at,old)

    def test_adopting_legacy_received_repair_requires_current_logistics_ack(self):
        old_time=(timezone.now()-__import__('datetime').timedelta(days=1)).isoformat()
        self.case.dispatch_route='FACTORY_AFTERSALES';self.case.assigned_user=self.af;self.case.assigned_organization=self.factory
        self.case.execution_stage='AWAITING_RETURN_SHIPMENT';self.case.repair_completed_at=timezone.now();self.case.repair_completed_by=self.af
        self.case.escalation.update(decision='FACTORY_REPAIR',factory_received_at=old_time,factory_received_by=self.af.pk)
        self.case.save()
        self.call('START_FACTORY_TRANSPORT')
        self.assertTrue(self.refresh()['legacy_custody']);self.assertEqual(self.refresh()['inbound'],[])
        with self.assertRaises(AfterSalesError):self.final()
        self.call('FACTORY_TRANSPORT_PICKUP',self.logistics,confirmed=True)
        t=self.refresh();self.assertEqual(self.case.escalation['factory_received_at'],old_time)
        self.assertEqual(self.case.escalation['factory_received_by'],self.af.pk);self.assertEqual(t['factory_received_by'],self.logistics.pk)
        self.assertTrue(F.describe(self.case,self.af)['can_finalize'])
        self.final();self.send('outbound',0,self.logistics);self.receive('outbound',0,self.customer_owner)
        self.assertEqual(self.refresh()['state'],'DELIVERED')

    def test_failed_binding_transfer_rolls_back_delivery_and_receipt_task_completion(self):
        self.begin('REPLACEMENT');self.pickup();self.stock(self.factory,'FACTORY-NEW')
        self.call('FACTORY_TRANSPORT_PREPARE_SWAP',qr_text='FACTORY-NEW');self.final(True);self.send('outbound',0,self.logistics)
        with patch.object(B,'_apply_swap',side_effect=AfterSalesError('binding conflict')):
            with self.assertRaises(AfterSalesError):self.receive('outbound',0,self.customer_owner)
        self.assertEqual(self.refresh()['outbound'][0]['status'],'SENT');self.assertNotEqual(self.case.status,'CLOSED')
        self.assertEqual(ReplacementFlow.objects.get(application=self.case).legs.get(direction='USER').status,'SENT')

    def test_only_current_leg_can_upload_and_final_receipt_closes_pending_task(self):
        self.begin();self.assertTrue(F.media_allowed(self.case,self.customer_owner,'REPAIR_RETURN_SHIPMENT_DOCUMENT'))
        self.assertFalse(F.media_allowed(self.case,self.logistics,'REPAIR_RETURN_SHIPMENT_DOCUMENT'))
        self.pickup();self.repair();self.final();self.send('outbound',0,self.logistics)
        self.receive('outbound',0,self.customer_owner)
        self.assertTrue(any('factory-transport-outbound-0-receive' in c.kwargs.get('steps',[]) for c in self.notices['_complete_tasks'].call_args_list))

    def test_quality_gate_is_rechecked_by_logistics_before_actual_new_machine_dispatch(self):
        self.begin('REPLACEMENT');self.pickup();self.stock(self.factory,'FACTORY-NEW')
        self.call('FACTORY_TRANSPORT_PREPARE_SWAP',qr_text='FACTORY-NEW');self.final(True)
        with patch.object(B,'enforce_device_operation',side_effect=AfterSalesError('quality hold')):
            with self.assertRaises(AfterSalesError):self.send('outbound',0,self.logistics)
        self.assertEqual(self.refresh()['outbound'][0]['status'],'PENDING')
        self.assertEqual(ReplacementFlow.objects.get(application=self.case).legs.get(direction='USER').status,'PENDING')

    def test_old_factory_swap_already_received_does_not_request_a_second_inbound(self):
        self.case.escalation.update(holder='APPLICANT');self.case.save()
        self.call('DECIDE',decision='REPLACEMENT',replacement_flow_version=3,execution_mode='FACTORY',receiving_address='厂家地址')
        flow=ReplacementFlow.objects.get(application=self.case);old=flow.legs.get(direction='OLD')
        old.status='RECEIVED';old.received_by=self.logistics;old.received_at=timezone.now();old.save()
        received_at=old.received_at.isoformat();flow.recovery={'state':'WAIT_DECISION','picked_up_at':received_at,'picked_up_by':self.logistics.pk};flow.save()
        self.call('START_FACTORY_TRANSPORT');self.assertEqual(self.refresh()['inbound'],[])
        self.call('FACTORY_TRANSPORT_PICKUP',self.logistics,confirmed=True)
        old.refresh_from_db();flow.refresh_from_db()
        self.assertEqual(old.received_at.isoformat(),received_at);self.assertEqual(flow.recovery['picked_up_at'],received_at)
        self.assertEqual(self.refresh()['state'],'FACTORY_RECEIVED')
