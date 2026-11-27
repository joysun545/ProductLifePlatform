from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone

from account.services.role_delegation_service import identity_has_role
from product.models import ProductModel
from sales.models import (
    FactoryAccessoryPrice, FactoryProductPrice, MerchantPricePolicy,
    OrderPlanAccessoryCompatibility, OrderPlanFinance,
)
from .order_finance_service import OrderFinanceError, _active, _complete, _identity, _plan, _reconcile
from .pricing_totals import pricing_amounts
from .order_receivable_service import financial_write_errors, lock_factory_account, lock_receivable_account

TIERS = ("A", "B", "C")
MAX_AMOUNT = Decimal("999999999999.99")
CENT = Decimal("0.01")


def _price(value):
    from .order_finance_service import _money

    if value == "0" or value == "0.00":
        return "0.00"
    return format(_money(value), ".2f")


def can_maintain_prices(identity):
    return bool(identity and identity.organization.organization_type.code == "OWNER"
                and identity.organization.factory_root_id == identity.organization_id
                and (identity.role.code in {"factory_admin", "factory_sales_assistant"}
                     or (identity.role.code == "factory_senior_manager"
                         and identity_has_role(identity, "factory_sales_assistant"))))


def _price_staff(user):
    identity = _identity(user)
    if not can_maintain_prices(identity):
        raise OrderFinanceError("当前岗位没有厂家价格维护权限。")
    return identity


def _merchant_policy_scope(user, merchant_id):
    from .factory_account_service import _relations

    identity = _price_staff(user)
    relation = _relations(identity, allow_price_staff=True).filter(target_organization_id=merchant_id).first()
    if not relation:
        raise OrderFinanceError("该商家不属于当前厂家直属范围。")
    return relation


def merchant_prices(user, merchant_id):
    relation = _merchant_policy_scope(user, merchant_id)
    policy = MerchantPricePolicy.objects.filter(
        account__factory_organization_id=relation.source_organization_id,
        account__merchant_organization_id=relation.target_organization_id,
    ).first()
    tier = policy.tier if policy else ""
    overrides = policy.overrides if policy else {}
    rows = catalog(user)
    for row in rows:
        row["override"] = overrides.get(row["price_key"], "")
        row["base"] = row["prices"].get(tier, "")
    return {"merchant": relation.target_organization, "tier": tier,
            "version": f"{tier}-01" if overrides else tier, "rows": rows}


@financial_write_errors(OrderFinanceError)
@transaction.atomic
def save_merchant_price(user, merchant_id, tier, model_id=None, price=None):
    relation = _merchant_policy_scope(user, merchant_id)
    account = lock_factory_account(factory_organization_id=relation.source_organization_id,
                                   merchant_organization_id=relation.target_organization_id)
    policy = MerchantPricePolicy.objects.filter(account=account).first()
    if model_id is None:
        if tier not in TIERS or (policy and policy.tier != tier):
            raise OrderFinanceError("请选择有效的初始档位，已确认的基础档位不能更换。")
        MerchantPricePolicy.objects.get_or_create(account=account, defaults={"tier": tier})
        return
    if not policy or policy.tier != tier:
        raise OrderFinanceError("请先确认商家基础档位，或刷新后重试。")
    _, _, key = _catalog_key(relation.source_organization_id, model_id)
    overrides = dict(policy.overrides)
    overrides[key] = _price(price)
    policy.overrides = overrides
    policy.save(update_fields=("overrides",))


def _accessory_specs(factory_id):
    return OrderPlanAccessoryCompatibility.objects.filter(
        factory_organization_id=factory_id, is_active=True,
    )


def _catalog_key(factory_id, key):
    key = str(key)
    accessory = key.startswith("accessory-")
    number = key.removeprefix("accessory-") if accessory else key
    if not number.isascii() or not number.isdecimal() or len(number) > 18:
        raise OrderFinanceError("请选择有效的设备型号或配件规格。")
    pk = int(number)
    if accessory:
        if _accessory_specs(factory_id).filter(material_spec_id=pk).exists():
            return FactoryAccessoryPrice, {"material_spec_id": pk}, f"accessory-{pk}"
    elif ProductModel.objects.filter(pk=pk).exists():
        return FactoryProductPrice, {"product_model_id": pk}, str(pk)
    raise OrderFinanceError("设备型号或本厂配件规格不存在。")


