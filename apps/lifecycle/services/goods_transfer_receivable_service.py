"""Directed merchant receivables with explicit payment-to-invoice allocation.

Physical receipt and the original invoice/payment records remain independent.
Read helpers never create rows or change tasks; mutating helpers require the
caller's atomic transaction and serialize all writes for a seller/buyer pair.
"""

from decimal import Decimal
from functools import wraps

from django.db import connections
from django.db import OperationalError
from django.db.models import Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from lifecycle.models import (
    GoodsTransfer,
    GoodsTransferCreditAllocation,
    GoodsTransferPayment,
    GoodsTransferPaymentAllocation,
    GoodsTransferReceivableAccount,
    GoodsTransferSettlement,
)


ZERO = Decimal("0.00")


class GoodsTransferReceivableError(Exception):
    pass


def financial_write_errors(error_type):
    """Let the atomic block roll back before translating a concurrent write.

    SQLite cannot upgrade an older read snapshot while another writer commits.
    The client can safely retry with the same idempotency key; never continue a
    financial operation after a failed reservation.
    """
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except GoodsTransferReceivableError as exc:
                raise error_type(str(exc)) from exc
            except OperationalError as exc:
                if "locked" not in str(exc).lower() or connections["default"].vendor != "sqlite":
                    raise
                raise error_type("账款正在由其他协作成员处理，请刷新后重试。") from exc
        return wrapped
    return decorate


def _eligible_pair(seller_id, buyer_id):
    return (
        GoodsTransferSettlement.objects.select_related(
            "transfer__from_organization", "transfer__to_organization"
        )
        .filter(
            transfer__flow_type=GoodsTransfer.FlowType.SHIPMENT,
            transfer__from_organization_id=seller_id,
            transfer__to_organization_id=buyer_id,
            statement_submitted_at__isnull=False,
        )
        .exclude(
            transfer__status__in=(
                GoodsTransfer.Status.SCANNING,
                GoodsTransfer.Status.CANCELLED,
            )
        )
        .annotate(
            receivable_order_at=Coalesce("transfer__submitted_at", "transfer__created_at")
        )
        .order_by("receivable_order_at", "transfer_id")
    )


def eligible_settlements(transfer):
    """Return priced shipments in this directed pair, oldest shipment first.

    Statement edits or late submission of an older shipment cannot make that
    older shipment the new payment entry. Unpriced, cancelled, return and
    sibling-transfer business records do not participate in this account.
    """
    if transfer.flow_type != GoodsTransfer.FlowType.SHIPMENT:
        return GoodsTransferSettlement.objects.none()
    return _eligible_pair(transfer.from_organization_id, transfer.to_organization_id)


def _require_atomic(alias="default"):
    if not connections[alias].in_atomic_block:
        raise GoodsTransferReceivableError("累计往来账写入必须在数据库事务中执行。")


def lock_receivable_account(transfer):
    """Reserve the pair for an atomic write before any balance is read.

    The initial UPDATE also obtains SQLite's write reservation; SQLite ignores
    SELECT FOR UPDATE. PostgreSQL/MySQL additionally lock the pair row. Even a
    missing pair performs the write first, then creates its unique account.
    """
    alias = transfer._state.db or "default"
    _require_atomic(alias)
    if transfer.flow_type != GoodsTransfer.FlowType.SHIPMENT:
        raise GoodsTransferReceivableError("只有向下级发货才建立累计应收账。")
    if transfer.from_organization_id == transfer.to_organization_id:
        raise GoodsTransferReceivableError("发货方与收货方不能相同。")
    query = GoodsTransferReceivableAccount.objects.using(alias)
    pair = {
        "seller_organization_id": transfer.from_organization_id,
        "buyer_organization_id": transfer.to_organization_id,
    }
    query.filter(**pair).update(updated_at=timezone.now())
    account, _ = query.get_or_create(**pair)
    return query.select_for_update().get(pk=account.pk)


