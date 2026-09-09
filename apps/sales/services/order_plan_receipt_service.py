from django.db import transaction
from django.utils import timezone

from account.models import UserOrganization
from lifecycle.models import Device, DeviceState, LifecycleEvent, OrganizationDevice
from manufacturing.models import ProductionBatch
from sales.models import OrderPlan
from sales.services.order_plan_reissue_service import mark_return_inventory_reissued
from workflow.models import WorkItem
from workflow.services.workflow_service import publish_workitem


class OrderPlanReceiptError(Exception):
    pass


MERCHANT_RECEIPT_ROLES = {
    "merchant_owner",
    "merchant_senior_manager",
    "merchant_sales",
    "merchant_stock",
}


def display_name(user):
    return user.wx_nickname or user.get_full_name() or user.username


def role_openids(organization, role_codes):
    return tuple(
        UserOrganization.objects.filter(
            organization=organization, is_active=True,
            role__code__in=role_codes, role__is_active=True,
            user__is_active=True,
        ).exclude(user__wx_openid__isnull=True).exclude(
            user__wx_openid=""
        ).values_list("user__wx_openid", flat=True).distinct()
    )


def _identity(user, roles, organization_type):
    identity = UserOrganization.objects.select_related(
        "organization", "organization__organization_type", "role"
    ).filter(
        user=user, is_active=True, user__is_active=True,
        organization__is_active=True, role__is_active=True,
        role__code__in=roles,
    ).first()
    if not identity or identity.organization.organization_type.code != organization_type:
        raise OrderPlanReceiptError("当前岗位无权执行该操作。")
    return identity


def _complete_anyone(
    workitem,
    user,
    now,
    *,
    actor_title,
    peer_title,
):
    if workitem.status == "DONE":
        raise OrderPlanReceiptError("该协作任务已由其他成员完成，请刷新页面。")

    task = workitem.user_tasks.select_for_update().filter(
        to_openid=user.wx_openid,
        need_action=True,
    ).exclude(
        state="DONE",
    ).first()
    if not task:
        raise OrderPlanReceiptError("当前账号不是该任务的处理人。")
    payload = dict(workitem.payload or {})
    payload.update({
        "completed_by_user_id": user.pk,
        "completed_by_name": display_name(user),
        "completed_at": now.isoformat(),
    })

    # Compare-and-set guarantees that an ANYONE_DO work item has only one
    # winner, even when two eligible members tap the button almost together.
    completed = WorkItem.objects.filter(
        pk=workitem.pk,
        status="OPEN",
    ).update(
        status="DONE",
        done_by_openid=user.wx_openid or "",
        done_at=now,
        last_activity_at=now,
        payload=payload,
        updated_at=now,
    )
    if completed != 1:
        raise OrderPlanReceiptError("该协作任务已由其他成员完成，请刷新页面。")

    workitem.status = "DONE"
    workitem.done_by_openid = user.wx_openid or ""
    workitem.done_at = now
    workitem.last_activity_at = now
    workitem.payload = payload
    workitem.user_tasks.filter(to_openid=user.wx_openid).update(
        state="DONE",
        need_action=False,
        done_at=now,
        done_reason="SELF_ACTION",
        display_title_snapshot=actor_title,
        display_subtitle_snapshot="由你完成",
        cursor_time=now,
        updated_at=now,
    )
    workitem.user_tasks.exclude(to_openid=user.wx_openid).update(
        state="DONE",
        need_action=False,
        done_at=now,
        done_reason="OTHER_ACTION",
        display_title_snapshot=peer_title,
        display_subtitle_snapshot=(
            f"{display_name(user)}已处理，同组织无需重复操作"
        ),
        cursor_time=now,
        updated_at=now,
    )


def _existing_plan_event(*, device, event_type, plan):
    """Return the event already written for this device and order plan."""
    events = LifecycleEvent.objects.filter(
        device=device,
        event_type=event_type,
        from_organization=plan.factory_organization,
        to_organization=plan.merchant_organization,
    ).order_by("-id")
    for event in events:
        data = event.data if isinstance(event.data, dict) else {}
        if (
            data.get("order_plan_id") == plan.pk
            or data.get("plan_code") == plan.plan_code
        ):
            return event
    return None


