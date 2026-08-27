# apps/lifecycle/api/urls.py

from django.urls import path

from . import views


app_name = "lifecycle"


urlpatterns = [

    # Canonical after-sales chain.  All task payloads and pages use
    # AfterSalesApplication.application_id; the retired AfterSalesCase routes
    # intentionally have no compatibility aliases.
    path(
        "customer/devices/<int:device_id>/after-sales/context/",
        views.AfterSalesRequestContextAPI.as_view(),
        name="after_sales_request_context",
    ),
    path(
        "customer/devices/<int:device_id>/after-sales/applications/",
        views.AfterSalesApplicationListCreateAPI.as_view(),
        name="after_sales_application_create",
    ),
    path(
        "after-sales/applications/",
        views.AfterSalesApplicationListCreateAPI.as_view(),
        name="after_sales_application_list",
    ),
    path(
        "after-sales/supplier-quality-notices/",
        views.AfterSalesSupplierQualityNoticeListAPI.as_view(),
        name="after_sales_supplier_quality_notice_list",
    ),
    path(
        "after-sales/applications/<int:application_id>/",
        views.AfterSalesApplicationDetailAPI.as_view(),
        name="after_sales_application_detail",
    ),
    path(
        "after-sales/applications/<int:application_id>/supplier-quality-notices/<int:supplier_id>/",
        views.AfterSalesSupplierQualityNoticeAPI.as_view(),
        name="after_sales_supplier_quality_notice",
    ),
    path(
        "after-sales/applications/<int:application_id>/media/",
        views.AfterSalesApplicationMediaAPI.as_view(),
        name="after_sales_application_media",
    ),
    path(
        "after-sales/media/<int:media_id>/file/",
        views.AfterSalesMediaFileAPI.as_view(),
        name="after_sales_media_file",
    ),
    path(
        "after-sales/applications/<int:application_id>/submit/",
        views.AfterSalesSubmitAPI.as_view(),
        name="after_sales_submit",
    ),
    path(
        "after-sales/applications/<int:application_id>/triage-options/",
        views.AfterSalesTriageOptionsAPI.as_view(),
        name="after_sales_triage_options",
    ),
    path(
        "after-sales/applications/<int:application_id>/triage/",
        views.AfterSalesTriageAPI.as_view(),
        name="after_sales_triage",
    ),
    path(
        "after-sales/applications/<int:application_id>/applicant-shipment/",
        views.AfterSalesApplicantShipmentAPI.as_view(),
        name="after_sales_applicant_shipment",
    ),
    path(
        "after-sales/applications/<int:application_id>/repair-receipt/",
        views.AfterSalesRepairReceiptAPI.as_view(),
        name="after_sales_repair_receipt",
    ),
    path(
        "after-sales/applications/<int:application_id>/repair-complete/",
        views.AfterSalesRepairCompleteAPI.as_view(),
        name="after_sales_repair_complete",
    ),
    path(
        "after-sales/applications/<int:application_id>/return-shipment/",
        views.AfterSalesReturnShipmentAPI.as_view(),
        name="after_sales_return_shipment",
    ),
    path(
        "after-sales/applications/<int:application_id>/applicant-confirm/",
        views.AfterSalesApplicantConfirmAPI.as_view(),
        name="after_sales_applicant_confirm",
    ),
    path(
        "after-sales/applications/<int:application_id>/third-party/material-shipment/",
        views.AfterSalesThirdPartyMaterialShipmentAPI.as_view(),
        name="after_sales_third_party_material_shipment",
    ),
    path(
        "after-sales/applications/<int:application_id>/third-party/expense/",
        views.AfterSalesThirdPartyExpenseAPI.as_view(),
        name="after_sales_third_party_expense",
    ),
    path(
        "after-sales/applications/<int:application_id>/third-party/fault-material-return/",
        views.AfterSalesThirdPartyFaultMaterialReturnAPI.as_view(),
        name="after_sales_third_party_fault_material_return",
    ),
    path(
        "after-sales/applications/<int:application_id>/factory-close/",
        views.AfterSalesFactoryCloseAPI.as_view(),
        name="after_sales_factory_close",
    ),

    path(
        "goods-transfers/options/",
        views.GoodsTransferOptionsAPI.as_view(),
        name="goods_transfer_options",
    ),

    path(
        "goods-transfers/",
        views.GoodsTransferListCreateAPI.as_view(),
        name="goods_transfer_list_create",
    ),

    path(
        "goods-transfers/<int:transfer_id>/",
        views.GoodsTransferDetailAPI.as_view(),
        name="goods_transfer_detail",
    ),

    path(
        "goods-transfers/<int:transfer_id>/scan/",
        views.GoodsTransferScanAPI.as_view(),
        name="goods_transfer_scan",
    ),

    path(
        "goods-transfers/<int:transfer_id>/submit/",
        views.GoodsTransferSubmitAPI.as_view(),
        name="goods_transfer_submit",
    ),

    path(
        "goods-transfers/<int:transfer_id>/receipt/",
        views.GoodsTransferReceiptAPI.as_view(),
        name="goods_transfer_receipt",
    ),

    path(
        "goods-transfers/<int:transfer_id>/factory-return/",
        views.FactoryReturnProcessAPI.as_view(),
        name="factory_return_process",
    ),

    path(
        "goods-transfers/<int:transfer_id>/factory-return/pickup/",
        views.FactoryReturnPickupAPI.as_view(),
        name="factory_return_pickup",
    ),

    path(
        "goods-transfers/<int:transfer_id>/factory-return/posting/",
        views.FactoryReturnPostingAPI.as_view(),
        name="factory_return_posting",
    ),

    path(
        "goods-transfers/<int:transfer_id>/factory-return/credit-amount/",
        views.FactoryReturnCreditAmountAPI.as_view(),
        name="factory_return_credit_amount",
    ),

    path(
        "factory-returns/inventory/",
        views.FactoryReturnInventoryAPI.as_view(),
        name="factory_return_inventory",
    ),

    path(
        "goods-transfers/<int:transfer_id>/factory-return/items/<int:item_id>/production/",
        views.FactoryReturnProductionActionAPI.as_view(),
        name="factory_return_production_action",
    ),

    path(
        "goods-transfers/<int:transfer_id>/factory-return/items/<int:item_id>/matching/",
        views.FactoryReturnMatchingActionAPI.as_view(),
        name="factory_return_matching_action",
    ),

    path(
        "supplier-warranty-lists/",
        views.SupplierWarrantyListsAPI.as_view(),
        name="supplier_warranty_lists",
    ),

    path(
        "factory-return-warranty-dispatches/<int:dispatch_id>/",
        views.SupplierWarrantyDispatchAPI.as_view(),
        name="supplier_warranty_dispatch",
    ),

    path(
        "factory-return-warranty-dispatches/<int:dispatch_id>/receive/",
        views.SupplierWarrantyDispatchReceiveAPI.as_view(),
        name="supplier_warranty_dispatch_receive",
    ),

    path(
        "goods-transfers/<int:transfer_id>/factory-return/logistics-documents/",
        views.FactoryReturnLogisticsDocumentsAPI.as_view(),
        name="factory_return_logistics_documents",
    ),

    path(
        "goods-transfers/<int:transfer_id>/factory-return/logistics-documents/<int:document_id>/file/",
        views.FactoryReturnLogisticsDocumentFileAPI.as_view(),
        name="factory_return_logistics_document_file",
    ),

    path(
        "goods-transfers/<int:transfer_id>/factory-return/goods-list-documents/",
        views.FactoryReturnGoodsListDocumentsAPI.as_view(),
        name="factory_return_goods_list_documents",
    ),

    path(
        "goods-transfers/<int:transfer_id>/factory-return/goods-list-documents/<int:document_id>/file/",
        views.FactoryReturnGoodsListDocumentFileAPI.as_view(),
        name="factory_return_goods_list_document_file",
    ),

    # v3兼容入口：原“物料单据”实为物流单据。
    path(
        "goods-transfers/<int:transfer_id>/factory-return/material-documents/",
        views.FactoryReturnLogisticsDocumentsAPI.as_view(),
        name="factory_return_material_documents_legacy",
    ),

    path(
        "goods-transfers/<int:transfer_id>/factory-return/material-documents/<int:document_id>/file/",
        views.FactoryReturnLogisticsDocumentFileAPI.as_view(),
        name="factory_return_material_document_file_legacy",
    ),

    path(
        "goods-transfers/<int:transfer_id>/settlement/",
        views.GoodsTransferSettlementDetailAPI.as_view(),
        name="goods_transfer_settlement_detail",
    ),

    path(
        "goods-transfers/<int:transfer_id>/settlement/statement/",
        views.GoodsTransferSettlementStatementAPI.as_view(),
        name="goods_transfer_settlement_statement",
    ),

    path(
        "goods-transfers/<int:transfer_id>/settlement/statement-file/",
        views.GoodsTransferSettlementStatementFileAPI.as_view(),
        name="goods_transfer_settlement_statement_file",
    ),

    path(
        "goods-transfers/<int:transfer_id>/settlement/logistics-documents/",
        views.GoodsTransferSettlementLogisticsDocumentsAPI.as_view(),
        name="goods_transfer_settlement_logistics_documents",
    ),

    path(
        "goods-transfers/<int:transfer_id>/settlement/logistics-documents/<int:document_id>/file/",
        views.GoodsTransferSettlementLogisticsDocumentFileAPI.as_view(),
        name="goods_transfer_settlement_logistics_document_file",
    ),

    path(
        "goods-transfers/<int:transfer_id>/settlement/payments/",
        views.GoodsTransferSettlementPaymentsAPI.as_view(),
        name="goods_transfer_settlement_payments",
    ),

    path(
        "goods-transfers/<int:transfer_id>/settlement/payments/<int:payment_id>/confirm/",
        views.GoodsTransferSettlementPaymentConfirmAPI.as_view(),
        name="goods_transfer_settlement_payment_confirm",
    ),

    path(
        "goods-transfers/<int:transfer_id>/settlement/payments/<int:payment_id>/voucher-file/",
        views.GoodsTransferSettlementPaymentVoucherFileAPI.as_view(),
        name="goods_transfer_settlement_payment_voucher_file",
    ),

    path(
        "goods-transfers/<int:transfer_id>/accounts/",
        views.GoodsTransferAccountsAPI.as_view(),
        name="goods_transfer_accounts",
    ),

    path(
        "goods-transfers/<int:transfer_id>/accounts/<str:case_type>/submit/",
        views.GoodsTransferAccountSubmitAPI.as_view(),
        name="goods_transfer_account_submit",
    ),

    path(
        "goods-transfers/<int:transfer_id>/accounts/<int:case_id>/confirm/",
        views.GoodsTransferAccountConfirmAPI.as_view(),
        name="goods_transfer_account_confirm",
    ),

    path(
        "goods-transfers/<int:transfer_id>/accounts/<int:case_id>/allocations/",
        views.GoodsTransferAccountAllocationsAPI.as_view(),
        name="goods_transfer_account_allocations",
    ),

    path(
        "goods-transfers/<int:transfer_id>/accounts/<int:case_id>/payments/",
        views.GoodsTransferAccountPaymentsAPI.as_view(),
        name="goods_transfer_account_payments",
    ),

    path(
        "goods-transfers/<int:transfer_id>/accounts/<int:case_id>/payments/<int:payment_id>/confirm/",
        views.GoodsTransferAccountPaymentConfirmAPI.as_view(),
        name="goods_transfer_account_payment_confirm",
    ),

    path(
        "goods-transfers/<int:transfer_id>/accounts/<int:case_id>/statement-file/",
        views.GoodsTransferAccountStatementFileAPI.as_view(),
        name="goods_transfer_account_statement_file",
    ),

    path(
        "goods-transfers/<int:transfer_id>/accounts/<int:case_id>/payments/<int:payment_id>/voucher-file/",
        views.GoodsTransferAccountPaymentVoucherFileAPI.as_view(),
        name="goods_transfer_account_payment_voucher_file",
    ),

    path(
        "scan/resolve/",
        views.QRCodeResolveAPI.as_view(),
        name="scan_resolve"
    ),

    path(
        "scan/install/",
        views.InstallDeviceAPI.as_view(),
        name="scan_install",
    ),

    path(
        "scan/customer-invitation/refresh/",
        views.RefreshInstallationInvitationAPI.as_view(),
        name="installation_invitation_refresh",
    ),

    path(
        "scan/customer-invitations/<uuid:token>/qrcode/",
        views.InstallationInvitationQRCodeAPI.as_view(),
        name="installation_invitation_qrcode",
    ),

    path(
        "scan/customer-invitations/<uuid:token>/",
        views.InstallationInvitationDetailAPI.as_view(),
        name="installation_invitation_detail",
    ),

    path(
        "scan/customer-invitations/<uuid:token>/accept/",
        views.AcceptInstallationInvitationAPI.as_view(),
        name="installation_invitation_accept",
    ),

    path(
        "customer/devices/",
        views.CustomerDeviceListAPI.as_view(),
        name="customer_device_list",
    ),

    path(
        "customer/vehicles/<int:vehicle_id>/driver-invitations/",
        views.DriverVehicleInvitationCreateAPI.as_view(),
        name="driver_vehicle_invitation_create",
    ),

    path(
        "customer/driver-invitations/<uuid:token>/",
        views.DriverVehicleInvitationDetailAPI.as_view(),
        name="driver_vehicle_invitation_detail",
    ),

    path(
        "customer/driver-invitations/<uuid:token>/accept/",
        views.AcceptDriverVehicleInvitationAPI.as_view(),
        name="driver_vehicle_invitation_accept",
    ),

    path(
        "customer/devices/<int:device_id>/",
        views.CustomerDeviceResultAPI.as_view(),
        name="customer_device_result",
    ),

    path(
        "customer/devices/<int:device_id>/activate/",
        views.ActivateCustomerDeviceAPI.as_view(),
        name="customer_device_activate",
    ),

    path(
        "customer/devices/<int:device_id>/documents/",
        views.CustomerDeviceDocumentsAPI.as_view(),
        name="customer_device_documents",
    ),

    path(
        "customer/devices/<int:device_id>/documents/<int:document_id>/",
        views.CustomerDeviceDocumentDetailAPI.as_view(),
        name="customer_device_document_detail",
    ),

    path(
        "customer/devices/<int:device_id>/documents/<int:document_id>/file/",
        views.CustomerDeviceDocumentFileAPI.as_view(),
        name="customer_device_document_file",
    ),

    path(
        "customer/devices/<int:device_id>/documents/<int:document_id>/read/",
        views.CustomerDeviceDocumentReadAPI.as_view(),
        name="customer_device_document_read",
    ),

    path(
        "customer/devices/<int:device_id>/telemetry/",
        views.CustomerDeviceTelemetryAPI.as_view(),
        name="customer_device_telemetry",
    ),

    path(
        "create/",
        views.LifecycleCreateAPI.as_view(),
        name="create"
    ),

]
