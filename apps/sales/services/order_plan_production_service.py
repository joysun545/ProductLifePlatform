from io import BytesIO

from django.db import transaction
from django.utils import timezone

from account.models import UserOrganization
from sales.models import OrderPlan, OrderPlanItem
from workflow.models import WorkItem
from workflow.services.workflow_service import publish_workitem
from sales.services.order_plan_receipt_service import (
    mark_devices_shipped, role_openids, display_name,
)
from sales.services.order_plan_shipment_authorization_service import (
    OrderPlanShipmentAuthorizationError,
    publish_shipment_authorization_task,
)
from sales.services.order_logistics_service import publish_logistics_upload_task


class OrderPlanProductionError(Exception):
    pass


def _identity(user, role_code):
    identity = UserOrganization.objects.select_related(
        "organization", "organization__organization_type", "role"
    ).filter(
        user=user, user__is_active=True, is_active=True,
        organization__is_active=True, role__is_active=True,
        role__code=role_code,
    ).first()
    if not identity or identity.organization.organization_type.code != "OWNER":
        name = "厂家生产经理" if role_code == "factory_production" else "厂家库管"
        raise OrderPlanProductionError(f"只有{name}可以执行该操作。")
    return identity


def _plan(identity, plan_id, lock=False):
    qs = OrderPlan.objects.select_related(
        "factory_organization", "merchant_organization", "brand", "created_by"
    )
    if lock:
        qs = qs.select_for_update()
    plan = qs.filter(pk=plan_id, factory_organization=identity.organization).first()
    if not plan:
        raise OrderPlanProductionError("订单计划不存在或不属于当前厂家。")
    return plan


def _items(plan):
    return list(OrderPlanItem.objects.select_related(
        "category", "product_model"
    ).filter(plan=plan).order_by("id"))


def _item_data(item):
    return {
        "id": item.pk,
        "category_name": item.category.name,
        "product_model_code": item.product_model.code,
        "product_model_name": item.product_model.name,
        "quantity": item.quantity,
        "order_batch_code": item.order_batch_code,
        "lifecycle_code": item.lifecycle_code,
    }


@transaction.atomic
def confirm_order_plan_production(*, user, plan_id):
    identity = _identity(user, "factory_production")
    plan = _plan(identity, plan_id, lock=True)
    workitem = WorkItem.objects.select_for_update().filter(
        type="ORDER_PLAN_PRODUCTION_CONFIRM",
        uniq_key=f"sales:order-plan:{plan.pk}:production-confirm",
    ).first()
    if not workitem:
        raise OrderPlanProductionError("该订单尚未进入生产确认阶段。")
    if workitem.status == "DONE":
        return {"order_plan_id": plan.pk, "plan_code": plan.plan_code}, False
    actor_task = workitem.user_tasks.filter(
        to_openid=user.wx_openid, need_action=True
    ).first()
    if not actor_task:
        raise OrderPlanProductionError("当前账号不是该生产任务的处理人。")

    now = timezone.now()
    workitem.status = "DONE"
    workitem.done_by_openid = user.wx_openid or ""
    workitem.done_at = now
    workitem.last_activity_at = now
    workitem.save(update_fields=(
        "status", "done_by_openid", "done_at", "last_activity_at", "updated_at"
    ))
    workitem.user_tasks.filter(to_openid=user.wx_openid).update(
        state="DONE", done_at=now, done_reason="SELF_ACTION",
        cursor_time=now, updated_at=now,
    )
    workitem.user_tasks.exclude(to_openid=user.wx_openid).update(
        state="DONE", done_at=now, done_reason="OTHER_ACTION",
        cursor_time=now, updated_at=now,
    )

    plan.status = OrderPlan.Status.PRODUCTION_COMPLETED
    plan.save(update_fields=("status", "updated_at"))
    plan.items.update(status=OrderPlan.Status.PRODUCTION_COMPLETED)

    item_rows = [_item_data(item) for item in _items(plan)]
    common = {
        "order_plan_id": plan.pk,
        "plan_code": plan.plan_code,
        "items": item_rows,
    }
    try:
        publish_shipment_authorization_task(
            plan=plan,
            actor_openid=user.wx_openid or "",
        )
    except OrderPlanShipmentAuthorizationError as exc:
        raise OrderPlanProductionError(str(exc)) from exc
    return common, True


def get_stock_outbound_detail(*, user, plan_id):
    identity = _identity(user, "factory_stock")
    plan = _plan(identity, plan_id)
    if not plan.shipment_authorized_at or not plan.shipment_authorized_by_id:
        raise OrderPlanProductionError(
            "创建订单的厂家销售经理尚未确认发货。"
        )
    workitem = WorkItem.objects.filter(
        type="ORDER_PLAN_STOCK_OUTBOUND",
        uniq_key=f"sales:order-plan:{plan.pk}:stock-outbound",
    ).prefetch_related("user_tasks").first()
    if not workitem:
        raise OrderPlanProductionError("该订单尚未进入出库阶段。")
    task = next((x for x in workitem.user_tasks.all() if x.to_openid == user.wx_openid), None)
    return {
        "order_plan_id": plan.pk,
        "plan_code": plan.plan_code,
        "merchant_name": plan.merchant_organization.name,
        "brand_name": plan.brand.name,
        "total_quantity": plan.total_quantity,
        "task_id": task.pk if task else None,
        "can_outbound": bool(task and task.need_action and task.state != "DONE"),
        "items": [_item_data(item) for item in _items(plan)],
    }


