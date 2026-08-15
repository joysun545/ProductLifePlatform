import io
import json
import tempfile
import zipfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework.test import APITestCase

from account.models import Organization, OrganizationType
from product.models import (
    BOM,
    BOMItem,
    ProductCategory,
    ProductDocument,
    ProductModel,
)
from supply_chain.models import Material, MaterialSpec, MaterialSpecSupplier


class ProductV1BOMAPITests(APITestCase):
    base_url = "/pldp/api/product"

    def setUp(self):
        self._media_root = tempfile.TemporaryDirectory()
        self.addCleanup(self._media_root.cleanup)
        self._media_override = override_settings(MEDIA_ROOT=self._media_root.name)
        self._media_override.enable()
        self.addCleanup(self._media_override.disable)

        self.category = ProductCategory.objects.create(code="GEN", name="发电机")
        self.other_category = ProductCategory.objects.create(code="AIR", name="驻车空调")

        self.material_a = Material.objects.create(
            code="A001",
            name="控制器",
            product_category=self.category,
        )
        self.material_b = Material.objects.create(
            code="B001",
            name="油箱",
            product_category=self.category,
        )
        self.other_material = Material.objects.create(
            code="C001",
            name="压缩机",
            product_category=self.other_category,
        )
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

    def test_create_model_and_v1_bom_is_atomic_and_brandless(self):
        response = self.client.post(
            f"{self.base_url}/model/add/",
            self.valid_payload(),
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        product = ProductModel.objects.get(code="JL-5000")
        self.assertIsNone(product.brand_id)
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
