import json

from django.http import FileResponse, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from account.services.auth_service import ApiAuthenticationError, authenticate_request
from sales.services.order_plan_service import (
    OrderPlanError,
    configure_direct_merchant,
    create_order_plan,
    get_order_plan_options,
)
from sales.services.order_plan_matching_service import (
    OrderPlanMatchingError,
    confirm_order_plan_matching,
    get_order_plan_item_bom_detail,
    get_order_plan_matching_detail,
)
from sales.services.order_plan_list_service import (
    OrderPlanListError,
    get_order_plan_list,
)
from sales.services.supplier_confirmation_service import (
    SupplierConfirmationError,
    confirm_supplier_order_materials,
)
from sales.services.order_plan_delivery_service import (
    OrderPlanDeliveryError,
    confirm_order_plan_delivery,
    get_order_plan_delivery_detail,
)
from sales.services.order_plan_production_service import (
    confirm_stock_outbound,
    OrderPlanProductionError,
    confirm_order_plan_production,
    get_stock_outbound_detail,
    render_lifecycle_qrcode,
)
from sales.services.order_plan_shipment_authorization_service import (
    OrderPlanShipmentAuthorizationError,
    confirm_shipment_authorization,
)
from sales.services.order_plan_receipt_service import (
    OrderPlanReceiptError, confirm_merchant_receipt, confirm_order_accounting,
)
from sales.services.order_finance_service import (
    OrderFinanceError,
    confirm_payment_received,
    get_order_finance_detail,
    get_payment_voucher_file,
    get_statement_file,
    submit_order_statement,
    upload_payment_voucher,
)


