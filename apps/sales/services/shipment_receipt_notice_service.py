"""End superseded shipment notices without acknowledging independent tasks."""

from django.db import transaction
from django.utils import timezone

from sales.models import OrderPlanShipment
from workflow.models import UserTask, WorkItem


MERCHANT_RECEIPT_ROLES = ("merchant_owner", "merchant_sales", "merchant_stock")


def pending_received_shipment_notices(shipment):
    if shipment.status != OrderPlanShipment.Status.RECEIVED or not shipment.received_at:
        return UserTask.objects.none()
    prefix = f"sales:order-plan:{shipment.order_plan_id}:shipment:{shipment.pk}"
    # The receipt task is the dispatch-time roster, including the verified
    # inbox binding repairs after login-slot changes. Never infer recipients
    # from today's factory/merchant role membership alone.
    recipients = UserTask.objects.filter(
        workitem__uniq_key=f"{prefix}:merchant-receipt",
        workitem__type="ORDER_PLAN_V2_MERCHANT_RECEIPT",
        to_role_snapshot__in=MERCHANT_RECEIPT_ROLES,
    ).values("to_openid")
    notices = WorkItem.objects.filter(
        uniq_key=f"{prefix}:shipped-notice",
        type="ORDER_PLAN_V2_SHIPPED_NOTICE",
        biz_ref=f"sales.OrderPlan:{shipment.order_plan_id}",
        payload__shipment_id=shipment.pk,
    )
    return UserTask.objects.filter(
        workitem__in=notices, to_openid__in=recipients,
        to_role_snapshot__in=MERCHANT_RECEIPT_ROLES,
        need_action=False, state__in=("NEW", "READ"),
    )


@transaction.atomic
def close_received_shipment_notices(shipment):
    # The caller holds the shipment lock. Repeated receipt and repair calls
    # leave completed rows and every historical read timestamp unchanged.
    tasks = pending_received_shipment_notices(shipment)
    return tasks.update(
        state="DONE", done_at=timezone.now(), done_reason="OTHER_ACTION",
        display_subtitle_snapshot="本批已完成商家入库，发货提醒已结束",
        cursor_time=shipment.received_at, updated_at=timezone.now(),
    )
