from datetime import date, timedelta
from uuid import uuid4

from django.db import models, transaction
from django.utils import timezone

from quality_trace.models import (
    BatchQualityAffectedDevice,
    BatchQualityCase,
    QualityControlAction,
    QualityRecallCampaign,
    QualityRecallCampaignCaseLink,
    QualityRecallDevice,
    QualityRecallDeviceEvent,
    QualityTraceEvent,
)

from .case_service import CONTROL_MANAGER_ROLES, QualityCaseError, _identity, start_recall


class QualityRecallError(QualityCaseError):
    pass


def _publish_tasks_automatically(*, user, campaign):
    # Local import keeps service initialization acyclic: the task resolver
    # imports QualityRecallError from this module.
    from .recall_task_service import publish_recall_tasks

    publish_recall_tasks(user=user, campaign_id=campaign.pk)


ALLOWED_TRANSITIONS = {
    QualityRecallDevice.Status.PENDING_NOTICE: {
        QualityRecallDevice.Status.NOTIFIED,
        QualityRecallDevice.Status.UNREACHABLE,
        QualityRecallDevice.Status.EXEMPTED,
    },
    QualityRecallDevice.Status.NOTIFIED: {
        QualityRecallDevice.Status.ACKNOWLEDGED,
        QualityRecallDevice.Status.UNREACHABLE,
        QualityRecallDevice.Status.EXEMPTED,
    },
    QualityRecallDevice.Status.ACKNOWLEDGED: {
        QualityRecallDevice.Status.RETURN_IN_PROGRESS,
        QualityRecallDevice.Status.RECEIVED,
        QualityRecallDevice.Status.EXEMPTED,
    },
    QualityRecallDevice.Status.UNREACHABLE: {
        QualityRecallDevice.Status.NOTIFIED,
        QualityRecallDevice.Status.EXEMPTED,
    },
    QualityRecallDevice.Status.RETURN_IN_PROGRESS: {
        QualityRecallDevice.Status.RECEIVED,
    },
    QualityRecallDevice.Status.RECEIVED: {
        QualityRecallDevice.Status.DISPOSED,
    },
    QualityRecallDevice.Status.DISPOSED: set(),
    QualityRecallDevice.Status.EXEMPTED: set(),
}


def _text(value, label):
    result = str(value or "").strip()
    if not result:
        raise QualityRecallError(f"请填写{label}。")
    return result


def _choice(value, choices, label):
    result = str(value or "").strip().upper()
    if result not in {item for item, _ in choices}:
        raise QualityRecallError(f"{label}无效。")
    return result


def _optional_choice(value, choices, label):
    result = str(value or "").strip().upper()
    if not result:
        return ""
    return _choice(result, choices, label)


def _deadline(value):
    if isinstance(value, date):
        result = value
    else:
        try:
            result = date.fromisoformat(str(value or ""))
        except ValueError as exc:
            raise QualityRecallError("召回截止日期格式无效。") from exc
    if result < timezone.localdate():
        raise QualityRecallError("召回截止日期不能早于今天。")
    return result


def _recall_no():
    return f"RC-{timezone.now():%Y%m%d%H%M%S}-{uuid4().hex[:6].upper()}"


LIVE_CAMPAIGN_STATUSES = (
    QualityRecallCampaign.Status.DRAFT,
    QualityRecallCampaign.Status.ACTIVE,
)


def _live_campaigns_for_case(quality_case):
    return (
        QualityRecallCampaign.objects.select_for_update()
        .filter(
            factory_organization_id=quality_case.factory_organization_id,
            status__in=LIVE_CAMPAIGN_STATUSES,
        )
        .filter(
            models.Q(quality_case=quality_case)
            | models.Q(case_links__quality_case=quality_case)
        )
        .distinct()
        .order_by("-id")
    )


def _overlapping_live_campaigns(*, factory_id, device_ids):
    return list(
        QualityRecallCampaign.objects.select_for_update()
        .filter(
            factory_organization_id=factory_id,
            status__in=LIVE_CAMPAIGN_STATUSES,
            devices__device_id__in=device_ids,
        )
        .distinct()
        .order_by("id")
    )


