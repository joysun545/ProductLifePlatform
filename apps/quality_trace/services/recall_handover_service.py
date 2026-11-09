import uuid
from datetime import timedelta

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from account.services.auth_service import get_active_identity
from account.services.role_delegation_service import (
    has_open_action_task_copy,
    identity_has_role,
)
from lifecycle.models import CustomerDeviceBinding, DriverVehicleBinding
from quality_trace.models import (
    QualityRecallCampaign,
    QualityRecallCustomerHandover,
    QualityRecallDevice,
    QualityRecallDeviceEvent,
    QualityRecallHandoverEvidence,
    QualityRecallTransportLeg,
    QualityTraceEvent,
)
from workflow.models import UserTask, WorkItem
from workflow.services.workflow_service import publish_workitem

from .case_service import RECALL_MANAGER_ROLES, _identity
from .recall_collaboration_service import (
    FACTORY_PICKUP_REQUEST_ROLES,
    FACTORY_RECALL_COLLABORATION_ROLES,
    close_customer_pickup_dispatch_task,
    organization_action_recipients,
    publish_factory_transport_tasks,
    publish_customer_pickup_dispatch_task,
    publish_factory_recall_progress,
    publish_merchant_return_task,
)
from .recall_service import QualityRecallError
from .recall_task_service import complete_recall_scope_tasks_if_ready


