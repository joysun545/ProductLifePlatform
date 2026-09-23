import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from supply_chain.services.material_inventory_service import (
    MaterialInventoryError, MaterialInventoryPermissionError, inventory_catalog, inventory_detail,
    inventory_form, inventory_list, inventory_warranty_list, submit_movement,
)
from .views import _authenticated_user


@csrf_exempt
def material_inventory(request, stock_id=None, action="list"):
    method = "POST" if action == "submit" else "GET"
    if request.method != method:
        return JsonResponse({"code": 1, "msg": f"仅支持{method}", "data": {}}, status=405)
    user, error = _authenticated_user(request)
    if error is not None:
        return error
    try:
        if action == "submit":
            try:
                data = json.loads(request.body)
            except (ValueError, UnicodeDecodeError):
                raise MaterialInventoryError("提交数据格式无效") from None
            if not isinstance(data, dict):
                raise MaterialInventoryError("提交数据格式无效")
            result = submit_movement(user=user, data=data)
        elif action == "catalog":
            result = inventory_catalog(user=user, query=request.GET.get("q", ""), cursor=request.GET.get("cursor"))
        elif action == "form":
            result = inventory_form(user=user, kind=request.GET.get("kind"), stock_id=request.GET.get("stock_id"), lot_id=request.GET.get("lot_id"), case_id=request.GET.get("case_id"))
        elif action == "warranty":
            result = inventory_warranty_list(user=user, query=request.GET.get("q", ""), cursor=request.GET.get("cursor"))
        elif stock_id is not None:
            result = inventory_detail(user=user, stock_id=stock_id, tab=request.GET.get("tab", "lots"), cursor=request.GET.get("cursor"), include_empty=request.GET.get("include_empty") == "1", case_id=request.GET.get("case_id"))
        else:
            result = inventory_list(user=user, query=request.GET.get("q", ""), cursor=request.GET.get("cursor"))
    except MaterialInventoryPermissionError as exc:
        return JsonResponse({"code": 403, "msg": str(exc), "data": {}}, status=403)
    except MaterialInventoryError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    return JsonResponse({"code": 0, "msg": "", "data": result}, status=200)