def refresh_active_settlement(account, *, transfer=None):
    """After a statement/cancellation change, persist the newest eligible invoice.

    The caller must hold this account's lock. ``transfer`` is accepted to allow
    call sites to assert that they are operating within the already locked pair.
    """
    _require_atomic(account._state.db or "default")
    if transfer is not None and (
        transfer.flow_type != GoodsTransfer.FlowType.SHIPMENT
        or transfer.from_organization_id != account.seller_organization_id
        or transfer.to_organization_id != account.buyer_organization_id
    ):
        raise GoodsTransferReceivableError("发货单与累计往来账组织不一致。")
    active = _eligible_pair(
        account.seller_organization_id, account.buyer_organization_id
    ).last()
    active_id = active.pk if active else None
    if account.active_settlement_id != active_id:
        account.active_settlement = active
        account.save(update_fields=("active_settlement", "updated_at"))
    return active


def _invoice_rows(settlements, *, exclude_payment_id=None):
    """Compute many invoices with three aggregate queries, avoiding join fan-out."""
    settlements = list(settlements)
    settlement_ids = [settlement.pk for settlement in settlements]
    if not settlement_ids:
        return []
    allocated_query = GoodsTransferPaymentAllocation.objects.filter(
        settlement_id__in=settlement_ids,
        payment__status=GoodsTransferPayment.Status.CONFIRMED,
    )
    legacy_query = GoodsTransferPayment.objects.filter(
        settlement_id__in=settlement_ids,
        status=GoodsTransferPayment.Status.CONFIRMED,
        allocations__isnull=True,
    )
    if exclude_payment_id is not None:
        allocated_query = allocated_query.exclude(payment_id=exclude_payment_id)
        legacy_query = legacy_query.exclude(pk=exclude_payment_id)
    allocated = dict(
        allocated_query.order_by().values("settlement_id")
        .annotate(total=Sum("amount")).values_list("settlement_id", "total")
    )
    legacy = dict(
        legacy_query.order_by().values("settlement_id")
        .annotate(total=Sum("confirmed_amount")).values_list("settlement_id", "total")
    )
    credits = dict(
        GoodsTransferCreditAllocation.objects.filter(
            target_shipment_settlement_id__in=settlement_ids
        ).order_by().values("target_shipment_settlement_id")
        .annotate(total=Sum("amount"))
        .values_list("target_shipment_settlement_id", "total")
    )
    rows = []
    for settlement in settlements:
        received = (allocated.get(settlement.pk) or ZERO) + (legacy.get(settlement.pk) or ZERO)
        credit = credits.get(settlement.pk) or ZERO
        receivable = settlement.receivable_amount
        balance = None if receivable is None else receivable - received - credit
        rows.append({
            "settlement": settlement,
            "receivable_amount": receivable,
            "received_amount": received,
            "credit_amount": credit,
            "outstanding_amount": balance,
            "signed_balance": balance,
        })
    return rows


def invoice_totals(settlement, *, exclude_payment_id=None):
    """Invoice facts: original receivable minus allocated cash and return credits.

    Confirmed legacy payments lacking allocation rows are counted on their
    original invoice once. Excluding the payment currently being confirmed
    prevents its temporary legacy fallback from reducing the FIFO balance.
    Signed negative balances are retained for diagnosis, never clamped away.
    """
    return _invoice_rows([settlement], exclude_payment_id=exclude_payment_id)[0]


