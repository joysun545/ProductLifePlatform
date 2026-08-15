from django.urls import path

from . import views


app_name = "sales"


urlpatterns = [
    path(
        "order-plans/",
        views.order_plan_list,
        name="order_plan_list",
    ),
    path(
        "order-plans/options/",
        views.order_plan_options,
        name="order_plan_options",
    ),
    path(
        "direct-merchants/configure/",
        views.configure_merchant_channel,
        name="configure_merchant_channel",
    ),
    path(
        "order-plans/create/",
        views.create_plan,
        name="create_order_plan",
    ),
    path(
        "order-plans/<int:plan_id>/matching/",
        views.order_plan_matching_detail,
        name="order_plan_matching_detail",
    ),
    path(
        "order-plans/<int:plan_id>/matching/items/<int:item_id>/boms/<int:bom_id>/",
        views.order_plan_item_bom_detail,
        name="order_plan_item_bom_detail",
    ),
    path(
        "order-plans/<int:plan_id>/matching/confirm/",
        views.confirm_plan_matching,
        name="confirm_order_plan_matching",
    ),
    path(
        "order-plans/<int:plan_id>/supplier-confirm/",
        views.confirm_supplier_materials,
        name="confirm_supplier_materials",
    ),
    path(
        "order-plans/<int:plan_id>/delivery-confirm/",
        views.order_plan_delivery,
        name="order_plan_delivery",
    ),
    path("order-plans/<int:plan_id>/production-confirm/", views.order_plan_production, name="order_plan_production"),
    path("order-plans/<int:plan_id>/stock-outbound/", views.stock_outbound_detail, name="stock_outbound_detail"),
    path("order-plans/<int:plan_id>/merchant-receipt/", views.merchant_receipt, name="merchant_receipt"),
    path("order-plans/<int:plan_id>/accounting-confirm/", views.accounting_confirm, name="accounting_confirm"),
    path("order-plans/<int:plan_id>/items/<int:item_id>/lifecycle-qrcode/", views.lifecycle_qrcode, name="lifecycle_qrcode"),
]
