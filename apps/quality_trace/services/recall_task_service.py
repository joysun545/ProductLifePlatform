from django.db import transaction
from django.utils import timezone

from account.models import UserOrganization
from lifecycle.models import (
    CustomerDeviceBinding,
    DeviceState,
    GoodsTransfer,
    GoodsTransferItem,
    OrganizationDevice,
)
from quality_trace.models import (
    QualityRecallCampaign,
    QualityRecallDevice,
    QualityRecallDeviceEvent,
    QualityTraceEvent,
)
from workflow.services.workflow_service import publish_workitem

from .case_service import CONTROL_MANAGER_ROLES, _identity
from .recall_service import QualityRecallError


ACTIVE_TRANSFER_STATES = {
    GoodsTransfer.Status.SCANNING,
    GoodsTransfer.Status.PENDING_RECEIPT,
    GoodsTransfer.Status.PENDING_FACTORY_PICKUP,
    GoodsTransfer.Status.PENDING_FACTORY_POSTING,
}


def _recipients(organization_id, role_codes):
    rows = (
        UserOrganization.objects.select_related("user", "role")
        .filter(
            organization_id=organization_id,
            role__code__in=role_codes,
            role__is_active=True,
            user__is_active=True,
            is_active=True,
        )
        .exclude(user__wx_openid__isnull=True)
        .exclude(user__wx_openid="")
        .order_by("role__code", "user_id")
    )
    return [
        {
            "openid": row.user.wx_openid,
            "user_id": row.user_id,
            "identity_id": row.pk,
            "role": row.role.code,
            "name": row.user.wx_nickname or row.user.username,
        }
        for row in rows
    ]


def _group(key, organization, role_codes, task_type, title, summary):
    return {
        "key": key,
        "organization_id": organization.pk,
        "organization_name": organization.name,
        "organization_type": organization.organization_type.code,
        "role_codes": list(role_codes),
        "task_type": task_type,
        "title": title,
        "summary": summary,
        "recipients": _recipients(organization.pk, role_codes),
    }


def resolve_recall_responsibility(recall_device):
    device = recall_device.device
    binding = (
        CustomerDeviceBinding.objects.select_related(
            "customer_organization__organization_type",
            "source_organization__organization_type",
        )
        .filter(device=device, status=CustomerDeviceBinding.Status.ACTIVE)
        .first()
    )
    if binding:
        return {
            "mode": "ACTIVATED_USER",
            "device_id": device.pk,
            "resolved_at": timezone.now().isoformat(),
            "groups": [
                _group(
                    "customer_ack",
                    binding.customer_organization,
                    ("customer_owner",),
                    "QUALITY_RECALL_CUSTOMER_ACK",
                    "请确认产品召回通知",
                    f"设备{device.sn}已进入召回范围，请确认知晓并等待商家联系。",
                ),
                _group(
                    "merchant_coordinate",
                    binding.source_organization,
                    ("merchant_owner", "merchant_manager", "merchant_sales"),
                    "QUALITY_RECALL_MERCHANT_COORDINATE",
                    "请联系用户并协助召回设备",
                    f"设备{device.sn}已激活，请联系当前用户并组织退回。",
                ),
            ],
        }

    transfer_item = (
        GoodsTransferItem.objects.select_related(
            "transfer__from_organization__organization_type",
            "transfer__to_organization__organization_type",
        )
        .filter(device=device, transfer__status__in=ACTIVE_TRANSFER_STATES)
        .order_by("-transfer__created_at", "-id")
        .first()
    )
    if transfer_item and transfer_item.transfer.status != GoodsTransfer.Status.SCANNING:
        transfer = transfer_item.transfer
        return {
            "mode": "IN_TRANSIT",
            "device_id": device.pk,
            "goods_transfer_id": transfer.pk,
            "flow_no": transfer.flow_no,
            "resolved_at": timezone.now().isoformat(),
            "groups": [
                _group(
                    "shipment_source",
                    transfer.from_organization,
                    ("merchant_owner", "merchant_manager", "merchant_sales", "merchant_stock"),
                    "QUALITY_RECALL_TRANSIT_SOURCE",
                    "在途设备进入召回，请停止后续交付",
                    f"设备{device.sn}正在流转，请协调停止交付并安排退回。",
                ),
                _group(
                    "shipment_target",
                    transfer.to_organization,
                    ("merchant_owner", "merchant_manager", "merchant_sales", "merchant_stock"),
                    "QUALITY_RECALL_TRANSIT_TARGET",
                    "在途设备进入召回，请隔离收货",
                    f"设备{device.sn}正在发往本组织，收到后请隔离并安排退回。",
                ),
            ],
        }

    ownership = (
        OrganizationDevice.objects.select_related("organization__organization_type")
        .filter(device=device)
        .first()
    )
    state = DeviceState.objects.filter(device=device).first()
    if ownership and ownership.organization.organization_type.code == "MERCHANT":
        return {
            "mode": "MERCHANT_INVENTORY",
            "device_id": device.pk,
            "device_state": state.status if state else "",
            "resolved_at": timezone.now().isoformat(),
            "groups": [
                _group(
                    "merchant_inventory",
                    ownership.organization,
                    ("merchant_owner", "merchant_manager", "merchant_sales", "merchant_stock"),
                    "QUALITY_RECALL_MERCHANT_RETURN",
                    "请隔离库存并退回召回设备",
                    f"库存设备{device.sn}已进入召回范围，请停止销售并安排退回。",
                ),
            ],
        }
    if ownership and ownership.organization.organization_type.code == "OWNER":
        return {
            "mode": "FACTORY_INVENTORY",
            "device_id": device.pk,
            "device_state": state.status if state else "",
            "resolved_at": timezone.now().isoformat(),
            "groups": [
                _group(
                    "factory_isolation",
                    ownership.organization,
                    ("factory_admin", "factory_chief_engineer"),
                    "QUALITY_RECALL_FACTORY_ISOLATE",
                    "请隔离厂家库存召回设备",
                    f"厂家库存设备{device.sn}已进入召回范围，请执行隔离。",
                ),
            ],
        }
    factory = recall_device.campaign.factory_organization
    return {
        "mode": "MANUAL_TRACE",
        "device_id": device.pk,
        "resolved_at": timezone.now().isoformat(),
        "groups": [
            _group(
                "factory_manual_trace",
                factory,
                ("factory_admin", "factory_chief_engineer"),
                "QUALITY_RECALL_MANUAL_TRACE",
                "召回设备位置待人工核查",
                f"暂时无法自动定位设备{device.sn}，请核查设备当前位置和责任人。",
            ),
        ],
    }


