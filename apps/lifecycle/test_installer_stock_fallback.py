from contextlib import ExitStack
from unittest.mock import patch
from decimal import Decimal
from django.test import TestCase
from django.utils import timezone
from account.models import Organization, OrganizationType, UserOrganization
from lifecycle.models import AfterSalesApplication, AfterSalesServiceStationApplication, AfterSalesServiceStationLiaison, InstallationRecord
from lifecycle.services.aftersales_service import AfterSalesService, AfterSalesError
from lifecycle.services.installer_dispatch_service import installer_row, stock_check, inspect_installer, validate_fallback
from lifecycle.services.repair_basis_service import summary, make_basis, catalog
from supply_chain.models import Material, MaterialSpec, FactoryMaterialStock, FactoryMaterialLot, OrganizationMaterialStock, OrganizationMaterialLot
import lifecycle.test_aftersales_access_basis as fixtures


class InstallerStockFallbackTests(TestCase):
    user=classmethod(fixtures.AfterSalesAccessBasisTests.user.__func__)
    completed=fixtures.AfterSalesAccessBasisTests.completed

    @classmethod
    def setUpTestData(cls):
        fixtures.AfterSalesAccessBasisTests.setUpTestData.__func__(cls)
        cls.installer_identity=UserOrganization.objects.get(user=cls.installer)
        cls.installer_identity.organization=cls.other_merchant
        cls.installer_identity.save(update_fields=['organization'])
        cls.installation=InstallationRecord.objects.create(device=cls.device,organization=cls.other_merchant,operator=cls.installer,status='INSTALLED')
        cls.supplier=Organization.objects.create(code='FALLBACK-SUPPLIER',name='配套供应商',organization_type=OrganizationType.objects.get(code='SUPPLIER'),factory_root=cls.factory)
        material=Material.objects.create(code='FALLBACK-MAT',name='控制器')
        cls.spec=MaterialSpec.objects.create(material=material,code='FALLBACK-SPEC',name='控制器A')
        cls.spec2=MaterialSpec.objects.create(material=material,code='FALLBACK-SPEC2',name='控制器B')
        cls.materials=[{'bom_item_id':7,'material_id':material.pk,'material_name':'控制器','specification_id':cls.spec.pk,'specification_name':'控制器A','quantity':1}]
        cls.case.bom_snapshot={'items':cls.materials}
        cls.case.status='SUBMITTED';cls.case.location_latitude=Decimal('29.6000000');cls.case.location_longitude=Decimal('106.5000000')
        cls.case.save()
        cls.station.service_latitude=Decimal('29.6100000');cls.station.service_longitude=Decimal('106.5000000');cls.station.save()

    def stock(self, org, spec=None, factory=None, reserved=0):
        spec=spec or self.spec;factory=factory or self.factory
        key='%s-%s-%s'%(org.pk,spec.pk,factory.pk)
        source,_=FactoryMaterialStock.objects.get_or_create(factory=factory,supplier=self.supplier,material_spec=spec,defaults={'unit':'件'})
        lot=FactoryMaterialLot.objects.create(stock=source,batch_code='FB-'+key,received_milli=1000,on_hand_milli=1000,received_by=self.admin)
        stock=OrganizationMaterialStock.objects.create(organization=org,source_factory=factory,supplier=self.supplier,material_spec=spec,unit='件')
        return OrganizationMaterialLot.objects.create(stock=stock,source_factory_lot=lot,source_reference='FB-'+key,
            source_order_plan_id=1,source_shipment_id=1,source_accessory_item_id=1,source_batch_code_snapshot=lot.batch_code,
            received_milli=1000,on_hand_milli=1000,reserved_milli=reserved,received_by=self.owner,received_at=timezone.now())

    def preview(self):
        return inspect_installer(AfterSalesService,self.case,self.af,'COMPONENT_SUSPECTED',self.materials)

    def add_far_station(self):
        org=Organization.objects.create(code='FALLBACK-FAR',name='稍远服务站',organization_type=self.merchant.organization_type,factory_root=self.factory)
        user,identity=self.user('far-station',org,'merchant_sales')
        station=AfterSalesServiceStationApplication.objects.create(organization=org,submitted_by=user,status='APPROVED',service_latitude=29.7,service_longitude=106.5,
            location_captured_at=timezone.now(),notice_agreed_at=timezone.now(),receiving_address='远站地址',receiving_contact_name='远站',receiving_contact_phone='13900000001')
        AfterSalesServiceStationLiaison.objects.create(application=station,identity=identity)
        self.stock(org)
        return station

    def test_nonstation_installer_stock_is_sufficient(self):
        self.stock(self.other_merchant)
        result,row=self.preview()
        self.assertEqual(result['status'],'READY')
        self.assertEqual(row['user'].pk,self.installer.pk)
        self.assertIsNone(result['nearest_station'])
        self.assertFalse(AfterSalesServiceStationApplication.objects.filter(organization=self.other_merchant).exists())

    def test_cancelled_installation_and_moved_identity_blocked(self):
        self.installation.status='CANCELLED';self.installation.save(update_fields=['status'])
        self.case=AfterSalesApplication.objects.get(pk=self.case.pk)
        self.assertIsNone(installer_row(AfterSalesService,self.case)[0])
        self.installation.status='INSTALLED';self.installation.save(update_fields=['status'])
        self.installer_identity.organization=self.merchant;self.installer_identity.save(update_fields=['organization'])
        self.case=AfterSalesApplication.objects.get(pk=self.case.pk)
        self.assertIsNone(installer_row(AfterSalesService,self.case)[0])

    def test_other_factory_and_reserved_stock_do_not_qualify(self):
        self.stock(self.other_merchant,factory=self.other_factory)
        self.assertEqual(self.preview()[0]['status'],'MISSING_PARTS')
        self.stock(self.other_merchant,reserved=1000)
        self.assertEqual(self.preview()[0]['status'],'MISSING_PARTS')

    def test_all_materials_required_and_missing_spec_fails_closed(self):
        self.stock(self.other_merchant)
        materials=self.materials+[dict(self.materials[0],bom_item_id=8,specification_id=self.spec2.pk)]
        rows=stock_check(self.case,self.other_merchant.pk,materials)
        self.assertEqual([row['available'] for row in rows],[True,False])
        rows=stock_check(self.case,self.other_merchant.pk,[dict(self.materials[0],specification_id=None)])
        self.assertFalse(rows[0]['available'])

    def test_no_parts_needed_does_not_block(self):
        result,row=inspect_installer(AfterSalesService,self.case,self.af,'INSTALLATION_OR_WIRING',[])
        self.assertEqual(result['status'],'READY')
        self.assertEqual(row['user'].pk,self.installer.pk)

    def test_preview_single_nearest_without_dispatch_or_tasks(self):
        from workflow.models import WorkItem
        self.stock(self.merchant);self.add_far_station()
        before=WorkItem.objects.count()
        result,row=self.preview()
        self.assertEqual(result['status'],'MISSING_PARTS')
        self.assertEqual(result['nearest_station']['station_application_id'],self.station.pk)
        self.assertNotIn('service_station_recommendations',result)
        self.assertTrue(result['confirmation_token'])
        self.case.refresh_from_db();self.assertEqual(self.case.status,'SUBMITTED')
        self.assertEqual(WorkItem.objects.count(),before)

    def test_no_matching_station_blocks_fallback(self):
        result,row=self.preview()
        self.assertEqual(result['status'],'MISSING_PARTS');self.assertIsNone(result['nearest_station']);self.assertIsNone(row)

    def test_direct_installer_submission_rejected_when_missing_stock(self):
        with self.assertRaises(AfterSalesError):
            AfterSalesService.triage(application_id=self.case.pk,user=self.af,data=self.dispatch_data())
        self.case.refresh_from_db();self.assertEqual(self.case.status,'SUBMITTED')

    def dispatch_data(self):
        return {'dispatch_route':'ORIGINAL_INSTALLER','assigned_user_id':self.installer.pk,'preliminary_fault_type':'COMPONENT_SUSPECTED',
            'preliminary_fault_materials':['7'],'diagnosis_note':'疑似控制器','repair_plan':'检查并更换控制器'}

    def fallback_data(self,preview):
        return {**self.dispatch_data(),'dispatch_route':'SERVICE_STATION','service_station_application_id':preview['nearest_station']['station_application_id'],
            'original_installer_fallback_confirmed':True,'original_installer_fallback_token':preview['confirmation_token']}

    def test_fallback_requires_explicit_confirmation_valid_token_and_nearest(self):
        self.stock(self.merchant);far=self.add_far_station();preview,_=self.preview();data=self.fallback_data(preview)
        for bad in ({**data,'original_installer_fallback_confirmed':False},{**data,'original_installer_fallback_token':'forged'},
                    {**data,'service_station_application_id':far.pk}):
            with self.assertRaises(ValueError):validate_fallback(AfterSalesService,self.case,self.af,'COMPONENT_SUSPECTED',self.materials,bad)
        with patch('django.core.signing.time.time',return_value=timezone.now().timestamp()+601):
            with self.assertRaises(ValueError):validate_fallback(AfterSalesService,self.case,self.af,'COMPONENT_SUSPECTED',self.materials,data)

    def test_stock_changes_after_preview_require_new_confirmation(self):
        lot=self.stock(self.merchant);far=self.add_far_station();preview,_=self.preview();data=self.fallback_data(preview)
        lot.reserved_milli=1000;lot.save(update_fields=['reserved_milli'])
        with self.assertRaises(ValueError):validate_fallback(AfterSalesService,self.case,self.af,'COMPONENT_SUSPECTED',self.materials,data)

    def test_missing_spec_has_no_fallback_even_with_other_stock(self):
        self.stock(self.merchant)
        result,_=inspect_installer(AfterSalesService,self.case,self.af,'COMPONENT_SUSPECTED',[dict(self.materials[0],specification_id=None)])
        self.assertEqual(result['status'],'MISSING_PARTS');self.assertIsNone(result['nearest_station'])

    def test_both_routes_share_repair_confirmation_close_and_basis(self):
        for fallback in (False,True):
            with self.subTest(fallback=fallback):
                if not fallback:
                    self.stock(self.other_merchant)
                    data=self.dispatch_data();executor=self.installer
                else:
                    OrganizationMaterialLot.objects.filter(stock__organization=self.other_merchant).update(on_hand_milli=0)
                    self.stock(self.merchant)
                    preview,_=self.preview();data=self.fallback_data(preview);executor=self.sales
                self.case.status='SUBMITTED';self.case.execution_stage='NONE';self.case.repair_settlement_basis={}
                self.case.repair_completed_at=None;self.case.save()
                with ExitStack() as stack:
                    for name in ('_publish_assignee_workorder','_publish_applicant_notice','_publish_factory_aftersales_notice',
                                 '_publish_applicant_workorder','_complete_tasks','_complete_factory_workorder','_complete_assignee_workorder',
                                 '_complete_applicant_workorder','_send_factory_close_task','_publish_supplier_quality_notices','_require_media','serialize'):
                        stack.enter_context(patch.object(AfterSalesService,name))
                    AfterSalesService.triage(application_id=self.case.pk,user=self.af,data=data)
                    self.case.refresh_from_db()
                    if fallback:self.assertIn('original_installer_fallback',self.case.assignee_snapshot)
                    AfterSalesService.repair_complete(application_id=self.case.pk,user=executor,data={'repair_summary':'完成更换','repair_material_replaced':True,
                        'repair_materials':[{'bom_item_id':7,'replacement_quantity':2}]})
                    self.case.refresh_from_db();self.assertEqual(self.case.status,'WAITING_CUSTOMER_CONFIRM')
                    AfterSalesService.applicant_confirm(application_id=self.case.pk,user=self.customer_owner,data={'confirmed':True})
                    self.case.refresh_from_db();self.assertEqual(self.case.status,'PENDING_FACTORY_CLOSE')
                    AfterSalesService.factory_close(application_id=self.case.pk,user=self.af,data={'final_fault_type':'OTHER','final_solution_type':'COMPONENT_REPLACEMENT','final_solution_note':'实际更换2件'})
                    self.case.refresh_from_db();self.assertEqual(self.case.status,'CLOSED')
                    self.assertEqual(self.case.repair_settlement_basis['replacement_pieces'],2)
                    self.assertEqual(self.case.repair_settlement_basis['executor_id'],executor.pk)

    def test_visible_device_service_is_not_installer_workload(self):
        app=self.completed();app.repair_settlement_basis=make_basis(app);app.save()
        from lifecycle.services.aftersales_access import visible_cases
        self.assertTrue(visible_cases(self.installer).filter(pk=app.pk).exists())
        self.assertEqual(summary(self.installer)['repair_completed_visits'],0)
        self.assertEqual(summary(self.sales)['repair_completed_visits'],1)
        self.assertEqual(summary(self.owner)['repair_completed_visits'],1)
        self.assertEqual(summary(self.customer_owner)['repair_completed_visits'],1)
        self.assertEqual(len(catalog(self.installer)['items']),0)

    def test_owner_and_manager_do_not_claim_other_organizations_repairs(self):
        app=self.completed();app.assigned_organization=self.other_merchant;app.repair_completed_by=self.installer
        app.repair_settlement_basis=make_basis(app);app.save()
        self.assertEqual(summary(self.owner)['repair_basis_visits'],0)
        other_owner,_=self.user('other-merchant-owner',self.other_merchant,'merchant_owner')
        self.assertEqual(summary(other_owner)['repair_basis_visits'],1)

    def test_changed_original_installer_invalidates_confirmation(self):
        self.stock(self.merchant);preview,_=self.preview();data=self.fallback_data(preview)
        new_user,_=self.user('replacement-installer',self.other_merchant,'merchant_electrician')
        self.installation.operator=new_user;self.installation.save(update_fields=['operator'])
        self.case=AfterSalesApplication.objects.get(pk=self.case.pk)
        with self.assertRaises(ValueError):validate_fallback(AfterSalesService,self.case,self.af,'COMPONENT_SUSPECTED',self.materials,data)

    def test_inactive_identity_and_changed_noninstallation_role_are_blocked(self):
        self.installer_identity.is_active=False;self.installer_identity.save(update_fields=['is_active'])
        self.assertEqual(self.preview()[0]['status'],'INSTALLER_UNAVAILABLE')
        self.installer_identity.is_active=True;self.installer_identity.role=self.sales_i.role
        self.installer_identity.save(update_fields=['is_active','role'])
        self.assertEqual(self.preview()[0]['status'],'INSTALLER_UNAVAILABLE')

    def test_different_factory_installation_record_is_blocked(self):
        self.installation.organization=self.other_factory;self.installation.save(update_fields=['organization'])
        self.case=AfterSalesApplication.objects.get(pk=self.case.pk)
        self.assertEqual(self.preview()[0]['status'],'INSTALLER_UNAVAILABLE')

    def test_manager_workload_counts_actual_managed_executor_only(self):
        from account.models import SeniorManagerProfile
        profile=SeniorManagerProfile.objects.create(identity=self.manager_i)
        profile.managed_roles.add(self.sales_i.role)
        self.sales_i.inviter=self.manager;self.sales_i.save(update_fields=['inviter'])
        AfterSalesServiceStationLiaison.objects.create(application=self.station,identity=self.plain_i)
        app=self.completed();app.repair_completed_by=self.plain;app.repair_settlement_basis=make_basis(app);app.save()
        from lifecycle.services.aftersales_access import visible_cases
        self.assertTrue(visible_cases(self.manager).filter(pk=app.pk).exists())
        self.assertEqual(summary(self.manager)['repair_basis_visits'],0)
        self.plain_i.inviter=self.manager;self.plain_i.save(update_fields=['inviter'])
        self.assertEqual(summary(self.manager)['repair_basis_visits'],1)
        profile.managed_roles.clear()
        self.assertEqual(summary(self.manager)['repair_basis_visits'],0)