def catalog(user):
    identity = _price_staff(user)
    factory_id = identity.organization_id
    prices = dict(FactoryProductPrice.objects.filter(factory_id=factory_id)
                  .values_list("product_model_id", "prices"))
    accessory_prices = dict(FactoryAccessoryPrice.objects.filter(factory_id=factory_id)
                            .values_list("material_spec_id", "prices"))
    result = [{"id": entry.pk, "price_key": str(entry.pk), "name": entry.name, "category": entry.category.name,
               "prices": prices.get(entry.pk, {})}
              for entry in ProductModel.objects.select_related("category").order_by("category__name", "name", "id")]
    specs = _accessory_specs(factory_id).order_by().values_list(
        "material_spec_id", "material_spec__material__name", "material_spec__name",
    ).distinct().order_by("material_spec__material__name", "material_spec__name", "material_spec_id")
    for pk, material, spec in specs:
        result.append({"id": pk, "price_key": f"accessory-{pk}", "name": f"{material} · {spec}",
                       "category": "配件", "prices": accessory_prices.get(pk, {})})
    return result


@transaction.atomic
def save_catalog_row(user, model_id, prices):
    identity = _price_staff(user)
    if not isinstance(prices, dict) or set(prices) != set(TIERS):
        raise OrderFinanceError("请填写 A、B、C 三档价格。")
    model, lookup, _ = _catalog_key(identity.organization_id, model_id)
    normalized = {tier: _price(prices[tier]) for tier in TIERS}
    model.objects.update_or_create(factory_id=identity.organization_id, **lookup, defaults={"prices": normalized})


def _with_price(line, price):
    subtotal = None
    if price is not None:
        subtotal = (Decimal(price) * Decimal(line["quantity"])).quantize(CENT, rounding=ROUND_HALF_UP)
    return {**line, "unit_price": price, "subtotal": format(subtotal, ".2f") if subtotal is not None else None}


def _lines(plan, tier, overrides):
    items = list(plan.items.select_related("product_model").order_by("id"))
    prices = dict(FactoryProductPrice.objects.filter(
        factory_id=plan.factory_organization_id, product_model_id__in=[item.product_model_id for item in items],
    ).values_list("product_model_id", "prices"))
    result = []
    for item in items:
        key = str(item.product_model_id)
        result.append(_with_price({
            "item_id": item.pk, "line_key": f"device-{item.pk}", "price_key": key,
            "kind": "DEVICE", "model_id": item.product_model_id, "name": item.product_model.name,
            "quantity": item.quantity, "unit": "台",
        }, overrides.get(key, prices.get(item.product_model_id, {}).get(tier))))

    grouped = {}
    for item in plan.accessory_items.select_related("material_spec__material").order_by("id"):
        spec = item.material_spec
        line = grouped.setdefault(spec.pk, {
            "item_id": item.pk, "line_key": f"accessory-{item.pk}", "price_key": f"accessory-{spec.pk}",
            "kind": "ACCESSORY", "material_spec_id": spec.pk, "name": f"{spec.material.name} · {spec.name}",
            "quantity": Decimal(0), "unit": item.unit_snapshot,
        })
        if line["unit"] != item.unit_snapshot:
            raise OrderFinanceError("同一配件规格的计价单位不一致，请核对订单。")
        line["quantity"] += Decimal(item.quantity_milli) / 1000
    accessory_prices = dict(FactoryAccessoryPrice.objects.filter(
        factory_id=plan.factory_organization_id, material_spec_id__in=grouped,
    ).values_list("material_spec_id", "prices"))
    for pk, line in grouped.items():
        line["quantity"] = format(line["quantity"], "f")
        result.append(_with_price(line, overrides.get(line["price_key"], accessory_prices.get(pk, {}).get(tier))))
    return result


