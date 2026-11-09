from django.test import TestCase

from account.models import Organization, OrganizationRole, OrganizationType, User, UserOrganization
from account.services.auth_service import issue_access_token
from supply_chain.models import FactoryMaterialLot, FactoryMaterialStock, Material, MaterialSpec


class MaterialPageAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.factory_type = OrganizationType.objects.get(code="OWNER")
        cls.factory = Organization.objects.create(
            name="本厂家", code="STATS-FACTORY", organization_type=cls.factory_type,
        )
        other_factory = Organization.objects.create(
            name="其他厂家", code="STATS-OTHER", organization_type=cls.factory_type,
        )
        supplier_type = OrganizationType.objects.get(code="SUPPLIER")
        cls.keeper = User.objects.create_user(username="stats-keeper", wx_openid="stats-keeper-openid")
        cls.identity = UserOrganization.objects.create(
            user=cls.keeper, organization=cls.factory,
            role=OrganizationRole.objects.get(
                organization_type=cls.factory_type, code="factory_material_stock",
            ),
        )
        material = Material.objects.create(code="STATS-MATERIAL", name="物料")
        spec = MaterialSpec.objects.create(material=material, code="STATS-SPEC", name="规格")
        for factory, batch_code in [(cls.factory, "OWN-LOT"), (other_factory, "FOREIGN-LOT")]:
            supplier = Organization.objects.create(
                name="供货商", code=f"{factory.code}-SUPPLIER",
                organization_type=supplier_type, factory_root=factory,
            )
            stock = FactoryMaterialStock.objects.create(
                factory=factory, supplier=supplier, material_spec=spec, unit="件",
            )
            FactoryMaterialLot.objects.create(
                stock=stock, batch_code=batch_code, received_milli=5000, on_hand_milli=0,
                received_by=cls.keeper,
            )

    def statistics(self, path, *, headers=None, **params):
        if headers is None:
            headers = {"HTTP_AUTHORIZATION": "Bearer " + issue_access_token(self.keeper)}
        return self.client.get(f"/pldp/api/data-statistics/{path}/", params, **headers).json()

    def assert_access_denied(self, payload):
        self.assertEqual(payload["error_code"], "STATISTICS_ACCESS_DENIED")
        self.assertEqual(payload["data"], {})

    def test_warehouse_keeper_has_history_entry_and_only_own_exhausted_lots(self):
        material = self.statistics("material")
        self.assertIs(material["data"]["can_view_warehouse_lots"], True)
        definitions = {
            item["code"]: item for item in self.statistics("metric-definitions")["data"]["items"]
        }
        self.assertIn("supply_chain.FactoryMaterialLot", definitions["material_batch_count"]["source_models"])
        history = self.statistics("material-warehouse-lots", q="LOT")["data"]["items"]
        self.assertEqual([lot["batch_code"] for lot in history], ["OWN-LOT"])
        self.assertEqual(history[0]["on_hand"], "0")
        bad_cursor = self.statistics("material-warehouse-lots", cursor="bad")
        self.assertEqual(bad_cursor["code"], 1)
        self.assertNotIn("error_code", bad_cursor)

    def test_aggregate_access_does_not_grant_history_access(self):
        self.identity.role = OrganizationRole.objects.get(
            organization_type=self.factory_type, code="factory_chief_engineer",
        )
        self.identity.save(update_fields=["role"])
        material = self.statistics("material")
        self.assertIs(material["data"]["can_view_warehouse_lots"], False)
        self.assert_access_denied(self.statistics("material-warehouse-lots"))

    def test_same_session_gets_distinct_denial_after_permissions_are_revoked(self):
        headers = {"HTTP_AUTHORIZATION": "Bearer " + issue_access_token(self.keeper)}
        self.assertTrue(self.statistics("dashboard", headers=headers)["data"]["sections"])
        self.identity.role = OrganizationRole.objects.get(
            organization_type=self.factory_type, code="factory_logistics",
        )
        self.identity.save(update_fields=["role"])
        for path in ("material", "material-warehouse-lots"):
            self.assert_access_denied(self.statistics(path, headers=headers))

        self.identity.is_active = False
        self.identity.save(update_fields=["is_active"])
        for path in ("dashboard", "material", "material-warehouse-lots", "repair-basis"):
            self.assert_access_denied(self.statistics(path, headers=headers))