def _attach_case(*, campaign, quality_case, user, role_code, reason):
    link, created = QualityRecallCampaignCaseLink.objects.get_or_create(
        campaign=campaign,
        quality_case=quality_case,
        defaults={
            "link_type": (
                QualityRecallCampaignCaseLink.LinkType.PRIMARY
                if campaign.quality_case_id == quality_case.pk
                else QualityRecallCampaignCaseLink.LinkType.CONTRIBUTING
            ),
            "linked_by": user,
            "reason": reason,
        },
    )
    if campaign.status == QualityRecallCampaign.Status.ACTIVE and (
        quality_case.status != BatchQualityCase.Status.RECALLING
    ):
        quality_case.status = BatchQualityCase.Status.RECALLING
        quality_case.save(update_fields=("status", "updated_at"))
    if created and link.link_type == QualityRecallCampaignCaseLink.LinkType.CONTRIBUTING:
        QualityTraceEvent.objects.create(
            quality_case=quality_case,
            alert=quality_case.source_alert,
            event_type="RECALL_CASE_ATTACHED_TO_CAMPAIGN",
            actor=user,
            actor_role_snapshot=role_code,
            payload={
                "recall_campaign_id": campaign.pk,
                "recall_no": campaign.recall_no,
                "case_link_id": link.pk,
                "reason": reason,
            },
        )
    return link


@transaction.atomic
def create_recall_campaign(
    *, user, case_id, title, reason, risk_level, remedy_type, deadline,
    device_ids=None,
):
    identity = _identity(user, CONTROL_MANAGER_ROLES)
    quality_case = (
        BatchQualityCase.objects.select_for_update()
        .filter(pk=case_id, factory_organization=identity.organization)
        .first()
    )
    if not quality_case:
        raise QualityRecallError("质量案件不存在或不属于当前厂家。")
    if quality_case.status == BatchQualityCase.Status.CLOSED:
        raise QualityRecallError("已结案案件不能创建召回批次。")

    existing_campaign = _live_campaigns_for_case(quality_case).first()
    if existing_campaign:
        raise QualityRecallError(
            f"该案件已存在待处理召回批次{existing_campaign.recall_no}，请继续处理原批次。"
        )

    active_recall_actions = list(
        quality_case.control_actions.select_for_update()
        .filter(
            action_type=QualityControlAction.ActionType.RECALL,
            status=QualityControlAction.Status.ACTIVE,
        )
        .order_by("id")
    )
    orphan_action = None
    for action in active_recall_actions:
        if not hasattr(action, "recall_campaign"):
            orphan_action = action
            break
    if active_recall_actions and not orphan_action:
        raise QualityRecallError("该案件的召回控制动作已经绑定其他召回批次。")

    memberships = quality_case.affected_devices.select_related("device").order_by("device_id")
    requested_ids = sorted({int(item) for item in (device_ids or [])})
    if requested_ids:
        memberships = memberships.filter(device_id__in=requested_ids)
    rows = list(memberships)
    if not rows:
        raise QualityRecallError("召回批次必须至少包含一台案件设备。")
    selected_ids = [row.device_id for row in rows]
    if requested_ids and selected_ids != requested_ids:
        raise QualityRecallError("召回设备必须全部来自该质量案件的冻结名单。")
    overlaps = _overlapping_live_campaigns(
        factory_id=identity.organization_id,
        device_ids=selected_ids,
    )
    if overlaps:
        recall_numbers = "、".join(item.recall_no for item in overlaps)
        raise QualityRecallError(
            f"所选设备已进入执行中的召回批次{recall_numbers}；"
            "请复用原批次或先执行重叠召回合并。"
        )

    now = timezone.now()
    campaign = QualityRecallCampaign.objects.create(
        recall_no=_recall_no(),
        quality_case=quality_case,
        factory_organization=identity.organization,
        title=_text(title, "召回标题"),
        reason=_text(reason, "召回原因"),
        risk_level=_optional_choice(
            risk_level, QualityRecallCampaign.RiskLevel.choices, "风险等级",
        ),
        remedy_type=_optional_choice(
            remedy_type, QualityRecallCampaign.RemedyType.choices, "处置方式",
        ),
        deadline=_deadline(deadline),
        scope_snapshot={
            "source": "QUALITY_CASE_FROZEN_SET",
            "quality_case_id": quality_case.pk,
            "quality_case_no": quality_case.case_no,
            "selected_device_ids": selected_ids,
            "selected_device_count": len(rows),
            "frozen_at": now.isoformat(),
        },
        device_count=len(rows),
        created_by=user,
    )
    _attach_case(
        campaign=campaign,
        quality_case=quality_case,
        user=user,
        role_code=identity.role.code,
        reason="召回批次创建时绑定主案件",
    )
    recall_devices = []
    events = []
    for membership in rows:
        location = dict(membership.location_snapshot or {})
        recall_device = QualityRecallDevice(
            campaign=campaign,
            case_membership=membership,
            device_id=membership.device_id,
            sn_snapshot=membership.sn_snapshot,
            product_model_snapshot=membership.product_model_snapshot,
            production_batch_snapshot=membership.production_batch_snapshot,
            location_snapshot=location,
            responsibility_snapshot={
                "organization_id": location.get("organization_id"),
                "organization_name": location.get("organization_name", ""),
                "activation_status": location.get("activation_status", ""),
            },
            trace_evidence_snapshot=membership.trace_evidence_snapshot,
            status_updated_at=now,
        )
        recall_devices.append(recall_device)
    QualityRecallDevice.objects.bulk_create(recall_devices)
    for row in campaign.devices.all():
        events.append(QualityRecallDeviceEvent(
            campaign=campaign,
            recall_device=row,
            to_status=QualityRecallDevice.Status.PENDING_NOTICE,
            reason="设备纳入召回冻结名单",
            actor=user,
            actor_role_snapshot=identity.role.code,
            payload={"device_id": row.device_id, "sn": row.sn_snapshot},
        ))
    QualityRecallDeviceEvent.objects.bulk_create(events)
    QualityTraceEvent.objects.create(
        quality_case=quality_case,
        alert=quality_case.source_alert,
        event_type="RECALL_CAMPAIGN_CREATED",
        actor=user,
        actor_role_snapshot=identity.role.code,
        payload={
            "recall_campaign_id": campaign.pk,
            "recall_no": campaign.recall_no,
            "device_count": campaign.device_count,
            "deadline": campaign.deadline.isoformat(),
        },
    )
    if orphan_action:
        campaign.control_action = orphan_action
        campaign.status = QualityRecallCampaign.Status.ACTIVE
        campaign.launched_by = orphan_action.acted_by or user
        campaign.launched_at = orphan_action.effective_at
        campaign.save(update_fields=(
            "control_action", "status", "launched_by", "launched_at", "updated_at",
        ))
        QualityTraceEvent.objects.create(
            quality_case=quality_case,
            alert=quality_case.source_alert,
            event_type="RECALL_CAMPAIGN_BACKFILLED",
            actor=user,
            actor_role_snapshot=identity.role.code,
            payload={
                "recall_campaign_id": campaign.pk,
                "recall_no": campaign.recall_no,
                "control_action_id": orphan_action.pk,
                "device_count": campaign.device_count,
                "compatibility_reason": "历史召回控制动作缺少召回批次绑定",
            },
        )
        _publish_tasks_automatically(user=user, campaign=campaign)
    return campaign


