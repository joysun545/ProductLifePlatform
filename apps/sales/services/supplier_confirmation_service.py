from django.db import transaction
from django.utils import timezone

from account.models import UserOrganization
from sales.models import OrderPlan
from workflow.models import UserTask, WorkItem
from workflow.services.workflow_service import publish_workitem


class SupplierConfirmationError(Exception):
    """供应商协同确认的账号、任务或业务状态不合法。"""


def _display_name(user):
    return user.wx_nickname or user.get_full_name() or user.username


def _supplier_identity(user):
    identity = (
        UserOrganization.objects.select_related(
            "organization",
            "organization__organization_type",
            "role",
        )
        .filter(
            user=user,
            is_active=True,
            user__is_active=True,
            organization__is_active=True,
            role__is_active=True,
            role__code__in=("supplier_owner", "supplier_sales"),
        )
        .first()
    )
    if not identity:
        raise SupplierConfirmationError("当前账号没有供应商确认权限。")
    if identity.organization.organization_type.code != "SUPPLIER":
        raise SupplierConfirmationError("当前岗位没有有效的供应商组织身份。")
    if not user.wx_openid:
        raise SupplierConfirmationError("当前微信业务账号登录状态无效。")
    return identity


def _integer(value, label):
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SupplierConfirmationError(f"{label}参数无效。") from exc
    if parsed <= 0:
        raise SupplierConfirmationError(f"{label}参数无效。")
    return parsed


def _scope_for(payload, openid):
    scopes = payload.get("recipient_scopes", {})
    scope = scopes.get(openid) if isinstance(scopes, dict) else None
    return scope if isinstance(scope, dict) else None


def _processed_actors(payload):
    value = payload.get("processed_actors", [])
    return value if isinstance(value, list) else []


def _material_line_count(payload, material_ids):
    material_ids = {int(value) for value in material_ids}
    return sum(
        1
        for row in payload.get("materials", [])
        if isinstance(row, dict)
        and row.get("material_id")
        and int(row["material_id"]) in material_ids
    )


def _all_materials_covered_by_sales(payload):
    all_material_ids = {
        int(row["material_id"])
        for row in payload.get("materials", [])
        if isinstance(row, dict) and row.get("material_id")
    }
    sales_material_ids = set()
    scopes = payload.get("recipient_scopes", {})
    if isinstance(scopes, dict):
        for scope in scopes.values():
            if isinstance(scope, dict) and scope.get("role") == "supplier_sales":
                sales_material_ids.update(
                    int(value) for value in scope.get("material_ids", [])
                )
    return bool(all_material_ids) and all_material_ids <= sales_material_ids


def _matching_openids(plan):
    return list(
        UserOrganization.objects.filter(
            organization=plan.factory_organization,
            role__code="factory_matching",
            role__is_active=True,
            is_active=True,
            user__is_active=True,
            user__wx_openid__isnull=False,
        )
        .exclude(user__wx_openid="")
        .values_list("user__wx_openid", flat=True)
    )


def _publish_matching_receipt(plan, supplier, workitem, actor):
    recipients = _matching_openids(plan)
    if not recipients:
        return 0
    payload = workitem.payload if isinstance(workitem.payload, dict) else {}
    materials = payload.get("materials", [])
    publish_workitem(
        domain="SALES",
        type="ORDER_PLAN_SUPPLIER_CONFIRMED",
        biz_id=f"{plan.plan_code}:{supplier.pk}",
        biz_ref=f"sales.OrderPlan:{plan.pk}",
        uniq_key=(
            f"sales:order-plan:{plan.pk}:supplier:{supplier.pk}:"
            "matching-receipt"
        ),
        title="物料已完成配送",
        summary=f"{supplier.name}｜{len(materials)}项订单物料已完成配送",
        link="/pages/sales/order_plan_list/order_plan_list",
        recipients_openids=recipients,
        need_action=False,
        action_mode="EACH_ACK",
        payload={
            "order_plan_id": plan.pk,
            "plan_code": plan.plan_code,
            "supplier_id": supplier.pk,
            "supplier_name": supplier.name,
            "materials": materials,
            "processed_actors": payload.get("processed_actors", []),
            "confirmation_source_workitem_id": workitem.pk,
        },
        actor_openid=actor.wx_openid,
        mark_workitem_done_immediately=True,
    )
    pending_supplier_tasks = WorkItem.objects.filter(
        type="ORDER_PLAN_SUPPLIER_PREPARE",
        biz_ref=f"sales.OrderPlan:{plan.pk}",
    ).exclude(status="DONE")
    supplier_tasks_exist = WorkItem.objects.filter(
        type="ORDER_PLAN_SUPPLIER_PREPARE",
        biz_ref=f"sales.OrderPlan:{plan.pk}",
    ).exists()
    if supplier_tasks_exist and not pending_supplier_tasks.exists():
        publish_workitem(
            domain="SALES",
            type="ORDER_PLAN_DELIVERY_CONFIRM",
            biz_id=plan.plan_code,
            biz_ref=f"sales.OrderPlan:{plan.pk}",
            uniq_key=f"sales:order-plan:{plan.pk}:delivery-confirm",
            title="确认物料配送全部完成",
            summary=(
                f"{plan.merchant_organization.name}｜{plan.brand.name}｜"
                f"共{plan.total_quantity}台"
            ),
            link=(
                "/pages/sales/matching_confirm/matching_confirm"
                f"?order_plan_id={plan.pk}"
            ),
            recipients_openids=recipients,
            need_action=True,
            action_mode="ANYONE_DO",
            payload={
                "order_plan_id": plan.pk,
                "plan_code": plan.plan_code,
            },
            actor_openid=actor.wx_openid,
            mark_workitem_done_immediately=False,
        )
    return len(set(recipients))