def _devices(plan):
    result = []
    for item in plan.items.select_related("product_model"):
        devices = []
        if item.production_quantity > 0:
            batch = ProductionBatch.objects.filter(
                batch_no=item.order_batch_code,
                manufacturer=plan.factory_organization,
                product_model=item.product_model,
            ).first()
            if not batch:
                raise OrderPlanReceiptError(
                    f"订单批次 {item.order_batch_code} 尚未生成新设备。"
                )
            devices = list(Device.objects.filter(batch=batch).order_by("id"))
            if len(devices) != item.production_quantity:
                raise OrderPlanReceiptError(
                    f"{item.product_model.name} 应新生产 {item.production_quantity} 台，"
                    f"实际找到 {len(devices)} 台。"
                )
        reissue_devices = [
            allocation.source_device
            for allocation in item.return_inventory_allocations.select_related(
                "source_device", "replacement_qr"
            ).order_by("id")
        ]
        if len(reissue_devices) != item.return_inventory_quantity:
            raise OrderPlanReceiptError(
                f"{item.product_model.name} 应调用退货库存 "
                f"{item.return_inventory_quantity} 台，实际找到 {len(reissue_devices)} 台。"
            )
        if len(devices) + len(reissue_devices) != item.quantity:
            raise OrderPlanReceiptError(
                f"{item.product_model.name} 新生产与退货库存合计数量不正确。"
            )
        result.extend(devices + reissue_devices)
    return result


def mark_devices_shipped(*, plan, user, now):
    for device in _devices(plan):
        event = _existing_plan_event(
            device=device,
            event_type="SHIP",
            plan=plan,
        )
        if not event:
            event = LifecycleEvent.objects.create(
                device=device, event_type="SHIP", operator=user,
                organization=plan.factory_organization,
                from_organization=plan.factory_organization,
                to_organization=plan.merchant_organization,
                data={"order_plan_id": plan.pk, "plan_code": plan.plan_code},
            )
        DeviceState.objects.update_or_create(
            device=device,
            defaults={
                "current_organization": plan.factory_organization,
                "status": "SHIPPED", "last_event": event,
                "last_event_time": now,
            },
        )
        OrganizationDevice.objects.update_or_create(
            device=device,
            defaults={
                "organization": plan.factory_organization,
                "status": "TRANSFERRED", "delivered_at": now,
                "last_event": event,
            },
        )
    mark_return_inventory_reissued(plan=plan)


def _mark_devices_received(*, plan, user, now):
    for device in _devices(plan):
        event = _existing_plan_event(
            device=device,
            event_type="RECEIVE",
            plan=plan,
        )
        if not event:
            event = LifecycleEvent.objects.create(
                device=device, event_type="RECEIVE", operator=user,
                organization=plan.merchant_organization,
                from_organization=plan.factory_organization,
                to_organization=plan.merchant_organization,
                data={"order_plan_id": plan.pk, "plan_code": plan.plan_code},
            )
        DeviceState.objects.update_or_create(
            device=device,
            defaults={
                "current_organization": plan.merchant_organization,
                "status": "IN_STOCK", "last_event": event,
                "last_event_time": now,
            },
        )
        OrganizationDevice.objects.update_or_create(
            device=device,
            defaults={
                "organization": plan.merchant_organization,
                "status": "IN_STOCK", "received_at": now,
                "last_event": event,
            },
        )


