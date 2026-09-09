# apps/inventory/urls.py

"""
PLDP - Inventory URL Configuration

库存模块接口路由


"""



from django.urls import path


from .views import (

    InventoryOutboundAPI,

    InventoryInboundAPI,

    InventoryTransferAPI,

    InventoryReturnAPI,

    InventoryQueryAPI,

)



urlpatterns = [


    path(

        "outbound/",

        InventoryOutboundAPI.as_view(),

        name="inventory-outbound"

    ),



    path(

        "inbound/",

        InventoryInboundAPI.as_view(),

        name="inventory-inbound"

    ),



    path(

        "transfer/",

        InventoryTransferAPI.as_view(),

        name="inventory-transfer"

    ),



    path(

        "return/",

        InventoryReturnAPI.as_view(),

        name="inventory-return"

    ),



    path(

        "query/",

        InventoryQueryAPI.as_view(),

        name="inventory-query"

    ),


]
