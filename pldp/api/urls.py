# pldp/api/urls.py

from django.urls import path, include


app_name = "api"


urlpatterns = [

    path(
        "account/",
        include(
            ("apps.account.api.urls", "account"),
            namespace="account"
        )
    ),

    path(
        "product/",
        include(
            ("apps.product.api.urls", "product"),
            namespace="product"
        )
    ),

    path(
        "lifecycle/",
        include(
            ("apps.lifecycle.api.urls", "lifecycle"),
            namespace="lifecycle"
        )
    ),

    path(
        "inventory/",
        include(
            ("apps.inventory.urls", "inventory"),
            namespace="inventory"
        )
    ),

    path(
        "workflow/",
        include(
            ("workflow.api.urls", "workflow"),
            namespace="workflow"
        )
    ),

    path(
        "sales/",
        include(
            ("apps.sales.api.urls", "sales"),
            namespace="sales"
        )
    ),

    path(
        "supply-chain/",
        include(
            ("apps.supply_chain.api.urls", "supply_chain"),
            namespace="supply_chain"
        )
    ),

    path(
        "quality-trace/",
        include(
            ("quality_trace.api.urls", "quality_trace"),
            namespace="quality_trace"
        )
    ),

]
