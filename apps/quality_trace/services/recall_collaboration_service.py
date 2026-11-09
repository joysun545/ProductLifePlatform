from django.db import transaction
from django.utils import timezone

from account.models import UserOrganization
from account.services.role_delegation_service import vacant_role_manager_openids
from lifecycle.models import CustomerDeviceBinding, DriverVehicleBinding
from workflow.models import UserTask, WorkItem
from workflow.services.workflow_service import publish_workitem


FACTORY_RECALL_COLLABORATION_ROLES = {
    "factory_chief_engineer",
    "factory_sales_assistant",
    "factory_logistics",
}
FACTORY_PICKUP_REQUEST_ROLES = {
    "factory_chief_engineer",
    "factory_logistics",
}
FACTORY_LOGISTICS_ROLES = {"factory_logistics"}
FACTORY_INVENTORY_POSTING_ROLES = {"factory_sales_assistant"}
FACTORY_RECEIPT_AWARENESS_ROLES = {
    "factory_chief_engineer", "factory_sales_assistant",
}
FACTORY_POSTING_AWARENESS_ROLES = {
    "factory_chief_engineer",
}
MERCHANT_RECALL_RETURN_ROLES = {
    "merchant_owner",
    "merchant_manager",
    "merchant_senior_manager",
    "merchant_sales",
    "merchant_stock",
}


def factory_row_recipients(organization, roles, row):
    from .recall_access_service import RecallViewer
    recipients = organization_recipients(organization, roles)
    for identity in UserOrganization.objects.select_related("user").filter(
            organization=organization, role__code="factory_sales", is_active=True,
            role__is_active=True, user__is_active=True):
        if identity.user.wx_openid and RecallViewer(identity.user).allows(row):
            recipients.append(identity.user.wx_openid)
    return list(dict.fromkeys(recipients))


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


def organization_action_recipients(organization, roles):
    return [
        *organization_recipients(organization, roles),
        *vacant_role_manager_openids(organization, roles),
    ]


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
    recipients = factory_row_recipients(factory, FACTORY_RECALL_COLLABORATION_ROLES, handover.recall_device)
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


def publish_factory_transport_tasks(leg, *, user):
    """At factory arrival, logistics acts and the other factory roles observe."""
    factory = leg.to_organization
    base = {
        "recall_campaign_id": leg.campaign_id,
        "transport_leg_id": leg.pk,
        "recall_device_id": leg.recall_device_id,
        "device_sn": leg.recall_device.sn_snapshot,
        "stage": "FACTORY_PENDING_RECEIPT",
    }
    link = (
        "/pages/quality_trace/recall_detail/recall_detail"
        f"?campaign_id={leg.campaign_id}&recall_device_id={leg.recall_device_id}"
    )
    logistics = organization_action_recipients(factory, FACTORY_LOGISTICS_ROLES)
    if not logistics:
        raise ValueError("厂家没有有效的厂家物流经理账号。")
    action = publish_workitem(
        domain="SYSTEM", type="QUALITY_RECALL_FACTORY_RECEIPT",
        biz_id=str(leg.campaign_id), biz_ref=f"QualityRecallTransportLeg:{leg.pk}",
        uniq_key=f"quality-recall-transport:{leg.pk}:receive",
        title=f"召回设备待厂家收货：{leg.recall_device.sn_snapshot}",
        summary="请厂家物流经理核对线下到货及凭证，并确认货已收到。",
        link=link, recipients_openids=logistics, need_action=True,
        action_mode="ANYONE_DO", payload=base,
        actor_openid=user.wx_openid or "", mark_workitem_done_immediately=False,
    )
    # publish_workitem updates an existing item by uniq_key but deliberately
    # does not rewrite its type.  Upgrade R13.8.5.4 pre-existing receipt items
    # so inbox grouping and operational audits use the new factory-only type.
    if action.type != "QUALITY_RECALL_FACTORY_RECEIPT":
        action.type = "QUALITY_RECALL_FACTORY_RECEIPT"
        action.save(update_fields=("type", "updated_at"))
    action.user_tasks.exclude(to_openid__in=logistics).delete()
    awareness = factory_row_recipients(factory, FACTORY_RECEIPT_AWARENESS_ROLES, leg.recall_device)
    notice = None
    if awareness:
        notice = publish_workitem(
            domain="SYSTEM", type="QUALITY_RECALL_FACTORY_RECEIPT_NOTICE",
            biz_id=str(leg.campaign_id), biz_ref=f"QualityRecallTransportLeg:{leg.pk}",
            uniq_key=f"quality-recall-transport:{leg.pk}:receive-notice",
            title=f"召回设备正在返厂：{leg.recall_device.sn_snapshot}",
            summary="厂家物流经理负责线下收货确认；本任务仅用于知晓和跟踪。",
            link=link, recipients_openids=awareness, need_action=False,
            action_mode="EACH_ACK", payload=base,
            actor_openid=user.wx_openid or "", mark_workitem_done_immediately=False,
        )
        notice.user_tasks.exclude(to_openid__in=awareness).delete()
    return action, notice


