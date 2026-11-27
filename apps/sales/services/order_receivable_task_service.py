"""Project one organization pair's financial operations onto its newest order."""
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from sales.models import OrderPlanFinance
from workflow.models import WorkItem
from workflow.services.workflow_service import publish_workitem

from .order_receivable_service import (
    lock_receivable_account, pair_context, synchronize_receivable_account,
)


PAYMENT_TYPES = ("ORDER_PLAN_PAYMENT_UPLOAD", "ORDER_PLAN_PAYMENT_CONFIRM")


def _available_key(base):
    latest = WorkItem.objects.filter(
        Q(uniq_key=base) | Q(uniq_key__startswith=base + ":resume:")
    ).order_by("pk").last()
    if latest is None or latest.status == "OPEN":
        return latest.uniq_key if latest else base
    return f"{base}:resume:{latest.pk}"


def _upload_key(plan, sequence):
    from .order_finance_service import _key
    # Keep an existing compatible active task, including an older key spelling.
    previous = WorkItem.objects.filter(
        biz_ref=f"sales.OrderPlan:{plan.pk}", type=PAYMENT_TYPES[0],
        payload__sequence_no=sequence, status="OPEN",
    ).order_by("pk").first()
    return previous.uniq_key if previous else _available_key(_key(plan, f"payment:{sequence}:upload"))


def _confirmation_key(plan, payment):
    from .order_finance_service import _key
    previous = WorkItem.objects.filter(
        biz_ref=f"sales.OrderPlan:{plan.pk}", type=PAYMENT_TYPES[1],
        payload__payment_id=payment.pk, status="OPEN",
    ).order_by("pk").first()
    if previous:
        return previous.uniq_key
    # Sequence numbers restart on every original order; the immutable payment
    # ID distinguishes several old pending vouchers carried into the same card.
    return _available_key(_key(plan, f"payment-id:{payment.pk}:confirm"))


def _retire(work, active, reason):
    now = timezone.now()
    plan = active.order_plan if active else None
    payload = dict(work.payload or {})
    payload.update(finance_closed_reason=reason, finance_closed_at=now.isoformat(),
                   finance_entry_order_plan_id=plan.pk if plan else None)
    if reason == "FINANCE_ROLLED_FORWARD":
        title = "本单未结款项已转至后续订单办理"
        summary = f"请在 {plan.plan_code} 办理累计货款，原清单和付款记录保留"
        payload["rolled_forward_to_order_plan_id"] = plan.pk
    elif reason == "FINANCE_WAITING_CONFIRMATION":
        title = "付款凭证待确认，暂不重复提交"
        summary = "销售助理确认现有凭证后，按累计未收余额继续付款"
    elif reason == "FINANCE_ORDER_INACTIVE":
        title = "订单已取消或正在取消"
        summary = "本单停止财务操作，原始款项与凭证保留"
    else:
        title = "累计应收已结清"
        summary = "累计实际到账已覆盖当前有效订单应收"
    changed = WorkItem.objects.filter(pk=work.pk, status="OPEN").update(
        status="DONE", done_at=now, done_by_openid="", last_activity_at=now,
        payload=payload, updated_at=now,
    )
    if changed:
        work.user_tasks.exclude(state="DONE").update(
            state="DONE", need_action=False, done_at=now, done_reason=reason,
            display_title_snapshot=title, display_subtitle_snapshot=summary,
            cursor_time=now, updated_at=now,
        )
    return changed