@transaction.atomic
def confirm_merchant_receipt(*, user, plan_id):
    identity = _identity(user, MERCHANT_RECEIPT_ROLES, "MERCHANT")
    plan = OrderPlan.objects.select_for_update().select_related(
        "factory_organization", "merchant_organization", "brand", "created_by"
    ).filter(pk=plan_id, merchant_organization=identity.organization).first()
    if not plan:
        raise OrderPlanReceiptError("订单不存在或不属于当前商家。")
    if plan.status == OrderPlan.Status.RECEIVED:
        return {
            "received_by_name": (
                display_name(plan.received_by) if plan.received_by else "其他成员"
            )
        }, False
    if plan.status != OrderPlan.Status.SHIPPED:
        raise OrderPlanReceiptError("订单尚未发货，不能入库。")
    workitem = WorkItem.objects.select_for_update().filter(
        type="ORDER_PLAN_MERCHANT_RECEIPT",
        uniq_key=f"sales:order-plan:{plan.pk}:merchant-receipt",
    ).first()
    if not workitem:
        raise OrderPlanReceiptError("未找到商家入库任务。")
    now = timezone.now()
    actor_name = display_name(user)
    _complete_anyone(
        workitem,
        user,
        now,
        actor_title="订单入库已完成",
        peer_title=f"{actor_name}已完成订单入库",
    )
    _mark_devices_received(plan=plan, user=user, now=now)
    plan.status = OrderPlan.Status.RECEIVED
    plan.received_at = now
    plan.received_by = user
    plan.save(update_fields=("status", "received_at", "received_by", "updated_at"))
    plan.items.update(status=OrderPlan.Status.RECEIVED)

    payload = {"order_plan_id": plan.pk, "plan_code": plan.plan_code,
               "received_by_name": display_name(user)}
    if plan.created_by.wx_openid:
        publish_workitem(
            domain="SALES", type="ORDER_PLAN_RECEIVED", biz_id=plan.plan_code,
            biz_ref=f"sales.OrderPlan:{plan.pk}",
            uniq_key=f"sales:order-plan:{plan.pk}:received:sales",
            title="商家已完成订单入库",
            summary=f"{plan.merchant_organization.name}｜{display_name(user)}已入库",
            link="/pages/sales/order_plan_list/order_plan_list",
            recipients_openids=(plan.created_by.wx_openid,), need_action=False,
            action_mode="EACH_ACK", payload=payload,
            actor_openid=user.wx_openid or "", mark_workitem_done_immediately=True,
        )
    assistant_openids = role_openids(plan.factory_organization, {"factory_sales_assistant"})
    if assistant_openids:
        publish_workitem(
            domain="SALES", type="ORDER_PLAN_ACCOUNTING_CONFIRM", biz_id=plan.plan_code,
            biz_ref=f"sales.OrderPlan:{plan.pk}",
            uniq_key=f"sales:order-plan:{plan.pk}:accounting-confirm",
            title="订单待入账", summary=f"{plan.merchant_organization.name}｜共{plan.total_quantity}台",
            link="/pages/sales/order_plan_list/order_plan_list",
            recipients_openids=assistant_openids, need_action=True,
            action_mode="ANYONE_DO", payload=payload,
            actor_openid=user.wx_openid or "", mark_workitem_done_immediately=False,
        )
    return {**payload, "received_at": now.isoformat()}, True


@transaction.atomic
def confirm_order_accounting(*, user, plan_id):
    identity = _identity(user, {"factory_sales_assistant"}, "OWNER")
    plan = OrderPlan.objects.select_for_update().filter(
        pk=plan_id, factory_organization=identity.organization
    ).first()
    if not plan:
        raise OrderPlanReceiptError("订单不存在或不属于当前厂家。")
    if plan.accounted_at:
        return {
            "accounted_by_name": (
                display_name(plan.accounted_by) if plan.accounted_by else "其他成员"
            )
        }, False
    if plan.status != OrderPlan.Status.RECEIVED:
        raise OrderPlanReceiptError("商家尚未入库，不能确认入账。")
    workitem = WorkItem.objects.select_for_update().filter(
        type="ORDER_PLAN_ACCOUNTING_CONFIRM",
        uniq_key=f"sales:order-plan:{plan.pk}:accounting-confirm",
    ).first()
    if not workitem:
        raise OrderPlanReceiptError("未找到入账任务。")
    now = timezone.now()
    actor_name = display_name(user)
    _complete_anyone(
        workitem,
        user,
        now,
        actor_title="订单入账已确认",
        peer_title=f"{actor_name}已确认订单入账",
    )
    plan.accounted_at = now
    plan.accounted_by = user
    plan.save(update_fields=("accounted_at", "accounted_by", "updated_at"))
    return {"accounted_by_name": display_name(user), "accounted_at": now.isoformat()}, True