def _result(workitem, task, payload, *, completed, receipt_count=0):
    actors = _processed_actors(payload)
    return {
        "workitem_id": workitem.pk,
        "task_id": task.pk,
        "state": "CONFIRMED" if completed else "PARTIAL",
        "state_name": (
            "配送完成"
            if completed
            else "本人已确认配送，等待协同账号"
        ),
        "completed": completed,
        "completion_policy": payload.get(
            "completion_policy",
            "OWNER_OR_ALL_SALES",
        ),
        "processed_actors": actors,
        "processed_by_text": "、".join(
            f"{actor.get('name', '协同账号')}已处理" for actor in actors
        ),
        "receipt_accounts": receipt_count,
    }


@transaction.atomic
def confirm_supplier_order_materials(
    *,
    user,
    plan_id,
    supplier_id=None,
    usertask_id=None,
):
    identity = _supplier_identity(user)
    plan_id = _integer(plan_id, "订单计划")
    supplier_id = (
        identity.organization_id
        if supplier_id in (None, "")
        else _integer(supplier_id, "供应商")
    )
    if supplier_id != identity.organization_id:
        raise SupplierConfirmationError("该任务不属于当前供应商组织。")

    plan = (
        OrderPlan.objects.select_related("factory_organization")
        .filter(pk=plan_id)
        .first()
    )
    if not plan:
        raise SupplierConfirmationError("订单计划不存在。")

    workitem = (
        WorkItem.objects.select_for_update()
        .filter(
            uniq_key=f"sales:order-plan:{plan.pk}:supplier:{supplier_id}",
            type="ORDER_PLAN_SUPPLIER_PREPARE",
        )
        .first()
    )
    if not workitem:
        raise SupplierConfirmationError("当前订单没有待确认的供应商任务。")

    tasks = list(workitem.user_tasks.select_for_update().order_by("id"))
    task = next(
        (item for item in tasks if item.to_openid == user.wx_openid),
        None,
    )
    if not task:
        raise SupplierConfirmationError("当前账号不在该供应协同任务中。")
    if usertask_id not in (None, "") and task.pk != _integer(
        usertask_id,
        "任务",
    ):
        raise SupplierConfirmationError("任务与当前订单不匹配。")

    payload = workitem.payload if isinstance(workitem.payload, dict) else {}
    scope = _scope_for(payload, user.wx_openid)
    if not scope or scope.get("role") != identity.role.code:
        raise SupplierConfirmationError("责任范围已变化，请刷新任务后重试。")
    if workitem.status == "DONE":
        return _result(workitem, task, payload, completed=True)
    if task.state == "DONE":
        return _result(workitem, task, payload, completed=False)

    now = timezone.now()
    task.state = "DONE"
    task.read_at = task.read_at or now
    task.done_at = now
    task.done_reason = "SELF_ACTION"
    task.cursor_time = now
    task.save(
        update_fields=(
            "state",
            "read_at",
            "done_at",
            "done_reason",
            "cursor_time",
            "updated_at",
        )
    )

    actors = _processed_actors(payload)
    if not any(actor.get("openid") == user.wx_openid for actor in actors):
        actors.append(
            {
                "openid": user.wx_openid,
                "name": _display_name(user),
                "role": identity.role.code,
                "role_name": identity.role.name,
                "material_ids": scope.get("material_ids", []),
                "material_count": _material_line_count(
                    payload,
                    scope.get("material_ids", []),
                ),
                "processed_at": now.isoformat(),
            }
        )
    payload["processed_actors"] = actors

    completed = identity.role.code == "supplier_owner"
    if identity.role.code == "supplier_sales":
        pending_sales = [
            item
            for item in tasks
            if item.pk != task.pk
            and item.state != "DONE"
            and (_scope_for(payload, item.to_openid) or {}).get("role")
            == "supplier_sales"
        ]
        completed = not pending_sales and _all_materials_covered_by_sales(payload)
        payload["waiting_for_owner"] = not completed and not pending_sales

    receipt_count = 0
    if completed:
        for other_task in tasks:
            if other_task.pk == task.pk or other_task.state == "DONE":
                continue
            other_task.state = "DONE"
            other_task.done_at = now
            other_task.done_reason = "OTHER_ACTION"
            other_task.cursor_time = now
            other_task.save(
                update_fields=(
                    "state",
                    "done_at",
                    "done_reason",
                    "cursor_time",
                    "updated_at",
                )
            )
        workitem.status = "DONE"
        workitem.done_by_openid = user.wx_openid
        workitem.done_at = now
        payload["confirmed_at"] = now.isoformat()
        payload["confirmed_by"] = {
            "openid": user.wx_openid,
            "name": _display_name(user),
            "role": identity.role.code,
        }

    workitem.payload = payload
    workitem.last_activity_at = now
    workitem.save(
        update_fields=(
            "payload",
            "status",
            "done_by_openid",
            "done_at",
            "last_activity_at",
            "updated_at",
        )
    )
    if completed:
        receipt_count = _publish_matching_receipt(
            plan,
            identity.organization,
            workitem,
            user,
        )
    return _result(
        workitem,
        task,
        payload,
        completed=completed,
        receipt_count=receipt_count,
    )
