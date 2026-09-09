# pldp/web/urls.py

from django.urls import path, include


app_name = "web"


urlpatterns = [

    # ==========================
    # 用户管理
    # ==========================

    path(
        "account/",
        include(
            ("apps.account.web.urls", "account"),
            namespace="account"
        )
    ),


    # ==========================
    # 产品管理
    # ==========================

    path(
        "product/",
        include(
            ("apps.product.web.urls", "product"),
            namespace="product"
        )
    ),


    # ==========================
    # 供应链管理
    # ==========================

    path(
        "supply-chain/",
        include(
            ("apps.supply_chain.web.urls", "supply_chain"),
            namespace="supply_chain"
        )
    ),


    # ==========================
    # 打印中心
    # ==========================

    path(
        "print/",
        include(
            ("apps.print.urls", "print"),
            namespace="print"
        )
    ),

    path(
        "quality-trace/",
        include(
            ("quality_trace.web.urls", "quality_trace"),
            namespace="quality_trace"
        )
    ),

]

