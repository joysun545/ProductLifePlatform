# apps/supply_chain/web/views.py

import json

from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.views.decorators.http import require_GET

from supply_chain.models import (
    Material,
    MaterialSpec,
    MaterialSpecSupplier,
)

from account.models import Organization
from product.models import ProductCategory
from supply_chain.services.supply_scope import active_supplier_ids, supplier_can_supply


MATERIAL_CODE_SEPARATOR = "-"


def _material_factory_id(request):
    principal = getattr(request, "web_portal_principal", {})
    if principal.get("kind") == "factory":
        return principal["organization_id"]
    return None


def build_material_code(category_code, input_code):
    """根据品类编码生成稳定且不会重复添加前缀的物料编码。"""
    prefix = str(category_code or "").strip().upper()
    code_part = str(input_code or "").strip().upper()

    if not prefix or not code_part:
        return ""

    full_prefix = f"{prefix}{MATERIAL_CODE_SEPARATOR}"
    if code_part.startswith(full_prefix):
        code_part = code_part[len(full_prefix):].strip()

    if not code_part:
        return ""

    return f"{full_prefix}{code_part}"




# =========================
# 页面
# =========================


def material_config(request):

    return render(
        request,
        "supply_chain/material_config.html"
    )





# =========================
# 获取物料
# =========================


def _material_category_names(material):
    return "、".join(category.name for category in material.product_categories.all())


def get_materials(request):


    materials = Material.objects.prefetch_related(
        "product_categories"
    ).order_by(
        "code"
    )


    category_id = request.GET.get(
        "category"
    )


    keyword = request.GET.get(
        "keyword"
    )


    if category_id:

        materials = materials.filter(
            product_categories__id=category_id
        )


    if keyword:

        materials = materials.filter(
            models.Q(
                code__icontains=keyword
            )
            |
            models.Q(
                name__icontains=keyword
            )
        )


    result=[]


    for material in materials:


        result.append({

            "id":
            material.id,


            "code":
            material.code,


            "name":
            material.name,


            "category":
            _material_category_names(material),


            "is_key_part":
            material.is_key_part,


            "description":
            material.description

        })


    return JsonResponse(
        result,
        safe=False
    )




# =========================
# 获取物料规格
# =========================


def get_material_specs(request):


    specs = MaterialSpec.objects.select_related("material").prefetch_related(
        "material__product_categories"
    )


    category_id = request.GET.get(
        "category"
    )


    material_id = request.GET.get(
        "material"
    )


    keyword = request.GET.get(
        "keyword"
    )


    if category_id:

        specs = specs.filter(
            material__product_categories__id=category_id
        )


    if material_id:

        specs = specs.filter(
            material_id=material_id
        )


    if keyword:

        specs = specs.filter(
            models.Q(
                code__icontains=keyword
            )
            |
            models.Q(
                name__icontains=keyword
            )
            |
            models.Q(
                material__name__icontains=keyword
            )
        )


    data = []


    for spec in specs:


        data.append({

            "id":
            spec.id,


            "code":
            spec.code,


            "name":
            spec.name,


            "material":
            spec.material.name,


            "material_id":
            spec.material.id,


            "category":
            _material_category_names(spec.material),


            "performance_description":
            spec.performance_description

        })


    return JsonResponse(
        data,
        safe=False
    )






# =========================
# 获取供应商
# =========================


def get_suppliers(request):

    suppliers = Organization.objects.filter(
        pk__in=active_supplier_ids(_material_factory_id(request)),
    ).values(
        "id",
        "name",
        "code"
    )


    return JsonResponse(
        list(suppliers),
        safe=False
    )



# =========================
# 根据产品分类获取供应商
# =========================

def get_suppliers_by_category(request):


    category_id = request.GET.get(
        "category"
    )


    if not category_id:


        return JsonResponse(
            [],
            safe=False
        )



    suppliers = Organization.objects.filter(
        pk__in=active_supplier_ids(_material_factory_id(request)),

        supplied_material_specs__material_spec__material__product_categories__id=category_id

    ).distinct().values(

        "id",
        "name",
        "code"

    )



    return JsonResponse(
        list(suppliers),
        safe=False
    )