def quote(user, plan_id, tier=None, *, mini_program=False):
    identity = _price_staff(user)
    if mini_program and identity.role.code != "factory_sales_assistant":
        raise OrderFinanceError("只有厂家销售助理可以在小程序办理订单计价。")
    if identity.role.code == "factory_sales_assistant":
        plan = _plan(user, plan_id)
    else:
        from sales.models import OrderPlan

        plan = OrderPlan.objects.filter(pk=plan_id, factory_organization_id=identity.organization_id).first()
        if not plan:
            raise OrderFinanceError("订单不存在或无财务访问权限。")
    if mini_program:
        _merchant_policy_scope(user, plan.merchant_organization_id)
    finance = OrderPlanFinance.objects.filter(order_plan=plan).first()
    if not finance:
        raise OrderFinanceError("该订单没有可查看的资金清单。")
    if finance.statement_submitted_at:
        return {"lines": finance.priced_lines, "version": finance.pricing_version,
                "confirmed": True, "total": format(finance.receivable_amount, ".2f"),
                **pricing_amounts(finance.priced_lines)}
    if not _active(plan):
        raise OrderFinanceError("订单已取消或正在取消，不能继续计价。")
    policy = MerchantPricePolicy.objects.filter(
        account__factory_organization_id=plan.factory_organization_id,
        account__merchant_organization_id=plan.merchant_organization_id,
    ).first()
    effective_tier = policy.tier if policy else (tier if tier in TIERS else "")
    overrides = policy.overrides if policy else {}
    lines = _lines(plan, effective_tier, overrides)
    initialized = bool(policy and lines and all(line["subtotal"] is not None for line in lines))
    total = sum((Decimal(line["subtotal"]) for line in lines), Decimal(0)) if initialized else None
    missing_price = any(line["unit_price"] is None for line in lines)
    return {"lines": lines, "total": format(total, ".2f") if total is not None else None,
            **pricing_amounts(lines), "can_confirm": initialized and 0 < total <= MAX_AMOUNT,
            "blocked_reason": "有设备或配件尚未初始化档位价格，请在 Web 维护。" if effective_tier and missing_price else "",
            "tier": effective_tier, "tier_locked": bool(policy), "confirmed": False,
            "version": f"{effective_tier}-01" if overrides else effective_tier}


@financial_write_errors(OrderFinanceError)
@transaction.atomic
def confirm_quote(user, plan_id, tier, edits, expected, *, initialized_prices_only=False):
    authorized = _plan(user, plan_id, role="assistant")
    account = lock_receivable_account(authorized)
    plan = _plan(user, plan_id, role="assistant", lock=True)
    if initialized_prices_only:
        _merchant_policy_scope(user, plan.merchant_organization_id)
    finance = OrderPlanFinance.objects.select_for_update().get(order_plan=plan)
    if finance.statement_submitted_at:
        return quote(user, plan_id)
    policy = MerchantPricePolicy.objects.filter(account=account).first()
    if initialized_prices_only and (not policy or edits):
        raise OrderFinanceError("请在 Web 端初始化商家档位或维护专用单价。")
    if policy and policy.tier != tier:
        raise OrderFinanceError("商家档位已确定，请刷新后重试。")
    if tier not in TIERS or not isinstance(edits, dict) or not isinstance(expected, dict):
        raise OrderFinanceError("请选择商家档位并确认清单。")
    overrides = dict(policy.overrides) if policy else {}
    current = _lines(plan, tier, overrides)
    current_prices = {line["price_key"]: line["unit_price"] for line in current}
    if current_prices != expected:
        raise OrderFinanceError("默认单价已更新，请重新查看清单。")
    if any(price is None for price in current_prices.values()):
        raise OrderFinanceError("有设备或配件尚未初始化该档位的价格，请先维护基础价目表。")
    if not set(edits) <= current_prices.keys():
        raise OrderFinanceError("清单中没有该设备型号或配件规格。")
    for key, value in edits.items():
        price = _price(value)
        if price != current_prices[key]:
            overrides[key] = price
    lines = []
    for line in current:
        price = overrides.get(line["price_key"], line["unit_price"])
        lines.append(_with_price(line, price))
    total = sum((Decimal(line["subtotal"]) for line in lines), Decimal("0.00"))
    if not 0 < total <= MAX_AMOUNT:
        raise OrderFinanceError("订单应收合计须大于零且不能超过金额上限。")
    MerchantPricePolicy.objects.update_or_create(account=account, defaults={"tier": tier, "overrides": overrides})
    finance.priced_lines = lines
    finance.pricing_version = f"{tier}-01" if overrides else tier
    finance.receivable_amount = total
    finance.statement_submitted_by = user
    finance.statement_submitted_at = timezone.now()
    finance.save(update_fields=("priced_lines", "pricing_version", "receivable_amount",
                                "statement_submitted_by", "statement_submitted_at", "updated_at"))
    _complete(plan, "statement", user)
    _reconcile(finance, user)
    return quote(user, plan_id)
