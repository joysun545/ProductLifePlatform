"""Group historical V2 logistics tasks under their single order-plan card.

``0017`` correctly bridged printed V2 shipments to the new per-shipment
logistics task, but those already-created work items predated the order-plan
inbox grouping payload.  Retrofitting only the projection metadata preserves
the immutable task history and recipient records while making all V2 updates
for one plan appear on one task card.
"""

from django.db import migrations


LOGISTICS_WORK_TYPE = "ORDER_PLAN_V2_SHIPMENT_LOGISTICS_CONFIRM"
ORDER_PLAN_BIZ_REF_PREFIX = "sales.OrderPlan:"


def _order_plan_id(workitem):
    payload = workitem.payload if isinstance(workitem.payload, dict) else {}
    value = payload.get("order_plan_id")
    if value not in (None, ""):
        try:
            return int(value)
        except (TypeError, ValueError):
            pass

    biz_ref = str(workitem.biz_ref or "")
    if biz_ref.startswith(ORDER_PLAN_BIZ_REF_PREFIX):
        value = biz_ref[len(ORDER_PLAN_BIZ_REF_PREFIX):]
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    return None


def repair_v2_logistics_inbox_groups(apps, schema_editor):
    """Add card-projection metadata without changing task state or history."""

    OrderPlan = apps.get_model("sales", "OrderPlan")
    WorkItem = apps.get_model("workflow", "WorkItem")

    workitems = list(
        WorkItem.objects.filter(type=LOGISTICS_WORK_TYPE).order_by("id")
    )
    plan_ids = {
        plan_id
        for workitem in workitems
        if (plan_id := _order_plan_id(workitem)) is not None
    }
    plan_codes = dict(
        OrderPlan.objects.filter(pk__in=plan_ids).values_list("pk", "plan_code")
    )

    for workitem in workitems:
        plan_id = _order_plan_id(workitem)
        if plan_id is None:
            # Never guess a group for an incomplete historical row.
            continue
        payload = dict(workitem.payload or {})
        plan_code = plan_codes.get(plan_id) or payload.get("plan_code") or workitem.biz_id
        payload["inbox_group_key"] = f"order-plan:{plan_id}"
        payload["inbox_group_kind"] = "ORDER_PLAN"
        payload["inbox_group_label"] = f"订单计划：{plan_code}"
        if payload != workitem.payload:
            workitem.payload = payload
            workitem.save(update_fields=("payload", "updated_at"))


def noop_reverse(apps, schema_editor):
    """Historical task grouping metadata must not be removed on rollback."""


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0018_v2_cancelled_finished_inventory"),
        ("workflow", "0003_alter_workitem_action_mode"),
    ]

    operations = [
        migrations.RunPython(repair_v2_logistics_inbox_groups, noop_reverse),
    ]
