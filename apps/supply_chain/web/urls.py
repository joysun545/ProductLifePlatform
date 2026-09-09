# apps/supply_chain/web/urls.py


from django.urls import path

from . import views



app_name = "supply_chain"



urlpatterns = [

    path(
        "api/materials-by-category/",
        views.get_materials_by_category,
        name="get_materials_by_category"
    ),



    # =========================
    # 页面
    # =========================


    path(
        "material-config/",
        views.material_config,
        name="material_config"
    ),



    # =========================
    # 数据查询接口
    # =========================


    path(
        "api/product-categories/",
        views.get_product_categories,
        name="get_product_categories"
    ),



    # 物料列表
    # 用于下拉选择

    path(
        "api/materials/",
        views.get_materials,
        name="get_materials"
    ),



    # 物料规格列表
    # 用于供应关系绑定

    path(
        "api/material-specs/",
        views.get_material_specs,
        name="get_material_specs"
    ),



    # 供应商列表
    # 只读取 Organization(SUPPLIER)

    path(
        "api/suppliers/",
        views.get_suppliers,
        name="get_suppliers"
    ),


    # 根据产品分类获取供应商

    path(
        "api/suppliers-by-category/",
        views.get_suppliers_by_category,
        name="get_suppliers_by_category"
    ),



    # 物料完整树形列表
    #
    # Material
    #    |
    # MaterialSpec
    #    |
    # Supplier

    path(
        "api/material-list/",
        views.material_list,
        name="material_list"
    ),


    path(
        "api/material-supplier-list/",
        views.get_material_supplier_list,
        name="material_supplier_list"
    ),




    # =========================
    # 数据创建接口
    # =========================


    # 创建物料
    #
    # Material

    path(
        "api/material/add/",
        views.add_material,
        name="add_material"
    ),



    # 创建物料规格
    #
    # MaterialSpec

    path(
        "api/material-spec/add/",
        views.add_material_spec_ajax,
        name="add_material_spec"
    ),



    # 创建物料规格供应关系
    #
    # MaterialSpecSupplier

    path(
        "api/material-spec-supplier/add/",
        views.add_material_spec_supplier_ajax,
        name="add_material_spec_supplier"
    ),




    # =========================
    # 数据删除接口
    # =========================


    # 删除物料

    path(
        "api/material/delete/",
        views.delete_material,
        name="delete_material"
    ),



    # 删除物料规格

    path(
        "api/material-spec/delete/",
        views.delete_material_spec,
        name="delete_material_spec"
    ),



    # 删除物料规格供应关系

    path(
        "api/material-spec-supplier/delete/",
        views.delete_material_spec_supplier,
        name="delete_material_spec_supplier"
    ),


]
