from django.db import transaction
from django.utils import timezone

from account.services.auth_service import get_active_identity
from quality_trace.models import (
    QualityRecallCampaign,
    QualityRecallCustomerHandover,
    QualityRecallDevice,
    QualityRecallDeviceEvent,
    QualityRecallHandoverEvidence,
    QualityTraceEvent,
)
from workflow.models import UserTask, WorkItem

from .case_service import CONTROL_MANAGER_ROLES, _identity
from .recall_service import QualityRecallError


MERCHANT_HANDOVER_ROLES = {
    "merchant_owner", "merchant_manager", "merchant_senior_manager", "merchant_sales",
}


def _group(row, key):
    return next(
        (group for group in (row.responsibility_snapshot or {}).get("groups", [])
         if group.get("key") == key),
        None,
    )


def _active_identity(user):
    identity = get_active_identity(user)
    if not identity:
        raise QualityRecallError("当前账号没有有效组织身份。")
    return identity


def _complete_task_type(row, task_type, user):
    now = timezone.now()
    ids = (row.responsibility_snapshot or {}).get("workitem_ids") or []
    workitems = WorkItem.objects.select_for_update().filter(
        pk__in=ids, type=task_type, status="OPEN",
        biz_ref__endswith=f":Device:{row.device_id}",
    )
    for item in workitems:
        item.status = "DONE"
        item.done_by_openid = user.wx_openid or ""
        item.done_at = now
        item.last_activity_at = now
        item.save(update_fields=(
            "status", "done_by_openid", "done_at", "last_activity_at", "updated_at",
        ))
        UserTask.objects.filter(workitem=item).exclude(state="DONE").update(
            state="DONE", done_at=now, done_reason="SELF_ACTION",
            cursor_time=now, updated_at=now,
        )


def _audit(handover, event_type, user, role, payload=None):
    QualityTraceEvent.objects.create(
        quality_case=handover.campaign.quality_case,
        alert=handover.campaign.quality_case.source_alert,
        event_type=event_type,
        actor=user,
        actor_role_snapshot=role,
        payload={
            "recall_campaign_id": handover.campaign_id,
            "recall_device_id": handover.recall_device_id,
            "handover_id": handover.pk,
            **(payload or {}),
        },
    )


@transaction.atomic
def initialize_customer_handover(*, user, recall_device_id):
    identity = _identity(user, CONTROL_MANAGER_ROLES)
    row = (
        QualityRecallDevice.objects.select_for_update()
        .select_related("campaign__quality_case")
        .filter(
            pk=recall_device_id,
            campaign__factory_organization=identity.organization,
            campaign__status=QualityRecallCampaign.Status.ACTIVE,
        ).first()
    )
    if not row:
        raise QualityRecallError("召回设备不存在或不属于当前厂家。")
    if (row.responsibility_snapshot or {}).get("mode") != "ACTIVATED_USER":
        raise QualityRecallError("只有已激活终端用户设备需要建立用户交接单。")
    customer = _group(row, "customer_ack")
    merchant = _group(row, "merchant_coordinate")
    if not customer or not merchant:
        raise QualityRecallError("召回责任链缺少用户或来源商家。")
    handover, created = QualityRecallCustomerHandover.objects.get_or_create(
        recall_device=row,
        defaults={
            "campaign": row.campaign,
            "customer_organization_id": customer["organization_id"],
            "merchant_organization_id": merchant["organization_id"],
            "customer_snapshot": customer,
            "merchant_snapshot": merchant,
        },
    )
    if created:
        _audit(handover, "RECALL_CUSTOMER_HANDOVER_INITIALIZED", user, identity.role.code)
    return handover, created


@transaction.atomic
def acknowledge_customer_recall(*, user, handover_id, note=""):
    identity = _active_identity(user)
    handover = (
        QualityRecallCustomerHandover.objects.select_for_update()
        .select_related("campaign__quality_case", "recall_device")
        .get(pk=handover_id)
    )
    if identity.organization_id != handover.customer_organization_id or identity.role.code != "customer_owner":
        raise QualityRecallError("只有该设备所属终端用户负责人可以确认召回。")
    if handover.status != QualityRecallCustomerHandover.Status.PENDING_CUSTOMER_ACK:
        raise QualityRecallError("当前交接单不处于待用户确认状态。")
    now = timezone.now()
    handover.status = QualityRecallCustomerHandover.Status.ACKNOWLEDGED
    handover.customer_confirmed_by = user
    handover.customer_confirmed_at = now
    handover.customer_note = str(note or "").strip()
    handover.save(update_fields=(
        "status", "customer_confirmed_by", "customer_confirmed_at",
        "customer_note", "updated_at",
    ))
    row = handover.recall_device
    old = row.status
    row.status = QualityRecallDevice.Status.ACKNOWLEDGED
    row.status_updated_at = now
    row.save(update_fields=("status", "status_updated_at"))
    _complete_task_type(row, "QUALITY_RECALL_CUSTOMER_ACK", user)
    QualityRecallDeviceEvent.objects.create(
        campaign=handover.campaign, recall_device=row, from_status=old,
        to_status=row.status, reason="终端用户已确认召回通知", actor=user,
        actor_role_snapshot=identity.role.code, payload={"handover_id": handover.pk},
    )
    _audit(handover, "RECALL_CUSTOMER_ACKNOWLEDGED", user, identity.role.code)
    return handover


