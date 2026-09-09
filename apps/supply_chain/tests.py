import json

from django.test import RequestFactory, TestCase

from account.models import Organization, OrganizationType
from product.models import ProductCategory
from supply_chain.models import Material
from supply_chain.web.views import add_material, build_material_code, get_suppliers


class MaterialCodeTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.generator_category = ProductCategory.objects.create(
            code="GEN",
            name="驻车发电机",
        )
        self.air_category = ProductCategory.objects.create(
            code="AIR",
            name="驻车空调",
        )

    def post_material(self, category, code="CTRL", name="控制器"):
        request = self.factory.post(
            "/pldp/web/supply-chain/api/material/add/",
            data=json.dumps(
                {
                    "code": code,
                    "name": name,
                    "product_category_id": category.id if category else "",
                    "is_key_part": True,
                    "description": "测试物料",
                }
            ),
            content_type="application/json",
        )
        response = add_material(request)
        return json.loads(response.content.decode("utf-8"))

    def test_build_material_code_adds_category_prefix(self):
        self.assertEqual(build_material_code("GEN", "CTRL"), "GEN-CTRL")

    def test_build_material_code_does_not_repeat_existing_prefix(self):
        self.assertEqual(build_material_code("GEN", "gen-ctrl"), "GEN-CTRL")

    def test_create_material_saves_prefixed_code(self):
        result = self.post_material(self.generator_category)

        self.assertTrue(result["success"])
        self.assertEqual(result["code"], "GEN-CTRL")
        self.assertTrue(
            Material.objects.filter(
                code="GEN-CTRL",
                product_category=self.generator_category,
            ).exists()
        )

    def test_same_code_part_can_be_used_in_different_categories(self):
        first = self.post_material(self.generator_category, name="发电机控制器")
        second = self.post_material(self.air_category, name="空调控制器")

        self.assertTrue(first["success"])
        self.assertTrue(second["success"])
        self.assertEqual(
            set(Material.objects.values_list("code", flat=True)),
            {"GEN-CTRL", "AIR-CTRL"},
        )

    def test_category_is_required(self):
        result = self.post_material(None)

        self.assertFalse(result["success"])
        self.assertEqual(result["message"], "请选择所属产品分类")
        self.assertEqual(Material.objects.count(), 0)

    def test_duplicate_full_code_is_rejected(self):
        first = self.post_material(self.generator_category)
        second = self.post_material(self.generator_category, code="GEN-CTRL")

        self.assertTrue(first["success"])
        self.assertFalse(second["success"])
        self.assertIn("GEN-CTRL", second["message"])
        self.assertEqual(Material.objects.count(), 1)

    def test_supplier_options_use_organization_type_foreign_key(self):
        supplier_type = OrganizationType.objects.get(code="SUPPLIER")
        merchant_type = OrganizationType.objects.get(code="MERCHANT")
        supplier = Organization.objects.create(
            name="测试供应商",
            code="TEST-SUPPLIER-OPTIONS",
            organization_type=supplier_type,
        )
        Organization.objects.create(
            name="停用供应商",
            code="TEST-INACTIVE-SUPPLIER",
            organization_type=supplier_type,
            is_active=False,
        )
        Organization.objects.create(
            name="测试商家",
            code="TEST-MERCHANT-OPTIONS",
            organization_type=merchant_type,
        )

        response = get_suppliers(
            self.factory.get("/pldp/web/supply-chain/api/suppliers/")
        )
        result = json.loads(response.content.decode("utf-8"))

        self.assertEqual([item["id"] for item in result], [supplier.pk])
