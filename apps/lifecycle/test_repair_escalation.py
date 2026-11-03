from contextlib import ExitStack
from unittest.mock import patch
from django.test import TestCase, RequestFactory
from django.utils import timezone
from account.services.auth_service import issue_access_token
from lifecycle.models import AfterSalesApplication, AfterSalesMedia, Device, OrganizationDevice, AfterSalesEvent, LifecycleEvent
from lifecycle.services.aftersales_service import AfterSalesService as S, AfterSalesError
from lifecycle.services.repair_escalation_service import execute, actions
from lifecycle.services.aftersales_access import visible_cases
from lifecycle.services.repair_basis_service import make_basis, summary
from lifecycle.api.views import AfterSalesEscalationAPI
import lifecycle.test_aftersales_access_basis as fixture


class RepairEscalationTests(TestCase):
    user=classmethod(fixture.AfterSalesAccessBasisTests.user.__func__)
    @classmethod
    def setUpTestData(cls):
        fixture.AfterSalesAccessBasisTests.setUpTestData.__func__(cls)
        cls.case.execution_stage='REPAIRING';cls.case.save()

    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        for name in ('_publish_factory_workorder','_publish_assignee_workorder','_publish_applicant_workorder',
                     '_publish_applicant_notice','_publish_task','_complete_tasks','_complete_assignee_workorder',
                     '_complete_factory_workorder','_complete_applicant_workorder','_send_factory_close_task',
                     '_publish_supplier_quality_notices'):
            self.stack.enter_context(patch.object(S,name))

    def media(self,purpose,user=None):
        return AfterSalesMedia.objects.create(application=self.case,purpose=purpose,media_type='IMAGE',file='test/no-file.jpg',uploaded_by=user or self.sales)

    def call(self,action,user=None,**data):
        return execute(application_id=self.case.pk,user=user or self.sales,data={'action':action,**data})

    def request(self,requested='REPLACEMENT',holder='EXECUTOR'):
        self.media('REPAIR_EVIDENCE')
        result=self.call('REQUEST',requested=requested,holder=holder,reason='已检查，现场无法修复')
        # Saved-r3 fixture: new requests are covered by v6 tests separately.
        self.case.refresh_from_db();es=dict(self.case.escalation);es.pop('requires_replacement_flow_v6',None);es.pop('requires_factory_transport',None)
        self.case.escalation=es;self.case.save(update_fields=['escalation'])
        return result

    def decide(self,decision='FACTORY_REPAIR'):
        return self.call('DECIDE',self.af,decision=decision,receiving_address='厂家维修部，李经理，13900000000')

    def handover(self,user=None):
        self.media('APPLICANT_SHIPMENT_DOCUMENT',user)
        return self.call('HANDOVER',user,handover_method='IN_PERSON',handover_note='核对SN现场交接')

    def receive(self):
        return self.call('RECEIVE',self.af,receipt_note='原设备SN、外观及配件一致')

    def stage(self):
        self.case.refresh_from_db();return self.case.execution_stage

    def replacement_ready(self):
        self.request()
        # This helper represents a persisted r3 request. New requests use v2;
        # these tests verify that already-saved historical r3 cases can finish.
        self.case.refresh_from_db()
        es=dict(self.case.escalation);es.pop('requires_replacement_flow_v2',None);es.pop('requires_replacement_flow_v6',None)
        self.case.escalation=es;self.case.save(update_fields=['escalation'])
        self.decide('REPLACEMENT');self.handover();self.receive()
        device=Device.objects.create(sn='NEW-REPLACEMENT',product_model=self.product,batch=self.batch)
        OrganizationDevice.objects.create(device=device,organization=self.customer,status='IN_STOCK')
        self.media('REPAIR_RETURN_SHIPMENT_DOCUMENT',self.af)
        return device

    def test_request_blocks_completion_and_creates_no_repair_fact(self):
        self.request();self.assertEqual(self.stage(),'PENDING_ESCALATION')
        self.assertIsNone(self.case.repair_completed_at)
        self.assertEqual(summary(self.owner)['repair_completed_visits'],0)
        self.assertFalse(actions(S,self.case,self.sales)['can_escalation_request'])
        with self.assertRaises(AfterSalesError):S.repair_complete(application_id=self.case.pk,user=self.sales,data={})
        with self.assertRaises(AfterSalesError):self.request()
        self.assertEqual(AfterSalesEvent.objects.filter(event_type='REPAIR_ESCALATION_REQUEST').count(),1)

    def test_only_executor_can_request_with_reason_and_evidence(self):
        with self.assertRaises(AfterSalesError):self.call('REQUEST',requested='FACTORY_REPAIR',holder='EXECUTOR',reason='无法维修')
        self.media('REPAIR_EVIDENCE')
        with self.assertRaises(AfterSalesError):self.call('REQUEST',self.plain,requested='FACTORY_REPAIR',holder='EXECUTOR',reason='无法维修')
        with self.assertRaises(AfterSalesError):self.call('REQUEST',requested='FACTORY_REPAIR',holder='EXECUTOR',reason='')
        self.assertEqual(self.stage(),'REPAIRING')

    def test_installer_has_same_escalation_path(self):
        self.case.dispatch_route='ORIGINAL_INSTALLER';self.case.assigned_user=self.installer
        self.case.execution_stage='CONTACT_INSTALLER';self.case.save()
        self.media('REPAIR_EVIDENCE',self.installer)
        self.call('REQUEST',self.installer,requested='FACTORY_REPAIR',holder='APPLICANT',reason='无法维修')
        self.assertEqual(self.stage(),'PENDING_ESCALATION')
        self.decide();self.assertEqual(self.stage(),'AWAITING_FACTORY_RECEIPT')
        self.assertEqual(self.case.escalation['factory_transport']['route'],'DIRECT')

    def test_no_reject_and_no_unrequested_replacement(self):
        self.request('FACTORY_REPAIR')
        for decision in ('REJECT','REPLACEMENT',''):
            with self.assertRaises(AfterSalesError):self.decide(decision)
        self.assertEqual(self.stage(),'PENDING_ESCALATION')
        self.decide();self.assertEqual(self.stage(),'AWAITING_FACTORY_HANDOVER')
        self.assertEqual(self.case.assigned_user_id,self.af.pk)

    def test_wrong_factory_and_plain_sales_cannot_decide(self):
        self.request()
        other,_=self.user('other-factory-af',self.other_factory,'factory_aftersales')
        for user in (self.sales,self.plain,other):
            with self.assertRaises(AfterSalesError):self.call('DECIDE',user,decision='FACTORY_REPAIR',receiving_address='地址')
        self.decide();self.assertEqual(self.stage(),'AWAITING_FACTORY_HANDOVER')
        self.assertTrue(visible_cases(self.sales).filter(pk=self.case.pk).exists())
        self.assertTrue(visible_cases(self.owner).filter(pk=self.case.pk).exists())

    def test_actual_holder_and_fresh_evidence_required(self):
        self.request(holder='APPLICANT');self.decide()
        self.assertFalse(actions(S,self.case,self.sales)['can_escalation_handover'])
        with self.assertRaises(AfterSalesError):self.call('HANDOVER',handover_method='IN_PERSON',handover_note='错误持有人')
        with self.assertRaises(AfterSalesError):self.call('HANDOVER',self.customer_owner,handover_method='IN_PERSON',handover_note='交接')
        self.handover(self.customer_owner);self.assertEqual(self.stage(),'AWAITING_FACTORY_RECEIPT')
        self.assertFalse(actions(S,self.case,self.sales)['can_escalation_handover'])
        with self.assertRaises(AfterSalesError):self.call('RECEIVE',self.sales,receipt_note='收件')
        self.receive();self.assertEqual(self.stage(),'REPAIRING')
        self.assertEqual(self.case.dispatch_route,'FACTORY_AFTERSALES')

    def test_logistics_requires_company_tracking_and_document(self):
        self.request();self.decide()
        with self.assertRaises(AfterSalesError):self.call('HANDOVER',handover_method='SHIPMENT',handover_note='寄件')
        self.media('APPLICANT_SHIPMENT_DOCUMENT')
        self.call('HANDOVER',handover_method='SHIPMENT',handover_note='寄件',logistics_company='顺丰',tracking_no='SF001')
        self.assertEqual(self.stage(),'AWAITING_FACTORY_RECEIPT')

    def test_no_early_receipt_or_delivery_or_close(self):
        self.request()
        for action in ('RECEIVE','DELIVER_REPLACEMENT','CONFIRM_REPLACEMENT'):
            with self.assertRaises(AfterSalesError):self.call(action,self.af,new_device_sn='X')
        with self.assertRaises(AfterSalesError):S.factory_close(application_id=self.case.pk,user=self.af,data={})

    def test_factory_repair_reuses_full_closed_loop_actual_executor(self):
        self.request();self.decide();self.handover();self.receive()
        self.media('REPAIR_EVIDENCE',self.af)
        S.repair_complete(application_id=self.case.pk,user=self.af,data={'repair_summary':'厂家修复','repair_material_replaced':False,'actual_processed_at':timezone.now().isoformat()})
        self.assertEqual(self.stage(),'AWAITING_RETURN_SHIPMENT')
        self.media('REPAIR_RETURN_SHIPMENT_DOCUMENT',self.af)
        S.return_shipment(application_id=self.case.pk,user=self.af,data={'logistics_company':'顺丰','tracking_no':'SF002'})
        self.assertEqual(self.stage(),'AWAITING_APPLICANT_CONFIRM')
        S.applicant_confirm(application_id=self.case.pk,user=self.customer_owner,data={'confirmed':True})
        S.factory_close(application_id=self.case.pk,user=self.af,data={'final_fault_type':'OTHER','final_fault_materials':[], 'final_solution_type':'MAIL_IN_REPAIR','final_solution_note':'返厂维修完成'})
        self.assertEqual(self.stage(),'CLOSED')
        self.assertEqual(self.case.repair_completed_by_id,self.af.pk)
        self.assertEqual(self.case.repair_settlement_basis['repair_visits'],1)
        self.assertEqual(summary(self.owner)['repair_completed_visits'],0)
        self.assertEqual(summary(self.af)['repair_basis_visits'],1)

    def test_replacement_full_flow_requires_explicit_sn_confirmation(self):
        device=self.replacement_ready()
        self.call('DELIVER_REPLACEMENT',self.af,new_device_sn=device.sn,delivery_note='新设备已交付')
        self.assertEqual(self.stage(),'AWAITING_REPLACEMENT_CONFIRM')
        self.assertIsNone(self.case.applicant_confirm_due_at)
        with self.assertRaises(AfterSalesError):self.call('CONFIRM_REPLACEMENT',self.customer_owner,new_device_sn='WRONG',confirmed=True)
        with self.assertRaises(AfterSalesError):self.call('CONFIRM_REPLACEMENT',self.customer_owner,new_device_sn=device.sn,confirmed='true')
        self.call('CONFIRM_REPLACEMENT',self.customer_owner,new_device_sn=device.sn,confirmed=True)
        S.factory_close(application_id=self.case.pk,user=self.af,data={'final_fault_type':'OTHER','final_fault_materials':[], 'final_solution_type':'DEVICE_REPLACEMENT','final_solution_note':'整机换新完成'})
        self.assertEqual(self.stage(),'CLOSED');self.assertIsNone(self.case.repair_completed_at)
        self.assertEqual(make_basis(self.case)['status'],'NOT_APPLICABLE')
        self.assertEqual(summary(self.af)['repair_completed_visits'],0)
        self.assertEqual(LifecycleEvent.objects.filter(event_type='AFTERSALE_TRANSFER').count(),4)

    def test_replacement_cannot_bypass_normal_device_circulation(self):
        device=self.replacement_ready()
        OrganizationDevice.objects.filter(device=device).update(organization=self.factory)
        with self.assertRaises(AfterSalesError):self.call('DELIVER_REPLACEMENT',self.af,new_device_sn=device.sn,delivery_note='已交付')
        self.assertEqual(self.stage(),'AWAITING_REPLACEMENT')
        with self.assertRaises(AfterSalesError):self.call('DELIVER_REPLACEMENT',self.af,new_device_sn=self.device.sn,delivery_note='同一SN')

    def test_same_replacement_device_cannot_be_used_twice(self):
        device=self.replacement_ready()
        AfterSalesApplication.objects.create(case_no='ALREADY-REPLACED',device=self.device,customer_organization=self.customer,
            factory_organization=self.factory,applicant=self.customer_owner,replacement_device=device,status='CLOSED')
        with self.assertRaises(AfterSalesError):self.call('DELIVER_REPLACEMENT',self.af,new_device_sn=device.sn,delivery_note='重复SN')

    def test_api_auth_and_authenticated_action(self):
        factory=RequestFactory()
        response=AfterSalesEscalationAPI.as_view()(factory.post('/escalation/',{'action':'REQUEST'},content_type='application/json'),application_id=self.case.pk)
        self.assertNotEqual(response.status_code,200)
        self.media('REPAIR_EVIDENCE')
        request=factory.post('/escalation/',{'action':'REQUEST','requested':'FACTORY_REPAIR','holder':'EXECUTOR','reason':'无法维修'},content_type='application/json',HTTP_AUTHORIZATION='Bearer '+issue_access_token(self.sales))
        response=AfterSalesEscalationAPI.as_view()(request,application_id=self.case.pk)
        self.assertEqual(response.status_code,200);self.assertEqual(self.stage(),'PENDING_ESCALATION')

    def test_factory_cannot_reuse_unable_repair_evidence_as_completed_repair(self):
        self.request();self.decide();self.handover();self.receive()
        with self.assertRaises(AfterSalesError):
            S.repair_complete(application_id=self.case.pk,user=self.af,data={'repair_summary':'修复','repair_material_replaced':False})
        self.assertIsNone(AfterSalesApplication.objects.get(pk=self.case.pk).repair_completed_at)

    def test_moved_executor_identity_cannot_submit(self):
        identity=self.sales_i;identity.organization=self.other_merchant;identity.save(update_fields=['organization'])
        self.media('REPAIR_EVIDENCE')
        with self.assertRaises(AfterSalesError):self.call('REQUEST',requested='FACTORY_REPAIR',holder='EXECUTOR',reason='无法维修')

    def test_replacement_rejects_preexisting_in_use_device(self):
        from lifecycle.models import CustomerDeviceBinding
        from datetime import timedelta
        device=self.replacement_ready()
        binding=CustomerDeviceBinding.objects.create(device=device,customer_organization=self.customer,source_organization=self.merchant,
            accepted_at=timezone.now()-timedelta(days=2),status='ACTIVE')
        with self.assertRaises(AfterSalesError):self.call('DELIVER_REPLACEMENT',self.af,new_device_sn=device.sn,delivery_note='已交付')
        self.assertEqual(self.stage(),'AWAITING_REPLACEMENT')

    def test_new_delivery_requires_own_fresh_document(self):
        device=self.replacement_ready()
        AfterSalesMedia.objects.filter(application=self.case,purpose='REPAIR_RETURN_SHIPMENT_DOCUMENT').delete()
        self.media('REPAIR_RETURN_SHIPMENT_DOCUMENT',self.sales)
        with self.assertRaises(AfterSalesError):self.call('DELIVER_REPLACEMENT',self.af,new_device_sn=device.sn,delivery_note='已交付')

    def test_delegated_manager_keeps_origin_history_after_factory_transfer(self):
        from account.models import SeniorManagerProfile
        profile=SeniorManagerProfile.objects.create(identity=self.manager_i)
        profile.managed_roles.add(self.sales_i.role)
        self.sales_i.inviter=self.manager;self.sales_i.save(update_fields=['inviter'])
        self.request();self.decide()
        self.assertTrue(visible_cases(self.manager).filter(pk=self.case.pk).exists())
        self.assertFalse(actions(S,AfterSalesApplication.objects.get(pk=self.case.pk),self.manager)['can_escalation_decide'])
        profile.managed_roles.clear()
        self.assertFalse(visible_cases(self.manager).filter(pk=self.case.pk).exists())