@transaction.atomic
def schedule_customer_pickup(*, user, handover_id, pickup_scheduled_at, pickup_address, note=""):
    identity = _active_identity(user)
    handover = QualityRecallCustomerHandover.objects.select_for_update().select_related(
        "campaign__quality_case",
    ).get(pk=handover_id)
    if (
        identity.organization_id != handover.merchant_organization_id
        or identity.role.code not in MERCHANT_HANDOVER_ROLES
    ):
        raise QualityRecallError("只有来源商家的负责人、管理人员或销售可以预约取回。")
    if handover.status != QualityRecallCustomerHandover.Status.ACKNOWLEDGED:
        raise QualityRecallError("用户确认召回后才能预约取回。")
    address = str(pickup_address or "").strip()
    if not address or not pickup_scheduled_at:
        raise QualityRecallError("请填写取回时间和地址。")
    handover.status = QualityRecallCustomerHandover.Status.PICKUP_SCHEDULED
    handover.pickup_scheduled_by = user
    handover.pickup_scheduled_at = pickup_scheduled_at
    handover.pickup_address = address
    handover.pickup_note = str(note or "").strip()
    handover.save(update_fields=(
        "status", "pickup_scheduled_by", "pickup_scheduled_at",
        "pickup_address", "pickup_note", "updated_at",
    ))
    _audit(handover, "RECALL_CUSTOMER_PICKUP_SCHEDULED", user, identity.role.code)
    return handover


@transaction.atomic
def add_handover_evidence(*, user, handover_id, kind, client_request_id, evidence):
    identity = _active_identity(user)
    handover = QualityRecallCustomerHandover.objects.select_for_update().get(pk=handover_id)
    if identity.organization_id not in {
        handover.customer_organization_id, handover.merchant_organization_id,
    }:
        raise QualityRecallError("当前账号不属于该召回交接链。")
    data = dict(evidence or {})
    storage_key = str(data.get("storage_key") or "").strip()
    request_id = str(client_request_id or "").strip()
    if not storage_key or not request_id:
        raise QualityRecallError("交接凭证缺少存储标识或请求标识。")
    valid = {value for value, _ in QualityRecallHandoverEvidence.Kind.choices}
    if kind not in valid:
        raise QualityRecallError("交接凭证类型无效。")
    row, _created = QualityRecallHandoverEvidence.objects.get_or_create(
        handover=handover, client_request_id=request_id,
        defaults={
            "kind": kind, "storage_key": storage_key,
            "original_name": str(data.get("original_name") or ""),
            "content_type": str(data.get("content_type") or ""),
            "file_size": max(0, int(data.get("file_size") or 0)),
            "sha256": str(data.get("sha256") or ""),
            "remark": str(data.get("remark") or ""), "uploaded_by": user,
        },
    )
    return row


@transaction.atomic
def confirm_merchant_handover_receipt(*, user, handover_id, receipt_note):
    identity = _active_identity(user)
    handover = (
        QualityRecallCustomerHandover.objects.select_for_update()
        .select_related("campaign__quality_case", "recall_device")
        .get(pk=handover_id)
    )
    if (
        identity.organization_id != handover.merchant_organization_id
        or identity.role.code not in MERCHANT_HANDOVER_ROLES
    ):
        raise QualityRecallError("只有来源商家的授权岗位可以验收召回设备。")
    if handover.status != QualityRecallCustomerHandover.Status.PICKUP_SCHEDULED:
        raise QualityRecallError("只有已预约取回的设备可以验收接管。")
    if not handover.evidence.filter(
        kind=QualityRecallHandoverEvidence.Kind.MERCHANT_RECEIPT,
    ).exists():
        raise QualityRecallError("请先上传商家验收凭证。")
    note = str(receipt_note or "").strip()
    if not note:
        raise QualityRecallError("请填写商家验收说明。")
    now = timezone.now()
    evidence_ids = list(handover.evidence.values_list("id", flat=True))
    handover.status = QualityRecallCustomerHandover.Status.MERCHANT_RECEIVED
    handover.merchant_received_by = user
    handover.merchant_received_at = now
    handover.receipt_note = note
    handover.custody_snapshot = {
        "custodian_type": "RECALL_MERCHANT_CUSTODY",
        "organization_id": identity.organization_id,
        "organization_name": identity.organization.name,
        "received_by_user_id": user.pk,
        "received_at": now.isoformat(),
        "evidence_ids": evidence_ids,
        "inventory_state_changed": False,
    }
    handover.save(update_fields=(
        "status", "merchant_received_by", "merchant_received_at", "receipt_note",
        "custody_snapshot", "updated_at",
    ))
    row = handover.recall_device
    old = row.status
    row.status = QualityRecallDevice.Status.RETURN_IN_PROGRESS
    row.status_updated_at = now
    row.save(update_fields=("status", "status_updated_at"))
    _complete_task_type(row, "QUALITY_RECALL_MERCHANT_COORDINATE", user)
    QualityRecallDeviceEvent.objects.create(
        campaign=handover.campaign, recall_device=row, from_status=old,
        to_status=row.status, reason="来源商家已验收并接管召回设备", actor=user,
        actor_role_snapshot=identity.role.code,
        payload={"handover_id": handover.pk, "evidence_ids": evidence_ids},
    )
    _audit(
        handover, "RECALL_CUSTOMER_HANDOVER_RECEIVED", user, identity.role.code,
        {"evidence_ids": evidence_ids, "inventory_state_changed": False},
    )
    return handover