@transaction.atomic
def ensure_recall_campaign(*, user, case_id, reason=""):
    """Return the case's live campaign, creating and launching it when needed.

    This is the only business-facing recall entry.  Campaign construction and
    legacy control-action backfill stay invisible to the operator.
    """
    identity = _identity(user, CONTROL_MANAGER_ROLES)
    quality_case = (
        BatchQualityCase.objects.select_for_update()
        .filter(pk=case_id, factory_organization=identity.organization)
        .first()
    )
    if not quality_case:
        raise QualityRecallError("质量案件不存在或不属于当前厂家。")
    if quality_case.status == BatchQualityCase.Status.CLOSED:
        raise QualityRecallError("已结案案件不能启动召回。")

    campaign = _live_campaigns_for_case(quality_case).first()
    if campaign:
        if campaign.status == QualityRecallCampaign.Status.DRAFT:
            return launch_recall_campaign(user=user, campaign_id=campaign.pk)
        _publish_tasks_automatically(user=user, campaign=campaign)
        return campaign

    case_device_ids = set(
        quality_case.affected_devices.values_list("device_id", flat=True)
    )
    overlaps = _overlapping_live_campaigns(
        factory_id=identity.organization_id,
        device_ids=case_device_ids,
    )
    covering = []
    for candidate in overlaps:
        candidate_ids = set(
            candidate.devices.values_list("device_id", flat=True)
        )
        if case_device_ids.issubset(candidate_ids):
            covering.append(candidate)
    if len(overlaps) == 1 and len(covering) == 1:
        campaign = covering[0]
        _attach_case(
            campaign=campaign,
            quality_case=quality_case,
            user=user,
            role_code=identity.role.code,
            reason="案件设备已完整包含在现有召回范围，复用主召回批次",
        )
        if campaign.status == QualityRecallCampaign.Status.DRAFT:
            return launch_recall_campaign(user=user, campaign_id=campaign.pk)
        _publish_tasks_automatically(user=user, campaign=campaign)
        return campaign
    if overlaps:
        recall_numbers = "、".join(item.recall_no for item in overlaps)
        raise QualityRecallError(
            f"案件设备与执行中的召回批次{recall_numbers}范围重叠；"
            "请先合并历史召回批次，禁止创建并行任务。"
        )

    active_recall = (
        quality_case.control_actions.select_for_update()
        .filter(
            action_type=QualityControlAction.ActionType.RECALL,
            status=QualityControlAction.Status.ACTIVE,
        )
        .order_by("id")
        .first()
    )
    operation_reason = str(reason or "").strip()
    if active_recall and not operation_reason:
        operation_reason = str(active_recall.reason or "").strip()
    if not operation_reason:
        raise QualityRecallError("请填写启动召回的具体理由。")

    campaign = create_recall_campaign(
        user=user,
        case_id=quality_case.pk,
        title=f"{quality_case.case_no}设备召回",
        reason=operation_reason,
        risk_level="",
        remedy_type="",
        deadline=timezone.localdate() + timedelta(days=30),
    )
    if campaign.status == QualityRecallCampaign.Status.DRAFT:
        campaign = launch_recall_campaign(user=user, campaign_id=campaign.pk)
    return campaign


