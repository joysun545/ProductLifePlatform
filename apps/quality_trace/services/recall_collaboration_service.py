from django.db import transaction
from django.utils import timezone

from account.models import UserOrganization
from lifecycle.models import CustomerDeviceBinding, DriverVehicleBinding
from workflow.models import UserTask, WorkItem
from workflow.services.workflow_service import publish_workitem


FACTORY_RECALL_COLLABORATION_ROLES = {
    "factory_chief_engineer",
    "factory_sales",
    "factory_sales_assistant",
    "factory_logistics",
}
FACTORY_PICKUP_REQUEST_ROLES = {
    "factory_chief_engineer",
    "factory_logistics",
}
MERCHANT_RECALL_RETURN_ROLES = {
    "merchant_owner",
    "merchant_manager",
    "merchant_senior_manager",
    "merchant_sales",
    "merchant_stock",
}


def organization_recipients(organization, roles):
    return list(
        UserOrganization.objects.filter(
            organization=organization,
            role__code__in=roles,
            role__is_active=True,
            user__is_active=True,
            is_active=True,
        )
        .exclude(user__wx_openid__isnull=True)
        .exclude(user__wx_openid="")
        .values_list("user__wx_openid", flat=True)
        .distinct()
    )


def customer_device_recipients(handover):
    customer = handover.customer_organization
    openids = list(
        UserOrganization.objects.filter(
            organization=customer,
            role__code="customer_owner",
            role__is_active=True,
            user__is_active=True,
            is_active=True,
        )
        .exclude(user__wx_openid__isnull=True)
        .exclude(user__wx_openid="")
        .values_list("user__wx_openid", flat=True)
    )
    binding = CustomerDeviceBinding.objects.filter(
        device_id=handover.recall_device.device_id,
        customer_organization=customer,
        status=CustomerDeviceBinding.Status.ACTIVE,
        vehicle_id__isnull=False,
    ).first()
    if binding:
        driver_ids = DriverVehicleBinding.objects.filter(
            vehicle_id=binding.vehicle_id,
            is_active=True,
        ).values_list("driver_id", flat=True)
        openids.extend(
            UserOrganization.objects.filter(
                organization=customer,
                role__code="driver",
                role__is_active=True,
                user_id__in=driver_ids,
                user__is_active=True,
                is_active=True,
            )
            .exclude(user__wx_openid__isnull=True)
            .exclude(user__wx_openid="")
            .values_list("user__wx_openid", flat=True)
        )
    return list(dict.fromkeys(openids))


def _recall_link(handover):
    return (
        "/pages/quality_trace/recall_detail/recall_detail"
        f"?campaign_id={handover.campaign_id}&recall_device_id={handover.recall_device_id}"
    )


@transaction.atomic
def close_workitem(uniq_key, *, user, reason):
    item = WorkItem.objects.select_for_update().filter(
        uniq_key=uniq_key,
        status="OPEN",
    ).first()
    if not item:
        return None
    now = timezone.now()
    item.status = "DONE"
    item.done_by_openid = user.wx_openid or ""
    item.done_at = now
    item.last_activity_at = now
    item.save(update_fields=(
        "status", "done_by_openid", "done_at", "last_activity_at", "updated_at",
    ))
    UserTask.objects.filter(workitem=item).exclude(state="DONE").update(
        state="DONE",
        done_at=now,
        done_reason=reason,
        cursor_time=now,
        updated_at=now,
    )
    return item


