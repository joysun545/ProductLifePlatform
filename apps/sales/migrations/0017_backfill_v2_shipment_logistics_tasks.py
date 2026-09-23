"""Bridge already-printed V2 batches into the per-shipment logistics stage.

The first V2 rollout could have an authorised shipment that had already
printed QR codes and was waiting for the former direct stock-outbound task.
Those batches must not become un-actionable after the per-shipment logistics
requirement is introduced.  This data migration replaces that open task with
the new logistics-manager task, without touching any legacy order task.
"""

from django.db import migrations
from django.utils import timezone


LOGISTICS_WORK_TYPE = "ORDER_PLAN_V2_SHIPMENT_LOGISTICS_CONFIRM"
OUTBOUND_WORK_TYPE = "ORDER_PLAN_V2_STOCK_OUTBOUND"
V2_FLOW_VERSION = "MATERIAL_STOCK_V2"


def backfill_logistics_tasks(apps, schema_editor):
    OrderPlanShipment = apps.get_model("sales", "OrderPlanShipment")
    UserOrganization = apps.get_model("account", "UserOrganization")
    WorkItem = apps.get_model("workflow", "WorkItem")
    UserTask = apps.get_model("workflow", "UserTask")
    now = timezone.now()

    queryset = (
        OrderPlanShipment.objects.select_related("order_plan")
        .filter(
            order_plan__flow_version=V2_FLOW_VERSION,
            status="AUTHORIZED",
            qrcode_printed_at__isnull=False,
            logistics_confirmed_at__isnull=True,
        )
        .order_by("id")
    )
    for shipment in queryset.iterator():
        plan = shipment.order_plan
        recipients = list(
            UserOrganization.objects.filter(
                organization_id=plan.factory_organization_id,
                role__code="factory_logistics",
                role__is_active=True,
                is_active=True,
                user__is_active=True,
            )
            .exclude(user__wx_openid__isnull=True)
            .exclude(user__wx_openid="")
            .values_list("user__wx_openid", flat=True)
            .distinct()
        )
        # Do not retire the earlier outbound task if this factory has no
        # logistics actor to receive the replacement task.
        if not recipients:
            continue
        shipment_label = (
            f"子计划{shipment.production_subplan.sequence_no}·发货批次{shipment.sequence_no}"
            if shipment.production_subplan_id
            else f"成品库存·发货批次{shipment.sequence_no}"
        )
        quantity = sum(shipment.items.values_list("quantity", flat=True))
        uniq_key = (
            f"sales:order-plan:{plan.pk}:shipment:{shipment.pk}:logistics-confirm"
        )
        workitem, _created = WorkItem.objects.get_or_create(
            uniq_key=uniq_key,
            defaults={
                "domain": "SALES",
                "type": LOGISTICS_WORK_TYPE,
                "biz_id": plan.plan_code,
                "biz_ref": f"sales.OrderPlan:{plan.pk}",
                "title": f"{shipment_label}待填写物流信息",
                "summary": (
                    f"{plan.merchant_organization.name}｜{quantity}台；"
                    "请填写物流公司和物流单号后推送成品库管出库。"
                ),
                "link": (
                    "/pages/sales/order_plan_detail/order_plan_detail"
                    f"?order_plan_id={plan.pk}"
                ),
                "payload": {
                    "order_plan_id": plan.pk,
                    "plan_code": plan.plan_code,
                    "shipment_id": shipment.pk,
                    "shipment_sequence_no": shipment.sequence_no,
                    "shipment_label": shipment_label,
                    "quantity": quantity,
                    "backfilled_for_v2_logistics": True,
                },
                "action_mode": "ANYONE_DO",
                "status": "OPEN",
                "last_activity_at": now,
            },
        )
        if workitem.status != "DONE":
            workitem.status = "OPEN"
            workitem.last_activity_at = now
            workitem.save(update_fields=("status", "last_activity_at", "updated_at"))
        for openid in recipients:
            UserTask.objects.update_or_create(
                workitem=workitem,
                to_openid=openid,
                defaults={
                    "to_role_snapshot": "factory_logistics",
                    "need_action": True,
                    "state": "NEW",
                    "read_at": None,
                    "done_at": None,
                    "done_reason": "",
                    "cursor_time": now,
                    "badge_group": "MY",
                    "display_title_snapshot": workitem.title,
                    "display_subtitle_snapshot": workitem.summary,
                },
            )
        old_outbound = WorkItem.objects.filter(
            type=OUTBOUND_WORK_TYPE,
            uniq_key=(
                f"sales:order-plan:{plan.pk}:shipment:{shipment.pk}:stock-outbound"
            ),
            status="OPEN",
        ).first()
        if old_outbound:
            old_outbound.status = "CANCELLED"
            old_outbound.done_at = now
            old_outbound.last_activity_at = now
            old_outbound.save(
                update_fields=("status", "done_at", "last_activity_at", "updated_at")
            )
            old_outbound.user_tasks.filter(state__in=("NEW", "READ")).update(
                state="DONE",
                need_action=False,
                done_at=now,
                done_reason="OTHER_ACTION",
                cursor_time=now,
                display_title_snapshot="待填写本批物流信息",
                display_subtitle_snapshot="物流信息确认后将重新推送出库任务",
            )


def reverse_backfill_logistics_tasks(apps, schema_editor):
    WorkItem = apps.get_model("workflow", "WorkItem")
    WorkItem.objects.filter(type=LOGISTICS_WORK_TYPE).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0016_order_plan_v2_shipment_logistics"),
        ("workflow", "0003_alter_workitem_action_mode"),
    ]

    operations = [
        migrations.RunPython(backfill_logistics_tasks, reverse_backfill_logistics_tasks),
    ]