def publish_inventory_posting_task(leg, *, user):
    recipients = organization_action_recipients(
        leg.to_organization, FACTORY_INVENTORY_POSTING_ROLES,
    )
    if not recipients:
        raise ValueError("厂家没有有效的厂家销售助理账号。")
    item = publish_workitem(
        domain="SYSTEM", type="QUALITY_RECALL_INVENTORY_POSTING",
        biz_id=str(leg.campaign_id), biz_ref=f"QualityRecallTransportLeg:{leg.pk}",
        uniq_key=f"quality-recall-transport:{leg.pk}:inventory-posting",
        title=f"召回设备待入库记账：{leg.recall_device.sn_snapshot}",
        summary="厂家物流经理已确认收货。请销售助理登记召回专用隔离位置并确认入库记账。",
        link=("/pages/quality_trace/recall_detail/recall_detail"
              f"?campaign_id={leg.campaign_id}&recall_device_id={leg.recall_device_id}"),
        recipients_openids=recipients, need_action=True, action_mode="ANYONE_DO",
        payload={"recall_campaign_id": leg.campaign_id, "transport_leg_id": leg.pk,
                 "recall_device_id": leg.recall_device_id,
                 "stage": "FACTORY_RECEIVED_PENDING_POSTING"},
        actor_openid=user.wx_openid or "", mark_workitem_done_immediately=False,
    )
    item.user_tasks.exclude(to_openid__in=recipients).delete()
    return item


def publish_inventory_posted_notice(receipt, *, user):
    recipients = factory_row_recipients(
        receipt.factory_organization, FACTORY_POSTING_AWARENESS_ROLES, receipt.recall_device,
    )
    if not recipients:
        return None
    return publish_workitem(
        domain="SYSTEM", type="QUALITY_RECALL_INVENTORY_POSTED_NOTICE",
        biz_id=str(receipt.campaign_id), biz_ref=f"QualityRecallIsolationReceipt:{receipt.pk}",
        uniq_key=f"quality-recall-device:{receipt.recall_device_id}:inventory-posted-notice",
        title=f"召回设备已入库记账：{receipt.recall_device.sn_snapshot}",
        summary=f"销售助理已完成召回隔离入库记账；位置：{receipt.isolation_location}。",
        link=("/pages/quality_trace/recall_detail/recall_detail"
              f"?campaign_id={receipt.campaign_id}&recall_device_id={receipt.recall_device_id}"),
        recipients_openids=recipients, need_action=False, action_mode="EACH_ACK",
        payload={"recall_campaign_id": receipt.campaign_id,
                 "recall_device_id": receipt.recall_device_id,
                 "stage": "FACTORY_INVENTORY_POSTED"},
        actor_openid=user.wx_openid or "", mark_workitem_done_immediately=False,
    )
