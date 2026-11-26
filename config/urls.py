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

# PLDP_PUBLIC_HOME_V1
from pldp.homepage_view import public_homepage
urlpatterns.insert(0, path("", public_homepage, name="pldp_public_homepage"))

# PLDP_UNIFIED_WEB_LOGIN_V1
urlpatterns.insert(0, path("pldp/web/portal/", include("web_portal.urls")))
# PLDP_ROOT_TRIAL_V1
urlpatterns.insert(0, path("", include("root_trial.urls")))
