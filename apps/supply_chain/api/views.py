import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from account.services.auth_service import ApiAuthenticationError, authenticate_request
from supply_chain.services.supplier_responsibility_service import (
    SupplierResponsibilityError,
    assign_supplier_sales_responsibilities,
    get_responsibility_options,
)


def _authenticated_user(request):
    try:
        return authenticate_request(request), None
    except ApiAuthenticationError as exc:
        return None, JsonResponse(
            {"code": 401, "msg": str(exc), "data": {}},
            status=401,
        )


def _request_data(request):
    if "application/json" in (request.content_type or "").lower() and request.body:
        try:
            value = json.loads(request.body.decode("utf-8"))
            return value if isinstance(value, dict) else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
    return request.POST.dict()


@csrf_exempt
def supplier_sales_responsibilities(request):
    if request.method not in {"GET", "POST"}:
        return JsonResponse(
            {"code": 1, "msg": "仅支持GET或POST", "data": {}},
            status=200,
        )
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    try:
        if request.method == "GET":
            result = get_responsibility_options(user=user)
            message = ""
        else:
            data = _request_data(request)
            result = assign_supplier_sales_responsibilities(
                user=user,
                sales_identity_id=data.get("sales_identity_id"),
                scope_type=data.get("scope_type"),
                target_ids=data.get("target_ids"),
            )
            message = "销售经理责任范围已保存"
    except SupplierResponsibilityError as exc:
        return JsonResponse(
            {"code": 1, "msg": str(exc), "data": {}},
            status=200,
        )
    return JsonResponse(
        {"code": 0, "msg": message, "data": result},
        status=200,
    )
