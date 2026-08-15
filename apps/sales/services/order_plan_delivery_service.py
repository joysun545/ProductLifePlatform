from django.db import transaction
from django.utils import timezone

from account.models import UserOrganization
from sales.models import OrderPlan
from workflow.models import WorkItem
from workflow.services.workflow_service import publish_workitem


class OrderPlanDeliveryError(Exception):
    """订单物料配送汇总确认失败。"""


def _identity(user):
    identity = UserOrganization.objects.select_related(
        "organization", "organization__organization_type", "role"
    ).filter(
        user=user, user__is_active=True, is_active=True,
        organization__is_active=True, role__is_active=True,
        role__code="factory_matching",
    ).first()
    if not identity or identity.organization.organization_type.code != "OWNER":
        raise OrderPlanDeliveryError("只有厂家配套经理可以确认物料配送。")
    return identity


def _role_openids(organization, role_code):
    return list(UserOrganization.objects.filter(
        organization=organization, role__code=role_code,
        role__is_active=True, is_active=True, user__is_active=True,
        user__wx_openid__isnull=False,
    ).exclude(user__wx_openid="").values_list("user__wx_openid", flat=True))


def _context(user, plan_id, *, lock=False):
    identity = _identity(user)
    plans = OrderPlan.objects.select_related(
        "factory_organization", "merchant_organization", "brand", "created_by"
    )
    if lock:
        plans = plans.select_for_update()
    plan = plans.filter(pk=plan_id, factory_organization=identity.organization).first()
    if not plan:
        raise OrderPlanDeliveryError("订单计划不存在或不属于当前厂家。")
    supplier_tasks = list(WorkItem.objects.filter(
        type="ORDER_PLAN_SUPPLIER_PREPARE",
        biz_ref=f"sales.OrderPlan:{plan.pk}",
    ).order_by("id"))
    if not supplier_tasks:
        raise OrderPlanDeliveryError("该订单尚未生成供应商配送任务。")
    aggregate = WorkItem.objects.filter(
        uniq_key=f"sales:order-plan:{plan.pk}:delivery-confirm",
        type="ORDER_PLAN_DELIVERY_CONFIRM",
    ).first()
    return identity, plan, supplier_tasks, aggregate


def _serialize(plan, supplier_tasks, aggregate):
    suppliers = []
    material_statuses = []
    for task in supplier_tasks:
        payload = task.payload if isinstance(task.payload, dict) else {}
        supplier_id = payload.get("supplier_id")
        supplier_name = payload.get("supplier_name", "供应商")
        completed = task.status == "DONE"
        suppliers.append({
            "supplier_id": supplier_id,
            "supplier_name": supplier_name,
            "completed": completed,
            "status_name": "已完成配送" if completed else "待配送",
            "materials_count": len(payload.get("materials", [])),
            "processed_actors": payload.get("processed_actors", []),
        })
        for material in payload.get("materials", []):
            if not isinstance(material, dict):
                continue
            material_statuses.append({
                "order_plan_item_id": material.get("order_plan_item_id"),
                "material_id": material.get("material_id"),
                "supplier_id": supplier_id,
                "supplier_name": supplier_name,
                "completed": completed,
                "status_name": "已完成配送" if completed else "待配送",
            })
    all_completed = bool(suppliers) and all(row["completed"] for row in suppliers)
    confirmed = bool(aggregate and aggregate.status == "DONE")
    return {
        "order_plan_id": plan.pk, "plan_code": plan.plan_code,
        "merchant_name": plan.merchant_organization.name,
        "brand_name": plan.brand.name, "region": plan.region_snapshot,
        "total_quantity": plan.total_quantity,
        "suppliers": suppliers, "all_suppliers_completed": all_completed,
        "material_statuses": material_statuses,
        "confirmed": confirmed, "can_confirm": all_completed and not confirmed,
        "state": "CONFIRMED" if confirmed else ("READY" if all_completed else "WAITING"),
    }


def get_order_plan_delivery_detail(*, user, plan_id):
    _identity_obj, plan, supplier_tasks, aggregate = _context(user, plan_id)
    return _serialize(plan, supplier_tasks, aggregate)


@transaction.atomic
def confirm_order_plan_delivery(*, user, plan_id):
    _identity_obj, plan, supplier_tasks, aggregate = _context(user, plan_id, lock=True)
    if any(task.status != "DONE" for task in supplier_tasks):
        raise OrderPlanDeliveryError("仍有供应商尚未完成配送，暂时不能确认。")
    if not aggregate:
        raise OrderPlanDeliveryError("汇总确认任务尚未生成，请刷新后重试。")
    if aggregate.status == "DONE":
        return _serialize(plan, supplier_tasks, aggregate), False

    now = timezone.now()
    aggregate.status = "DONE"
    aggregate.done_by_openid = user.wx_openid or ""
    aggregate.done_at = now
    aggregate.last_activity_at = now
    aggregate.save(update_fields=("status", "done_by_openid", "done_at", "last_activity_at", "updated_at"))
    aggregate.user_tasks.filter(to_openid=user.wx_openid).update(
        state="DONE", done_at=now, done_reason="SELF_ACTION", cursor_time=now, updated_at=now
    )
    aggregate.user_tasks.exclude(to_openid=user.wx_openid).update(
        state="DONE", done_at=now, done_reason="OTHER_ACTION", cursor_time=now, updated_at=now
    )

    plan.status = OrderPlan.Status.PRODUCTION_PENDING
    plan.save(update_fields=("status", "updated_at"))
    plan.items.update(status=OrderPlan.Status.PRODUCTION_PENDING)

    common_payload = {"order_plan_id": plan.pk, "plan_code": plan.plan_code}
    production_openids = _role_openids(plan.factory_organization, "factory_production")
    if production_openids:
        publish_workitem(
            domain="SALES", type="ORDER_PLAN_PRODUCTION_CONFIRM",
            biz_id=plan.plan_code, biz_ref=f"sales.OrderPlan:{plan.pk}",
            uniq_key=f"sales:order-plan:{plan.pk}:production-confirm",
            title="确认生产完成",
            summary=f"{plan.merchant_organization.name}｜{plan.brand.name}｜共{plan.total_quantity}台",
            link="/pages/sales/order_plan_list/order_plan_list",
            recipients_openids=production_openids, need_action=True,
            action_mode="ANYONE_DO", payload=common_payload,
            actor_openid=user.wx_openid or "", mark_workitem_done_immediately=False,
        )
    sales_openid = plan.created_by.wx_openid or ""
    if sales_openid:
        publish_workitem(
            domain="SALES", type="ORDER_PLAN_DELIVERY_COMPLETED",
            biz_id=plan.plan_code, biz_ref=f"sales.OrderPlan:{plan.pk}",
            uniq_key=f"sales:order-plan:{plan.pk}:delivery-completed:sales",
            title="订单物料配送全部完成",
            summary=f"{plan.merchant_organization.name}｜{plan.brand.name}｜共{plan.total_quantity}台",
            link="/pages/sales/order_plan_list/order_plan_list",
            recipients_openids=(sales_openid,), need_action=False,
            action_mode="EACH_ACK", payload=common_payload,
            actor_openid=user.wx_openid or "", mark_workitem_done_immediately=True,
        )
    return _serialize(plan, supplier_tasks, aggregate), True