@transaction.atomic
def launch_recall_campaign(*, user, campaign_id):
    identity = _identity(user, CONTROL_MANAGER_ROLES)
    campaign = (
        QualityRecallCampaign.objects.select_for_update()
        .select_related("quality_case")
        .filter(pk=campaign_id, factory_organization=identity.organization)
        .first()
    )
    if not campaign:
        raise QualityRecallError("召回批次不存在或不属于当前厂家。")
    if campaign.status != QualityRecallCampaign.Status.DRAFT:
        raise QualityRecallError("只有待发布召回批次可以发布。")
    if campaign.devices.count() != campaign.device_count:
        raise QualityRecallError("召回设备冻结名单不完整，禁止发布。")
    action = start_recall(
        user=user,
        case_id=campaign.quality_case_id,
        reason=f"发布召回批次{campaign.recall_no}：{campaign.reason}",
    )
    now = timezone.now()
    campaign.control_action = action
    campaign.status = QualityRecallCampaign.Status.ACTIVE
    campaign.launched_by = user
    campaign.launched_at = now
    campaign.save(update_fields=(
        "control_action", "status", "launched_by", "launched_at", "updated_at",
    ))
    BatchQualityCase.objects.filter(
        recall_campaign_links__campaign=campaign,
    ).exclude(status=BatchQualityCase.Status.CLOSED).update(
        status=BatchQualityCase.Status.RECALLING,
        updated_at=now,
    )
    QualityTraceEvent.objects.create(
        quality_case=campaign.quality_case,
        alert=campaign.quality_case.source_alert,
        event_type="RECALL_CAMPAIGN_LAUNCHED",
        actor=user,
        actor_role_snapshot=identity.role.code,
        payload={
            "recall_campaign_id": campaign.pk,
            "recall_no": campaign.recall_no,
            "control_action_id": action.pk,
            "device_count": campaign.device_count,
        },
    )
    _publish_tasks_automatically(user=user, campaign=campaign)
    return campaign


@transaction.atomic
def transition_recall_device(*, user, recall_device_id, to_status, reason="", payload=None):
    identity = _identity(user, CONTROL_MANAGER_ROLES)
    row = (
        QualityRecallDevice.objects.select_for_update()
        .select_related("campaign")
        .filter(pk=recall_device_id, campaign__factory_organization=identity.organization)
        .first()
    )
    if not row:
        raise QualityRecallError("召回设备记录不存在或不属于当前厂家。")
    if row.campaign.status != QualityRecallCampaign.Status.ACTIVE:
        raise QualityRecallError("召回批次尚未发布或已经结束。")
    target = _choice(to_status, QualityRecallDevice.Status.choices, "召回设备状态")
    if target not in ALLOWED_TRANSITIONS[row.status]:
        raise QualityRecallError(f"召回设备不能从{row.status}直接变更为{target}。")
    old = row.status
    row.status = target
    row.status_updated_at = timezone.now()
    row.save(update_fields=("status", "status_updated_at"))
    QualityRecallDeviceEvent.objects.create(
        campaign=row.campaign,
        recall_device=row,
        from_status=old,
        to_status=target,
        reason=str(reason or "").strip(),
        actor=user,
        actor_role_snapshot=identity.role.code,
        payload=dict(payload) if isinstance(payload, dict) else {},
    )
    return row