def _request_data(request):
    content_type = (request.content_type or "").lower()
    if "application/json" in content_type and request.body:
        try:
            data = json.loads(request.body.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
    return request.POST.dict()


def _authenticated_user(request):
    try:
        return authenticate_request(request), None
    except ApiAuthenticationError as exc:
        return None, JsonResponse(
            {"code": 401, "msg": str(exc), "data": {}},
            status=401,
        )


def _method_error(method):
    return JsonResponse({"code": 1, "msg": f"仅支持{method}", "data": {}}, status=200)


def order_plan_options(request):
    if request.method != "GET":
        return _method_error("GET")
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    try:
        data = get_order_plan_options(user)
    except OrderPlanError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    return JsonResponse({"code": 0, "msg": "", "data": data}, status=200)


def order_plan_list(request):
    if request.method != "GET":
        return _method_error("GET")
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    try:
        data = get_order_plan_list(
            user=user,
            status=request.GET.get("status", ""),
            limit=request.GET.get("limit", 50),
        )
    except OrderPlanListError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    return JsonResponse({"code": 0, "msg": "", "data": data}, status=200)


@csrf_exempt
def configure_merchant_channel(request):
    if request.method != "POST":
        return _method_error("POST")
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    data = _request_data(request)
    try:
        result = configure_direct_merchant(
            user=user,
            merchant_id=data.get("merchant_id"),
            brand_id=data.get("brand_id"),
            region=data.get("region"),
        )
    except OrderPlanError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    return JsonResponse(
        {"code": 0, "msg": "商家区域和品牌已保存", "data": result},
        status=200,
    )


@csrf_exempt
def create_plan(request):
    if request.method != "POST":
        return _method_error("POST")
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    data = _request_data(request)
    try:
        result, created = create_order_plan(
            user=user,
            merchant_id=data.get("merchant_id"),
            client_request_id=data.get("client_request_id"),
            items=data.get("items"),
            remark=data.get("remark", ""),
        )
    except OrderPlanError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    return JsonResponse(
        {
            "code": 0,
            "msg": "订单计划创建成功" if created else "订单计划已创建，请勿重复提交",
            "data": result,
        },
        status=200,
    )


def order_plan_matching_detail(request, plan_id):
    if request.method != "GET":
        return _method_error("GET")
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    try:
        result = get_order_plan_matching_detail(user=user, plan_id=plan_id)
    except OrderPlanMatchingError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    return JsonResponse({"code": 0, "msg": "", "data": result}, status=200)


def order_plan_item_bom_detail(request, plan_id, item_id, bom_id):
    if request.method != "GET":
        return _method_error("GET")
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    try:
        result = get_order_plan_item_bom_detail(
            user=user,
            plan_id=plan_id,
            item_id=item_id,
            bom_id=bom_id,
        )
    except OrderPlanMatchingError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    return JsonResponse({"code": 0, "msg": "", "data": result}, status=200)


@csrf_exempt
def confirm_plan_matching(request, plan_id):
    if request.method != "POST":
        return _method_error("POST")
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    data = _request_data(request)
    try:
        result, confirmed, dispatched = confirm_order_plan_matching(
            user=user,
            plan_id=plan_id,
            items=data.get("items"),
        )
    except OrderPlanMatchingError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    result["task_dispatch"] = dispatched
    return JsonResponse(
        {
            "code": 0,
            "msg": "订单BOM配套确认成功" if confirmed else "该订单已经确认",
            "data": result,
        },
        status=200,
    )


@csrf_exempt
def confirm_supplier_materials(request, plan_id):
    if request.method != "POST":
        return _method_error("POST")
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    data = _request_data(request)
    try:
        result = confirm_supplier_order_materials(
            user=user,
            plan_id=plan_id,
            supplier_id=data.get("supplier_id"),
            usertask_id=data.get("usertask_id"),
        )
    except SupplierConfirmationError as exc:
        return JsonResponse(
            {"code": 1, "msg": str(exc), "data": {}},
            status=200,
        )
    return JsonResponse(
        {
            "code": 0,
            "msg": (
                "物料已完成配送"
                if result["completed"]
                else "您的责任物料已确认配送，等待协同账号"
            ),
            "data": result,
        },
        status=200,
    )


@csrf_exempt
def order_plan_delivery(request, plan_id):
    if request.method not in ("GET", "POST"):
        return _method_error("GET或POST")
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    try:
        if request.method == "GET":
            result = get_order_plan_delivery_detail(user=user, plan_id=plan_id)
            message = ""
        else:
            result, confirmed = confirm_order_plan_delivery(user=user, plan_id=plan_id)
            message = "物料配送已汇总确认" if confirmed else "该订单已经确认"
    except OrderPlanDeliveryError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    return JsonResponse({"code": 0, "msg": message, "data": result}, status=200)


@csrf_exempt
def order_plan_production(request, plan_id):
    if request.method != "POST":
        return _method_error("POST")
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    try:
        result, confirmed = confirm_order_plan_production(user=user, plan_id=plan_id)
    except OrderPlanProductionError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    return JsonResponse({
        "code": 0,
        "msg": "生产完成已确认" if confirmed else "该订单已经确认生产完成",
        "data": result,
    }, status=200)


@csrf_exempt
def shipment_authorization(request, plan_id):
    if request.method != "POST":
        return _method_error("POST")
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    try:
        result, changed = confirm_shipment_authorization(
            user=user,
            plan_id=plan_id,
        )
    except OrderPlanShipmentAuthorizationError as exc:
        return JsonResponse(
            {"code": 1, "msg": str(exc), "data": {}},
            status=200,
        )
    return JsonResponse(
        {
            "code": 0,
            "msg": "发货指令已下达" if changed else "该订单已经下达发货指令",
            "data": result,
        },
        status=200,
    )


@csrf_exempt
def stock_outbound_detail(request, plan_id):
    if request.method not in ("GET", "POST"):
        return _method_error("GET或POST")
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    try:
        if request.method == "GET":
            result = get_stock_outbound_detail(user=user, plan_id=plan_id)
            message = ""
        else:
            result, confirmed = confirm_stock_outbound(user=user, plan_id=plan_id)
            message = "出库已确认" if confirmed else "该订单已经确认出库"
    except OrderPlanProductionError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    return JsonResponse({"code": 0, "msg": message, "data": result}, status=200)


def lifecycle_qrcode(request, plan_id, item_id):
    if request.method != "GET":
        return _method_error("GET")
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    try:
        content, code = render_lifecycle_qrcode(
            user=user, plan_id=plan_id, item_id=item_id
        )
    except OrderPlanProductionError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    response = HttpResponse(content, content_type="image/png")
    response["Content-Disposition"] = f'inline; filename="{code}.png"'
    response["Cache-Control"] = "no-store"
    return response


@csrf_exempt
def merchant_receipt(request, plan_id):
    if request.method != "POST":
        return _method_error("POST")
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    try:
        result, changed = confirm_merchant_receipt(user=user, plan_id=plan_id)
    except OrderPlanReceiptError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    return JsonResponse({"code": 0, "msg": "入库完成" if changed else "该订单已入库", "data": result})


@csrf_exempt
def accounting_confirm(request, plan_id):
    if request.method != "POST":
        return _method_error("POST")
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    try:
        result, changed = confirm_order_accounting(user=user, plan_id=plan_id)
    except OrderPlanReceiptError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    return JsonResponse({"code": 0, "msg": "入账完成" if changed else "该订单已入账", "data": result})


def order_finance_detail(request, plan_id):
    if request.method != "GET":
        return _method_error("GET")
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    try:
        result = get_order_finance_detail(user=user, plan_id=plan_id)
    except OrderFinanceError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    return JsonResponse({"code": 0, "msg": "", "data": result}, status=200)


@csrf_exempt
def order_finance_statement(request, plan_id):
    if request.method != "POST":
        return _method_error("POST")
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    try:
        result = submit_order_statement(
            user=user,
            plan_id=plan_id,
            receivable_amount=request.POST.get("receivable_amount"),
            image=request.FILES.get("statement_image"),
        )
    except OrderFinanceError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    return JsonResponse(
        {"code": 0, "msg": "订单清单已提交", "data": result},
        status=200,
    )


@csrf_exempt
def order_finance_payments(request, plan_id):
    if request.method != "POST":
        return _method_error("POST")
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    try:
        result, created = upload_payment_voucher(
            user=user,
            plan_id=plan_id,
            client_request_id=request.POST.get("client_request_id"),
            image=request.FILES.get("voucher_image"),
        )
    except OrderFinanceError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    return JsonResponse(
        {
            "code": 0,
            "msg": "付款凭证已上传" if created else "付款凭证已经上传，请勿重复提交",
            "data": result,
        },
        status=200,
    )


@csrf_exempt
def order_finance_payment_confirm(request, plan_id, payment_id):
    if request.method != "POST":
        return _method_error("POST")
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    data = _request_data(request)
    try:
        result, changed = confirm_payment_received(
            user=user,
            plan_id=plan_id,
            payment_id=payment_id,
            amount=data.get("confirmed_amount"),
        )
    except OrderFinanceError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    return JsonResponse(
        {
            "code": 0,
            "msg": "收款已确认" if changed else "该笔收款已经确认",
            "data": result,
        },
        status=200,
    )


def order_finance_statement_image(request, plan_id):
    if request.method != "GET":
        return _method_error("GET")
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    try:
        field_file, original_name, content_type = get_statement_file(
            user=user,
            plan_id=plan_id,
        )
    except OrderFinanceError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    response = FileResponse(
        field_file.open("rb"),
        content_type=content_type,
        as_attachment=False,
        filename=original_name or "order-statement.jpg",
    )
    response["Cache-Control"] = "private, no-store"
    return response


def order_finance_voucher_image(request, plan_id, payment_id):
    if request.method != "GET":
        return _method_error("GET")
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    try:
        field_file, original_name, content_type = get_payment_voucher_file(
            user=user,
            plan_id=plan_id,
            payment_id=payment_id,
        )
    except OrderFinanceError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    response = FileResponse(
        field_file.open("rb"),
        content_type=content_type,
        as_attachment=False,
        filename=original_name or "payment-voucher.jpg",
    )
    response["Cache-Control"] = "private, no-store"
    return response
