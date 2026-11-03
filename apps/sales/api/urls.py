from django.urls import include, path

from . import views


app_name = "sales"


urlpatterns = [
    path("replacement-supply-plans/",views.replacement_supply_plans,name="replacement_supply_plans"),
    path("replacement-supply-plans/<int:plan_id>/",views.replacement_supply_plans,name="replacement_supply_plan_detail"),
    path("order-plans/", views.order_plan_list, name="order_plan_list"),
    path("order-plans/options/", views.order_plan_options, name="order_plan_options"),
    path("order-plans/reissue-options/", views.order_plan_reissue_options, name="order_plan_reissue_options"),
    path("direct-merchants/configure/", views.configure_merchant_channel, name="configure_merchant_channel"),
    path("order-plans/material-flow/options/", views.material_flow_options, name="material_flow_options"),
    path("order-plans/material-flow/bom-detail/", views.material_flow_bom_detail, name="material_flow_bom_detail"),
    path("order-plans/material-flow/", views.create_material_flow_plan, name="create_material_flow_plan"),
    path("order-plans/<int:plan_id>/material-flow/detail/", views.material_flow_detail, name="material_flow_detail"),
    path("order-plans/<int:plan_id>/material-flow/warehouse-review/", views.material_flow_warehouse_review, name="material_flow_warehouse_review"),
    path("order-plans/<int:plan_id>/material-flow/supplier-confirm/", views.material_flow_supplier_confirm, name="material_flow_supplier_confirm"),
    path("order-plans/<int:plan_id>/material-flow/order-delivery-receipt/", views.material_flow_order_delivery_receipt, name="material_flow_order_delivery_receipt"),
    path("order-plans/<int:plan_id>/material-flow/material-ready/", views.material_flow_material_ready, name="material_flow_material_ready"),
    path("order-plans/<int:plan_id>/material-flow/production-confirm/", views.material_flow_production_confirm, name="material_flow_production_confirm"),
    path("order-plans/<int:plan_id>/material-flow/shipment-create/", views.material_flow_shipment_create, name="material_flow_shipment_create"),
    path("order-plans/<int:plan_id>/material-flow/shipment-authorize/", views.material_flow_shipment_authorize, name="material_flow_shipment_authorize"),
    path("order-plans/<int:plan_id>/material-flow/shipment-qrcode-print/", views.material_flow_shipment_qrcode_print, name="material_flow_shipment_qrcode_print"),
    path("order-plans/<int:plan_id>/material-flow/shipment-logistics-confirm/", views.material_flow_shipment_logistics_confirm, name="material_flow_shipment_logistics_confirm"),
    path("order-plans/<int:plan_id>/material-flow/shipment-logistics-batch-confirm/", views.material_flow_shipments_logistics_confirm, name="material_flow_shipments_logistics_confirm"),
    path("order-plans/<int:plan_id>/material-flow/shipment-logistics-document/", views.material_flow_shipment_logistics_document, name="material_flow_shipment_logistics_document"),
    path("order-plans/<int:plan_id>/material-flow/shipment-logistics-documents/<int:document_id>/file/", views.material_flow_shipment_logistics_document_file, name="material_flow_shipment_logistics_document_file"),
    path("order-plans/<int:plan_id>/material-flow/shipment-outbound/", views.material_flow_shipment_outbound, name="material_flow_shipment_outbound"),
    path("order-plans/<int:plan_id>/material-flow/shipment-receipt/", views.material_flow_shipment_receipt, name="material_flow_shipment_receipt"),
    path("order-plans/<int:plan_id>/material-flow/accounting-confirm/", views.material_flow_accounting_confirm, name="material_flow_accounting_confirm"),
    path("order-plans/<int:plan_id>/material-flow/cancel/", views.material_flow_cancel, name="material_flow_cancel"),
]

urlpatterns += [path("", include("sales.api.finance_urls"))]