def _publish(active, key, *, sequence, outstanding, payment=None, user=None):
    from .order_finance_service import _recipients
    plan = active.order_plan
    payload = {
        "order_plan_id": plan.pk, "plan_code": plan.plan_code,
        "factory_id": plan.factory_organization_id,
        "merchant_id": plan.merchant_organization_id,
        "inbox_group_key": f"order-plan:{plan.pk}",
        "inbox_group_kind": "ORDER_PLAN",
        "inbox_group_label": f"订单计划：{plan.plan_code}",
        "sequence_no": payment.sequence_no if payment else sequence,
    }
    if payment:
        payload.update(payment_id=payment.pk, payment_source_order_plan_id=payment.finance.order_plan_id,
                       payment_source_plan_code=payment.finance.order_plan.plan_code)
        title = f"第{payment.sequence_no}笔收款待确认"
        summary = f"凭证来源 {payment.finance.order_plan.plan_code}；确认实际到账后按较早欠款优先分配"
    else:
        title = f"订单第{sequence}笔付款待处理"
        summary = f"累计待付 ¥{outstanding:.2f}，包含前单未收余额"
    return publish_workitem(
        domain="SALES", type=PAYMENT_TYPES[1] if payment else PAYMENT_TYPES[0],
        biz_id=plan.plan_code, biz_ref=f"sales.OrderPlan:{plan.pk}", uniq_key=key,
        title=title, summary=summary,
        link=(f"/pages/sales/order_plan_detail/order_plan_detail?order_plan_id={plan.pk}" if payment
              else "/pages/sales/factory_payments/factory_payments"),
        recipients_openids=_recipients(plan, "factory_sales_assistant" if payment else "merchant_owner"),
        need_action=True, action_mode="ANYONE_DO", payload=payload,
        actor_openid=getattr(user, "wx_openid", "") or "",
        mark_workitem_done_immediately=False,
    )


@transaction.atomic
def reconcile_order_receivable_tasks(finance, user=None):
    account = lock_receivable_account(finance.order_plan)
    sync = synchronize_receivable_account(account, plan=finance.order_plan)
    context = pair_context(finance)
    active = context["active_finance"]
    pending = context["pending_payments"]
    pair_orders = list(OrderPlanFinance.objects.filter(
        order_plan__factory_organization_id=finance.order_plan.factory_organization_id,
        order_plan__merchant_organization_id=finance.order_plan.merchant_organization_id,
    ).values_list("order_plan_id", flat=True))
    desired = {}
    sequence = 1
    if active:
        sequence = (active.payments.aggregate(value=Max("sequence_no"))["value"] or 0) + 1
        if pending:
            desired = {_confirmation_key(active.order_plan, payment): payment for payment in pending}
        elif context["outstanding_amount"] > 0:
            desired[_upload_key(active.order_plan, sequence)] = None

    tasks = WorkItem.objects.select_for_update().filter(
        biz_ref__in=[f"sales.OrderPlan:{pk}" for pk in pair_orders],
        type__in=PAYMENT_TYPES, status="OPEN",
    )
    closed = 0
    active_ref = f"sales.OrderPlan:{active.order_plan_id}" if active else None
    eligible_ids = {row["finance"].order_plan_id for row in context["rows"]}
    for task in tasks:
        if task.uniq_key in desired:
            continue
        source_id = task.payload.get("payment_source_order_plan_id") if isinstance(task.payload, dict) else None
        task_order_id = int(task.biz_ref.rsplit(":", 1)[-1])
        if not active or task_order_id not in eligible_ids or (source_id and source_id not in eligible_ids):
            reason = "FINANCE_ORDER_INACTIVE"
        elif task.biz_ref != active_ref:
            reason = "FINANCE_ROLLED_FORWARD"
        elif pending:
            reason = "FINANCE_WAITING_CONFIRMATION"
        else:
            reason = "FINANCE_SETTLED"
        closed += _retire(task, active, reason)
    created = 0
    for key, payment in desired.items():
        existing = WorkItem.objects.filter(uniq_key=key, status="OPEN").first()
        if existing:
            if payment is None:
                WorkItem.objects.filter(pk=existing.pk).exclude(link="/pages/sales/factory_payments/factory_payments").update(
                    link="/pages/sales/factory_payments/factory_payments",
                )
                summary = f"累计待付 ¥{context['outstanding_amount']:.2f}，包含前单未收余额"
                if existing.summary != summary:
                    WorkItem.objects.filter(pk=existing.pk).update(summary=summary)
                    existing.user_tasks.exclude(state="DONE").update(display_subtitle_snapshot=summary)
            continue
        _publish(active, key, sequence=sequence, outstanding=context["outstanding_amount"], payment=payment, user=user)
        created += 1
    return {"closed": closed, "created": created,
            "active_order_plan_id": active.order_plan_id if active else None,
            "allocations_released": sync["released"],
            "allocations_created": len(sync["allocations_created"])}
