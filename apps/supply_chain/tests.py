import json
import time

from unittest.mock import patch

from django.db import IntegrityError
from django.test import Client, RequestFactory, TestCase

from account.models import Organization, OrganizationRelation, OrganizationRole, OrganizationType, User, UserOrganization
from account.services.auth_service import _wechat_token_binding
from product.models import ProductCategory
from supply_chain.models import Material, MaterialSpecSupplier
from supply_chain.web.views import add_material, build_material_code, get_suppliers
from web_portal.access import GRANT, principal_for


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

    def post_material(self, category, code="CTRL", name="控制器", **extra_fields):
        request = self.factory.post(
            "/pldp/web/supply-chain/api/material/add/",
            data=json.dumps(
                {
                    "code": code,
                    "name": name,
                    "product_category_id": category.id if category else "",
                    "is_key_part": True,
                    "description": "测试物料",
                    **extra_fields,
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
                product_categories=self.generator_category,
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

    def test_invalid_applicable_category_leaves_no_material(self):
        missing_id = self.air_category.pk + 1
        result = self.post_material(self.generator_category, product_category_ids=[missing_id])

        self.assertFalse(result["success"], result)
        self.assertFalse(Material.objects.exists())

    def test_category_attachment_failure_rolls_back_material(self):
        with patch.object(
            Material.product_categories.related_manager_cls, "add",
            side_effect=IntegrityError("fixture relation insert failed"),
        ):
            result = self.post_material(self.generator_category)

        self.assertFalse(result["success"], result)
        self.assertFalse(Material.objects.exists())

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

        factory = Organization.objects.create(
            name="测试厂家", code="TEST-SUPPLIER-FACTORY",
            organization_type=OrganizationType.objects.get(code="OWNER"),
        )
        OrganizationRelation.objects.create(
            source_organization=factory, target_organization=supplier,
            relation_type="SUPPLY",
        )
        request = self.factory.get("/pldp/web/supply-chain/api/suppliers/")
        request.web_portal_principal = {"kind": "factory", "organization_id": factory.pk}
        response = get_suppliers(request)
        result = json.loads(response.content.decode("utf-8"))

        self.assertEqual([item["id"] for item in result], [supplier.pk])


class MaterialWebCategoryTests(TestCase):
    url = "/pldp/web/supply-chain/"

    def setUp(self):
        self.browser = Client(enforce_csrf_checks=True)
        factory_type = OrganizationType.objects.get(code="OWNER")
        self.factory = Organization.objects.create(
            name="配套测试厂家", code="WEB-MATERIAL-FACTORY",
            organization_type=factory_type,
        )
        matching = User.objects.create_user(username="web-material-matching", wx_openid="web-material-matching")
        UserOrganization.objects.create(
            user=matching, organization=self.factory,
            role=OrganizationRole.objects.get(code="factory_matching", organization_type=factory_type),
        )
        self.browser.force_login(matching)
        session = self.browser.session
        session[GRANT] = {
            "principal": principal_for(matching),
            "binding": _wechat_token_binding(matching),
            "expires_at": time.time() + 600,
        }
        session.save()
        self.browser.get("/pldp/web/portal/api/session/")
        self.csrf = self.browser.cookies["csrftoken"].value

    def post(self, endpoint, payload):
        response = self.browser.post(
            self.url + "api/" + endpoint,
            payload, content_type="application/json", HTTP_X_CSRFTOKEN=self.csrf,
        )
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertTrue(data["success"], data)
        return data

    def rows(self, endpoint, **params):
        response = self.browser.get(self.url + "api/" + endpoint, params)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_browser_can_create_material_and_query_each_applicable_category(self):
        generator = ProductCategory.objects.create(name="发电机", code="GEN")
        air = ProductCategory.objects.create(name="空调", code="AIR")
        unrelated = ProductCategory.objects.create(name="蓄电池", code="BAT")
        foreign_factory = Organization.objects.create(
            name="外部厂家", code="WEB-MATERIAL-OTHER-FACTORY",
            organization_type=self.factory.organization_type,
        )
        supplier_type = OrganizationType.objects.get(code="SUPPLIER")
        suppliers = []
        for factory in (self.factory, foreign_factory):
            supplier = Organization.objects.create(
                name=factory.name + "供应商", code=factory.code + "-SUPPLIER",
                organization_type=supplier_type,
            )
            OrganizationRelation.objects.create(
                source_organization=factory, target_organization=supplier,
                relation_type="SUPPLY",
            )
            suppliers.append(supplier)
        local, foreign = suppliers

        page = self.browser.get(self.url + "material-config/")
        self.assertContains(page, 'id="material_applicable_categories"')
        created = self.post("material/add/", {
            "code": "gen-ctrl", "name": "通用控制器",
            "product_category_id": str(generator.pk),
            "product_category_ids": [str(air.pk), str(generator.pk)],
        })
        self.assertEqual(created["code"], "GEN-CTRL")
        self.assertSetEqual(
            set(Material.objects.get(pk=created["id"]).product_categories.values_list("pk", flat=True)),
            {generator.pk, air.pk},
        )
        spec = self.post("material-spec/add/", {
            "material_id": created["id"], "code": "CTRL-1", "name": "控制器规格",
        })
        local_link = self.post("material-spec-supplier/add/", {
            "material_spec_id": spec["id"], "supplier_id": local.pk,
        })
        MaterialSpecSupplier.objects.create(material_spec_id=spec["id"], supplier=foreign)

        for category in (generator, air):
            with self.subTest(category=category.code):
                filters = {"category": category.pk}
                materials = self.rows("materials/", **filters)
                self.assertEqual([row["id"] for row in materials], [created["id"]])
                self.assertEqual(materials[0]["category"], "空调、发电机")
                specs = self.rows("material-specs/", **filters)
                self.assertEqual([row["id"] for row in specs], [spec["id"]])
                self.assertEqual(specs[0]["category"], "空调、发电机")
                self.assertEqual([row["id"] for row in self.rows("materials-by-category/", **filters)], [created["id"]])
                self.assertEqual([row["id"] for row in self.rows("suppliers-by-category/", **filters)], [local.pk])
                tree = self.rows("material-supplier-list/", **filters)
                self.assertEqual([row["id"] for row in tree], [created["id"]])
                self.assertEqual([row["id"] for row in tree[0]["specs"][0]["suppliers"]], [local_link["id"]])

        for endpoint in (
            "materials/", "material-specs/", "materials-by-category/",
            "suppliers-by-category/", "material-supplier-list/",
        ):
            self.assertEqual(self.rows(endpoint, category=unrelated.pk), [], endpoint)
        self.assertEqual([row["id"] for row in self.rows("materials/")], [created["id"]])
        self.assertEqual([row["id"] for row in self.rows("material-specs/")], [spec["id"]])
        self.assertEqual(self.rows("material-supplier-list/", category=air.pk, supplier=foreign.pk), [])