# =========================
# 创建物料
# =========================

@require_POST
def add_material(request):
    try:
        data = json.loads(request.body)
    except ValueError:
        return JsonResponse({"success": False, "message": "物料数据格式错误"})
    if not isinstance(data, dict):
        return JsonResponse({"success": False, "message": "物料数据格式错误"})

    input_code = str(data.get("code") or "").strip()
    name = str(data.get("name") or "").strip()
    if not input_code or not name:
        return JsonResponse({"success": False, "message": "物料编码和名称不能为空"})

    category_id = data.get("product_category_id")
    if not category_id:
        return JsonResponse({"success": False, "message": "请选择所属产品分类"})

    extra_category_ids = data.get("product_category_ids", [])
    if not isinstance(extra_category_ids, list):
        return JsonResponse({"success": False, "message": "适用产品分类格式错误"})
    submitted_ids = [str(value) for value in (category_id, *extra_category_ids)]
    if any(
        not value.isascii() or not value.isdecimal() or len(value) > 19
        or not 0 < int(value) < 2**63
        for value in submitted_ids
    ):
        return JsonResponse({"success": False, "message": "所选产品分类不存在"})

    category_ids = {int(value) for value in submitted_ids}
    categories = ProductCategory.objects.in_bulk(category_ids)
    if categories.keys() != category_ids:
        return JsonResponse({"success": False, "message": "所选产品分类不存在"})

    code = build_material_code(categories[int(category_id)].code, input_code)
    if not code:
        return JsonResponse({"success": False, "message": "物料编码不能为空"})
    if len(code) > Material._meta.get_field("code").max_length:
        return JsonResponse({
            "success": False,
            "message": "品类编码与物料编码组合后不能超过64个字符",
        })
    if Material.objects.filter(code=code).exists():
        return JsonResponse({"success": False, "message": f"物料编码 {code} 已存在"})

    try:
        with transaction.atomic():
            material = Material.objects.create(
                code=code,
                name=name,
                is_key_part=data.get("is_key_part", False),
                description=data.get("description", ""),
            )
            material.product_categories.add(*categories.values())
    except IntegrityError:
        if Material.objects.filter(code=code).exists():
            return JsonResponse({"success": False, "message": f"物料编码 {code} 已存在"})
        return JsonResponse({"success": False, "message": "物料及适用产品分类未保存，请重新提交"})
    except (TypeError, ValueError, ValidationError):
        return JsonResponse({"success": False, "message": "物料数据格式错误"})

    return JsonResponse({
        "success": True,
        "id": material.id,
        "code": material.code,
        "message": "物料创建成功",
    })


# =========================
# 创建物料规格
# =========================


def add_material_spec_ajax(request):


    if request.method != "POST":

        return JsonResponse({

            "success":False,

            "error":"请求方式错误"

        })


    try:


        data = json.loads(
            request.body
        )


        spec = MaterialSpec.objects.create(


            material_id=data.get(
                "material_id"
            ),


            code=data.get(
                "code"
            ),


            name=data.get(
                "name"
            ),


            performance_description=data.get(
                "performance_description",
                ""
            ),


            parameters=data.get(
                "parameters",
                {}
            ),


            description=data.get(
                "description",
                ""
            )

        )


        return JsonResponse({

            "success":True,

            "id":spec.id,

            "message":"物料规格创建成功"

        })



    except IntegrityError:


        return JsonResponse({

            "success":False,

            "error":"规格编码已存在"

        })





# =========================
# 绑定供应商
# =========================


