import json
import zipfile
from pathlib import Path

from django.db import IntegrityError, transaction
from django.db.models import Count, Exists, OuterRef, Prefetch, Q
from django.db.models.deletion import ProtectedError
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from account.services.auth_service import (
    ApiAuthenticationError,
    authenticate_request,
    get_active_identity,
)
from product.models import (
    BOM,
    BOMItem,
    Brand,
    ProductCategory,
    ProductDocument,
    ProductModel,
)
from supply_chain.models import Material, MaterialSpec, MaterialSpecSupplier
from supply_chain.services.supply_scope import active_supplier_ids


V1_VERSION = "v1"
MAX_DOCUMENT_FILE_SIZE = 20 * 1024 * 1024
DOCUMENT_FILE_CONFIG = {
    ".txt": (ProductDocument.FileType.TEXT, "text/plain"),
    ".md": (ProductDocument.FileType.MARKDOWN, "text/markdown"),
    ".docx": (
        ProductDocument.FileType.DOCX,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    ".pdf": (ProductDocument.FileType.PDF, "application/pdf"),
}

REQUIRED_DOCUMENTS = (
    (
        ProductDocument.DocumentType.USER_MANUAL,
        "使用说明书",
        "user_manual",
    ),
    (
        ProductDocument.DocumentType.DAILY_MAINTENANCE,
        "日常维护",
        "daily_maintenance",
    ),
    (
        ProductDocument.DocumentType.TROUBLESHOOTING_GUIDE,
        "自助故障排除指南",
        "troubleshooting_guide",
    ),
)


def error_response(message, http_status=status.HTTP_400_BAD_REQUEST):
    return Response(
        {"status": "error", "error": message},
        status=http_status,
    )


def request_user_or_none(request):
    user = getattr(request, "user", None)
    if user and user.is_authenticated:
        return user
    return None


def _bom_factory_id(request):
    principal = getattr(request, "web_portal_principal", {})
    if principal.get("kind") == "factory":
        return principal["organization_id"]
    if (request.headers.get("Authorization") or "").lower().startswith("bearer "):
        try:
            identity = get_active_identity(authenticate_request(request))
        except ApiAuthenticationError:
            return None
        root = identity.organization.get_factory_root() if identity else None
        if root:
            return root.pk
    return None


def serialize_bom_item(item):
    spec = item.material_spec
    supplier = item.supplier
    return {
        "id": item.id,
        "material_id": item.material_id,
        "material_code": item.material.code,
        "material_name": item.material.name,
        "material_spec_id": item.material_spec_id,
        "material_spec_code": spec.code if spec else "",
        "material_spec_name": spec.name if spec else "",
        "supplier_id": item.supplier_id,
        "supplier_name": supplier.name if supplier else "",
        "quantity": item.quantity,
        "remark": item.remark,
    }


def serialize_document(document, include_content=False):
    data = {
        "id": document.id,
        "document_type": document.document_type,
        "title": document.title,
        "version": document.version,
        "is_active": document.is_active,
        "has_file": bool(document.document_file),
        "original_filename": document.original_filename,
        "file_type": document.file_type,
        "mime_type": document.mime_type,
        "file_size": document.file_size,
        "file_url": document.document_file.url if document.document_file else "",
        "updated_at": document.updated_at.isoformat(),
    }
    if include_content:
        data["content"] = document.content
    return data


def parse_json_value(raw_value):
    if not isinstance(raw_value, str):
        return raw_value
    try:
        return json.loads(raw_value)
    except (TypeError, ValueError):
        return None


def validate_document_upload(uploaded_file, title):
    if uploaded_file.size > MAX_DOCUMENT_FILE_SIZE:
        return None, f"“{title}”文件不能超过20MB"

    original_filename = Path(uploaded_file.name).name
    extension = Path(original_filename).suffix.lower()
    file_config = DOCUMENT_FILE_CONFIG.get(extension)
    if not file_config:
        return None, f"“{title}”只支持 .docx、.pdf、.txt、.md 文件"

    file_type, mime_type = file_config
    extracted_content = ""
    try:
        if extension in (".txt", ".md"):
            extracted_content = uploaded_file.read().decode("utf-8-sig").strip()
            if not extracted_content:
                return None, f"“{title}”文本文件不能为空"
        elif extension == ".pdf":
            if uploaded_file.read(5) != b"%PDF-":
                return None, f"“{title}”不是有效的PDF文件"
        else:
            with zipfile.ZipFile(uploaded_file) as archive:
                names = set(archive.namelist())
                if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                    return None, f"“{title}”不是有效的DOCX文件"
    except (UnicodeDecodeError, OSError, zipfile.BadZipFile):
        return None, f"“{title}”文件损坏或编码不正确"
    finally:
        uploaded_file.seek(0)

    return {
        "uploaded_file": uploaded_file,
        "original_filename": original_filename,
        "file_type": file_type,
        "mime_type": mime_type,
        "file_size": uploaded_file.size,
        "extracted_content": extracted_content,
    }, None


def normalize_required_documents(raw_documents, uploaded_files=None, existing=None):
    """校验三篇固定资料；正文或受支持的原始文件至少存在一种。"""
    raw_documents = parse_json_value(raw_documents)
    if not isinstance(raw_documents, dict):
        return None, "请完整填写使用说明书、日常维护和自助故障排除指南"

    uploaded_files = uploaded_files or {}
    existing = existing or {}
    documents = []
    for document_type, title, field_name in REQUIRED_DOCUMENTS:
        content = str(raw_documents.get(field_name) or "").strip()
        uploaded_file = uploaded_files.get(f"document_{field_name}")
        upload_data = None
        if uploaded_file:
            upload_data, upload_error = validate_document_upload(uploaded_file, title)
            if upload_error:
                return None, upload_error
            if upload_data["extracted_content"]:
                content = upload_data["extracted_content"]

        existing_document = existing.get(document_type)
        has_existing_file = bool(
            existing_document and existing_document.document_file
        )
        if not content and not upload_data and not has_existing_file:
            return None, f"“{title}”必须填写正文或选择资料文件"

        item = {
            "document_type": document_type,
            "title": title,
            "content": content,
            "upload_data": upload_data,
        }
        documents.append(item)
    return documents, None


def validate_bom_items(category, raw_items, *, factory_id):
    """
    校验一条完整链路：
    产品品类 -> 物料 -> 物料规格 -> 可供应该规格的组织。
    """
    raw_items = parse_json_value(raw_items)
    if not isinstance(raw_items, list):
        return None, "BOM清单格式不正确"

    if any(not isinstance(row, dict) for row in raw_items):
        return None, "BOM清单行格式不正确"

    active_rows = [row for row in raw_items if not row.get("is_deleted", False)]
    if not active_rows:
        return None, "v1版BOM清单至少需要保留一项物料"

    normalized_rows = []
    material_ids = []
    spec_ids = []
    supplier_ids = []

    for index, row in enumerate(active_rows, start=1):
        try:
            material_id = int(row.get("material_id"))
            spec_id = int(row.get("material_spec_id"))
            supplier_id = int(row.get("supplier_id"))
        except (TypeError, ValueError):
            return None, f"BOM第{index}行必须选择物料规格和供应商"

        if material_id <= 0 or spec_id <= 0 or supplier_id <= 0:
            return None, f"BOM第{index}行必须选择物料规格和供应商"

        normalized_rows.append(
            {
                "material_id": material_id,
                "material_spec_id": spec_id,
                "supplier_id": supplier_id,
                "remark": str(row.get("remark") or "").strip(),
            }
        )
        material_ids.append(material_id)
        spec_ids.append(spec_id)
        supplier_ids.append(supplier_id)

    if len(set(material_ids)) != len(material_ids):
        return None, "同一种物料不能在v1版BOM清单中重复出现"

    materials = {
        material.id: material
        for material in Material.objects.filter(id__in=material_ids)
    }
    specs = {
        spec.id: spec
        for spec in MaterialSpec.objects.filter(id__in=spec_ids)
    }
    valid_supplier_links = set(
        MaterialSpecSupplier.objects.filter(
            material_spec_id__in=spec_ids,
            supplier_id__in=supplier_ids,
        ).filter(
            supplier_id__in=active_supplier_ids(factory_id),
        ).values_list("material_spec_id", "supplier_id")
    )

    for index, row in enumerate(normalized_rows, start=1):
        material = materials.get(row["material_id"])
        if not material:
            return None, f"BOM第{index}行的物料不存在"
        if not material.product_categories.filter(pk=category.pk).exists():
            return None, f"物料“{material.name}”不属于当前产品品类"

        spec = specs.get(row["material_spec_id"])
        if not spec or spec.material_id != material.id:
            return None, f"BOM第{index}行的物料规格与物料不匹配"

        if (spec.id, row["supplier_id"]) not in valid_supplier_links:
            return None, f"BOM第{index}行的供应商在当前厂家链路内没有所选物料规格的有效供应关系"

    return normalized_rows, None


def replace_bom_items(bom, rows):
    bom.items.all().delete()
    BOMItem.objects.bulk_create(
        [
            BOMItem(
                bom=bom,
                material_id=row["material_id"],
                material_spec_id=row["material_spec_id"],
                supplier_id=row["supplier_id"],
                quantity=1,
                remark=row["remark"],
            )
            for row in rows
        ]
    )


def assign_document_upload(document, upload_data):
    if not upload_data:
        return
    document.document_file = upload_data["uploaded_file"]
    document.original_filename = upload_data["original_filename"]
    document.file_type = upload_data["file_type"]
    document.mime_type = upload_data["mime_type"]
    document.file_size = upload_data["file_size"]


def create_product_document(product, item):
    document = ProductDocument(
        product_model=product,
        document_type=item["document_type"],
        title=item["title"],
        content=item["content"],
        version=1,
        is_active=True,
    )
    assign_document_upload(document, item.get("upload_data"))
    document.save()
    return document


class CategoryListAPI(APIView):
    def get(self, request):
        return Response(
            [
                {"id": category.id, "name": category.name, "code": category.code}
                for category in ProductCategory.objects.all()
            ]
        )


class BrandListAPI(APIView):
    def get(self, request):
        return Response(
            [
                {"id": brand.id, "name": brand.name, "code": brand.code}
                for brand in Brand.objects.all()
            ]
        )


class ProductModelListAPI(APIView):
    def get(self, request):
        category_id = request.GET.get("category_id")
        v1_boms = BOM.objects.filter(
            product_model_id=OuterRef("pk"),
            version=V1_VERSION,
        )
        products = ProductModel.objects.select_related("category").annotate(
            has_v1_bom=Exists(v1_boms),
            active_document_count=Count(
                "documents",
                filter=Q(documents__is_active=True),
                distinct=True,
            ),
        )
        if category_id:
            products = products.filter(category_id=category_id)
        products = products.order_by("-created_at", "-id")

        return Response(
            [
                {
                    "id": product.id,
                    "name": product.name,
                    "code": product.code,
                    "description": product.description,
                    "category_id": product.category_id,
                    "category": product.category.name,
                    "has_v1_bom": product.has_v1_bom,
                    "documents_complete": product.active_document_count == 3,
                    "document_count": product.active_document_count,
                }
                for product in products
            ]
        )


class CategoryCreateAPI(APIView):
    def post(self, request):
        name = str(request.data.get("name") or "").strip()
        code = str(request.data.get("code") or "").strip() or name.upper()
        if not name:
            return error_response("品类名称不能为空")
        if ProductCategory.objects.filter(code=code).exists():
            return error_response("品类编码已存在")
        if ProductCategory.objects.filter(name=name).exists():
            return error_response("品类名称已存在")

        category = ProductCategory.objects.create(name=name, code=code)
        return Response(
            {"status": "ok", "id": category.id, "name": category.name},
            status=status.HTTP_201_CREATED,
        )


class BrandCreateAPI(APIView):
    def post(self, request):
        name = str(request.data.get("name") or "").strip()
        code = str(request.data.get("code") or "").strip() or name.upper()
        if not name:
            return error_response("品牌名称不能为空")
        if Brand.objects.filter(code=code).exists():
            return error_response("品牌编码已存在")
        if Brand.objects.filter(name=name).exists():
            return error_response("品牌名称已存在")

        brand = Brand.objects.create(name=name, code=code)
        return Response(
            {"status": "ok", "id": brand.id, "name": brand.name},
            status=status.HTTP_201_CREATED,
        )


class ProductModelCreateAPI(APIView):
    """原子创建不绑定品牌的产品型号及其v1版BOM。"""

    def post(self, request):
        code = str(request.data.get("code") or "").strip()
        name = str(request.data.get("name") or "").strip()
        description = str(request.data.get("description") or "").strip()
        category_id = request.data.get("category_id")

        if not code or not name:
            return error_response("型号编码和型号名称不能为空")
        if not description:
            return error_response("规格描述不能为空")
        try:
            category = ProductCategory.objects.get(id=int(category_id))
        except (TypeError, ValueError, ProductCategory.DoesNotExist):
            return error_response("请选择有效的产品品类")
        if ProductModel.objects.filter(code=code).exists():
            return error_response("型号编码已存在")

        rows, validation_error = validate_bom_items(
            category,
            request.data.get("bom_items"),
            factory_id=_bom_factory_id(request),
        )
        if validation_error:
            return error_response(validation_error)

        documents, document_error = normalize_required_documents(
            request.data.get("documents"),
            uploaded_files=request.FILES,
        )
        if document_error:
            return error_response(document_error)

        try:
            with transaction.atomic():
                product = ProductModel.objects.create(
                    code=code,
                    name=name,
                    category=category,
                    description=description,
                )
                bom = BOM.objects.create(
                    product_model=product,
                    version_no=1,
                    version=V1_VERSION,
                    is_active=True,
                    created_by=request_user_or_none(request),
                )
                replace_bom_items(bom, rows)
                for document in documents:
                    create_product_document(product, document)
        except IntegrityError:
            return error_response("型号编码已存在，或BOM中存在重复物料")

        return Response(
            {
                "status": "ok",
                "id": product.id,
                "name": product.name,
                "bom_id": bom.id,
                "bom_version": bom.version,
                "item_count": len(rows),
                "document_count": len(documents),
            },
            status=status.HTTP_201_CREATED,
        )


class CategoryBOMOptionsAPI(APIView):
    """返回某个品类下按物料编码排序的规格及可供应组织。"""

    def get(self, request, category_id):
        try:
            category = ProductCategory.objects.get(id=category_id)
        except ProductCategory.DoesNotExist:
            return error_response("产品品类不存在", status.HTTP_404_NOT_FOUND)

        supplier_links = MaterialSpecSupplier.objects.filter(
            supplier_id__in=active_supplier_ids(_bom_factory_id(request)),
        ).select_related("supplier").order_by(
            "supplier__name", "supplier_id"
        )
        specs = MaterialSpec.objects.order_by("code").prefetch_related(
            Prefetch("suppliers", queryset=supplier_links)
        )
        materials = Material.objects.filter(product_categories=category).distinct().order_by(
            "code", "id"
        ).prefetch_related(Prefetch("specs", queryset=specs))

        material_data = []
        for material in materials:
            spec_data = []
            for spec in material.specs.all():
                spec_data.append(
                    {
                        "id": spec.id,
                        "code": spec.code,
                        "name": spec.name,
                        "suppliers": [
                            {
                                "id": link.supplier_id,
                                "code": link.supplier.code,
                                "name": link.supplier.name,
                            }
                            for link in spec.suppliers.all()
                        ],
                    }
                )
            material_data.append(
                {
                    "id": material.id,
                    "code": material.code,
                    "name": material.name,
                    "specs": spec_data,
                }
            )

        return Response(
            {
                "status": "ok",
                "category": {
                    "id": category.id,
                    "code": category.code,
                    "name": category.name,
                },
                "materials": material_data,
            }
        )


class ProductModelV1BOMAPI(APIView):
    def get_product(self, model_id):
        try:
            return ProductModel.objects.select_related("category").get(id=model_id)
        except ProductModel.DoesNotExist:
            return None

    def get(self, request, model_id):
        product = self.get_product(model_id)
        if not product:
            return error_response("商品型号不存在", status.HTTP_404_NOT_FOUND)

        bom = (
            BOM.objects.filter(product_model=product, version=V1_VERSION)
            .prefetch_related(
                Prefetch(
                    "items",
                    queryset=BOMItem.objects.select_related(
                        "material", "material_spec", "supplier"
                    ).order_by("material__code", "id"),
                )
            )
            .first()
        )
        return Response(
            {
                "status": "ok",
                "model": {
                    "id": product.id,
                    "code": product.code,
                    "name": product.name,
                    "description": product.description,
                    "category_id": product.category_id,
                    "category": product.category.name,
                },
                "bom": (
                    {
                        "id": bom.id,
                        "version": bom.version,
                        "version_no": bom.version_no,
                        "is_active": bom.is_active,
                        "created_at": bom.created_at.isoformat(),
                        "items": [serialize_bom_item(item) for item in bom.items.all()],
                    }
                    if bom
                    else None
                ),
                "documents": [
                    serialize_document(document, include_content=True)
                    for document in product.documents.filter(is_active=True)
                    .order_by("document_type", "id")
                ],
            }
        )

    def put(self, request, model_id):
        product = self.get_product(model_id)
        if not product:
            return error_response("商品型号不存在", status.HTTP_404_NOT_FOUND)

        rows, validation_error = validate_bom_items(
            product.category,
            request.data.get("bom_items"),
            factory_id=_bom_factory_id(request),
        )
        if validation_error:
            return error_response(validation_error)

        try:
            with transaction.atomic():
                locked_product = ProductModel.objects.select_for_update().get(id=product.id)
                bom, created = BOM.objects.select_for_update().get_or_create(
                    product_model=locked_product,
                    version=V1_VERSION,
                    defaults={
                        "version_no": 1,
                        "is_active": True,
                        "created_by": request_user_or_none(request),
                    },
                )
                in_use = not created and (
                    bom.production_batches.exists()
                    or bom.sales_order_frozen_selections.exists()
                    or bom.order_plan_confirmations.exists()
                )
                if in_use:
                    fields = ("material_id", "material_spec_id", "supplier_id", "remark")
                    current_rows = list(bom.items.values(*fields).order_by("material_id"))
                    if current_rows != sorted(rows, key=lambda row: row["material_id"]):
                        return error_response("该v1版BOM已被订单或生产批次使用，不能修改原版本")
                else:
                    if not created:
                        bom.version_no = 1
                        bom.is_active = True
                        bom.save(update_fields=["version_no", "is_active"])
                    replace_bom_items(bom, rows)
        except ProtectedError:
            return error_response("该v1版BOM明细已被业务数据使用，不能修改")
        except IntegrityError:
            return error_response("保存失败：BOM中存在重复物料")

        return Response(
            {
                "status": "ok",
                "message": "v1版BOM清单已保存",
                "bom_id": bom.id,
                "created": created,
                "item_count": len(rows),
            }
        )

    def delete(self, request, model_id):
        product = self.get_product(model_id)
        if not product:
            return error_response("商品型号不存在", status.HTTP_404_NOT_FOUND)
        try:
            bom = BOM.objects.get(product_model=product, version=V1_VERSION)
        except BOM.DoesNotExist:
            return error_response("该商品型号没有v1版BOM清单", status.HTTP_404_NOT_FOUND)

        try:
            bom.delete()
        except ProtectedError:
            return error_response("该v1版BOM已被业务数据使用，不能删除")
        return Response({"status": "ok", "message": "v1版BOM清单已删除，商品型号已保留"})


class ProductModelDocumentsAPI(APIView):
    """查看或更新型号的三篇固定用户资料。"""

    @staticmethod
    def get_product(model_id):
        return ProductModel.objects.filter(id=model_id).first()

    def get(self, request, model_id):
        product = self.get_product(model_id)
        if not product:
            return error_response("商品型号不存在", status.HTTP_404_NOT_FOUND)
        documents = product.documents.filter(is_active=True).order_by(
            "document_type", "id"
        )
        return Response(
            {
                "status": "ok",
                "model_id": product.id,
                "documents": [
                    serialize_document(document, include_content=True)
                    for document in documents
                ],
            }
        )

    def put(self, request, model_id):
        product = self.get_product(model_id)
        if not product:
            return error_response("商品型号不存在", status.HTTP_404_NOT_FOUND)

        existing_documents = {
            document.document_type: document
            for document in product.documents.filter(is_active=True)
        }
        documents, document_error = normalize_required_documents(
            request.data.get("documents"),
            uploaded_files=request.FILES,
            existing=existing_documents,
        )
        if document_error:
            return error_response(document_error)

        with transaction.atomic():
            for item in documents:
                document = ProductDocument.objects.select_for_update().filter(
                    product_model=product,
                    document_type=item["document_type"],
                ).first()
                if not document:
                    create_product_document(product, item)
                    continue

                upload_data = item.get("upload_data")
                changed = document.content != item["content"] or bool(upload_data)
                old_file_name = document.document_file.name if upload_data else ""
                old_storage = document.document_file.storage if old_file_name else None
                document.title = item["title"]
                document.content = item["content"]
                document.is_active = True
                assign_document_upload(document, upload_data)
                if changed:
                    document.version += 1
                document.save()
                if old_file_name and old_file_name != document.document_file.name:
                    transaction.on_commit(
                        lambda storage=old_storage, name=old_file_name: storage.delete(name)
                    )

        return Response(
            {
                "status": "ok",
                "message": "型号必读资料已保存",
                "documents": [
                    serialize_document(document, include_content=True)
                    for document in product.documents.filter(is_active=True).order_by(
                        "document_type", "id"
                    )
                ],
            }
        )


class CategoryDeleteAPI(APIView):
    def post(self, request):
        try:
            with transaction.atomic():
                category = ProductCategory.objects.select_for_update().get(id=request.data.get("id"))
                if category.materials.exists():
                    return error_response("该品类已被物料使用，不能删除")
                category.delete()
        except (TypeError, ValueError, ProductCategory.DoesNotExist):
            return error_response("品类不存在", status.HTTP_404_NOT_FOUND)
        except ProtectedError:
            return error_response("该品类已被商品型号或物料使用，不能删除")
        return Response({"status": "ok", "message": "删除成功"})


class BrandDeleteAPI(APIView):
    def post(self, request):
        try:
            brand = Brand.objects.get(id=request.data.get("id"))
            brand.delete()
        except (TypeError, ValueError, Brand.DoesNotExist):
            return error_response("品牌不存在", status.HTTP_404_NOT_FOUND)
        except ProtectedError:
            return error_response("该品牌已经被其他数据使用，不能删除")
        return Response({"status": "ok", "message": "删除成功"})


class ProductModelDeleteAPI(APIView):
    def post(self, request):
        try:
            product = ProductModel.objects.get(id=request.data.get("id"))
            product.delete()
        except (TypeError, ValueError, ProductModel.DoesNotExist):
            return error_response("商品型号不存在", status.HTTP_404_NOT_FOUND)
        except ProtectedError:
            return error_response("该商品型号已经被使用，不能删除")
        return Response({"status": "ok", "message": "删除成功"})
