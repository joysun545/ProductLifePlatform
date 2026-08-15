# apps/lifecycle/api/urls.py

from django.urls import path

from . import views


app_name = "lifecycle"


urlpatterns = [

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
