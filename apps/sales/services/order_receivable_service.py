"""Directed factory/merchant rolling receivables and conserved cash allocations.

Original invoice and payment facts are never reparented. Read helpers are pure;
write helpers require a transaction and serialize each pair, including SQLite.
Excess confirmed cash remains credit until another eligible order needs it.
"""
from decimal import Decimal
from functools import wraps

from django.db import OperationalError, connections
from django.db.models import Sum
from django.utils import timezone

from sales.models import (
    OrderPlanFinance, OrderPlanPayment, OrderPlanPaymentAllocation,
    OrderPlanReceivableAccount,
)

ZERO = Decimal("0.00")
PENDING_CANCELLATIONS = ("WAITING_PRODUCTION_COMPLETION", "WAITING_FINISHED_STOCK")


class OrderReceivableError(Exception):
    pass


def financial_write_errors(error_type):
    """Translate only after the caller's atomic block has rolled back."""
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except OrderReceivableError as exc:
                raise error_type(str(exc)) from exc
            except OperationalError as exc:
                if connections["default"].vendor != "sqlite" or "locked" not in str(exc).lower():
                    raise
                raise error_type("账款正在由其他协作成员处理，请刷新后重试。") from exc
        return wrapped
    return decorate


def _pair(plan):
    return {
        "factory_organization_id": plan.factory_organization_id,
        "merchant_organization_id": plan.merchant_organization_id,
    }


def _eligible_pair(factory_id, merchant_id):
    return (
        OrderPlanFinance.objects.select_related(
            "order_plan__factory_organization", "order_plan__merchant_organization"
        ).filter(
            order_plan__factory_organization_id=factory_id,
            order_plan__merchant_organization_id=merchant_id,
            statement_submitted_at__isnull=False,
            receivable_amount__isnull=False,
        ).exclude(order_plan__status="CANCELLED")
        .exclude(order_plan__cancellation_audit__status__in=PENDING_CANCELLATIONS)
        .order_by("order_plan__submitted_at", "order_plan_id")
    )


def eligible_finances(plan):
    """Oldest order first; accounted orders remain part of the same account."""
    return _eligible_pair(plan.factory_organization_id, plan.merchant_organization_id)


def _require_atomic(alias="default"):
    if not connections[alias].in_atomic_block:
        raise OrderReceivableError("订单累计往来账写入必须在数据库事务中执行。")


def lock_receivable_account(plan):
    alias = plan._state.db or "default"
    _require_atomic(alias)
    if plan.factory_organization_id == plan.merchant_organization_id:
        raise OrderReceivableError("厂家与商家不能为同一组织。")
    query = OrderPlanReceivableAccount.objects.using(alias)
    pair = _pair(plan)
    # A zero-row UPDATE still obtains a SQLite write reservation before INSERT.
    # Updating a column to itself preserves the audit timestamp for a no-op sync.
    from django.db.models import F
    query.filter(**pair).update(updated_at=F("updated_at"))
    account, _ = query.get_or_create(**pair)
    return query.select_for_update().get(pk=account.pk)


def _confirmed_payments(finance_ids):
    return OrderPlanPayment.objects.filter(
        finance_id__in=finance_ids, status=OrderPlanPayment.Status.CONFIRMED
    )


def _invoice_rows(finances):
    finances = list(finances)
    ids = [finance.pk for finance in finances]
    if not ids:
        return []
    allocated = dict(
        OrderPlanPaymentAllocation.objects.filter(
            finance_id__in=ids, released_at__isnull=True,
            payment__status=OrderPlanPayment.Status.CONFIRMED,
        ).order_by().values("finance_id").annotate(total=Sum("amount"))
        .values_list("finance_id", "total")
    )
    payments = list(_confirmed_payments(ids).order_by("id"))
    cash_used = dict(
        OrderPlanPaymentAllocation.objects.filter(
            payment_id__in=[p.pk for p in payments], released_at__isnull=True,
        ).order_by().values("payment_id").annotate(total=Sum("amount"))
        .values_list("payment_id", "total")
    )
    unused = {finance_id: ZERO for finance_id in ids}
    for payment in payments:
        amount = payment.confirmed_amount or ZERO
        unused[payment.finance_id] += max(amount - (cash_used.get(payment.pk) or ZERO), ZERO)
    rows = []
    for finance in finances:
        received = allocated.get(finance.pk) or ZERO
        own_unused = unused[finance.pk]
        charge = finance.receivable_amount
        rows.append({
            "finance": finance,
            "receivable_amount": charge,
            "allocated_received_amount": received,
            "unapplied_amount": own_unused,
            "received_amount": received + own_unused,
            "outstanding_amount": None if charge is None else max(charge - received, ZERO),
            "overpaid_amount": own_unused,
        })
    return rows


