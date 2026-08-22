"""归档全量退货库存订单的旧生产任务，并补齐打印/发货入口。

默认只预览：
    venv/bin/python manage.py shell \
        < scripts/reconcile_full_reissue_order_tasks_20260820.py

确认数据库备份后执行：
    APPLY_FULL_REISSUE_RECONCILE=1 venv/bin/python manage.py shell \
        < scripts/reconcile_full_reissue_order_tasks_20260820.py

本脚本不删除 WorkItem/UserTask，也不改写历史操作人字段；被新规则淘汰的
旧任务只从当前收件箱归档。已经发货、已经入库、已取消的订单不会处理。
"""

import os

from django.db import transaction
from django.db.models import IntegerField, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from sales.models import OrderPlan
from sales.services.order_plan_reissue_service import (
    mark_return_inventory_ready_to_ship,
)
from sales.services.order_qrcode_print_service import (
    publish_stock_outbound_preparation_task,
    publish_stock_outbound_task_if_ready,
)
from workflow.models import WorkItem


APPLY = os.environ.get("APPLY_FULL_REISSUE_RECONCILE") == "1"
OBSOLETE_TASK_TYPES = {
    "ORDER_PLAN_PRODUCTION_CONFIRM",
    "ORDER_PLAN_CREATED_PRODUCTION_NOTICE",
    "ORDER_PLAN_SHIPMENT_AUTHORIZATION",
}


plans = list(
    OrderPlan.objects.select_related("created_by")
    .annotate(
        production_total=Coalesce(
            Sum("items__production_quantity"),
            Value(0),
            output_field=IntegerField(),
        ),
        return_inventory_total=Coalesce(
            Sum("items__return_inventory_quantity"),
            Value(0),
            output_field=IntegerField(),
        ),
    )
    .filter(production_total=0, return_inventory_total__gt=0)
    .exclude(
        status__in=(
            OrderPlan.Status.SHIPPED,
            OrderPlan.Status.RECEIVED,
            OrderPlan.Status.CANCELLED,
        )
    )
    .order_by("id")
)

print("===== 全量退货库存订单任务对账 =====")
print({"mode": "APPLY" if APPLY else "DRY_RUN", "candidate_count": len(plans)})

for candidate in plans:
    ref = f"sales.OrderPlan:{candidate.pk}"
    obsolete = list(
        WorkItem.objects.filter(
            biz_ref=ref,
            type__in=OBSOLETE_TASK_TYPES,
        ).order_by("id")
    )
    print(
        {
            "order_plan_id": candidate.pk,
            "plan_code": candidate.plan_code,
            "status": candidate.status,
            "return_inventory_quantity": candidate.return_inventory_total,
            "obsolete_workitems": [
                {"id": row.pk, "type": row.type, "status": row.status}
                for row in obsolete
            ],
        }
    )
    if not APPLY:
        continue

    with transaction.atomic():
        plan = (
            OrderPlan.objects.select_for_update()
            .select_related(
                "created_by",
                "factory_organization",
                "merchant_organization",
                "brand",
            )
            .get(pk=candidate.pk)
        )
        now = timezone.now()
        plan.status = OrderPlan.Status.PRODUCTION_COMPLETED
        update_fields = ["status", "updated_at"]
        if not plan.shipment_authorized_at or not plan.shipment_authorized_by_id:
            plan.shipment_authorized_at = now
            plan.shipment_authorized_by = plan.created_by
            update_fields.extend(
                ("shipment_authorized_at", "shipment_authorized_by")
            )
        plan.save(update_fields=tuple(update_fields))
        plan.items.update(
            status=OrderPlan.Status.PRODUCTION_COMPLETED,
            updated_at=now,
        )
        mark_return_inventory_ready_to_ship(plan=plan)

        obsolete_qs = WorkItem.objects.select_for_update().filter(
            biz_ref=ref,
            type__in=OBSOLETE_TASK_TYPES,
        )
        obsolete_ids = list(obsolete_qs.values_list("id", flat=True))
        obsolete_qs.filter(status="OPEN").update(
            status="CANCELLED",
            last_activity_at=now,
            updated_at=now,
        )
        for workitem in WorkItem.objects.filter(pk__in=obsolete_ids):
            workitem.user_tasks.update(
                state="DONE",
                need_action=False,
                done_at=now,
                done_reason="RECONCILED_FULL_REISSUE",
                badge_group="ARCHIVE",
                cursor_time=now,
                updated_at=now,
            )

        qrcodes_ready = not plan.items.filter(
            qrcode_printed_at__isnull=True
        ).exists()
        if qrcodes_ready:
            publish_stock_outbound_task_if_ready(plan)
        else:
            publish_stock_outbound_preparation_task(plan=plan)

    print(
        {
            "plan_code": candidate.plan_code,
            "result": "reconciled",
            "qrcodes_ready": qrcodes_ready,
        }
    )

if not APPLY:
    print(
        "当前为预览模式，未修改数据。确认备份后设置 "
        "APPLY_FULL_REISSUE_RECONCILE=1 再执行。"
    )
