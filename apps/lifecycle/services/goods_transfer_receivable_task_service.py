"""Move payment operations to the newest priced shipment, preserving history."""
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from workflow.models import WorkItem


def _upload_key(active, sequence_no):
    from .goods_transfer_settlement_service import _payment_upload_key
    base = _payment_upload_key(active.transfer, sequence_no)
    latest = WorkItem.objects.filter(
        Q(uniq_key=base) | Q(uniq_key__startswith=base + ":")
    ).order_by("-pk").first()
    if latest is None or latest.status == "OPEN":
        return latest.uniq_key if latest else base
    # A task suppressed while an older voucher awaited confirmation is not
    # erased/reopened. Resume in a new auditable child of the same inbox card.
    return f"{base}:resume:{latest.pk}"


def _retire(workitem, active, reason):
    now = timezone.now()
    payload = dict(workitem.payload or {})
    payload.update(finance_closed_reason=reason,
                   finance_entry_transfer_id=active.transfer_id,
                   finance_closed_at=now.isoformat())
    if reason == "FINANCE_ROLLED_FORWARD":
        title = "本单未结款项已转至后续发货单办理"
        summary = f"请在 {active.transfer.flow_no} 办理累计货款，原收款记录保留"
        payload["rolled_forward_to_transfer_id"] = active.transfer_id
    elif reason == "FINANCE_WAITING_CONFIRMATION":
        title = "付款凭证待确认，暂不重复提交"
        summary = "发货方确认现有凭证后，按剩余累计余额继续付款"
    else:
        title = "累计应收已结清"
        summary = "实际到账与已关联退货冲抵已覆盖累计应收"
    changed = WorkItem.objects.filter(pk=workitem.pk, status="OPEN").update(
        status="DONE", done_at=now, done_by_openid="", last_activity_at=now,
        payload=payload, updated_at=now,
    )
    if changed:
        workitem.user_tasks.exclude(state="DONE").update(
            state="DONE", need_action=False, done_at=now, done_reason=reason,
            display_title_snapshot=title, display_subtitle_snapshot=summary,
            cursor_time=now, updated_at=now,
        )
    return changed


@transaction.atomic
def reconcile_receivable_tasks(settlement, actor_openid=""):
    from .goods_transfer_receivable_service import (
        lock_receivable_account, pair_context, refresh_active_settlement,
    )
    from .goods_transfer_settlement_service import (
        PAYMENT_UPLOAD_WORK_TYPE, PAYMENT_CONFIRM_WORK_TYPE,
        _payment_confirm_key, _publish_payment_upload_task,
        _publish_payment_confirm_task,
    )
    from django.db.models import Max

    account = lock_receivable_account(settlement.transfer)
    refresh_active_settlement(account, transfer=settlement.transfer)
    context = pair_context(settlement)
    active = context["active_settlement"]
    if active is None:
        return {"closed": 0, "created": 0, "active_transfer_id": None}
    ids = [row["settlement"].transfer_id for row in context["rows"]]
    pending = list(context["pending_payments"])
    desired = {}
    for payment in pending:
        desired[_payment_confirm_key(active.transfer, payment)] = payment
    sequence_no = (active.payments.aggregate(value=Max("sequence_no"))["value"] or 0) + 1
    upload_key = None
    if not pending and context["outstanding_amount"] > 0:
        upload_key = _upload_key(active, sequence_no)
        desired[upload_key] = None
    active_biz_ref = f"lifecycle.GoodsTransfer:{active.transfer_id}"
    finance_tasks = WorkItem.objects.select_for_update().filter(
        Q(biz_ref__in=[f"lifecycle.GoodsTransfer:{pk}" for pk in ids])
        | Q(payload__goods_transfer_id__in=ids),
        type__in=(PAYMENT_UPLOAD_WORK_TYPE, PAYMENT_CONFIRM_WORK_TYPE),
        status="OPEN",
    )
    closed = 0
    for workitem in finance_tasks:
        if workitem.uniq_key in desired:
            continue
        is_old = workitem.biz_ref != active_biz_ref
        reason = ("FINANCE_ROLLED_FORWARD" if is_old else
                  "FINANCE_WAITING_CONFIRMATION" if pending else "FINANCE_SETTLED")
        closed += _retire(workitem, active, reason)
    created = 0
    for key, payment in desired.items():
        existing = WorkItem.objects.filter(uniq_key=key, status="OPEN").first()
        if existing:
            if payment is None:
                summary = f"累计待付 ¥{context['outstanding_amount']:.2f}，包含前单未收余额"
                if existing.summary != summary:
                    WorkItem.objects.filter(pk=existing.pk).update(summary=summary)
                    existing.user_tasks.exclude(state="DONE").update(display_subtitle_snapshot=summary)
            continue
        if payment is None:
            row = _publish_payment_upload_task(
                active, sequence_no, actor_openid,
                task_key=key, outstanding=context["outstanding_amount"],
            )
        else:
            row = _publish_payment_confirm_task(payment, actor_openid, entry_settlement=active)
        created += int(row is not None)
    return {"closed": closed, "created": created, "active_transfer_id": active.transfer_id}
