"""Installed before existing URLs; only the QR approval endpoint is wrapped."""
from django.urls import path

from . import views

app_name = "root_trial"
urlpatterns = [
    path("pldp/web/portal/api/approve/", views.approve_web_login, name="approve_web_login"),
    path("pldp/api/root-trial/context/", views.context, name="context"),
    path("pldp/api/root-trial/license/", views.upload_license, name="upload_license"),
    path("pldp/api/root-trial/applications/", views.application_collection, name="applications"),
    path("pldp/api/root-trial/applications/<int:application_id>/", views.detail, name="detail"),
    path("pldp/api/root-trial/applications/<int:application_id>/review/", views.review, name="review"),
    path("pldp/api/root-trial/licenses/<uuid:upload_id>/", views.license_file, name="license_file"),
]
