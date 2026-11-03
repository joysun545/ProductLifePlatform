from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
from django.test import TestCase, RequestFactory
from django.utils import timezone
from account.models import Organization, OrganizationType, OrganizationRole, User, UserOrganization, SeniorManagerProfile
from account.services.auth_service import issue_access_token
from lifecycle.models import (AfterSalesApplication, AfterSalesServiceStationApplication,
    AfterSalesServiceStationLiaison, Device, OrganizationDevice, InstallationRecord,
    CustomerVehicle, CustomerDeviceBinding, DriverVehicleBinding)
from lifecycle.services.aftersales_access import visible_cases, capabilities
from lifecycle.services.aftersales_service import AfterSalesService
from lifecycle.services.repair_basis_service import make_basis, summary, catalog, actual_quantity, normalize_replacements
from data_statistics.services import get_statistics_identity, build_dashboard
from data_statistics.api.views import repair_basis
from manufacturing.models import ProductionBatch
from product.models import BOM, Brand, ProductCategory, ProductModel

class AfterSalesAccessBasisTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        def org(code, kind, root=None):
            return Organization.objects.create(code=code, name=code,
                organization_type=OrganizationType.objects.get(code=kind), factory_root=root)
        cls.factory=org('BASIS-FACTORY','OWNER')
        cls.other_factory=org('BASIS-OTHER','OWNER')
        cls.merchant=org('BASIS-MERCHANT','MERCHANT',cls.factory)
        cls.other_merchant=org('BASIS-OTHER-M','MERCHANT',cls.factory)
        cls.customer=org('BASIS-CUSTOMER','CUSTOMER',cls.factory)
        cls.owner,cls.owner_i=cls.user('owner',cls.merchant,'merchant_owner')
        cls.sales,cls.sales_i=cls.user('sales',cls.merchant,'merchant_sales')
        cls.plain,cls.plain_i=cls.user('plain',cls.merchant,'merchant_sales')
        cls.installer,_=cls.user('installer',cls.merchant,'merchant_electrician')
        cls.customer_owner,_=cls.user('customer',cls.customer,'customer_owner')
        cls.driver,_=cls.user('driver',cls.customer,'driver')
        cls.af,_=cls.user('af',cls.factory,'factory_aftersales')
        cls.admin,_=cls.user('admin',cls.factory,'factory_admin')
        cls.manager,cls.manager_i=cls.user('manager',cls.merchant,'merchant_senior_manager')
        cls.fm,cls.fm_i=cls.user('factory-manager',cls.factory,'factory_senior_manager')
        cls.station=AfterSalesServiceStationApplication.objects.create(organization=cls.merchant,submitted_by=cls.owner,status='APPROVED',service_latitude=29.6,service_longitude=106.5,location_captured_at=timezone.now(),notice_agreed_at=timezone.now(),receiving_address='测试地址',receiving_contact_name='测试人',receiving_contact_phone='13900000000')
        AfterSalesServiceStationLiaison.objects.create(application=cls.station,identity=cls.sales_i)
        cat=ProductCategory.objects.create(name='依据品类',code='BASIS-CAT')
        brand=Brand.objects.create(name='依据品牌',code='BASIS-BRAND')
        cls.product=ProductModel.objects.create(code='BASIS-MODEL',name='依据型号',category=cat)
        bom=BOM.objects.create(product_model=cls.product,version='V1',created_by=cls.admin)
        cls.batch=ProductionBatch.objects.create(batch_no='BASIS-BATCH',product_model=cls.product,bom=bom,manufacturer=cls.factory,production_date='2026-11-02',quantity=100)
        cls.device=Device.objects.create(sn='BASIS-SN',product_model=cls.product,batch=cls.batch)
        OrganizationDevice.objects.create(device=cls.device,organization=cls.customer,status='ACTIVATED')
        cls.case=AfterSalesApplication.objects.create(case_no='BASIS-AS',device=cls.device,customer_organization=cls.customer,
            factory_organization=cls.factory,applicant=cls.customer_owner,assigned_user=cls.sales,assigned_organization=cls.merchant,
            dispatch_route='SERVICE_STATION',status='PROCESSING',assignee_snapshot={'service_station_application_id':cls.station.pk})
    @classmethod
    def user(cls,name,org,role):
        user=User.objects.create_user(username='basis-'+name,wx_openid='basis-openid-'+name)
        identity=UserOrganization.objects.create(user=user,organization=org,role=OrganizationRole.objects.get(organization_type=org.organization_type,code=role))
        return user,identity
    def test_station_sales_entry_without_granting_plain_sales(self):
        self.assertTrue(capabilities(self.sales)['can_view_aftersales'])
        self.assertFalse(capabilities(self.plain)['can_view_aftersales'])
        self.assertTrue(visible_cases(self.sales).filter(pk=self.case.pk).exists())
        self.assertFalse(visible_cases(self.plain).exists())
    def test_other_liaison_reads_station_case(self):
        AfterSalesServiceStationLiaison.objects.create(application=self.station,identity=self.plain_i)
        self.assertTrue(AfterSalesService.can_view_application(application=self.case,user=self.plain))
    def test_owner_and_installer_scope(self):
        self.assertTrue(AfterSalesService.can_view_application(application=self.case,user=self.owner))
        self.assertFalse(visible_cases(self.installer).exists())
        InstallationRecord.objects.create(device=self.device,organization=self.merchant,operator=self.installer)
        self.assertTrue(visible_cases(self.installer).filter(pk=self.case.pk).exists())
        other,_=self.user('outsider',self.other_merchant,'merchant_owner')
        self.assertFalse(visible_cases(other).exists())
    def test_factory_tenant_boundary(self):
        for user in (self.af,self.admin): self.assertTrue(visible_cases(user).filter(pk=self.case.pk).exists())
        other,_=self.user('other-admin',self.other_factory,'factory_admin')
        self.assertFalse(visible_cases(other).exists())
    def test_factory_manager_explicit_delegation(self):
        profile=SeniorManagerProfile.objects.create(identity=self.fm_i)
        self.assertFalse(visible_cases(self.fm).exists())
        profile.managed_roles.add(OrganizationRole.objects.get(organization_type=self.factory.organization_type,code='factory_aftersales'))
        self.assertTrue(visible_cases(self.fm).filter(pk=self.case.pk).exists())
    def test_merchant_manager_manages_specific_employees(self):
        profile=SeniorManagerProfile.objects.create(identity=self.manager_i)
        profile.managed_roles.add(self.sales_i.role)
        self.assertFalse(visible_cases(self.manager).exists())
        self.sales_i.inviter=self.manager;self.sales_i.save(update_fields=['inviter'])
        self.assertTrue(visible_cases(self.manager).filter(pk=self.case.pk).exists())
        profile.managed_roles.clear()
        self.assertFalse(visible_cases(self.manager).exists())
    def test_customer_and_driver_revoked_vehicle(self):
        self.assertTrue(visible_cases(self.customer_owner).filter(pk=self.case.pk).exists())
        self.assertFalse(visible_cases(self.driver).exists())
        vehicle=CustomerVehicle.objects.create(customer_organization=self.customer,vehicle_no='A1',normalized_vehicle_no='A1')
        CustomerDeviceBinding.objects.create(device=self.device,customer_organization=self.customer,source_organization=self.merchant,vehicle=vehicle,status='ACTIVE')
        binding=DriverVehicleBinding.objects.create(vehicle=vehicle,driver=self.driver,is_active=True)
        self.assertTrue(visible_cases(self.driver).filter(pk=self.case.pk).exists())
        self.case.applicant=self.driver;self.case.save(update_fields=['applicant'])
        binding.is_active=False;binding.save(update_fields=['is_active'])
        self.assertFalse(AfterSalesService.can_view_application(application=self.case,user=self.driver))
        self.assertEqual(build_dashboard(get_statistics_identity(self.driver))['modules'],['AFTERSALES'])
    def completed(self,app=None,quantity=2):
        app=app or self.case;now=timezone.now()
        app.repair_completed_at=now;app.repair_completed_by=self.sales;app.repair_material_replaced=True
        app.repair_materials=[{'bom_item_id':1,'material_id':1,'quantity':100,'replacement_quantity':quantity}]
        app.applicant_confirmed_at=now;app.applicant_confirm_mode='MANUAL'
        app.status='CLOSED';app.closed_at=now;app.closed_by=self.af
        return app
    def test_same_device_two_repairs_actual_pieces_not_bom(self):
        app=self.completed();app.repair_settlement_basis=make_basis(app);app.save()
        second=AfterSalesApplication.objects.create(case_no='BASIS-AS-2',device=self.device,customer_organization=self.customer,factory_organization=self.factory,
            applicant=self.customer_owner,assigned_user=self.sales,assigned_organization=self.merchant,status='CLOSED')
        second=self.completed(second,3);second.repair_settlement_basis=make_basis(second);second.save()
        totals=summary(self.owner)
        self.assertEqual(totals['repair_basis_visits'],2)
        self.assertEqual(totals['replacement_basis_pieces'],5)
    def test_historical_missing_quantity_not_inferred(self):
        app=self.completed(quantity=None);app.save();totals=summary(self.owner)
        self.assertEqual(totals['repair_basis_visits'],0)
        self.assertEqual(totals['replacement_basis_pieces'],0)
        self.assertEqual(totals['repair_basis_review_visits'],1)
        self.assertEqual(catalog(self.owner)['items'][0]['status'],'LEGACY_REVIEW')
    def test_incomplete_and_cancelled_excluded(self):
        app=self.completed();app.status='WAITING_CUSTOMER_CONFIRM';app.applicant_confirmed_at=None;app.save()
        self.assertEqual(summary(self.owner)['repair_waiting_visits'],1)
        self.assertEqual(make_basis(app)['status'],'REVIEW_REQUIRED')
        app.status='CANCELLED';app.save(update_fields=['status'])
        self.assertEqual(summary(self.owner)['repair_completed_visits'],0)
    def test_quantity_validation_legacy_explicit_unknown(self):
        for value in (True,0,-1,'NaN','2.5',100001):self.assertIsNone(actual_quantity(value))
        service=SimpleNamespace(_normalize_materials=lambda *a,**k:[{'bom_item_id':1,'quantity':100}])
        self.assertEqual(normalize_replacements(service,None,[{'bom_item_id':1,'replacement_quantity':2}])[0]['replacement_quantity'],2)
        self.assertIsNone(normalize_replacements(service,None,['1'])[0]['replacement_quantity'])
        with self.assertRaises(ValueError):normalize_replacements(service,None,[{'bom_item_id':1,'replacement_quantity':0}])
    def test_close_freezes_and_read_api_does_not_write(self):
        app=self.completed();app.repair_settlement_basis={};app.save()
        with patch.object(AfterSalesService,'_validate_final_data',return_value=('OTHER',[],'OTHER','测试结案')),patch.object(AfterSalesService,'_event'),patch.object(AfterSalesService,'_publish_supplier_quality_notices'),patch.object(AfterSalesService,'_publish_applicant_notice'):
            AfterSalesService._close_application(app,user=self.af,identity=None,data={})
        app.refresh_from_db();frozen=deepcopy(app.repair_settlement_basis)
        self.assertEqual(frozen['replacement_pieces'],2)
        request=RequestFactory().get('/data-statistics/repair-basis/',HTTP_AUTHORIZATION='Bearer '+issue_access_token(self.owner))
        self.assertEqual(repair_basis(request).status_code,200)
        app.refresh_from_db();self.assertEqual(app.repair_settlement_basis,frozen)
        with self.assertRaises(ValueError):catalog(self.owner,'bad')

    def test_observer_can_read_but_not_perform_repair(self):
        self.assertTrue(AfterSalesService.can_view_application(application=self.case,user=self.owner))
        from lifecycle.services.aftersales_service import AfterSalesError
        with self.assertRaises(AfterSalesError):
            AfterSalesService._require_assignee(self.case,self.owner)

    def test_basis_api_forbidden_for_plain_sales(self):
        import json
        request=RequestFactory().get('/data-statistics/repair-basis/',HTTP_AUTHORIZATION='Bearer '+issue_access_token(self.plain))
        self.assertNotEqual(json.loads(repair_basis(request).content)['code'],0)

    def test_fifty_record_keyset_pagination_no_repeat(self):
        app=self.completed();app.repair_settlement_basis=make_basis(app);app.save()
        for index in range(51):
            row=AfterSalesApplication(case_no='BASIS-PAGE-%s'%index,device=self.device,customer_organization=self.customer,
                factory_organization=self.factory,applicant=self.customer_owner,assigned_user=self.sales,
                assigned_organization=self.merchant,status='CLOSED')
            row=self.completed(row);row.save()
            row.repair_settlement_basis=make_basis(row);row.save(update_fields=['repair_settlement_basis'])
        first=catalog(self.owner);second=catalog(self.owner,first['next_cursor'])
        self.assertEqual(len(first['items']),50)
        self.assertEqual(len(second['items']),2)
        self.assertIsNone(second['next_cursor'])
        self.assertFalse({r['application_id'] for r in first['items']} & {r['application_id'] for r in second['items']})
        self.assertEqual(first['metrics']['repair_basis_visits'],52)

    def test_station_supervisor_entry_exists_before_first_case(self):
        profile=SeniorManagerProfile.objects.create(identity=self.manager_i)
        profile.managed_roles.add(self.sales_i.role)
        self.sales_i.inviter=self.manager;self.sales_i.save(update_fields=['inviter'])
        self.case.delete()
        self.assertTrue(capabilities(self.manager)['can_view_aftersales'])
        profile.managed_roles.clear()
        self.assertFalse(capabilities(self.manager)['can_view_aftersales'])
