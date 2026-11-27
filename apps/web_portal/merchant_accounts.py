from django.contrib import messages
from django.http import FileResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from sales.services.factory_account_service import accounts, confirm, voucher_for
from sales.services.order_finance_service import OrderFinanceError
from sales.services.order_pricing_service import TIERS, catalog, confirm_quote, merchant_prices, quote, save_catalog_row, save_merchant_price
from .access import require_web_identity


@never_cache
@csrf_protect
@require_http_methods(["GET", "POST"])
def workbench(request):
    identity = require_web_identity(request, "merchant_accounts")
    can_operate = identity.role.code == "factory_sales_assistant"
    user = request.user
    voucher = request.GET.get("voucher")
    if voucher:
        try:
            response = FileResponse(voucher_for(user, int(voucher)))
        except (OrderFinanceError, ValueError):
            from django.http import Http404
            raise Http404("付款凭证不存在。")
        response["Cache-Control"] = "private, no-store"
        return response
    section = request.GET.get("section", "")
    plan_id = request.GET.get("order")
    merchant_id = request.GET.get("merchant_prices")
    price_sheet = None
    error = ""
    selected = None
    if request.method == "POST":
        try:
            action = request.POST.get("action")
            if action == "catalog":
                save_catalog_row(user, request.POST.get("model"), {tier: request.POST.get(tier) for tier in TIERS})
            elif action == "merchant_tier":
                save_merchant_price(user, merchant_id, request.POST.get("tier"))
            elif action == "merchant_price":
                save_merchant_price(user, merchant_id, request.POST.get("tier"),
                                    request.POST.get("model"), request.POST.get("price"))
            elif action == "pricing":
                current = quote(user, plan_id, request.POST.get("tier"))
                keys = {line["price_key"] for line in current["lines"]}
                expected = {key: request.POST.get(f"expected_{key}") or None for key in keys}
                edits = {key: request.POST.get(f"price_{key}") for key in keys if request.POST.get(f"price_{key}") != expected[key]}
                confirm_quote(user, plan_id, request.POST.get("tier"), edits, expected)
            elif action == "receipt":
                confirm(user, request.POST.get("payment"), request.POST.get("amount"))
            else:
                raise OrderFinanceError("请选择有效的操作。")
        except (OrderFinanceError, ValueError) as exc:
            error = str(exc)
        else:
            messages.success(request, "已保存")
            return redirect(request.get_full_path())
    if plan_id:
        try:
            selected = quote(user, plan_id, request.GET.get("tier"))
        except OrderFinanceError as exc:
            error = str(exc)
    if merchant_id:
        try:
            price_sheet = merchant_prices(user, merchant_id)
        except (OrderFinanceError, ValueError) as exc:
            error = str(exc)
    return render(request, "web_portal/merchant_accounts.html", {
        "accounts": accounts(user, allow_price_staff=True)["accounts"], "catalog": catalog(user) if section == "prices" else None,
        "merchant_prices": price_sheet, "can_operate": can_operate, "section": section, "order": plan_id, "quote": selected, "tiers": TIERS, "error": error,
    })