def add_material_spec_supplier_ajax(request):


    if request.method != "POST":

        return JsonResponse({

            "success":False,

            "error":"请求方式错误"

        })



    try:


        data = json.loads(
            request.body
        )

        supplier_id = str(data.get("supplier_id") or "")
        if (
            not supplier_id.isascii() or not supplier_id.isdecimal()
            or len(supplier_id) > 19 or not 0 < int(supplier_id) < 2**63
        ):
            return JsonResponse({"success": False, "error": "请选择有效的供应商"}, status=400)
        if not supplier_can_supply(_material_factory_id(request), supplier_id):
            return JsonResponse({
                "success": False, "error": "该供应商在当前厂家链路内没有有效供货关系",
            }, status=403)

        relation = MaterialSpecSupplier.objects.create(

            material_spec_id=data.get(
                "material_spec_id"
            ),


            supplier_id=supplier_id,


            warranty_months=data.get(
                "warranty_months",
                12
            ),


            reference_price=data.get(
                "price",
                data.get("reference_price")
            ),


            remark=data.get(
                "remark",
                ""
            )

        )


        return JsonResponse({

            "success":True,

            "id":relation.id,

            "message":"供应关系创建成功"

        })


    except IntegrityError as e:

        return JsonResponse({

            "success":False,

            "error":"该规格与供应商关系已存在",
            "detail":str(e)

        })


    except Exception as e:

        return JsonResponse({

            "success":False,

            "error":"服务器异常",
            "detail":str(e)

        })


# =====================================
# 删除物料
# =====================================

@require_POST
def delete_material(request):

    try:

        data = json.loads(
            request.body.decode("utf-8")
        )

        material_id = data.get("id")

        if not material_id:
            return JsonResponse({
                "success": False,
                "message": "缺少物料ID"
            })


        material = Material.objects.get(
            id=material_id
        )


        # 检查是否被BOM引用
        if hasattr(material, "bom_items") and material.bom_items.exists():

            return JsonResponse({
                "success": False,
                "message": "该物料已经被BOM使用，不能删除"
            })


        material.delete()


        return JsonResponse({

            "success": True,

            "message": "物料删除成功"

        })


    except Material.DoesNotExist:


        return JsonResponse({

            "success": False,

            "message": "物料不存在"

        })



    except Exception as e:


        return JsonResponse({

            "success": False,

            "message": str(e)

        })



# =====================================
# 删除物料规格
# =====================================

@require_POST
def delete_material_spec(request):


    try:


        data = json.loads(
            request.body.decode("utf-8")
        )

        spec_id = data.get("id")


        if not spec_id:

            return JsonResponse({

                "success":False,

                "message":"缺少规格ID"

            })


        spec = MaterialSpec.objects.get(
            id=spec_id
        )


        spec.delete()


        return JsonResponse({

            "success":True,

            "message":"物料规格删除成功"

        })


    except MaterialSpec.DoesNotExist:


        return JsonResponse({

            "success":False,

            "message":"物料规格不存在"

        })


    except Exception as e:


        return JsonResponse({

            "success":False,

            "message":str(e)

        })




# =====================================
# 删除物料规格供应关系
# =====================================

@require_POST
def delete_material_spec_supplier(request):


    try:


        data = json.loads(
            request.body.decode("utf-8")
        )

        relation_id = data.get("id")


        if not relation_id:

            return JsonResponse({

                "success":False,

                "message":"缺少供应关系ID"

            })


        relation = MaterialSpecSupplier.objects.get(
            id=relation_id,
            supplier_id__in=active_supplier_ids(_material_factory_id(request)),
        )


        relation.delete()


        return JsonResponse({

            "success":True,

            "message":"供应关系删除成功"

        })



    except MaterialSpecSupplier.DoesNotExist:


        return JsonResponse({

            "success":False,

            "message":"供应关系不存在"

        })


    except Exception as e:


        return JsonResponse({

            "success":False,

            "message":str(e)

        })



# =====================================
# 配套物料列表
# =====================================

@require_GET