MERCHANT_HANDOVER_ROLES = {
    "merchant_owner", "merchant_manager", "merchant_senior_manager", "merchant_sales",
    "merchant_stock",
}
CUSTOMER_HANDOVER_ROLES = {"customer_owner", "driver"}
FACTORY_PICKUP_ROLES = {
    "factory_chief_engineer", "factory_logistics",
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


def _require_manager_task_copy(identity, *uniq_keys):
    if not has_open_action_task_copy(identity, uniq_keys):
        raise QualityRecallError("当前高级经理没有收到这项召回协同任务。")


def _ensure_active_handover(handover):
    if (handover.campaign.status != QualityRecallCampaign.Status.ACTIVE
            or handover.status == QualityRecallCustomerHandover.Status.CANCELLED):
        raise QualityRecallError("当前召回或交接已结束，不能继续办理交付。")


def _customer_identity(user, handover):
    _ensure_active_handover(handover)
    identity = _active_identity(user)
    if (
        identity.organization_id != handover.customer_organization_id
        or not identity_has_role(identity, CUSTOMER_HANDOVER_ROLES)
    ):
        raise QualityRecallError("只有设备所属用户负责人或关联共享司机可以处理召回交接。")
    if identity.role.code == "driver":
        binding = CustomerDeviceBinding.objects.filter(
            device_id=handover.recall_device.device_id,
            customer_organization_id=handover.customer_organization_id,
            status=CustomerDeviceBinding.Status.ACTIVE,
            vehicle_id__isnull=False,
        ).first()
        if not binding or not DriverVehicleBinding.objects.filter(
            vehicle_id=binding.vehicle_id,
            driver=user,
            is_active=True,
        ).exists():
            raise QualityRecallError("当前共享司机未关联该召回设备所在车辆。")
    return identity


def _organization_snapshot(organization):
    return {
        "id": organization.pk,
        "name": organization.name,
        "type": organization.organization_type.code,
    }


def _close_factory_pickup_task(handover, *, user, reason):
    now = timezone.now()
    item = WorkItem.objects.select_for_update().filter(
        uniq_key=f"quality-recall-pickup:{handover.pk}", status="OPEN",
    ).first()
    if not item:
        return
    item.status = "DONE"
    item.done_by_openid = user.wx_openid or ""
    item.done_at = now
    item.last_activity_at = now
    item.save(update_fields=(
        "status", "done_by_openid", "done_at", "last_activity_at", "updated_at",
    ))
    UserTask.objects.filter(workitem=item).exclude(state="DONE").update(
        state="DONE", done_at=now, done_reason=reason,
        cursor_time=now, updated_at=now,
    )


def _publish_factory_pickup_task(handover, *, user):
    factory = handover.campaign.factory_organization
    recipients = organization_action_recipients(factory, FACTORY_PICKUP_REQUEST_ROLES)
    if not recipients:
        raise QualityRecallError("厂家没有可接收上门取件任务的总工程师或物流经理岗位。")
    payload = handover.method_payload if isinstance(handover.method_payload, dict) else {}
    return publish_workitem(
        domain="SYSTEM",
        type="QUALITY_RECALL_FACTORY_PICKUP",
        biz_id=str(handover.campaign_id),
        biz_ref=f"QualityRecallCustomerHandover:{handover.pk}",
        uniq_key=f"quality-recall-pickup:{handover.pk}",
        title=f"召回上门取件：{handover.recall_device.sn_snapshot}",
        summary=(
            f"用户已选择厂家上门取件，地址：{payload.get('pickup_address', '')}；"
            f"时间：{payload.get('pickup_time_window', '')}。"
        ),
        link=(
            "/pages/quality_trace/recall_detail/recall_detail"
            f"?campaign_id={handover.campaign_id}&recall_device_id={handover.recall_device_id}"
        ),
        recipients_openids=recipients,
        need_action=True,
        action_mode="ANYONE_DO",
        payload={
            "recall_campaign_id": handover.campaign_id,
            "recall_device_id": handover.recall_device_id,
            "handover_id": handover.pk,
            "delivery_method": handover.delivery_method,
            "pickup_request": payload,
        },
        actor_openid=user.wx_openid or "",
        mark_workitem_done_immediately=False,
    )


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


def ensure_customer_handover(*, recall_device, actor, actor_role="SYSTEM"):
    """Idempotently materialize the activated-device customer interaction.

    Callers must resolve and freeze responsibility first.  This keeps the
    generated handover tied to the same customer/source-merchant facts as the
    chain-scoped recall task.
    """
    if (recall_device.responsibility_snapshot or {}).get("mode") != "ACTIVATED_USER":
        return None, False
    customer = _group(recall_device, "customer_ack")
    merchant = _group(recall_device, "merchant_coordinate")
    if not customer or not merchant:
        raise QualityRecallError("召回责任链缺少用户或来源商家，无法自动建立交接。")
    handover, created = QualityRecallCustomerHandover.objects.get_or_create(
        recall_device=recall_device,
        defaults={
            "campaign": recall_device.campaign,
            "customer_organization_id": customer["organization_id"],
            "merchant_organization_id": merchant["organization_id"],
            "customer_snapshot": customer,
            "merchant_snapshot": merchant,
            "status": QualityRecallCustomerHandover.Status.PENDING_CUSTOMER_CHOICE,
        },
    )
    if created:
        _audit(
            handover,
            "RECALL_CUSTOMER_HANDOVER_AUTO_CREATED",
            actor,
            actor_role,
            {"creation_policy": "RECALL_TASK_PUBLICATION"},
        )
    return handover, created


@transaction.atomic
def initialize_customer_handover(*, user, recall_device_id):
    """Compatibility wrapper; normal operation creates the row automatically."""
    identity = _identity(user, RECALL_MANAGER_ROLES)
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
    return ensure_customer_handover(
        recall_device=row,
        actor=user,
        actor_role=identity.role.code,
    )


def _customer_payload(method, payload):
    data = dict(payload or {})
    note = str(data.get("note") or "").strip()
    result = {"note": note}
    if method == QualityRecallCustomerHandover.DeliveryMethod.FACTORY_PICKUP:
        required = {
            "contact_name": "联系人",
            "contact_phone": "联系电话",
            "pickup_address": "取件地址",
            "pickup_time_window": "可取件时间",
        }
        for key, label in required.items():
            value = str(data.get(key) or "").strip()
            if not value:
                raise QualityRecallError(f"请选择厂家上门取件时填写{label}。")
            result[key] = value
    return result


@transaction.atomic
def select_customer_delivery_method(*, user, handover_id, delivery_method, payload=None):
    handover = (
        QualityRecallCustomerHandover.objects.select_for_update()
        .select_related(
            "campaign__quality_case",
            "campaign__factory_organization__organization_type",
            "recall_device",
            "customer_organization",
            "merchant_organization",
        )
        .get(pk=handover_id)
    )
    identity = _customer_identity(user, handover)
    valid = {value for value, _label in QualityRecallCustomerHandover.DeliveryMethod.choices}
    method = str(delivery_method or "").strip()
    if method not in valid:
        raise QualityRecallError("请选择商家接管、用户直寄厂家或厂家上门取件。")
    if handover.method_locked_at:
        if handover.delivery_method == method:
            return handover, False
        raise QualityRecallError("该设备的召回交付已开始，不能再更换交付方式。")

    changed = handover.delivery_method != method
    if changed and handover.delivery_method == QualityRecallCustomerHandover.DeliveryMethod.FACTORY_PICKUP:
        _close_factory_pickup_task(
            handover,
            user=user,
            reason="RECALL_DELIVERY_METHOD_CHANGED_BEFORE_EXECUTION",
        )
    now = timezone.now()
    normalized_payload = _customer_payload(method, payload)
    handover.delivery_method = method
    handover.method_selected_by = user
    handover.method_selected_role = identity.role.code
    handover.method_selected_at = now
    handover.method_payload = normalized_payload
    handover.customer_confirmed_by = user
    handover.customer_confirmed_at = handover.customer_confirmed_at or now
    handover.customer_note = normalized_payload.get("note", "")
    handover.merchant_transfer_token = None
    handover.merchant_transfer_expires_at = None
    if method == QualityRecallCustomerHandover.DeliveryMethod.MERCHANT_HANDOVER:
        handover.status = QualityRecallCustomerHandover.Status.METHOD_SELECTED
        handover.merchant_transfer_token = uuid.uuid4()
        handover.merchant_transfer_expires_at = now + timedelta(days=7)
    elif method == QualityRecallCustomerHandover.DeliveryMethod.CUSTOMER_DIRECT_SHIP:
        handover.status = QualityRecallCustomerHandover.Status.METHOD_SELECTED
    else:
        handover.status = QualityRecallCustomerHandover.Status.PICKUP_REQUESTED
    handover.save(update_fields=(
        "delivery_method", "method_selected_by", "method_selected_role",
        "method_selected_at", "method_payload", "customer_confirmed_by",
        "customer_confirmed_at", "customer_note", "merchant_transfer_token",
        "merchant_transfer_expires_at", "status", "updated_at",
    ))

    row = handover.recall_device
    old = row.status
    if row.status in {
        QualityRecallDevice.Status.PENDING_NOTICE,
        QualityRecallDevice.Status.NOTIFIED,
    }:
        row.status = QualityRecallDevice.Status.ACKNOWLEDGED
        row.status_updated_at = now
        row.save(update_fields=("status", "status_updated_at"))
        QualityRecallDeviceEvent.objects.create(
            campaign=handover.campaign,
            recall_device=row,
            from_status=old,
            to_status=row.status,
            reason="用户已选择召回交付方式",
            actor=user,
            actor_role_snapshot=identity.role.code,
            payload={"handover_id": handover.pk, "delivery_method": method},
        )
    if method == QualityRecallCustomerHandover.DeliveryMethod.FACTORY_PICKUP:
        _publish_factory_pickup_task(handover, user=user)
    complete_recall_scope_tasks_if_ready(
        recall_device=row,
        user=user,
        audience_type="CUSTOMER",
    )
    _audit(
        handover,
        "RECALL_CUSTOMER_DELIVERY_METHOD_SELECTED",
        user,
        identity.role.code,
        {
            "delivery_method": method,
            "changed_before_execution": changed,
            "collaboration_policy": "CUSTOMER_OWNER_OR_ASSOCIATED_DRIVER_FIRST_WRITE",
        },
    )
    return handover, changed


def _create_customer_factory_leg(handover):
    row = handover.recall_device
    if row.transport_legs.exclude(
        status=QualityRecallTransportLeg.Status.CANCELLED,
    ).exists():
        raise QualityRecallError("该设备已经存在召回运输记录，不能重复发起。")
    sequence = (row.transport_legs.aggregate(value=Max("sequence_no"))["value"] or 0) + 1
    factory = handover.campaign.factory_organization
    return QualityRecallTransportLeg.objects.create(
        campaign=handover.campaign,
        recall_device=row,
        sequence_no=sequence,
        from_organization=handover.customer_organization,
        to_organization=factory,
        relation_snapshot={
            "relation_type": "ACTIVATED_CUSTOMER_RECALL",
            "delivery_method": handover.delivery_method,
            "from": _organization_snapshot(handover.customer_organization),
            "to": _organization_snapshot(factory),
        },
        is_factory_destination=True,
    )


def _publish_factory_receipt_task(handover, leg, *, user):
    try:
        return publish_factory_transport_tasks(leg, user=user)[0]
    except ValueError as exc:
        raise QualityRecallError(str(exc)) from exc


@transaction.atomic
def submit_customer_direct_shipment(
    *, user, handover_id, logistics_provider, tracking_no, evidence, note="",
):
    handover = (
        QualityRecallCustomerHandover.objects.select_for_update()
        .select_related(
            "campaign__quality_case", "campaign__factory_organization__organization_type",
            "recall_device", "customer_organization", "merchant_organization",
        )
        .get(pk=handover_id)
    )
    identity = _customer_identity(user, handover)
    if handover.delivery_method != QualityRecallCustomerHandover.DeliveryMethod.CUSTOMER_DIRECT_SHIP:
        raise QualityRecallError("请先选择用户直寄厂家。")
    if handover.method_locked_at:
        raise QualityRecallError("该设备已经提交直寄物流，不能重复提交。")
    provider = str(logistics_provider or "").strip()
    number = str(tracking_no or "").strip()
    proof = dict(evidence or {})
    if not provider or not number:
        raise QualityRecallError("请填写物流承运方和物流单号。")
    if not str(proof.get("storage_key") or "").strip():
        raise QualityRecallError("请上传实物包装或物流单据照片。")
    now = timezone.now()
    leg = _create_customer_factory_leg(handover)
    leg.status = QualityRecallTransportLeg.Status.IN_TRANSIT
    leg.logistics_provider = provider
    leg.tracking_no = number
    leg.dispatch_evidence = proof
    leg.dispatched_by = user
    leg.dispatched_at = now
    leg.save(update_fields=(
        "status", "logistics_provider", "tracking_no", "dispatch_evidence",
        "dispatched_by", "dispatched_at", "updated_at",
    ))
    handover.status = QualityRecallCustomerHandover.Status.DIRECT_SHIPPED
    handover.method_locked_at = now
    handover.method_payload = {
        **(handover.method_payload or {}),
        "logistics_provider": provider,
        "tracking_no": number,
        "shipment_note": str(note or "").strip(),
        "transport_leg_id": leg.pk,
    }
    handover.save(update_fields=(
        "status", "method_locked_at", "method_payload", "updated_at",
    ))
    row = handover.recall_device
    old = row.status
    row.status = QualityRecallDevice.Status.RETURN_IN_PROGRESS
    row.status_updated_at = now
    row.save(update_fields=("status", "status_updated_at"))
    _publish_factory_receipt_task(handover, leg, user=user)
    QualityRecallDeviceEvent.objects.create(
        campaign=handover.campaign,
        recall_device=row,
        from_status=old,
        to_status=row.status,
        reason="用户已直接寄回厂家",
        actor=user,
        actor_role_snapshot=identity.role.code,
        payload={"handover_id": handover.pk, "transport_leg_id": leg.pk},
    )
    _audit(
        handover,
        "RECALL_CUSTOMER_DIRECT_SHIPPED",
        user,
        identity.role.code,
        {"transport_leg_id": leg.pk, "tracking_no": number},
    )
    return handover, leg


@transaction.atomic
def accept_factory_pickup_request(
    *, user, handover_id, pickup_scheduled_at, logistics_provider="", note="",
):
    identity = _active_identity(user)
    handover = (
        QualityRecallCustomerHandover.objects.select_for_update()
        .select_related(
            "campaign__quality_case", "campaign__factory_organization__organization_type",
            "recall_device", "customer_organization", "merchant_organization",
        )
        .get(pk=handover_id)
    )
    _ensure_active_handover(handover)
    if (
        identity.organization_id != handover.campaign.factory_organization_id
        or not identity_has_role(identity, FACTORY_PICKUP_ROLES)
    ):
        raise QualityRecallError("只有厂家总工程师或厂家物流经理可以受理上门取件。")
    _require_manager_task_copy(identity, f"quality-recall-pickup:{handover.pk}")
    if handover.delivery_method != QualityRecallCustomerHandover.DeliveryMethod.FACTORY_PICKUP:
        raise QualityRecallError("该设备未选择厂家上门取件。")
    if handover.method_locked_at:
        raise QualityRecallError("厂家已经受理该取件申请。")
    if not pickup_scheduled_at:
        raise QualityRecallError("请填写实际预约取件时间。")
    now = timezone.now()
    leg = _create_customer_factory_leg(handover)
    leg.logistics_provider = str(logistics_provider or "").strip()
    leg.relation_snapshot = {
        **(leg.relation_snapshot or {}),
        "pickup_scheduled_at": pickup_scheduled_at.isoformat(),
        "accepted_by_user_id": user.pk,
    }
    leg.save(update_fields=("logistics_provider", "relation_snapshot", "updated_at"))
    handover.status = QualityRecallCustomerHandover.Status.PICKUP_ACCEPTED
    handover.method_locked_at = now
    handover.pickup_scheduled_by = user
    handover.pickup_scheduled_at = pickup_scheduled_at
    handover.pickup_address = str((handover.method_payload or {}).get("pickup_address") or "")
    handover.pickup_note = str(note or "").strip()
    handover.method_payload = {
        **(handover.method_payload or {}),
        "accepted_logistics_provider": leg.logistics_provider,
        "transport_leg_id": leg.pk,
    }
    handover.save(update_fields=(
        "status", "method_locked_at", "pickup_scheduled_by", "pickup_scheduled_at",
        "pickup_address", "pickup_note", "method_payload", "updated_at",
    ))
    _close_factory_pickup_task(handover, user=user, reason="RECALL_FACTORY_PICKUP_ACCEPTED")
    if not publish_customer_pickup_dispatch_task(handover, user=user):
        raise QualityRecallError("设备所属用户没有可接收取件交付确认任务的负责人或关联司机。")
    _audit(
        handover,
        "RECALL_FACTORY_PICKUP_ACCEPTED",
        user,
        identity.role.code,
        {"transport_leg_id": leg.pk, "pickup_scheduled_at": pickup_scheduled_at.isoformat()},
    )
    return handover, leg


@transaction.atomic
def submit_customer_pickup_shipment(
    *, user, handover_id, logistics_provider, tracking_no="", evidence=None,
):
    handover = (
        QualityRecallCustomerHandover.objects.select_for_update()
        .select_related(
            "campaign__quality_case", "campaign__factory_organization",
            "recall_device", "customer_organization", "merchant_organization",
        )
        .get(pk=handover_id)
    )
    identity = _customer_identity(user, handover)
    if handover.status != QualityRecallCustomerHandover.Status.PICKUP_ACCEPTED:
        raise QualityRecallError("厂家受理并安排物流后，用户才能确认设备已交付物流。")
    leg_id = (handover.method_payload or {}).get("transport_leg_id")
    leg = QualityRecallTransportLeg.objects.select_for_update().filter(
        pk=leg_id,
        recall_device=handover.recall_device,
        status=QualityRecallTransportLeg.Status.PREPARING,
    ).first()
    if not leg:
        raise QualityRecallError("上门取件运输记录不存在或已经发出。")
    provider = str(logistics_provider or leg.logistics_provider or "").strip()
    proof = dict(evidence or {})
    if not provider:
        raise QualityRecallError("请填写上门取件物流承运方。")
    if not str(proof.get("storage_key") or "").strip():
        raise QualityRecallError("请上传物流单据、取件或实物包装凭证。")
    now = timezone.now()
    leg.status = QualityRecallTransportLeg.Status.IN_TRANSIT
    leg.logistics_provider = provider
    leg.tracking_no = str(tracking_no or "").strip()
    leg.dispatch_evidence = proof
    leg.dispatched_by = user
    leg.dispatched_at = now
    leg.save(update_fields=(
        "status", "logistics_provider", "tracking_no", "dispatch_evidence",
        "dispatched_by", "dispatched_at", "updated_at",
    ))
    handover.status = QualityRecallCustomerHandover.Status.PICKUP_SHIPPED
    handover.method_payload = {
        **(handover.method_payload or {}),
        "logistics_provider": provider,
        "tracking_no": leg.tracking_no,
        "dispatch_evidence": proof,
        "transport_leg_id": leg.pk,
    }
    handover.save(update_fields=("status", "method_payload", "updated_at"))
    row = handover.recall_device
    old = row.status
    row.status = QualityRecallDevice.Status.RETURN_IN_PROGRESS
    row.status_updated_at = now
    row.save(update_fields=("status", "status_updated_at"))
    close_customer_pickup_dispatch_task(handover, user=user)
    _publish_factory_receipt_task(handover, leg, user=user)
    QualityRecallDeviceEvent.objects.create(
        campaign=handover.campaign,
        recall_device=row,
        from_status=old,
        to_status=row.status,
        reason="用户确认召回设备已交付厂家安排的物流",
        actor=user,
        actor_role_snapshot=identity.role.code,
        payload={"handover_id": handover.pk, "transport_leg_id": leg.pk},
    )
    _audit(
        handover,
        "RECALL_CUSTOMER_PICKUP_SHIPPED",
        user,
        identity.role.code,
        {"transport_leg_id": leg.pk, "tracking_no": leg.tracking_no},
    )
    return handover, leg


def confirm_factory_pickup_collected(
    *, user, handover_id, logistics_provider, tracking_no="", evidence=None,
):
    """Compatibility alias for the pre-R13.8.5 endpoint name."""
    return submit_customer_pickup_shipment(
        user=user,
        handover_id=handover_id,
        logistics_provider=logistics_provider,
        tracking_no=tracking_no,
        evidence=evidence,
    )


def sync_recall_delivery_collaboration(*, campaign, actor):
    """Idempotently restore collaboration tasks for already-active handovers."""
    synced = []
    handovers = campaign.customer_handovers.select_related(
        "campaign__factory_organization", "recall_device",
        "customer_organization", "merchant_organization",
    ).order_by("id")
    for handover in handovers:
        status = handover.status
        latest_leg = handover.recall_device.transport_legs.order_by("-sequence_no").first()
        if status == QualityRecallCustomerHandover.Status.PICKUP_REQUESTED:
            _publish_factory_pickup_task(handover, user=actor)
            synced.append({"handover_id": handover.pk, "stage": "PICKUP_REQUESTED"})
        elif status == QualityRecallCustomerHandover.Status.PICKUP_ACCEPTED:
            publish_customer_pickup_dispatch_task(handover, user=actor)
            synced.append({"handover_id": handover.pk, "stage": "PICKUP_ACCEPTED"})
        elif status == QualityRecallCustomerHandover.Status.MERCHANT_RECEIVED:
            if not latest_leg:
                publish_merchant_return_task(
                    handover, handover.merchant_organization, user=actor,
                )
                stage = "SOURCE_MERCHANT_ACCEPTED"
                summary = (
                    f"{handover.merchant_organization.name}已接管召回设备"
                    f"{handover.recall_device.sn_snapshot}，待逐级退回厂家。"
                )
            elif (
                latest_leg.status == QualityRecallTransportLeg.Status.RECEIVED
                and not latest_leg.is_factory_destination
            ):
                publish_merchant_return_task(
                    handover, latest_leg.to_organization, user=actor,
                )
                stage = "UPSTREAM_MERCHANT_RECEIVED"
                summary = (
                    f"{latest_leg.to_organization.name}已验收召回设备"
                    f"{handover.recall_device.sn_snapshot}，待继续逐级退回厂家。"
                )
            elif (
                latest_leg.status == QualityRecallTransportLeg.Status.RECEIVED
                and latest_leg.is_factory_destination
            ):
                stage = "FACTORY_RECEIVED_PENDING_ISOLATION"
                summary = (
                    f"召回设备{handover.recall_device.sn_snapshot}已由厂家验收，"
                    "当前待进入召回专用隔离区。"
                )
            else:
                stage = (
                    "RETURN_IN_TRANSIT"
                    if latest_leg.status == QualityRecallTransportLeg.Status.IN_TRANSIT
                    else "RETURN_LEG_CREATED"
                )
                summary = (
                    f"召回设备{handover.recall_device.sn_snapshot}当前运输节点："
                    f"{latest_leg.from_organization.name}至{latest_leg.to_organization.name}；"
                    f"状态：{latest_leg.get_status_display()}。"
                )
            publish_factory_recall_progress(
                handover, user=actor, stage=stage, summary=summary,
            )
            synced.append({"handover_id": handover.pk, "stage": stage})
        elif (
            status in {
                QualityRecallCustomerHandover.Status.DIRECT_SHIPPED,
                QualityRecallCustomerHandover.Status.PICKUP_SHIPPED,
            }
            and latest_leg
            and latest_leg.status == QualityRecallTransportLeg.Status.IN_TRANSIT
        ):
            _publish_factory_receipt_task(handover, latest_leg, user=actor)
            synced.append({"handover_id": handover.pk, "stage": status})
    return synced


@transaction.atomic
def accept_merchant_handover_by_token(*, user, token, receipt_note=""):
    identity = _active_identity(user)
    handover = (
        QualityRecallCustomerHandover.objects.select_for_update()
        .select_related("campaign__quality_case", "recall_device", "merchant_organization")
        .filter(merchant_transfer_token=token)
        .first()
    )
    if not handover:
        raise QualityRecallError("临时召回流转码无效。")
    _ensure_active_handover(handover)
    if (
        identity.organization_id != handover.merchant_organization_id
        or not identity_has_role(identity, MERCHANT_HANDOVER_ROLES)
    ):
        raise QualityRecallError("只有该设备来源商家的授权岗位可以扫码接管。")
    _require_manager_task_copy(
        identity,
        f"quality-recall-merchant-return:{handover.recall_device_id}:org:{handover.merchant_organization_id}",
        f"quality-recall:{handover.campaign_id}:org:{handover.merchant_organization_id}",
    )
    if handover.delivery_method != QualityRecallCustomerHandover.DeliveryMethod.MERCHANT_HANDOVER:
        raise QualityRecallError("用户当前未选择商家接管。")
    if handover.method_locked_at:
        if handover.status == QualityRecallCustomerHandover.Status.MERCHANT_RECEIVED:
            return handover
        raise QualityRecallError("该设备的召回交付已经开始。")
    if not handover.merchant_transfer_expires_at or handover.merchant_transfer_expires_at < timezone.now():
        raise QualityRecallError("临时召回流转码已过期，请让用户重新选择商家接管。")
    note = str(receipt_note or "").strip()
    if not note:
        raise QualityRecallError("请填写商家接管说明。")
    now = timezone.now()
    handover.status = QualityRecallCustomerHandover.Status.MERCHANT_RECEIVED
    handover.method_locked_at = now
    handover.merchant_received_by = user
    handover.merchant_received_at = now
    handover.receipt_note = note
    handover.custody_snapshot = {
        "custodian_type": "RECALL_MERCHANT_CUSTODY",
        "organization_id": identity.organization_id,
        "organization_name": identity.organization.name,
        "received_by_user_id": user.pk,
        "received_at": now.isoformat(),
        "transfer_token_consumed": True,
        "inventory_state_changed": False,
    }
    handover.save(update_fields=(
        "status", "method_locked_at", "merchant_received_by", "merchant_received_at",
        "receipt_note", "custody_snapshot", "updated_at",
    ))
    row = handover.recall_device
    old = row.status
    row.status = QualityRecallDevice.Status.RETURN_IN_PROGRESS
    row.status_updated_at = now
    row.save(update_fields=("status", "status_updated_at"))
    complete_recall_scope_tasks_if_ready(
        recall_device=row, user=user, audience_type="MERCHANT",
    )
    QualityRecallDeviceEvent.objects.create(
        campaign=handover.campaign,
        recall_device=row,
        from_status=old,
        to_status=row.status,
        reason="来源商家通过用户临时召回流转码接管设备",
        actor=user,
        actor_role_snapshot=identity.role.code,
        payload={"handover_id": handover.pk},
    )
    _audit(
        handover,
        "RECALL_MERCHANT_HANDOVER_TOKEN_ACCEPTED",
        user,
        identity.role.code,
    )
    publish_merchant_return_task(handover, handover.merchant_organization, user=user)
    publish_factory_recall_progress(
        handover,
        user=user,
        stage="SOURCE_MERCHANT_ACCEPTED",
        summary=(
            f"{handover.merchant_organization.name}已通过用户临时召回流转码接管设备"
            f"{handover.recall_device.sn_snapshot}，待沿召回专用链路逐级退回厂家。"
        ),
    )
    return handover


@transaction.atomic
def acknowledge_customer_recall(*, user, handover_id, note=""):
    handover = (
        QualityRecallCustomerHandover.objects.select_for_update()
        .select_related("campaign__quality_case", "recall_device")
        .get(pk=handover_id)
    )
    identity = _customer_identity(user, handover)
    if handover.status not in {
        QualityRecallCustomerHandover.Status.PENDING_CUSTOMER_ACK,
        QualityRecallCustomerHandover.Status.PENDING_CUSTOMER_CHOICE,
    }:
        raise QualityRecallError("当前交接单不处于待用户确认状态。")
    now = timezone.now()
    handover.status = QualityRecallCustomerHandover.Status.ACKNOWLEDGED
    handover.delivery_method = QualityRecallCustomerHandover.DeliveryMethod.MERCHANT_HANDOVER
    handover.method_selected_by = user
    handover.method_selected_role = identity.role.code
    handover.method_selected_at = now
    handover.merchant_transfer_token = uuid.uuid4()
    handover.merchant_transfer_expires_at = now + timedelta(days=7)
    handover.customer_confirmed_by = user
    handover.customer_confirmed_at = now
    handover.customer_note = str(note or "").strip()
    handover.save(update_fields=(
        "status", "delivery_method", "method_selected_by", "method_selected_role",
        "method_selected_at", "merchant_transfer_token", "merchant_transfer_expires_at",
        "customer_confirmed_by", "customer_confirmed_at", "customer_note", "updated_at",
    ))
    row = handover.recall_device
    old = row.status
    row.status = QualityRecallDevice.Status.ACKNOWLEDGED
    row.status_updated_at = now
    row.save(update_fields=("status", "status_updated_at"))
    _complete_task_type(row, "QUALITY_RECALL_CUSTOMER_ACK", user)
    complete_recall_scope_tasks_if_ready(
        recall_device=row,
        user=user,
        audience_type="CUSTOMER",
    )
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
    _ensure_active_handover(handover)
    if (
        identity.organization_id != handover.merchant_organization_id
        or not identity_has_role(identity, MERCHANT_HANDOVER_ROLES)
    ):
        raise QualityRecallError("只有来源商家的负责人、管理人员或销售可以预约取回。")
    _require_manager_task_copy(
        identity,
        f"quality-recall-merchant-return:{handover.recall_device_id}:org:{handover.merchant_organization_id}",
        f"quality-recall:{handover.campaign_id}:org:{handover.merchant_organization_id}",
    )
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
    _ensure_active_handover(handover)
    if identity.organization_id == handover.customer_organization_id:
        _customer_identity(user, handover)
    elif (identity.organization_id != handover.merchant_organization_id
          or not identity_has_role(identity, MERCHANT_HANDOVER_ROLES)):
        raise QualityRecallError("当前岗位无权提交该召回交接凭证。")
    else:
        _require_manager_task_copy(
            identity,
            f"quality-recall-merchant-return:{handover.recall_device_id}:org:{handover.merchant_organization_id}",
            f"quality-recall:{handover.campaign_id}:org:{handover.merchant_organization_id}",
        )
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
    _ensure_active_handover(handover)
    if (
        identity.organization_id != handover.merchant_organization_id
        or not identity_has_role(identity, MERCHANT_HANDOVER_ROLES)
    ):
        raise QualityRecallError("只有来源商家的授权岗位可以验收召回设备。")
    _require_manager_task_copy(
        identity,
        f"quality-recall-merchant-return:{handover.recall_device_id}:org:{handover.merchant_organization_id}",
        f"quality-recall:{handover.campaign_id}:org:{handover.merchant_organization_id}",
    )
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
    handover.delivery_method = QualityRecallCustomerHandover.DeliveryMethod.MERCHANT_HANDOVER
    handover.method_locked_at = now
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
        "status", "delivery_method", "method_locked_at", "merchant_received_by",
        "merchant_received_at", "receipt_note",
        "custody_snapshot", "updated_at",
    ))
    row = handover.recall_device
    old = row.status
    row.status = QualityRecallDevice.Status.RETURN_IN_PROGRESS
    row.status_updated_at = now
    row.save(update_fields=("status", "status_updated_at"))
    _complete_task_type(row, "QUALITY_RECALL_MERCHANT_COORDINATE", user)
    complete_recall_scope_tasks_if_ready(
        recall_device=row,
        user=user,
        audience_type="MERCHANT",
    )
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
