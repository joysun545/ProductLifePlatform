# config/urls.py

from django.contrib import admin
from django.urls import include, path


urlpatterns = [

    # ==========================
    # Django 管理后台
    # ==========================
    path(
        "pldp/admin/",
        admin.site.urls
    ),

    # ==========================
    # PLDP 中转路由
    # ==========================
    path(
        "pldp/",
        include(
            ("pldp.urls", "pldp"),
            namespace="pldp"
        )
    ),

]