def get_material_supplier_list(request):


    from django.db.models import Q

    supplier_ids = set(active_supplier_ids(_material_factory_id(request)))

    materials = Material.objects.prefetch_related(
        "specs__suppliers__supplier"
    )


    category_id = request.GET.get(
        "category"
    )


    supplier_id = request.GET.get(
        "supplier"
    )


    keyword = request.GET.get(
        "keyword"
    )


    if category_id:

        materials = materials.filter(
            product_categories__id=category_id
        )


    if supplier_id:

        materials = materials.filter(
            specs__suppliers__supplier_id=supplier_id,
            specs__suppliers__supplier_id__in=supplier_ids,
        )


    if keyword:

        materials = materials.filter(
            Q(name__icontains=keyword)
            |
            Q(specs__name__icontains=keyword)
            |
            Q(specs__code__icontains=keyword)
            |
            Q(specs__suppliers__supplier__name__icontains=keyword,
              specs__suppliers__supplier_id__in=supplier_ids)
        )


    materials = materials.distinct()


    result = []


    for material in materials:


        specs = []


        specs_queryset = material.specs.all()


        if keyword:

            specs_queryset = specs_queryset.filter(
                Q(name__icontains=keyword)
                |
                Q(code__icontains=keyword)
                |
                Q(suppliers__supplier__name__icontains=keyword,
                  suppliers__supplier_id__in=supplier_ids)
            ).distinct()



        for spec in specs_queryset:


            suppliers = []


            for relation in spec.suppliers.all():

                if relation.supplier_id not in supplier_ids:
                    continue

                if supplier_id and str(relation.supplier.id) != str(supplier_id):

                    continue


                suppliers.append({

                    "id":
                    relation.id,


                    "name":
                    relation.supplier.name,


                    "warranty_months":
                    relation.warranty_months,


                    "reference_price":
                    str(relation.reference_price)
                    if relation.reference_price is not None
                    else ""

                })


            if suppliers:


                specs.append({

                    "id":
                    spec.id,


                    "name":
                    spec.name,


                    "code":
                    spec.code,


                    "suppliers":
                    suppliers

                })


        if specs:


            result.append({

                "id":
                material.id,


                "name":
                material.name,


                "code":
                material.code,


                "specs":
                specs

            })


    return JsonResponse(
        result,
        safe=False
    )


def material_list(request):

    supplier_ids = set(active_supplier_ids(_material_factory_id(request)))

    materials = Material.objects.prefetch_related(
        "specs__suppliers__supplier"
    ).all()



    result = []


    for material in materials:


        specs = []


        for spec in material.specs.all():


            suppliers=[]


            for relation in spec.suppliers.all():

                if relation.supplier_id not in supplier_ids:
                    continue

                suppliers.append({

                    "id":
                    relation.id,

                    "name":
                    relation.supplier.name,

                    "warranty_months":
                    relation.warranty_months,

                    "reference_price":
                    str(relation.reference_price)
                    if relation.reference_price is not None
                    else ""

                })


            specs.append({

                "id":
                spec.id,

                "name":
                spec.name,

                "code":
                spec.code,

                "performance_description":
                spec.performance_description,

                "parameters":
                spec.parameters,

                "suppliers":
                suppliers

            })



        result.append({

            "id":
            material.id,

            "name":
            material.name,

            "code":
            material.code,

            "is_key_part":
            material.is_key_part,

            "description":
            material.description,

            "specs":
            specs

        })



    return JsonResponse(

        result,

        safe=False

    )


def get_product_categories(request):


    from product.models import ProductCategory


    categories = ProductCategory.objects.all()


    data = []


    for item in categories:

        data.append({

            "id": item.id,

            "name": item.name,

            "code": item.code

        })


    return JsonResponse(
        data,
        safe=False
    )


# =====================================
# 根据产品分类获取物料
# 用于规格筛选第二级下拉
# =====================================

def get_materials_by_category(request):


    category_id = request.GET.get(
        "category"
    )


    materials = Material.objects.all()


    if category_id:

        materials = materials.filter(
            product_categories__id=category_id
        )



    result = []


    for material in materials:


        result.append({

            "id":
            material.id,


            "name":
            material.name,


            "code":
            material.code

        })



    return JsonResponse(
        result,
        safe=False
    )
