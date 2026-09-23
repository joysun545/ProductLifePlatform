"""Authenticated private documents and independent order finance actions."""

import json
from django.http import FileResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from account.services.auth_service import ApiAuthenticationError, authenticate_request
from sales.services.order_finance_service import (
    OrderFinanceError, confirm_order_payment, get_order_finance,
    get_order_finance_file, submit_order_finance_statement, submit_order_payment,
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
    return _handle(request, "POST", lambda user: submit_order_finance_statement(
        user=user, plan_id=plan_id, receivable_amount=request.POST.get("receivable_amount"),
        statement_file=request.FILES.get("file"),
    ))


@csrf_exempt
def finance_payment_upload(request, plan_id):
    return _handle(request, "POST", lambda user: submit_order_payment(
        user=user, plan_id=plan_id, client_request_id=request.POST.get("client_request_id"),
        voucher_file=request.FILES.get("file"),
    ))


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
