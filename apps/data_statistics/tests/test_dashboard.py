from django.test import TestCase
from django.urls import resolve

from account.models import Organization, OrganizationRole, OrganizationType, User, UserOrganization
from data_statistics.services import build_dashboard, get_statistics_identity
from data_statistics.services.flow_service import _counts
from data_statistics.services.flow_service import build_flow_statistics
from lifecycle.models import ActivationRecord, AfterSalesApplication, Device, InstallationRecord, OrganizationDevice
from manufacturing.models import ProductionBatch
from product.models import BOM, ProductCategory, ProductModel
from supply_chain.models import FactoryMaterialLot, FactoryMaterialStock, Material, MaterialSpec
from data_statistics.services.material_service import build_factory_material_lot_catalog


class DataStatisticsDashboardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        owner_type = OrganizationType.objects.get(code="OWNER")
        cls.factory = Organization.objects.create(
            name="统计测试厂家", code="STATS-FACTORY", organization_type=owner_type,
        )
        role = OrganizationRole.objects.get(
            organization_type=owner_type, code="factory_chief_engineer",
        )
        cls.user = User.objects.create_user(username="stats-chief")
        UserOrganization.objects.create(user=cls.user, organization=cls.factory, role=role)
        category = ProductCategory.objects.create(name="统计品类", code="STATS-CATEGORY")
        product = ProductModel.objects.create(code="STATS-MODEL", name="统计型号", category=category)
        bom = BOM.objects.create(product_model=product, version="V1", created_by=cls.user)
        batch = ProductionBatch.objects.create(
            batch_no="STATS-BATCH", product_model=product, bom=bom,
            manufacturer=cls.factory, production_date="2026-08-31", quantity=1,
        )
        cls.device = Device.objects.create(sn="STATS-SN-1", product_model=product, batch=batch)
        OrganizationDevice.objects.create(device=cls.device, organization=cls.factory, status="IN_STOCK")
        InstallationRecord.objects.create(device=cls.device, organization=cls.factory, status="INSTALLED")
        ActivationRecord.objects.create(device=cls.device, organization=cls.factory, status="ACTIVATED")
        AfterSalesApplication.objects.create(
            case_no="STATS-AS-1", device=cls.device,
            customer_organization=cls.factory, factory_organization=cls.factory,
            applicant=cls.user, status=AfterSalesApplication.Status.SUBMITTED,
        )

    def test_role_dashboard_uses_independent_statistics_service(self):
        dashboard = build_dashboard(get_statistics_identity(self.user))
        self.assertEqual(dashboard["dashboard_name"], "质量与技术风险驾驶舱")
        self.assertEqual(dashboard["modules"], ["AFTERSALES", "MATERIAL", "QUALITY"])
        aftersales = next(row for row in dashboard["sections"] if row["domain"] == "AFTERSALES")
        self.assertEqual(aftersales["metrics"]["aftersales_fault_device_count"], 1)
        self.assertEqual(aftersales["metrics"]["activated_exposure_device_count"], 1)
        self.assertEqual(aftersales["metrics"]["aftersales_fault_rate"], 100.0)

    def test_statistics_urls_are_owned_by_new_app(self):
        match = resolve("/pldp/api/data-statistics/dashboard/")
        self.assertEqual(match.namespace, "pldp:api:data_statistics")

    def test_flow_statistics_accepts_statistics_identity(self):
        identity = get_statistics_identity(self.user)
        data = build_flow_statistics(identity)
        self.assertEqual(data["domain"], "FLOW")
        self.assertIn("responsibility_tree", data)

    def test_market_flow_states_are_mutually_exclusive(self):
        rows = [
            {"market_status": "MARKET_INVENTORY"},
            {"market_status": "ACTIVATED"},
            {"market_status": "IN_TRANSIT"},
            {"market_status": "RETURN_OR_RECALL"},
        ]
        totals = _counts(rows)
        self.assertEqual(totals["market_device_count"], 4)
        self.assertEqual(totals["market_inventory_device_count"], 1)
        self.assertEqual(totals["activated_device_count"], 1)
        self.assertEqual(
            sum(value for key, value in totals.items() if key != "market_device_count"),
            totals["market_device_count"],
        )

    def test_material_warehouse_keeper_sees_historical_lot_catalog_only_in_statistics(self):
        role = OrganizationRole.objects.get(organization_type__code="OWNER", code="factory_material_stock")
        keeper = User.objects.create_user(username="stats-material-keeper")
        UserOrganization.objects.create(user=keeper, organization=self.factory, role=role)
        supplier = Organization.objects.create(name="南方供应商", code="STATS-SUPPLIER", organization_type=OrganizationType.objects.get(code="SUPPLIER"), factory_root=self.factory)
        material = Material.objects.create(code="STATS-MATERIAL", name="物料控制器")
        spec = MaterialSpec.objects.create(material=material, code="STATS-SPEC", name="统计规格")
        stock = FactoryMaterialStock.objects.create(factory=self.factory, supplier=supplier, material_spec=spec, unit="件")
        FactoryMaterialLot.objects.create(stock=stock, batch_code="ML-STATS-HISTORY", received_milli=5000, on_hand_milli=0, received_by=keeper)
        identity = get_statistics_identity(keeper)
        dashboard = build_dashboard(identity)
        self.assertEqual(dashboard["modules"], ["MATERIAL"])
        catalog = build_factory_material_lot_catalog(identity, query="南")
        self.assertEqual(catalog["items"][0]["batch_code"], "ML-STATS-HISTORY")
        self.assertEqual(catalog["items"][0]["status"], "已用完或已退完")
