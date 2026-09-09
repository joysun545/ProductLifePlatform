# pldp/urls.py


from django.urls import path, include

from django.contrib import admin



app_name = "pldp"



urlpatterns = [

    # ==========================
    # API统一入口
    # ==========================

    path(
        "api/",
        include(
            ("pldp.api.urls", "api"),
            namespace="api"
        )
    ),



    # ==========================
    # Web管理后台统一入口
    # ==========================

    path(
        "web/",
        include(
            ("pldp.web.urls", "web"),
            namespace="web"
        )
    ),

]