@transaction.atomic
def confirm_stock_outbound(*, user, plan_id):
    identity = _identity(user, "factory_stock")
    plan = _plan(identity, plan_id, lock=True)
    if not plan.shipment_authorized_at or not plan.shipment_authorized_by_id:
        raise OrderPlanProductionError(
            "创建订单的厂家销售经理尚未确认发货，不能出库。"
        )
    workitem = WorkItem.objects.select_for_update().filter(
        type="ORDER_PLAN_STOCK_OUTBOUND",
        uniq_key=f"sales:order-plan:{plan.pk}:stock-outbound",
    ).first()
    if not workitem:
        raise OrderPlanProductionError("该订单尚未进入出库阶段。")
    if workitem.status == "DONE":
        return {
            "order_plan_id": plan.pk,
            "plan_code": plan.plan_code,
            "outbound_completed": True,
        }, False

    actor_task = workitem.user_tasks.select_for_update().filter(
        to_openid=user.wx_openid,
        need_action=True,
    ).first()
    if not actor_task:
        raise OrderPlanProductionError("当前账号不是该出库任务的处理人。")

    unprinted_items = plan.items.filter(qrcode_printed_at__isnull=True)
    if unprinted_items.exists():
        names = "、".join(
            unprinted_items.select_related("product_model")
            .values_list("product_model__name", flat=True)
        )
        raise OrderPlanProductionError(
            f"请先在网页打印中心完成全部产品二维码：{names}。"
        )

    merchant_openids = role_openids(
        plan.merchant_organization,
        {
            "merchant_owner",
            "merchant_senior_manager",
            "merchant_sales",
            "merchant_stock",
        },
    )
    if not merchant_openids:
        raise OrderPlanProductionError(
            "接收商家尚未配置负责人、高级经理、销售经理或库管，"
            "无法创建入库任务。"
        )

    now = timezone.now()
    workitem.status = "DONE"
    workitem.done_by_openid = user.wx_openid or ""
    workitem.done_at = now
    workitem.last_activity_at = now
    workitem.save(update_fields=(
        "status", "done_by_openid", "done_at", "last_activity_at", "updated_at"
    ))
    workitem.user_tasks.filter(to_openid=user.wx_openid).update(
        state="DONE", need_action=False, done_at=now,
        done_reason="SELF_ACTION",
        cursor_time=now, updated_at=now,
    )
    workitem.user_tasks.exclude(to_openid=user.wx_openid).update(
        state="DONE", need_action=False, done_at=now,
        done_reason="OTHER_ACTION",
        cursor_time=now, updated_at=now,
    )
    plan.status = OrderPlan.Status.SHIPPED
    plan.shipped_at = now
    plan.shipped_by = user
    plan.save(update_fields=("status", "shipped_at", "shipped_by", "updated_at"))
    plan.items.update(status=OrderPlan.Status.SHIPPED)
    try:
        mark_devices_shipped(plan=plan, user=user, now=now)
    except Exception as exc:
        raise OrderPlanProductionError(str(exc)) from exc

    payload = {
        "order_plan_id": plan.pk,
        "plan_code": plan.plan_code,
        "shipped_by_name": display_name(user),
        "items": [_item_data(item) for item in _items(plan)],
    }
    awareness_openids = set(role_openids(
        plan.factory_organization,
        {"factory_sales_assistant"},
    ))
    if plan.created_by.wx_openid:
        awareness_openids.add(plan.created_by.wx_openid)
    if awareness_openids:
        publish_workitem(
            domain="SALES", type="ORDER_PLAN_OUTBOUND_COMPLETED",
            biz_id=plan.plan_code, biz_ref=f"sales.OrderPlan:{plan.pk}",
            uniq_key=f"sales:order-plan:{plan.pk}:outbound-completed",
            title="订单已出库发货",
            summary=f"{plan.merchant_organization.name}｜共{plan.total_quantity}台",
            link="/pages/sales/order_plan_list/order_plan_list",
            recipients_openids=tuple(awareness_openids), need_action=False,
            action_mode="EACH_ACK", payload=payload,
            actor_openid=user.wx_openid or "", mark_workitem_done_immediately=True,
        )
    publish_workitem(
        domain="SALES", type="ORDER_PLAN_MERCHANT_RECEIPT",
        biz_id=plan.plan_code, biz_ref=f"sales.OrderPlan:{plan.pk}",
        uniq_key=f"sales:order-plan:{plan.pk}:merchant-receipt",
        title="订单到货待入库",
        summary=f"{plan.factory_organization.name}｜{plan.brand.name}｜共{plan.total_quantity}台",
        link="/pages/sales/order_plan_list/order_plan_list",
        recipients_openids=merchant_openids, need_action=True,
        action_mode="ANYONE_DO", payload=payload,
        actor_openid=user.wx_openid or "", mark_workitem_done_immediately=False,
    )
    logistics_workitem = publish_logistics_upload_task(
        plan=plan,
        actor_openid=user.wx_openid or "",
    )
    return {
        "order_plan_id": plan.pk,
        "plan_code": plan.plan_code,
        "outbound_completed": True,
        "logistics_task_created": logistics_workitem is not None,
    }, True


def render_lifecycle_qrcode(*, user, plan_id, item_id):
    identity = _identity(user, "factory_stock")
    plan = _plan(identity, plan_id)
    if not plan.shipment_authorized_at or not plan.shipment_authorized_by_id:
        raise OrderPlanProductionError(
            "创建订单的厂家销售经理尚未确认发货。"
        )
    item = OrderPlanItem.objects.filter(pk=item_id, plan=plan).first()
    if not item:
        raise OrderPlanProductionError("订单产品明细不存在。")
    try:
        import qrcode
    except ImportError as exc:
        raise OrderPlanProductionError("服务器尚未安装二维码组件。") from exc
    image = qrcode.make(item.lifecycle_code)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue(), item.lifecycle_code
