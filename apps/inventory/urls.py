"""Retired aggregate inventory URLs."""

from django.http import JsonResponse
from django.urls import path


def retired_inventory(request):
    return JsonResponse(
        {"code": 410, "msg": "旧库存接口已停用，请使用当前库存业务入口。", "data": {}},
        status=410,
    )


urlpatterns = [
    path("outbound/", retired_inventory, name="inventory-outbound"),
    path("inbound/", retired_inventory, name="inventory-inbound"),
    path("transfer/", retired_inventory, name="inventory-transfer"),
    path("return/", retired_inventory, name="inventory-return"),
    path("query/", retired_inventory, name="inventory-query"),
]
