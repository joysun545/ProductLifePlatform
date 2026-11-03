from django.urls import path

from . import views


app_name = "device_code_registry"

urlpatterns = [
    path("", views.registry_page, name="page"),
    path("api/login/start/", views.login_start, name="login_start"),
    path("api/login/approve/", views.login_approve, name="login_approve"),
    path("api/login/status/", views.login_status, name="login_status"),
    path("api/logout/", views.logout, name="logout"),
    path("api/batches/", views.list_batches, name="batches"),
    path("api/batches/generate/", views.generate_batch, name="generate_batch"),
    path(
        "api/batches/<uuid:batch_id>/packaging/",
        views.save_packaging_info,
        name="save_packaging_info",
    ),
    path(
        "api/batches/<uuid:batch_id>/download/qr/",
        views.download_qr_package,
        name="download_qr_package",
    ),
    path(
        "api/batches/<uuid:batch_id>/download/codes/",
        views.download_codes_package,
        name="download_codes_package",
    ),
]
