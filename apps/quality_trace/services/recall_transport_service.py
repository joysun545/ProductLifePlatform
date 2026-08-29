from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from account.models import OrganizationRelation, OrganizationRelationType, UserOrganization
from account.services.auth_service import get_active_identity
from quality_trace.models import (
    QualityRecallCampaign,
    QualityRecallCustomerHandover,
    QualityRecallDevice,
    QualityRecallDeviceEvent,
    QualityRecallIsolationReceipt,
    QualityRecallTransportLeg,
    QualityTraceEvent,
)
from workflow.models import UserTask, WorkItem
from workflow.services.workflow_service import publish_workitem

from .recall_collaboration_service import (
    close_factory_recall_progress,
    close_merchant_return_task,
    publish_factory_recall_progress,
    publish_merchant_return_task,
)
from .recall_service import QualityRecallError


MERCHANT_ROLES = {
    "merchant_owner", "merchant_manager", "merchant_senior_manager",
    "merchant_sales", "merchant_stock",
}
FACTORY_RECEIPT_ROLES = {
    "factory_admin", "factory_chief_engineer", "factory_sales",
    "factory_logistics", "factory_sales_assistant",
}
FACTORY_ISOLATION_ROLES = {
    "factory_admin", "factory_chief_engineer", "factory_logistics",
    "factory_sales_assistant",
}


def _identity(user):
    identity = get_active_identity(user)
    if not identity:
        raise QualityRecallError("当前账号没有有效组织身份。")
    return identity


def _evidence(value, label):
    data = dict(value or {})
    if not str(data.get("storage_key") or "").strip():
        raise QualityRecallError(f"请上传{label}凭证。")
    return data


def _snapshot(organization):
    return {
        "id": organization.pk,
        "name": organization.name,
        "type": organization.organization_type.code,
    }


def _recipients(organization, roles):
    return list(
        UserOrganization.objects.filter(
            organization=organization, role__code__in=roles, role__is_active=True,
            user__is_active=True, is_active=True,
        ).exclude(user__wx_openid__isnull=True).exclude(user__wx_openid="")
        .values_list("user__wx_openid", flat=True)
    )


def _event(row, event_type, user, role, payload):
    QualityTraceEvent.objects.create(
        quality_case=row.campaign.quality_case,
        alert=row.campaign.quality_case.source_alert,
        event_type=event_type,
        actor=user,
        actor_role_snapshot=role,
        payload={
            "recall_campaign_id": row.campaign_id,
            "recall_device_id": row.pk,
            **payload,
        },
    )


def _complete_workitem(leg, user):
    now = timezone.now()
    items = WorkItem.objects.select_for_update().filter(
        uniq_key=f"quality-recall-transport:{leg.pk}:receive", status="OPEN",
    )
    for item in items:
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


def _current_custodian(row):
    last = row.transport_legs.filter(status=QualityRecallTransportLeg.Status.RECEIVED).order_by(
        "-sequence_no",
    ).first()
    if last:
        return last.to_organization
    handover = getattr(row, "customer_handover", None)
    if handover and handover.status == QualityRecallCustomerHandover.Status.MERCHANT_RECEIVED:
        return handover.merchant_organization
    raise QualityRecallError("召回设备尚未由商家完成实物接管。")


