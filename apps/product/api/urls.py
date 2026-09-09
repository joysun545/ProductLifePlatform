# apps/product/api/urls.py

from django.urls import path

from . import views


app_name = "product"


urlpatterns = [

    # =========================
    # 产品分类
    # =========================

    path(
        "categories/",
        views.CategoryListAPI.as_view(),
        name="category_list"
    ),


    path(
        "category/add/",
        views.CategoryCreateAPI.as_view(),
        name="category_create"
    ),


    path(
        "category/delete/",
        views.CategoryDeleteAPI.as_view(),
        name="category_delete"
    ),

    # =========================
    # 品牌
    # =========================

    path(
        "brands/",
        views.BrandListAPI.as_view(),
        name="brand_list"
    ),


    path(
        "brand/add/",
        views.BrandCreateAPI.as_view(),
        name="brand_create"
    ),

    path(
        "brand/delete/",
        views.BrandDeleteAPI.as_view(),
        name="brand_delete"
),


    # =========================
    # 产品型号
    # =========================

    path(
        "models/",
        views.ProductModelListAPI.as_view(),
        name="model_list"
    ),


    path(
        "model/add/",
        views.ProductModelCreateAPI.as_view(),
        name="model_create"
    ),


    path(
        "category/<int:category_id>/bom-options/",
        views.CategoryBOMOptionsAPI.as_view(),
        name="category_bom_options"
    ),


    path(
        "model/<int:model_id>/bom/v1/",
        views.ProductModelV1BOMAPI.as_view(),
        name="model_v1_bom"
    ),

    path(
        "model/<int:model_id>/documents/",
        views.ProductModelDocumentsAPI.as_view(),
        name="model_documents"
    ),


    path(
        "model/delete/",
        views.ProductModelDeleteAPI.as_view(),
        name="model_delete"
    ),
]