@transaction.atomic
def publish_recall_tasks(*, user, campaign_id):
    identity = _identity(user, CONTROL_MANAGER_ROLES)
    campaign = (
        QualityRecallCampaign.objects.select_for_update()
        .select_related("quality_case")
        .filter(pk=campaign_id, factory_organization=identity.organization)
        .first()
    )
    if not campaign:
        raise QualityRecallError("召回批次不存在或不属于当前厂家。")
    if campaign.status != QualityRecallCampaign.Status.ACTIVE:
        raise QualityRecallError("只有已发布的召回批次可以生成召回任务。")
    published = []
    for row in campaign.devices.select_for_update().select_related("device").order_by("device_id"):
        responsibility = resolve_recall_responsibility(row)
        task_ids = []
        for group in responsibility["groups"]:
            recipients = [item["openid"] for item in group["recipients"]]
            if not recipients:
                continue
            workitem = publish_workitem(
                domain="SYSTEM",
                type=group["task_type"],
                biz_id=str(campaign.pk),
                biz_ref=f"QualityRecallCampaign:{campaign.pk}:Device:{row.device_id}",
                uniq_key=(
                    f"quality-recall:{campaign.pk}:device:{row.device_id}:"
                    f"{group['key']}:org:{group['organization_id']}"
                ),
                title=group["title"],
                summary=group["summary"],
                link=(
                    "/pages/quality_trace/recall_detail/recall_detail"
                    f"?campaign_id={campaign.pk}&recall_device_id={row.pk}"
                ),
                recipients_openids=recipients,
                need_action=True,
                action_mode="ANYONE_DO",
                payload={
                    "recall_campaign_id": campaign.pk,
                    "recall_no": campaign.recall_no,
                    "recall_device_id": row.pk,
                    "device_id": row.device_id,
                    "device_sn": row.sn_snapshot,
                    "risk_level": campaign.risk_level,
                    "deadline": campaign.deadline.isoformat(),
                    "responsibility_mode": responsibility["mode"],
                    "responsibility_group": group,
                },
                actor_openid=user.wx_openid or "",
                mark_workitem_done_immediately=False,
            )
            task_ids.append(workitem.pk)
        if not task_ids:
            raise QualityRecallError(f"设备{row.sn_snapshot}未找到有效召回任务收件人。")
        old_status = row.status
        row.responsibility_snapshot = {
            **responsibility,
            "workitem_ids": task_ids,
            "published_at": timezone.now().isoformat(),
        }
        row.status = QualityRecallDevice.Status.NOTIFIED
        row.status_updated_at = timezone.now()
        row.save(update_fields=(
            "responsibility_snapshot", "status", "status_updated_at",
        ))
        QualityRecallDeviceEvent.objects.create(
            campaign=campaign,
            recall_device=row,
            from_status=old_status,
            to_status=QualityRecallDevice.Status.NOTIFIED,
            reason="已根据设备实时位置发布召回任务",
            actor=user,
            actor_role_snapshot=identity.role.code,
            payload={"mode": responsibility["mode"], "workitem_ids": task_ids},
        )
        published.append({
            "recall_device_id": row.pk,
            "device_id": row.device_id,
            "mode": responsibility["mode"],
            "workitem_ids": task_ids,
        })
    QualityTraceEvent.objects.create(
        quality_case=campaign.quality_case,
        alert=campaign.quality_case.source_alert,
        event_type="RECALL_TASKS_PUBLISHED",
        actor=user,
        actor_role_snapshot=identity.role.code,
        payload={
            "recall_campaign_id": campaign.pk,
            "recall_no": campaign.recall_no,
            "device_count": len(published),
            "workitem_count": sum(len(item["workitem_ids"]) for item in published),
        },
    )
    return published
