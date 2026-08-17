# apps/lifecycle/api/urls.py

from django.urls import path

from . import views


app_name = "lifecycle"


urlpatterns = [

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
