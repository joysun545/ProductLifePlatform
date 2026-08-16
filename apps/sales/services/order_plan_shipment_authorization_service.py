from django.db import transaction
from django.utils import timezone

from account.models import UserOrganization
from sales.models import OrderPlan
from sales.services.order_plan_receipt_service import display_name, role_openids
from sales.services.order_qrcode_print_service import (
    OrderQRCodePrintError,
    publish_stock_outbound_task_if_ready,
)
from workflow.models import WorkItem
from workflow.services.workflow_service import publish_workitem


AUTHORIZATION_WORK_TYPE = "ORDER_PLAN_SHIPMENT_AUTHORIZATION"
STOCK_NOTICE_WORK_TYPE = "ORDER_PLAN_SHIPMENT_AUTHORIZED_STOCK"


class OrderPlanShipmentAuthorizationError(Exception):
    pass


def _sales_identity(user):
    identity = (
        UserOrganization.objects.select_related(
            "organization",
            "organization__organization_type",
            "role",
        )
        .filter(
            user=user,
            user__is_active=True,
            is_active=True,
            organization__is_active=True,
            role__is_active=True,
            role__code="factory_sales",
        )
        .first()
    )
    if (
        not identity
        or identity.organization.organization_type.code != "OWNER"
    ):
        raise OrderPlanShipmentAuthorizationError(
            "只有创建订单的厂家销售经理可以下达发货指令。"
        )
    return identity


def _plan(identity, plan_id, *, lock=False):
    plans = OrderPlan.objects.select_related(
        "factory_organization",
        "merchant_organization",
        "brand",
        "created_by",
        "shipment_authorized_by",
    )
    if lock:
        plans = plans.select_for_update()
    plan = plans.filter(
        pk=plan_id,
        factory_organization=identity.organization,
    ).first()
    if not plan:
        raise OrderPlanShipmentAuthorizationError(
            "订单计划不存在或不属于当前厂家。"
        )
    if plan.created_by_id != identity.user_id:
        raise OrderPlanShipmentAuthorizationError(
            "只有创建该订单的厂家销售经理可以下达发货指令。"
        )
    return plan


def _payload(plan):
    return {
        "order_plan_id": plan.pk,
        "plan_code": plan.plan_code,
        "items": [
            {
                "id": item.pk,
                "category_name": item.category.name,
                "product_model_code": item.product_model.code,
                "product_model_name": item.product_model.name,
                "quantity": item.quantity,
                "order_batch_code": item.order_batch_code,
                "lifecycle_code": item.lifecycle_code,
            }
            for item in plan.items.select_related(
                "category", "product_model"
            ).order_by("id")
        ],
    }


def publish_shipment_authorization_task(*, plan, actor_openid):
    sales_openid = (plan.created_by.wx_openid or "").strip()
    if not sales_openid:
        raise OrderPlanShipmentAuthorizationError(
            "创建订单的厂家销售经理没有有效微信账号，无法下达发货任务。"
        )
    return publish_workitem(
        domain="SALES",
        type=AUTHORIZATION_WORK_TYPE,
        biz_id=plan.plan_code,
        biz_ref=f"sales.OrderPlan:{plan.pk}",
        uniq_key=f"sales:order-plan:{plan.pk}:shipment-authorization",
        title="生产完成，待下达发货指令",
        summary=(
            f"{plan.merchant_organization.name}｜"
            f"{plan.brand.name}｜共{plan.total_quantity}台"
        ),
        link=(
            "/pages/sales/order_plan_list/order_plan_list"
            f"?focus_plan_id={plan.pk}"
        ),
        recipients_openids=(sales_openid,),
        need_action=True,
        action_mode="ANYONE_DO",
        payload=_payload(plan),
        actor_openid=actor_openid,
        mark_workitem_done_immediately=False,
    )


def _complete_workitem(workitem, user, now, payload):
    payload.update(
        {
            "shipment_authorized_by_name": display_name(user),
            "shipment_authorized_at": now.isoformat(),
        }
    )
    workitem.payload = payload
    workitem.status = "DONE"
    workitem.done_by_openid = user.wx_openid or ""
    workitem.done_at = now
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
    workitem.user_tasks.filter(to_openid=user.wx_openid).update(
        state="DONE",
        need_action=False,
        done_at=now,
        done_reason="SELF_ACTION",
        cursor_time=now,
        updated_at=now,
    )
    workitem.user_tasks.exclude(to_openid=user.wx_openid).update(
        state="DONE",
        need_action=False,
        done_at=now,
        done_reason="OTHER_ACTION",
        cursor_time=now,
        updated_at=now,
    )