@transaction.atomic
def create_next_recall_transport_leg(*, user, recall_device_id):
    identity = _identity(user)
    row = (
        QualityRecallDevice.objects.select_for_update()
        .select_related("campaign__quality_case", "customer_handover__merchant_organization")
        .filter(pk=recall_device_id, campaign__status=QualityRecallCampaign.Status.ACTIVE)
        .first()
    )
    if not row:
        raise QualityRecallError("召回设备不存在或召回批次未发布。")
    current = _current_custodian(row)
    if identity.organization_id != current.pk or identity.role.code not in MERCHANT_ROLES:
        raise QualityRecallError("只有当前保管商家的授权岗位可以发起下一程退回。")
    if row.transport_legs.exclude(
        status__in=(QualityRecallTransportLeg.Status.RECEIVED, QualityRecallTransportLeg.Status.CANCELLED),
    ).exists():
        raise QualityRecallError("当前设备已有未完成的召回运输程次。")
    relations = list(
        OrganizationRelation.objects.select_related(
            "source_organization__organization_type", "target_organization__organization_type",
        ).filter(
            target_organization=current,
            relation_type=OrganizationRelationType.DISTRIBUTION,
            is_active=True,
            source_organization__is_active=True,
            source_organization__organization_type__code__in=("MERCHANT", "OWNER"),
        )
    )
    if len(relations) != 1:
        raise QualityRecallError("当前商家的有效上级经销链必须且只能有一个。")
    relation = relations[0]
    target = relation.source_organization
    sequence = (row.transport_legs.aggregate(value=Max("sequence_no"))["value"] or 0) + 1
    leg = QualityRecallTransportLeg.objects.create(
        campaign=row.campaign,
        recall_device=row,
        sequence_no=sequence,
        from_organization=current,
        to_organization=target,
        relation_snapshot={
            "organization_relation_id": relation.pk,
            "relation_type": relation.relation_type,
            "from": _snapshot(current),
            "to": _snapshot(target),
        },
        is_factory_destination=target.organization_type.code == "OWNER",
    )
    roles = FACTORY_RECEIPT_ROLES if leg.is_factory_destination else MERCHANT_ROLES
    recipients = _recipients(target, roles)
    if not recipients:
        raise QualityRecallError("上级接收组织没有有效召回收件岗位。")
    publish_workitem(
        domain="SYSTEM",
        type="QUALITY_RECALL_TRANSPORT_RECEIPT",
        biz_id=str(row.campaign_id),
        biz_ref=f"QualityRecallTransportLeg:{leg.pk}",
        uniq_key=f"quality-recall-transport:{leg.pk}:receive",
        title="请验收召回退回设备",
        summary=f"召回设备{row.sn_snapshot}将由{current.name}退回，请验收并保持隔离。",
        link=(
            "/pages/quality_trace/recall_transport/recall_transport"
            f"?leg_id={leg.pk}&recall_device_id={row.pk}"
        ),
        recipients_openids=recipients,
        need_action=True,
        action_mode="ANYONE_DO",
        payload={
            "transport_leg_id": leg.pk,
            "recall_device_id": row.pk,
            "device_sn": row.sn_snapshot,
            "sequence_no": sequence,
            "from_organization": _snapshot(current),
            "to_organization": _snapshot(target),
            "is_factory_destination": leg.is_factory_destination,
        },
        actor_openid=user.wx_openid or "",
        mark_workitem_done_immediately=False,
    )
    handover = getattr(row, "customer_handover", None)
    if handover:
        close_merchant_return_task(handover, current, user=user)
        publish_factory_recall_progress(
            handover,
            user=user,
            stage="RETURN_LEG_CREATED",
            summary=(
                f"{current.name}已建立召回专用退回程次，设备{row.sn_snapshot}"
                f"将发往{target.name}。"
            ),
        )
    _event(row, "RECALL_TRANSPORT_LEG_CREATED", user, identity.role.code, {
        "transport_leg_id": leg.pk, "sequence_no": sequence,
        "from_organization_id": current.pk, "to_organization_id": target.pk,
    })
    return leg


@transaction.atomic
def dispatch_recall_transport_leg(*, user, leg_id, logistics_provider, tracking_no, evidence):
    identity = _identity(user)
    leg = QualityRecallTransportLeg.objects.select_for_update().select_related(
        "recall_device__campaign__quality_case", "from_organization", "to_organization",
    ).get(pk=leg_id)
    if identity.organization_id != leg.from_organization_id or identity.role.code not in MERCHANT_ROLES:
        raise QualityRecallError("只有本程发出商家的授权岗位可以确认发出。")
    if leg.status != QualityRecallTransportLeg.Status.PREPARING:
        raise QualityRecallError("当前召回运输程次不能重复发出。")
    provider = str(logistics_provider or "").strip()
    number = str(tracking_no or "").strip()
    if not provider or not number:
        raise QualityRecallError("请填写物流承运方和物流单号。")
    now = timezone.now()
    leg.status = QualityRecallTransportLeg.Status.IN_TRANSIT
    leg.logistics_provider = provider
    leg.tracking_no = number
    leg.dispatch_evidence = _evidence(evidence, "发出")
    leg.dispatched_by = user
    leg.dispatched_at = now
    leg.save(update_fields=(
        "status", "logistics_provider", "tracking_no", "dispatch_evidence",
        "dispatched_by", "dispatched_at", "updated_at",
    ))
    row = leg.recall_device
    if row.status != QualityRecallDevice.Status.RETURN_IN_PROGRESS:
        old = row.status
        row.status = QualityRecallDevice.Status.RETURN_IN_PROGRESS
        row.status_updated_at = now
        row.save(update_fields=("status", "status_updated_at"))
        QualityRecallDeviceEvent.objects.create(
            campaign=row.campaign, recall_device=row, from_status=old,
            to_status=row.status, reason="召回专用运输已发出", actor=user,
            actor_role_snapshot=identity.role.code, payload={"transport_leg_id": leg.pk},
        )
    handover = getattr(row, "customer_handover", None)
    if handover:
        publish_factory_recall_progress(
            handover,
            user=user,
            stage="RETURN_IN_TRANSIT",
            summary=(
                f"召回设备{row.sn_snapshot}已由{leg.from_organization.name}发出，"
                f"正在运往{leg.to_organization.name}；物流：{provider} {number}。"
            ),
        )
    _event(row, "RECALL_TRANSPORT_DISPATCHED", user, identity.role.code, {
        "transport_leg_id": leg.pk, "tracking_no": number,
    })
    return leg