def invoice_totals(finance):
    """Original invoice debt and allocated cash, retaining own unused cash.

    A sole invoice of 100 paid 110 still displays received=110 / overpaid=10.
    When the extra 10 funds a later invoice it becomes an explicit allocation.
    """
    return _invoice_rows([finance])[0]


def pair_context(finance):
    """Read-only balances; no task, account, payment, or allocation changes."""
    plan = finance.order_plan
    finances = list(eligible_finances(plan))
    ids = [row.pk for row in finances]
    rows = _invoice_rows(finances)
    payments = OrderPlanPayment.objects.filter(finance_id__in=ids)
    own_cash = payments.filter(status=OrderPlanPayment.Status.CONFIRMED).aggregate(
        total=Sum("confirmed_amount")
    )["total"] or ZERO
    # Cancelled-origin cash already funding another eligible order remains valid.
    retained_cash = OrderPlanPaymentAllocation.objects.filter(
        finance_id__in=ids, released_at__isnull=True,
        payment__status=OrderPlanPayment.Status.CONFIRMED,
    ).exclude(payment__finance_id__in=ids).aggregate(total=Sum("amount"))["total"] or ZERO
    received = own_cash + retained_cash
    charge = sum((row["receivable_amount"] or ZERO for row in rows), ZERO)
    active = finances[-1] if finances else None
    return {
        "account": OrderPlanReceivableAccount.objects.filter(**_pair(plan)).first(),
        "active_finance": active,
        "rows": rows,
        "receivable_amount": charge,
        "received_amount": received,
        "outstanding_amount": max(charge - received, ZERO),
        "overpaid_amount": max(received - charge, ZERO),
        "unapplied_amount": sum((row["unapplied_amount"] for row in rows), ZERO),
        "carry_forward_amount": sum((row["outstanding_amount"] or ZERO for row in rows[:-1]), ZERO),
        "pending_payments": list(payments.filter(status=OrderPlanPayment.Status.PENDING)
            .select_related("finance__order_plan", "uploaded_by")
            .order_by("uploaded_at", "id")),
    }