def _notify_factory_stock(plan, user, payload):
    stock_openids = role_openids(
        plan.factory_organization,
        {"factory_stock"},
    )
    if not stock_openids:
        return False
    publish_workitem(
        domain="SALES",
        type=STOCK_NOTICE_WORK_TYPE,
        biz_id=plan.plan_code,
        biz_ref=f"sales.OrderPlan:{plan.pk}",
        uniq_key=f"sales:order-plan:{plan.pk}:shipment-authorized:stock",
        title="销售经理已下达发货指令",
        summary=(
            f"{plan.merchant_organization.name}｜"
            f"{plan.brand.name}｜共{plan.total_quantity}台；"
            "请在网页打印中心完成二维码"
        ),
        link=(
            "/pages/sales/order_plan_list/order_plan_list"
            f"?focus_plan_id={plan.pk}"
        ),
        recipients_openids=stock_openids,
        need_action=False,
        action_mode="EACH_ACK",
        payload=payload,
        actor_openid=user.wx_openid or "",
        mark_workitem_done_immediately=True,
    )
    return True


@transaction.atomic
def confirm_shipment_authorization(*, user, plan_id):
    identity = _sales_identity(user)
    plan = _plan(identity, plan_id, lock=True)

    if plan.shipment_authorized_at and plan.shipment_authorized_by_id:
        return {
            "order_plan_id": plan.pk,
            "plan_code": plan.plan_code,
            "shipment_authorized": True,
            "shipment_authorized_at": plan.shipment_authorized_at.isoformat(),
            "shipment_authorized_by_name": (
                display_name(plan.shipment_authorized_by)
                if plan.shipment_authorized_by_id
                else ""
            ),
        }, False

    if plan.status != OrderPlan.Status.PRODUCTION_COMPLETED:
        raise OrderPlanShipmentAuthorizationError(
            "只有已确认生产完成、尚未实际发货的订单可以下达发货指令。"
        )

    workitem = WorkItem.objects.select_for_update().filter(
        type=AUTHORIZATION_WORK_TYPE,
        uniq_key=f"sales:order-plan:{plan.pk}:shipment-authorization",
    ).first()
    if not workitem:
        publish_shipment_authorization_task(
            plan=plan,
            actor_openid="",
        )
        workitem = WorkItem.objects.select_for_update().get(
            uniq_key=f"sales:order-plan:{plan.pk}:shipment-authorization"
        )
    if workitem.status == "DONE":
        raise OrderPlanShipmentAuthorizationError(
            "发货授权任务已完成，请刷新订单列表。"
        )

    actor_task = workitem.user_tasks.select_for_update().filter(
        to_openid=user.wx_openid,
        need_action=True,
    ).first()
    if not actor_task:
        raise OrderPlanShipmentAuthorizationError(
            "当前账号不是该发货授权任务的处理人。"
        )

    now = timezone.now()
    payload = workitem.payload if isinstance(workitem.payload, dict) else {}
    payload = dict(payload)
    _complete_workitem(workitem, user, now, payload)

    plan.shipment_authorized_at = now
    plan.shipment_authorized_by = user
    plan.save(
        update_fields=(
            "shipment_authorized_at",
            "shipment_authorized_by",
            "updated_at",
        )
    )

    payload.update(
        {
            "shipment_authorized_by_name": display_name(user),
            "shipment_authorized_at": now.isoformat(),
        }
    )
    stock_notified = _notify_factory_stock(plan, user, payload)
    try:
        stock_task_created = publish_stock_outbound_task_if_ready(plan)
    except OrderQRCodePrintError as exc:
        raise OrderPlanShipmentAuthorizationError(str(exc)) from exc

    return {
        "order_plan_id": plan.pk,
        "plan_code": plan.plan_code,
        "shipment_authorized": True,
        "shipment_authorized_at": now.isoformat(),
        "shipment_authorized_by_name": display_name(user),
        "stock_notified": stock_notified,
        "stock_task_created": stock_task_created,
    }, True