@transaction.atomic
def receive_recall_transport_leg(*, user, leg_id, evidence, receipt_note=""):
    identity = _identity(user)
    leg = QualityRecallTransportLeg.objects.select_for_update().select_related(
        "recall_device__campaign__quality_case", "from_organization", "to_organization",
    ).get(pk=leg_id)
    roles = FACTORY_RECEIPT_ROLES if leg.is_factory_destination else MERCHANT_ROLES
    if identity.organization_id != leg.to_organization_id or identity.role.code not in roles:
        raise QualityRecallError("当前账号不是本程召回设备的授权验收人。")
    if leg.status != QualityRecallTransportLeg.Status.IN_TRANSIT:
        raise QualityRecallError("只有运输中的召回设备可以验收。")
    now = timezone.now()
    leg.status = QualityRecallTransportLeg.Status.RECEIVED
    leg.receipt_evidence = _evidence(evidence, "验收")
    leg.received_by = user
    leg.received_at = now
    leg.custody_snapshot = {
        "custodian_type": (
            "RECALL_FACTORY_PENDING_ISOLATION" if leg.is_factory_destination
            else "RECALL_MERCHANT_CUSTODY"
        ),
        "organization": _snapshot(leg.to_organization),
        "received_by_user_id": user.pk,
        "received_at": now.isoformat(),
        "receipt_note": str(receipt_note or "").strip(),
        "ordinary_inventory_created": False,
    }
    leg.save(update_fields=(
        "status", "receipt_evidence", "received_by", "received_at",
        "custody_snapshot", "updated_at",
    ))
    _complete_workitem(leg, user)
    handover = getattr(leg.recall_device, "customer_handover", None)
    if handover:
        if leg.is_factory_destination:
            publish_factory_recall_progress(
                handover,
                user=user,
                stage="FACTORY_RECEIVED_PENDING_ISOLATION",
                summary=(
                    f"召回设备{leg.recall_device.sn_snapshot}已由厂家验收，"
                    "当前待进入召回专用隔离区。"
                ),
            )
        else:
            publish_merchant_return_task(handover, leg.to_organization, user=user)
            publish_factory_recall_progress(
                handover,
                user=user,
                stage="UPSTREAM_MERCHANT_RECEIVED",
                summary=(
                    f"{leg.to_organization.name}已验收召回设备"
                    f"{leg.recall_device.sn_snapshot}，待继续逐级退回厂家。"
                ),
            )
    _event(leg.recall_device, "RECALL_TRANSPORT_RECEIVED", user, identity.role.code, {
        "transport_leg_id": leg.pk,
        "factory_pending_isolation": leg.is_factory_destination,
        "ordinary_inventory_created": False,
    })
    return leg


@transaction.atomic
def isolate_recalled_device(*, user, leg_id, disposition, isolation_location, evidence):
    identity = _identity(user)
    leg = QualityRecallTransportLeg.objects.select_for_update().select_related(
        "recall_device__campaign__quality_case", "to_organization",
    ).get(pk=leg_id)
    if (
        not leg.is_factory_destination
        or identity.organization_id != leg.to_organization_id
        or identity.role.code not in FACTORY_ISOLATION_ROLES
    ):
        raise QualityRecallError("只有目标厂家的库存、管理或负责人岗位可以执行隔离。")
    if leg.status != QualityRecallTransportLeg.Status.RECEIVED:
        raise QualityRecallError("厂家验收召回设备后才能隔离。")
    valid = {value for value, _ in QualityRecallIsolationReceipt.Disposition.choices}
    if disposition not in valid:
        raise QualityRecallError("隔离处置分类无效。")
    location = str(isolation_location or "").strip()
    if not location:
        raise QualityRecallError("请填写召回隔离位置。")
    now = timezone.now()
    row = leg.recall_device
    receipt, created = QualityRecallIsolationReceipt.objects.get_or_create(
        recall_device=row,
        defaults={
            "campaign": row.campaign, "transport_leg": leg,
            "factory_organization": leg.to_organization,
            "disposition": disposition, "isolation_location": location,
            "evidence_snapshot": _evidence(evidence, "隔离"),
            "isolated_by": user, "isolated_at": now,
            "source_activation_preserved": True,
            "ordinary_inventory_created": False,
        },
    )
    if not created:
        raise QualityRecallError("该召回设备已经完成厂家隔离。")
    old = row.status
    row.status = QualityRecallDevice.Status.RECEIVED
    row.status_updated_at = now
    row.save(update_fields=("status", "status_updated_at"))
    QualityRecallDeviceEvent.objects.create(
        campaign=row.campaign, recall_device=row, from_status=old,
        to_status=row.status, reason="厂家已验收并完成召回专用隔离", actor=user,
        actor_role_snapshot=identity.role.code,
        payload={"transport_leg_id": leg.pk, "isolation_receipt_id": receipt.pk},
    )
    _event(row, "RECALL_DEVICE_FACTORY_ISOLATED", user, identity.role.code, {
        "transport_leg_id": leg.pk, "isolation_receipt_id": receipt.pk,
        "disposition": disposition, "source_activation_preserved": True,
        "ordinary_inventory_created": False,
    })
    handover = getattr(row, "customer_handover", None)
    if handover:
        publish_factory_recall_progress(
            handover,
            user=user,
            stage="FACTORY_ISOLATED",
            summary=(
                f"召回设备{row.sn_snapshot}已由厂家完成收货和隔离入库，"
                f"隔离位置：{location}。"
            ),
        )
        close_factory_recall_progress(handover, user=user)
    return receipt