def publish_customer_pickup_dispatch_task(handover, *, user):
    recipients = customer_device_recipients(handover)
    if not recipients:
        return None
    payload = handover.method_payload if isinstance(handover.method_payload, dict) else {}
    return publish_workitem(
        domain="SYSTEM",
        type="QUALITY_RECALL_CUSTOMER_PICKUP_DISPATCH",
        biz_id=str(handover.campaign_id),
        biz_ref=f"QualityRecallCustomerHandover:{handover.pk}",
        uniq_key=f"quality-recall-pickup-customer-dispatch:{handover.pk}",
        title=f"厂家已安排召回取件：{handover.recall_device.sn_snapshot}",
        summary=(
            f"预约时间：{handover.pickup_scheduled_at.isoformat() if handover.pickup_scheduled_at else ''}；"
            f"承运方：{payload.get('accepted_logistics_provider') or '待物流联系'}。"
            "设备交给物流后，请上传物流单据或取件凭证并确认已发出。"
        ),
        link=_recall_link(handover),
        recipients_openids=recipients,
        need_action=True,
        action_mode="ANYONE_DO",
        payload={
            "recall_campaign_id": handover.campaign_id,
            "recall_device_id": handover.recall_device_id,
            "handover_id": handover.pk,
            "delivery_method": handover.delivery_method,
            "pickup_scheduled_at": (
                handover.pickup_scheduled_at.isoformat()
                if handover.pickup_scheduled_at else ""
            ),
        },
        actor_openid=user.wx_openid or "",
        mark_workitem_done_immediately=False,
    )


def close_customer_pickup_dispatch_task(handover, *, user):
    return close_workitem(
        f"quality-recall-pickup-customer-dispatch:{handover.pk}",
        user=user,
        reason="RECALL_CUSTOMER_CONFIRMED_PICKUP_DISPATCH",
    )


def publish_merchant_return_task(handover, organization, *, user):
    recipients = organization_recipients(organization, MERCHANT_RECALL_RETURN_ROLES)
    if not recipients:
        return None
    return publish_workitem(
        domain="SYSTEM",
        type="QUALITY_RECALL_MERCHANT_RETURN",
        biz_id=str(handover.campaign_id),
        biz_ref=f"QualityRecallCustomerHandover:{handover.pk}",
        uniq_key=f"quality-recall-merchant-return:{handover.recall_device_id}:org:{organization.pk}",
        title=f"请逐级退回召回设备：{handover.recall_device.sn_snapshot}",
        summary=(
            f"设备已由{organization.name}接管。请建立召回专用退回程次，"
            "上传物流单据和实物包装照片后发往上级组织。"
        ),
        link=_recall_link(handover),
        recipients_openids=recipients,
        need_action=True,
        action_mode="ANYONE_DO",
        payload={
            "recall_campaign_id": handover.campaign_id,
            "recall_device_id": handover.recall_device_id,
            "handover_id": handover.pk,
            "custodian_organization_id": organization.pk,
        },
        actor_openid=user.wx_openid or "",
        mark_workitem_done_immediately=False,
    )


def close_merchant_return_task(handover, organization, *, user):
    return close_workitem(
        f"quality-recall-merchant-return:{handover.recall_device_id}:org:{organization.pk}",
        user=user,
        reason="RECALL_NEXT_RETURN_LEG_CREATED",
    )


def publish_factory_recall_progress(handover, *, user, stage, summary):
    factory = handover.campaign.factory_organization
    recipients = organization_recipients(factory, FACTORY_RECALL_COLLABORATION_ROLES)
    if not recipients:
        return None
    return publish_workitem(
        domain="SYSTEM",
        type="QUALITY_RECALL_DEVICE_PROGRESS",
        biz_id=str(handover.campaign_id),
        biz_ref=f"QualityRecallCustomerHandover:{handover.pk}",
        uniq_key=f"quality-recall-device-progress:{handover.recall_device_id}",
        title=f"召回设备进度：{handover.recall_device.sn_snapshot}",
        summary=summary,
        link=_recall_link(handover),
        recipients_openids=recipients,
        need_action=False,
        action_mode="EACH_ACK",
        payload={
            "recall_campaign_id": handover.campaign_id,
            "recall_device_id": handover.recall_device_id,
            "handover_id": handover.pk,
            "stage": stage,
            "delivery_method": handover.delivery_method,
        },
        actor_openid=user.wx_openid or "",
        mark_workitem_done_immediately=False,
    )


def close_factory_recall_progress(handover, *, user):
    return close_workitem(
        f"quality-recall-device-progress:{handover.recall_device_id}",
        user=user,
        reason="RECALL_DEVICE_FACTORY_ISOLATED",
    )
