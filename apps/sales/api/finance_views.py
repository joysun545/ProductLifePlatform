"""Authenticated private documents and independent order finance actions."""

import json
from django.http import FileResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from account.services.auth_service import ApiAuthenticationError, authenticate_request
from sales.services.order_finance_service import (
    OrderFinanceError, confirm_order_payment, get_order_finance,
    get_order_finance_file,
)


def _json(data=None, msg="", code=0, status=200):
    response = JsonResponse({"code": code, "msg": msg, "data": data or {}}, status=status)
    response["Cache-Control"] = "private, no-store"
    return response


def _handle(request, method, action):
    if request.method != method:
        return _json(msg=f"仅支持{method}", code=1, status=405)
    try:
        user = authenticate_request(request)
        return _json(action(user))
    except ApiAuthenticationError as exc:
        return _json(msg=str(exc), code=401, status=401)
    except OrderFinanceError as exc:
        return _json(msg=str(exc), code=1)


def finance_detail(request, plan_id):
    return _handle(request, "GET", lambda user: get_order_finance(user=user, plan_id=plan_id))


@csrf_exempt
def finance_statement(request, plan_id):
    return _json(msg="请在 Web 端直属商家往来账确认订单清单。", code=1, status=410)

@csrf_exempt
def finance_payment_upload(request, plan_id):
    return _json(msg="请在向厂家付款工作域提交付款凭证。", code=1, status=410)

@csrf_exempt
def finance_payment_confirm(request, plan_id, payment_id):
    def action(user):
        if "application/json" in (request.content_type or "").lower():
            try:
                data = json.loads(request.body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise OrderFinanceError("请求数据格式错误。") from exc
            if not isinstance(data, dict):
                raise OrderFinanceError("请求数据格式错误。")
        else:
            data = request.POST
        return confirm_order_payment(user=user, plan_id=plan_id, payment_id=payment_id, confirmed_amount=data.get("confirmed_amount"))
    return _handle(request, "POST", action)


def finance_document(request, plan_id, payment_id=None):
    if request.method != "GET":
        return _json(msg="仅支持GET", code=1, status=405)
    try:
        user = authenticate_request(request)
        stream, mime, filename = get_order_finance_file(user=user, plan_id=plan_id, payment_id=payment_id)
        response = FileResponse(stream, content_type=mime, as_attachment=True, filename=filename)
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        response["Content-Security-Policy"] = "default-src 'none'; sandbox"
        return response
    except ApiAuthenticationError as exc:
        return _json(msg=str(exc), code=401, status=401)
    except OrderFinanceError as exc:
        return _json(msg=str(exc), code=1, status=403)


def factory_payment_accounts(request):
    from sales.services.factory_account_service import accounts
    return _handle(request, "GET", accounts)


@csrf_exempt
def factory_payment_submit(request, factory_id):
    from sales.services.factory_account_service import submit
    return _handle(request, "POST", lambda user: submit(
        user, factory_id, request.POST.get("client_request_id"), request.FILES.get("file"),
    ))


@csrf_exempt
def factory_payment_confirm(request, payment_id):
    from sales.services.factory_account_service import confirm
    return _handle(request, "POST", lambda user: confirm(user, payment_id, request.POST.get("confirmed_amount")))


def factory_payment_file(request, payment_id):
    from sales.services.factory_account_service import voucher_for
    try:
        user = authenticate_request(request)
        response = FileResponse(voucher_for(user, payment_id))
        response["Cache-Control"] = "private, no-store"
        return response
    except ApiAuthenticationError as exc:
        return _json(msg=str(exc), code=401, status=401)
    except OrderFinanceError as exc:
        return _json(msg=str(exc), code=1, status=404)


def factory_order_quote(request, plan_id):
    from sales.services.order_pricing_service import quote
    return _handle(request, "GET", lambda user: quote(user, plan_id, mini_program=True))


@csrf_exempt
def factory_order_price_confirm(request, plan_id):
    from sales.services.order_pricing_service import confirm_quote

    def action(user):
        try:
            payload = json.loads(request.body)
            if not isinstance(payload, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError) as exc:
            raise OrderFinanceError("计价清单格式错误。") from exc
        if set(payload) - {"tier", "expected"}:
            raise OrderFinanceError("档位和专用单价只能在 Web 端维护。")
        return confirm_quote(user, plan_id, payload.get("tier"), {}, payload.get("expected"),
                             initialized_prices_only=True)
    return _handle(request, "POST", action)