def synchronize_receivable_account(account, *, plan=None):
    """Release cancelled target debt, then use available cash oldest debt first.

    Cash whose original order is cancelled/pending cancellation is held. Its
    already-live allocations to other eligible invoices are never unwound.
    All released allocations stay queryable with amount and original timestamps.
    """
    alias = account._state.db or "default"
    _require_atomic(alias)
    if plan is not None and _pair(plan) != {
        "factory_organization_id": account.factory_organization_id,
        "merchant_organization_id": account.merchant_organization_id,
    }:
        raise OrderReceivableError("订单与累计往来账的组织不一致。")
    finances = list(_eligible_pair(account.factory_organization_id, account.merchant_organization_id))
    eligible_ids = {finance.pk for finance in finances}
    all_ids = list(OrderPlanFinance.objects.filter(
        order_plan__factory_organization_id=account.factory_organization_id,
        order_plan__merchant_organization_id=account.merchant_organization_id,
    ).values_list("pk", flat=True))
    changed = False
    released = OrderPlanPaymentAllocation.objects.filter(
        finance_id__in=all_ids, released_at__isnull=True,
    ).exclude(finance_id__in=eligible_ids).update(
        released_at=timezone.now(), release_reason="TARGET_ORDER_INELIGIBLE",
    )
    changed = bool(released)
    payments = list(_confirmed_payments(all_ids).select_for_update().order_by("confirmed_at", "id"))
    by_payment = {payment.pk: [] for payment in payments}
    all_allocations = list(OrderPlanPaymentAllocation.objects.filter(
        payment_id__in=by_payment, released_at__isnull=True,
    ).select_related("finance__order_plan").order_by("id"))
    for allocation in all_allocations:
        target = allocation.finance.order_plan
        if (target.factory_organization_id != account.factory_organization_id
                or target.merchant_organization_id != account.merchant_organization_id):
            raise OrderReceivableError("历史收款分配涉及不同往来组织，请核实后处理。")
        by_payment[allocation.payment_id].append(allocation)
    rows = _invoice_rows(finances)
    remaining_debt = {}
    for row in rows:
        amount = row["receivable_amount"]
        if amount is None or amount <= ZERO or row["allocated_received_amount"] > amount:
            raise OrderReceivableError("历史订单应收与收款分配不一致，请核实后处理。")
        remaining_debt[row["finance"].pk] = amount - row["allocated_received_amount"]
    created = []
    for payment in payments:
        amount = payment.confirmed_amount
        if amount is None or not amount.is_finite() or amount <= ZERO:
            raise OrderReceivableError(f"已确认付款金额无效，请核实付款记录 {payment.pk}。")
        existing = by_payment[payment.pk]
        used = sum((allocation.amount for allocation in existing), ZERO)
        if used > amount:
            raise OrderReceivableError(f"付款记录 {payment.pk} 的分配金额超过实际到账。")
        if payment.finance_id not in eligible_ids:
            continue
        available = amount - used
        for target in finances:
            value = min(available, remaining_debt[target.pk])
            if value <= ZERO:
                continue
            existing_target = next((a for a in existing if a.finance_id == target.pk), None)
            if existing_target is not None:
                # Cancellation elsewhere can free more of this same payment.
                # Retain the original allocation amount/timestamp, then create
                # its larger live successor instead of overwriting history.
                existing_target.released_at = timezone.now()
                existing_target.release_reason = "REALLOCATED_INCREMENT"
                existing_target.save(update_fields=("released_at", "release_reason"))
                allocation = OrderPlanPaymentAllocation.objects.create(
                    payment=payment, finance=target,
                    amount=existing_target.amount + value,
                    allocated_at=timezone.now(),
                )
                created.append(allocation)
                existing.remove(existing_target)
                existing.append(allocation)
            else:
                allocation = OrderPlanPaymentAllocation.objects.create(
                    payment=payment, finance=target, amount=value,
                    allocated_at=timezone.now(),
                )
                created.append(allocation)
                existing.append(allocation)
            changed = True
            available -= value
            remaining_debt[target.pk] -= value
            if available == ZERO:
                break
    active = finances[-1] if finances else None
    active_id = active.pk if active else None
    if account.active_finance_id != active_id:
        account.active_finance = active
        changed = True
    if changed:
        account.save(update_fields=("active_finance", "updated_at"))
    # Conservation: eligible cash + retained cancelled-origin cash equals all
    # live eligible debt allocations plus available excess eligible cash.
    if active is not None:
        context = pair_context(active)
        allocated = sum((row["allocated_received_amount"] for row in context["rows"]), ZERO)
        if allocated + context["unapplied_amount"] != context["received_amount"]:
            raise OrderReceivableError("订单累计收款与分配明细不一致，请核实后处理。")
    return {
        "active_finance": active, "released": released,
        "allocations_created": created, "changed": changed,
    }


def allocate_payment_fifo(payment):
    """Synchronize newly confirmed cash without changing its immutable origin."""
    _require_atomic(payment._state.db or "default")
    if payment.status != OrderPlanPayment.Status.CONFIRMED:
        raise OrderReceivableError("请先确认本笔实际到账金额。")
    account = lock_receivable_account(payment.finance.order_plan)
    synchronize_receivable_account(account, plan=payment.finance.order_plan)
    return list(payment.allocations.filter(released_at__isnull=True).order_by("allocated_at", "id"))