def pair_context(settlement, *, exclude_payment_id=None):
    """Read the directed pair's live totals and oldest-first invoice breakdown.

    The active entry is derived from eligible invoices, not a possibly stale
    cached pointer. No rows are created and no task/read state is changed.
    Payment totals are confirmed payment facts once per proof; invoice totals
    are the allocations received by each invoice, which can cross invoices.
    """
    transfer = settlement.transfer
    if transfer.flow_type != GoodsTransfer.FlowType.SHIPMENT:
        raise GoodsTransferReceivableError("只有向下级发货才读取累计应收账。")
    settlements = list(eligible_settlements(transfer))
    rows = _invoice_rows(settlements, exclude_payment_id=exclude_payment_id)
    settlement_ids = [invoice.pk for invoice in settlements]
    payments = GoodsTransferPayment.objects.filter(settlement_id__in=settlement_ids)
    if exclude_payment_id is not None:
        payments = payments.exclude(pk=exclude_payment_id)
    received = payments.filter(status=GoodsTransferPayment.Status.CONFIRMED).aggregate(
        total=Sum("confirmed_amount")
    )["total"] or ZERO
    receivable = sum((row["receivable_amount"] or ZERO for row in rows), ZERO)
    credit = sum((row["credit_amount"] for row in rows), ZERO)
    account = GoodsTransferReceivableAccount.objects.filter(
        seller_organization_id=transfer.from_organization_id,
        buyer_organization_id=transfer.to_organization_id,
    ).first()
    return {
        "account": account,
        "active_settlement": settlements[-1] if settlements else None,
        "rows": rows,
        "receivable_amount": receivable,
        "received_amount": received,
        "credit_amount": credit,
        "outstanding_amount": receivable - received - credit,
        "pending_payments": list(
            payments.filter(status=GoodsTransferPayment.Status.PENDING)
            .select_related("settlement__transfer", "uploaded_by")
            .order_by("uploaded_at", "id")
        ),
    }


def allocate_payment_fifo(payment):
    """Allocate a newly confirmed payment to oldest unpaid invoices atomically.

    A retry returns the existing complete allocation without altering it.
    An old pending proof keeps its original invoice even when a newer shipment
    has become the account's task entry. Historical allocations are immutable.
    """
    _require_atomic(payment._state.db or "default")
    transfer = payment.settlement.transfer
    lock_receivable_account(transfer)
    payment = GoodsTransferPayment.objects.select_for_update().select_related(
        "settlement__transfer"
    ).get(pk=payment.pk)
    if payment.status != GoodsTransferPayment.Status.CONFIRMED:
        raise GoodsTransferReceivableError("请先确认本笔实际到账金额。")
    amount = payment.confirmed_amount
    if amount is None or not amount.is_finite() or amount <= ZERO:
        raise GoodsTransferReceivableError("本笔实际到账金额必须大于0。")
    existing = list(payment.allocations.select_related("settlement__transfer").order_by("id"))
    if existing:
        if sum((allocation.amount for allocation in existing), ZERO) != amount:
            raise GoodsTransferReceivableError("本笔历史收款分配金额不一致，请核实后处理。")
        if any(
            allocation.settlement.transfer.flow_type != GoodsTransfer.FlowType.SHIPMENT
            or allocation.settlement.transfer.from_organization_id != transfer.from_organization_id
            or allocation.settlement.transfer.to_organization_id != transfer.to_organization_id
            for allocation in existing
        ):
            raise GoodsTransferReceivableError("本笔历史收款分配涉及不同往来组织，请核实后处理。")
        return existing
    context = pair_context(payment.settlement, exclude_payment_id=payment.pk)
    if payment.settlement_id not in {row["settlement"].pk for row in context["rows"]}:
        raise GoodsTransferReceivableError("本笔付款所属发货单尚未形成有效应收。")
    if any(
        row["outstanding_amount"] is None or row["outstanding_amount"] < ZERO
        for row in context["rows"]
    ):
        raise GoodsTransferReceivableError("历史发货应收余额异常，请核实后再确认收款。")
    invoice_balance = sum((row["outstanding_amount"] for row in context["rows"]), ZERO)
    if invoice_balance != context["outstanding_amount"]:
        raise GoodsTransferReceivableError("历史收款分配与累计余额不一致，请核实后处理。")
    if amount > context["outstanding_amount"]:
        raise GoodsTransferReceivableError("本笔实际到账金额不能超过双方累计未收余额。")
    remaining = amount
    allocations = []
    allocated_at = payment.confirmed_at or timezone.now()
    for row in context["rows"]:
        value = min(remaining, row["outstanding_amount"])
        if value > ZERO:
            allocations.append(GoodsTransferPaymentAllocation(
                payment=payment,
                settlement=row["settlement"],
                amount=value,
                allocated_at=allocated_at,
            ))
            remaining -= value
        if remaining == ZERO:
            break
    if remaining != ZERO:
        raise GoodsTransferReceivableError("本笔实际到账金额未能完整分配，请刷新后重试。")
    GoodsTransferPaymentAllocation.objects.bulk_create(allocations)
    return allocations
