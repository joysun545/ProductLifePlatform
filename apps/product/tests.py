import io
import json
import tempfile
import zipfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from account.models import (
    Organization, OrganizationRelation, OrganizationRole, OrganizationType, User, UserOrganization,
)
from account.services.auth_service import issue_access_token
from channel.models import MerchantChannelAssignment
from manufacturing.models import ProductionBatch
from product.models import (
    Brand,
    BOM,
    BOMItem,
    ProductCategory,
    ProductDocument,
    ProductModel,
)
from sales.models import OrderPlan, OrderPlanItem, OrderPlanItemBOMConfirmation, OrderPlanSalesBOMSelection
from supply_chain.models import Material, MaterialSpec, MaterialSpecSupplier


class ProductV1BOMAPITests(APITestCase):
    base_url = "/pldp/api/product"

    def setUp(self):
        self._media_root = tempfile.TemporaryDirectory()
        self.addCleanup(self._media_root.cleanup)
        self._media_override = override_settings(MEDIA_ROOT=self._media_root.name)
        self._media_override.enable()
        self.addCleanup(self._media_override.disable)

        owner_type = OrganizationType.objects.get(code="OWNER")
        self.factory = Organization.objects.create(
            code="BOM-TEST-FACTORY", name="产品资料测试厂家", organization_type=owner_type,
        )
        self.operator = User.objects.create_user(username="bom-test-operator", wx_openid="bom-test-openid")
        UserOrganization.objects.create(
            user=self.operator, organization=self.factory,
            role=OrganizationRole.objects.get(organization_type=owner_type, code="factory_production"),
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_access_token(self.operator)}")

        self.category = ProductCategory.objects.create(code="GEN", name="发电机")
        self.other_category = ProductCategory.objects.create(code="AIR", name="驻车空调")

        self.material_a = Material.objects.create(
            code="A001",
            name="控制器",
        )
        self.material_a.product_categories.add(self.category)
        self.material_b = Material.objects.create(
            code="B001",
            name="油箱",
        )
        self.material_b.product_categories.add(self.category)
        self.other_material = Material.objects.create(
            code="C001",
            name="压缩机",
        )
        self.other_material.product_categories.add(self.other_category)
        self.spec_a = MaterialSpec.objects.create(
            material=self.material_a,
            code="CTRL-01",
            name="标准控制器",
        )
        self.spec_b = MaterialSpec.objects.create(
            material=self.material_b,
            code="TANK-01",
            name="15L油箱",
        )
        self.other_spec = MaterialSpec.objects.create(
            material=self.other_material,
            code="COMP-01",
            name="空调压缩机",
        )

        supplier_type, _ = OrganizationType.objects.get_or_create(
            code="SUPPLIER",
            defaults={"name": "供应商"},
        )
        self.supplier_a = Organization.objects.create(
            code="SUP-A",
            name="甲供应商",
            organization_type=supplier_type,
        )
        self.supplier_b = Organization.objects.create(
            code="SUP-B",
            name="乙供应商",
            organization_type=supplier_type,
        )
        for supplier in (self.supplier_a, self.supplier_b):
            OrganizationRelation.objects.create(
                source_organization=self.factory, target_organization=supplier, relation_type="SUPPLY",
            )
        MaterialSpecSupplier.objects.create(
            material_spec=self.spec_a,
            supplier=self.supplier_a,
        )
        MaterialSpecSupplier.objects.create(
            material_spec=self.spec_b,
            supplier=self.supplier_b,
        )

    def valid_payload(self):
        return {
            "code": "JL-5000",
            "name": "5000W静音发电机",
            "category_id": self.category.id,
            "description": "汽油静音款",
            "documents": {
                "user_manual": "请按使用说明书启动设备。",
                "daily_maintenance": "每次使用前检查油液。",
                "troubleshooting_guide": "无法启动时先检查燃油和电源。",
            },
            "bom_items": [
                {
                    "material_id": self.material_a.id,
                    "material_spec_id": self.spec_a.id,
                    "supplier_id": self.supplier_a.id,
                    "remark": "核心控制器",
                    "is_deleted": False,
                },
                {
                    "material_id": self.material_b.id,
                    "material_spec_id": None,
                    "supplier_id": None,
                    "remark": "",
                    "is_deleted": True,
                },
            ],
        }

    @staticmethod
    def valid_docx_bytes():
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types></Types>")
            archive.writestr("word/document.xml", "<document></document>")
        return buffer.getvalue()

    def test_category_linked_to_material_cannot_be_deleted(self):
        self.material_a.product_categories.add(self.other_category)
        response = self.client.post(
            f"{self.base_url}/category/delete/", {"id": self.category.pk}, format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("物料使用", response.data["error"])
        self.assertTrue(ProductCategory.objects.filter(pk=self.category.pk).exists())
        self.assertSetEqual(
            set(self.material_a.product_categories.values_list("pk", flat=True)),
            {self.category.pk, self.other_category.pk},
        )

    def test_category_linked_only_to_model_cannot_be_deleted(self):
        category = ProductCategory.objects.create(code="MODEL-ONLY", name="仅关联型号")
        product = ProductModel.objects.create(code="MODEL-CAT", name="保留型号", category=category)
        response = self.client.post(
            f"{self.base_url}/category/delete/", {"id": category.pk}, format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertTrue(ProductCategory.objects.filter(pk=category.pk).exists())
        self.assertTrue(ProductModel.objects.filter(pk=product.pk).exists())

    def test_unused_category_can_be_deleted(self):
        category = ProductCategory.objects.create(code="UNUSED", name="未关联品类")
        response = self.client.post(
            f"{self.base_url}/category/delete/", {"id": category.pk}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(ProductCategory.objects.filter(pk=category.pk).exists())

    def test_bom_options_are_category_scoped_and_nested(self):
        response = self.client.get(
            f"{self.base_url}/category/{self.category.id}/bom-options/"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["code"] for item in response.data["materials"]],
            ["A001", "B001"],
        )
        first_spec = response.data["materials"][0]["specs"][0]
        self.assertEqual(first_spec["id"], self.spec_a.id)
        self.assertEqual(first_spec["suppliers"][0]["id"], self.supplier_a.id)

    def test_bom_options_and_writes_require_active_supplier_relation_in_current_root(self):
        foreign_factory = Organization.objects.create(
            code="BOM-OTHER-ROOT", name="另一厂家", organization_type=self.factory.organization_type,
        )
        foreign_supplier = Organization.objects.create(
            code="BOM-OTHER-SUPPLIER", name="另一链路供应商", organization_type=self.supplier_a.organization_type,
        )
        OrganizationRelation.objects.create(
            source_organization=foreign_factory, target_organization=foreign_supplier, relation_type="SUPPLY",
        )
        MaterialSpecSupplier.objects.create(material_spec=self.spec_a, supplier=foreign_supplier)
        MaterialSpecSupplier.objects.create(material_spec=self.spec_a, supplier=self.supplier_b)
        OrganizationRelation.objects.filter(target_organization=self.supplier_b).update(is_active=False)

        options = self.client.get(f"{self.base_url}/category/{self.category.pk}/bom-options/")
        self.assertEqual(options.status_code, 200)
        suppliers = options.data["materials"][0]["specs"][0]["suppliers"]
        self.assertEqual([row["id"] for row in suppliers], [self.supplier_a.pk])

        payload = self.valid_payload()
        created = self.client.post(f"{self.base_url}/model/add/", payload, format="json")
        self.assertEqual(created.status_code, 201)
        product = ProductModel.objects.get(code=payload["code"])
        payload["code"] = "JL-OTHER"
        for supplier in (foreign_supplier, self.supplier_b):
            with self.subTest(supplier=supplier.pk):
                payload["bom_items"][0]["supplier_id"] = supplier.pk
                rejected = self.client.post(f"{self.base_url}/model/add/", payload, format="json")
                self.assertEqual(rejected.status_code, 400, rejected.data)
                self.assertIn("当前厂家链路", rejected.data["error"])
                rejected = self.client.put(
                    f"{self.base_url}/model/{product.pk}/bom/v1/", payload, format="json",
                )
                self.assertEqual(rejected.status_code, 400, rejected.data)
                self.assertIn("当前厂家链路", rejected.data["error"])
        self.assertEqual(BOMItem.objects.get(bom__product_model=product).supplier_id, self.supplier_a.pk)

    def test_create_model_and_v1_bom_is_atomic_and_brandless(self):
        response = self.client.post(
            f"{self.base_url}/model/add/",
            self.valid_payload(),
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        product = ProductModel.objects.get(code="JL-5000")
        self.assertNotIn("brand", {field.name for field in product._meta.get_fields()})
        bom = BOM.objects.get(product_model=product, version="v1")
        item = BOMItem.objects.get(bom=bom)
        self.assertEqual(item.material_id, self.material_a.id)
        self.assertEqual(item.material_spec_id, self.spec_a.id)
        self.assertEqual(item.supplier_id, self.supplier_a.id)
        self.assertEqual(product.documents.count(), 3)

    def test_create_model_accepts_docx_pdf_txt_and_persists_original_files(self):
        payload = self.valid_payload()
        payload["documents"] = {
            "user_manual": "",
            "daily_maintenance": "",
            "troubleshooting_guide": "",
        }
        response = self.client.post(
            f"{self.base_url}/model/add/",
            {
                "code": payload["code"],
                "name": payload["name"],
                "category_id": payload["category_id"],
                "description": payload["description"],
                "bom_items": json.dumps(payload["bom_items"]),
                "documents": json.dumps(payload["documents"]),
                "document_user_manual": SimpleUploadedFile(
                    "使用说明书.docx",
                    self.valid_docx_bytes(),
                    content_type=(
                        "application/vnd.openxmlformats-officedocument."
                        "wordprocessingml.document"
                    ),
                ),
                "document_daily_maintenance": SimpleUploadedFile(
                    "日常维护.pdf",
                    b"%PDF-1.4\n%%EOF",
                    content_type="application/pdf",
                ),
                "document_troubleshooting_guide": SimpleUploadedFile(
                    "故障排除.txt",
                    "先检查电源。".encode("utf-8"),
                    content_type="text/plain",
                ),
            },
            format="multipart",
        )

        self.assertEqual(response.status_code, 201, response.data)
        product = ProductModel.objects.get(code="JL-5000")
        manual = product.documents.get(
            document_type=ProductDocument.DocumentType.USER_MANUAL
        )
        maintenance = product.documents.get(
            document_type=ProductDocument.DocumentType.DAILY_MAINTENANCE
        )
        guide = product.documents.get(
            document_type=ProductDocument.DocumentType.TROUBLESHOOTING_GUIDE
        )
        self.assertEqual(manual.file_type, ProductDocument.FileType.DOCX)
        self.assertEqual(maintenance.file_type, ProductDocument.FileType.PDF)
        self.assertEqual(guide.file_type, ProductDocument.FileType.TEXT)
        self.assertEqual(guide.content, "先检查电源。")
        self.assertTrue(manual.document_file.name)

    def test_missing_required_document_does_not_leave_product_or_bom(self):
        payload = self.valid_payload()
        payload["documents"]["daily_maintenance"] = ""

        response = self.client.post(
            f"{self.base_url}/model/add/",
            payload,
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(ProductModel.objects.filter(code="JL-5000").exists())
        self.assertEqual(BOM.objects.count(), 0)

    def test_document_edit_increments_only_changed_document_version(self):
        create_response = self.client.post(
            f"{self.base_url}/model/add/",
            self.valid_payload(),
            format="json",
        )
        product_id = create_response.data["id"]
        manual = ProductDocument.objects.get(
            product_model_id=product_id,
            document_type=ProductDocument.DocumentType.USER_MANUAL,
        )

        documents = self.valid_payload()["documents"]
        documents["user_manual"] = "新版使用说明书"
        response = self.client.put(
            f"{self.base_url}/model/{product_id}/documents/",
            {"documents": documents},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        manual.refresh_from_db()
        self.assertEqual(manual.version, 2)
        self.assertEqual(manual.content, "新版使用说明书")
        self.assertEqual(
            ProductDocument.objects.get(
                product_model_id=product_id,
                document_type=ProductDocument.DocumentType.DAILY_MAINTENANCE,
            ).version,
            1,
        )

    def test_invalid_supplier_link_does_not_leave_product(self):
        payload = self.valid_payload()
        payload["bom_items"][0]["supplier_id"] = self.supplier_b.id

        response = self.client.post(
            f"{self.base_url}/model/add/",
            payload,
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(ProductModel.objects.filter(code="JL-5000").exists())
        self.assertEqual(BOM.objects.count(), 0)

    def test_material_from_another_category_is_rejected(self):
        payload = self.valid_payload()
        payload["bom_items"][0].update(
            material_id=self.other_material.id,
            material_spec_id=self.other_spec.id,
            supplier_id=self.supplier_a.id,
        )

        response = self.client.post(
            f"{self.base_url}/model/add/",
            payload,
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("不属于当前产品品类", response.data["error"])
        self.assertFalse(ProductModel.objects.filter(code="JL-5000").exists())

    def test_edit_and_delete_v1_bom_keeps_product(self):
        create_response = self.client.post(
            f"{self.base_url}/model/add/",
            self.valid_payload(),
            format="json",
        )
        product_id = create_response.data["id"]

        save_response = self.client.put(
            f"{self.base_url}/model/{product_id}/bom/v1/",
            {
                "bom_items": [
                    {
                        "material_id": self.material_b.id,
                        "material_spec_id": self.spec_b.id,
                        "supplier_id": self.supplier_b.id,
                        "remark": "改为油箱",
                    }
                ]
            },
            format="json",
        )
        self.assertEqual(save_response.status_code, 200)
        bom = BOM.objects.get(product_model_id=product_id, version="v1")
        self.assertEqual(list(bom.items.values_list("material_id", flat=True)), [self.material_b.id])

        delete_response = self.client.delete(
            f"{self.base_url}/model/{product_id}/bom/v1/"
        )
        self.assertEqual(delete_response.status_code, 200)
        self.assertTrue(ProductModel.objects.filter(id=product_id).exists())
        self.assertFalse(BOM.objects.filter(product_model_id=product_id, version="v1").exists())

    def test_v1_bom_used_in_production_cannot_be_rewritten(self):
        payload = self.valid_payload()
        created = self.client.post(f"{self.base_url}/model/add/", payload, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        bom = BOM.objects.get(pk=created.data["bom_id"])
        batch = ProductionBatch.objects.create(
            batch_no="PRODUCT-BOM-LOCK", product_model=bom.product_model, bom=bom,
            manufacturer=self.factory, production_date=timezone.localdate(), quantity=1,
        )
        original_item = bom.items.get()
        url = f"{self.base_url}/model/{bom.product_model_id}/bom/v1/"
        same = self.client.put(url, {"bom_items": payload["bom_items"]}, format="json")
        self.assertEqual(same.status_code, 200, same.data)
        self.assertEqual(bom.items.get().pk, original_item.pk)

        payload["bom_items"][0]["remark"] = "改写历史批次"
        updated = self.client.put(url, {"bom_items": payload["bom_items"]}, format="json")
        self.assertEqual(updated.status_code, 400, updated.data)
        self.assertIn("不能修改原版本", updated.data["error"])
        self.assertEqual(batch.bom.items.get().pk, original_item.pk)
        self.assertEqual(batch.bom.items.get().remark, original_item.remark)

    def test_v1_bom_frozen_for_an_order_cannot_be_rewritten(self):
        payload = self.valid_payload()
        created = self.client.post(f"{self.base_url}/model/add/", payload, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        bom = BOM.objects.get(pk=created.data["bom_id"])
        merchant = Organization.objects.create(
            code="BOM-TEST-MERCHANT", name="BOM测试商家",
            organization_type=OrganizationType.objects.get(code="MERCHANT"),
        )
        relation = OrganizationRelation.objects.create(
            source_organization=self.factory, target_organization=merchant, relation_type="DISTRIBUTION",
        )
        brand = Brand.objects.create(code="BOM-TEST-BRAND", name="BOM测试品牌")
        channel = MerchantChannelAssignment.objects.create(
            relation=relation, brand=brand, region="重庆", assigned_by=self.operator,
        )
        order = OrderPlan.objects.create(
            plan_code="BOM-TEST-PLAN", client_request_id="bom-test-order",
            factory_organization=self.factory, merchant_organization=merchant,
            merchant_relation=relation, channel_assignment=channel, brand=brand,
            region_snapshot="重庆", created_by=self.operator, total_quantity=1,
        )
        item = OrderPlanItem.objects.create(
            plan=order, category=self.category, product_model=bom.product_model, quantity=1,
            production_quantity=1, order_batch_code="BOM-TEST-ORDER-BATCH", lifecycle_code="BOM-TEST-LIFE",
        )
        selection = OrderPlanSalesBOMSelection.objects.create(
            order_plan_item=item, source_bom=bom, frozen_bom=bom, selected_by=self.operator,
        )
        payload["bom_items"][0]["remark"] = "改写冻结清单"
        url = f"{self.base_url}/model/{bom.product_model_id}/bom/v1/"
        for stage in ("frozen", "confirmed"):
            with self.subTest(stage=stage):
                if stage == "confirmed":
                    selection.delete()
                    OrderPlanItemBOMConfirmation.objects.create(
                        order_plan_item=item, source_bom=bom, confirmed_bom=bom,
                        confirmed_by=self.operator,
                    )
                response = self.client.put(url, {"bom_items": payload["bom_items"]}, format="json")
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn("不能修改原版本", response.data["error"])
                self.assertEqual(bom.items.get().remark, "核心控制器")

    def test_deleted_v1_bom_can_be_created_again(self):
        create_response = self.client.post(
            f"{self.base_url}/model/add/",
            self.valid_payload(),
            format="json",
        )
        product_id = create_response.data["id"]
        self.client.delete(f"{self.base_url}/model/{product_id}/bom/v1/")

        response = self.client.put(
            f"{self.base_url}/model/{product_id}/bom/v1/",
            {
                "bom_items": [
                    {
                        "material_id": self.material_a.id,
                        "material_spec_id": self.spec_a.id,
                        "supplier_id": self.supplier_a.id,
                        "remark": "重新创建",
                    }
                ]
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["created"])
        self.assertEqual(BOM.objects.filter(product_model_id=product_id, version="v1").count(), 1)

    def test_model_list_puts_newest_product_first(self):
        older = ProductModel.objects.create(
            code="MODEL-OLD",
            name="旧型号",
            category=self.category,
            description="旧型号规格",
        )
        newer = ProductModel.objects.create(
            code="MODEL-NEW",
            name="新型号",
            category=self.category,
            description="新型号规格",
        )

        response = self.client.get(f"{self.base_url}/models/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["id"] for item in response.data[:2]],
            [newer.id, older.id],
        )
