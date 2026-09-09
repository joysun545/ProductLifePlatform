from collections import Counter

from django.db import transaction
from django.utils import timezone

from account.models import UserOrganization
from quality_trace.models import (
    BatchQualityCase,
    QualityControlAction,
    QualityRecallCampaign,
    QualityRecallCloseout,
    QualityRecallCustomerHandover,
    QualityRecallDevice,
    QualityRecallExceptionDecision,
    QualityRecallInventoryBatchConfirmation,
    QualityRecallProgressSnapshot,
    QualityRecallPostDisposition,
    QualityRecallTransportLeg,
    QualityTraceEvent,
)
from workflow.models import UserTask, WorkItem
from workflow.services.workflow_service import publish_workitem

from .case_service import RECALL_MANAGER_ROLES, _identity, complete_recall
from .recall_service import QualityRecallError


PROGRESS_KEYS = (
    "PENDING_NOTICE", "PENDING_ACK", "PENDING_PICKUP", "PENDING_RETURN",
    "IN_TRANSIT", "PENDING_ISOLATION", "RECEIVED", "DISPOSED",
    "UNREACHABLE", "EXEMPTED",
)
ELIGIBLE_STATUSES = {
    QualityRecallDevice.Status.RECEIVED,
    QualityRecallDevice.Status.DISPOSED,
    QualityRecallDevice.Status.EXEMPTED,
}


def _approved_unreachable(row):
    decision = getattr(row, "exception_decision", None)
    return bool(
        decision
        and decision.decision == QualityRecallExceptionDecision.Decision.APPROVED
    )


def _is_completion_eligible(row):
    return row.status in ELIGIBLE_STATUSES or (
        row.status == QualityRecallDevice.Status.UNREACHABLE
        and _approved_unreachable(row)
    )


def _bucket(row):
    if row.status in {
        QualityRecallDevice.Status.RECEIVED,
        QualityRecallDevice.Status.DISPOSED,
        QualityRecallDevice.Status.UNREACHABLE,
        QualityRecallDevice.Status.EXEMPTED,
    }:
        return row.status
    legs = list(row.transport_legs.all())
    if any(item.status == QualityRecallTransportLeg.Status.IN_TRANSIT for item in legs):
        return "IN_TRANSIT"
    last_received = next(
        (item for item in reversed(legs)
         if item.status == QualityRecallTransportLeg.Status.RECEIVED),
        None,
    )
    if last_received:
        return "PENDING_ISOLATION" if last_received.is_factory_destination else "PENDING_RETURN"
    handover = getattr(row, "customer_handover", None)
    if handover:
        if handover.status == QualityRecallCustomerHandover.Status.PENDING_CUSTOMER_ACK:
            return "PENDING_ACK"
        if handover.status in {
            QualityRecallCustomerHandover.Status.ACKNOWLEDGED,
            QualityRecallCustomerHandover.Status.PICKUP_SCHEDULED,
        }:
            return "PENDING_PICKUP"
        if handover.status == QualityRecallCustomerHandover.Status.MERCHANT_RECEIVED:
            return "PENDING_RETURN"
    if row.status in {QualityRecallDevice.Status.NOTIFIED, QualityRecallDevice.Status.ACKNOWLEDGED}:
        return "PENDING_ACK" if row.status == QualityRecallDevice.Status.NOTIFIED else "PENDING_RETURN"
    return "PENDING_NOTICE"


def _factory_recipients(campaign):
    return list(
        UserOrganization.objects.filter(
            organization=campaign.factory_organization,
            role__code__in=("factory_chief_engineer",),
            role__is_active=True, user__is_active=True, is_active=True,
        ).exclude(user__wx_openid__isnull=True).exclude(user__wx_openid="")
        .values_list("user__wx_openid", flat=True)
    )


def _publish_overdue_escalation(campaign, overdue_ids):
    recipients = _factory_recipients(campaign)
    if not recipients:
        raise QualityRecallError("厂家没有可接收召回逾期升级的授权岗位。")
    return publish_workitem(
        domain="SYSTEM",
        type="QUALITY_RECALL_OVERDUE_ESCALATION",
        biz_id=str(campaign.pk),
        biz_ref=f"QualityRecallCampaign:{campaign.pk}",
        uniq_key=f"quality-recall:{campaign.pk}:overdue-escalation",
        title="召回批次已逾期，请立即研判",
        summary=(
            f"召回批次{campaign.recall_no}超过截止日期，"
            f"仍有{len(overdue_ids)}台设备未完成。"
        ),
        link=(
            "/pages/quality_trace/recall_detail/recall_detail"
            f"?campaign_id={campaign.pk}"
        ),
        recipients_openids=recipients,
        need_action=True,
        action_mode="ANYONE_DO",
        payload={
            "recall_campaign_id": campaign.pk,
            "recall_no": campaign.recall_no,
            "deadline": campaign.deadline.isoformat(),
            "overdue_device_ids": overdue_ids,
            "overdue_device_count": len(overdue_ids),
        },
        actor_openid="",
        mark_workitem_done_immediately=False,
    )


@transaction.atomic
def calculate_recall_progress(*, campaign_id, at=None, source="MANUAL"):
    at = at or timezone.now()
    campaign = (
        QualityRecallCampaign.objects.select_for_update()
        .select_related("factory_organization", "quality_case__source_alert")
        .get(pk=campaign_id)
    )
    rows = list(
        campaign.devices.select_related("exception_decision", "customer_handover")
        .prefetch_related("transport_legs").order_by("device_id")
    )
    counts = Counter(_bucket(row) for row in rows)
    normalized_counts = {key: counts.get(key, 0) for key in PROGRESS_KEYS}
    incomplete_ids = [row.device_id for row in rows if not _is_completion_eligible(row)]
    is_overdue = bool(
        campaign.status == QualityRecallCampaign.Status.ACTIVE
        and campaign.deadline < (timezone.localdate(at) if timezone.is_aware(at) else at.date())
        and incomplete_ids
    )
    overdue_ids = list(incomplete_ids) if is_overdue else []
    workitem = _publish_overdue_escalation(campaign, overdue_ids) if overdue_ids else None
    snapshot = QualityRecallProgressSnapshot.objects.create(
        campaign=campaign,
        counts=normalized_counts,
        completion_eligible_count=len(rows) - len(incomplete_ids),
        incomplete_device_ids=incomplete_ids,
        overdue_device_ids=overdue_ids,
        is_overdue=is_overdue,
        escalation_workitem_id=workitem.pk if workitem else None,
        calculated_at=at,
        calculation_source=str(source or "MANUAL").upper(),
    )
    QualityTraceEvent.objects.create(
        quality_case=campaign.quality_case,
        alert=campaign.quality_case.source_alert,
        event_type="RECALL_PROGRESS_CALCULATED",
        payload={
            "recall_campaign_id": campaign.pk,
            "progress_snapshot_id": snapshot.pk,
            "counts": normalized_counts,
            "incomplete_device_ids": incomplete_ids,
            "overdue_device_ids": overdue_ids,
            "escalation_workitem_id": snapshot.escalation_workitem_id,
        },
    )
    return snapshot


def evaluate_active_recall_progress(*, at=None, source="SCHEDULED"):
    at = at or timezone.now()
    results = []
    ids = list(QualityRecallCampaign.objects.filter(
        status=QualityRecallCampaign.Status.ACTIVE,
    ).values_list("pk", flat=True))
    for campaign_id in ids:
        row = calculate_recall_progress(campaign_id=campaign_id, at=at, source=source)
        results.append({
            "campaign_id": campaign_id,
            "snapshot_id": row.pk,
            "is_overdue": row.is_overdue,
            "incomplete_device_count": len(row.incomplete_device_ids),
        })
    return results


@transaction.atomic
def decide_unreachable_device(*, user, recall_device_id, decision, reason):
    identity = _identity(user, RECALL_MANAGER_ROLES)
    row = (
        QualityRecallDevice.objects.select_for_update()
        .select_related("campaign__quality_case__source_alert")
        .filter(
            pk=recall_device_id,
            campaign__factory_organization=identity.organization,
            campaign__status=QualityRecallCampaign.Status.ACTIVE,
        ).first()
    )
    if not row:
        raise QualityRecallError("召回设备不存在或不属于当前厂家。")
    if row.status != QualityRecallDevice.Status.UNREACHABLE:
        raise QualityRecallError("只有已标记无法联系的设备可以提交例外审批。")
    valid = {value for value, _ in QualityRecallExceptionDecision.Decision.choices}
    decision = str(decision or "").upper()
    reason = str(reason or "").strip()
    if decision not in valid or not reason:
        raise QualityRecallError("请填写有效的无法联系审批结论和理由。")
    now = timezone.now()
    result, _created = QualityRecallExceptionDecision.objects.update_or_create(
        recall_device=row,
        defaults={
            "campaign": row.campaign, "decision": decision, "reason": reason,
            "decided_by": user, "decided_role_snapshot": identity.role.code,
            "decided_at": now,
        },
    )
    QualityTraceEvent.objects.create(
        quality_case=row.campaign.quality_case,
        alert=row.campaign.quality_case.source_alert,
        event_type="RECALL_UNREACHABLE_DECIDED",
        actor=user,
        actor_role_snapshot=identity.role.code,
        payload={
            "recall_campaign_id": row.campaign_id,
            "recall_device_id": row.pk,
            "decision_id": result.pk,
            "decision": decision,
            "reason": reason,
        },
    )
    return result


def _complete_escalation(campaign, user):
    now = timezone.now()
    items = WorkItem.objects.select_for_update().filter(
        uniq_key=f"quality-recall:{campaign.pk}:overdue-escalation", status="OPEN",
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


def _complete_campaign_workitems(campaign, user):
    """Close every remaining recall inbox row after terminal completion."""

    now = timezone.now()
    items = WorkItem.objects.select_for_update().filter(
        biz_id=str(campaign.pk),
        type__startswith="QUALITY_RECALL_",
        status="OPEN",
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
            state="DONE",
            done_at=now,
            done_reason="RECALL_CAMPAIGN_COMPLETED",
            cursor_time=now,
            updated_at=now,
        )


@transaction.atomic
def complete_recall_campaign(*, user, campaign_id, reason):
    identity = _identity(user, RECALL_MANAGER_ROLES)
    if identity.role.code != "factory_chief_engineer":
        raise QualityRecallError("只有厂家总工程师可以确认召回任务完成。")
    campaign = (
        QualityRecallCampaign.objects.select_for_update()
        .select_related("control_action", "quality_case__source_alert")
        .filter(
            pk=campaign_id,
            factory_organization=identity.organization,
            status=QualityRecallCampaign.Status.ACTIVE,
        ).first()
    )
    if not campaign or not campaign.control_action_id:
        raise QualityRecallError("有效召回批次或召回管控行动不存在。")
    reason = str(reason or "").strip()
    if not reason:
        raise QualityRecallError("请填写召回完成说明。")
    progress = calculate_recall_progress(
        campaign_id=campaign.pk, at=timezone.now(), source="CLOSEOUT",
    )
    if progress.incomplete_device_ids:
        raise QualityRecallError(
            "召回批次仍有未完成设备："
            + ",".join(str(item) for item in progress.incomplete_device_ids)
        )
    if not QualityRecallInventoryBatchConfirmation.objects.filter(
        campaign=campaign,
    ).exists():
        raise QualityRecallError(
            "厂家销售助理尚未确认本批设备全部入库记账。"
        )
    dispositions = QualityRecallPostDisposition.objects.filter(campaign=campaign)
    physical_device_count = campaign.devices.exclude(
        status__in=(QualityRecallDevice.Status.UNREACHABLE,
                    QualityRecallDevice.Status.EXEMPTED),
    ).count()
    if dispositions.count() != physical_device_count:
        raise QualityRecallError("尚未启动本批已返厂设备的召回后处置。")
    if dispositions.exclude(
        status=QualityRecallPostDisposition.Status.COMPLETED,
    ).exists():
        raise QualityRecallError("本批仍有设备未完成召回后处置。")
    recall_action = complete_recall(
        user=user, action_id=campaign.control_action_id, reason=reason,
    )
    terminal_control = QualityControlAction.objects.filter(
        quality_case=campaign.quality_case,
        action_type__in=(
            QualityControlAction.ActionType.FORMAL_HOLD,
            QualityControlAction.ActionType.TEMPORARY_HOLD,
        ),
        status=QualityControlAction.Status.COMPLETED,
    ).order_by("-ended_at", "-id").first() or recall_action
    now = timezone.now()
    campaign.status = QualityRecallCampaign.Status.COMPLETED
    campaign.completed_by = user
    campaign.completed_at = now
    campaign.save(update_fields=(
        "status", "completed_by", "completed_at", "updated_at",
    ))
    linked_cases = list(
        BatchQualityCase.objects.select_for_update()
        .select_related("source_alert")
        .filter(recall_campaign_links__campaign=campaign)
        .exclude(pk=campaign.quality_case_id)
        .distinct()
    )
    for linked_case in linked_cases:
        completed_action_ids = []
        for action in linked_case.control_actions.select_for_update().filter(
            action_type__in=(
                QualityControlAction.ActionType.RECALL,
                QualityControlAction.ActionType.FORMAL_HOLD,
                QualityControlAction.ActionType.TEMPORARY_HOLD,
            ),
            status=QualityControlAction.Status.ACTIVE,
        ):
            action.status = QualityControlAction.Status.COMPLETED
            action.ended_at = now
            action.ended_by = user
            action.end_reason = f"主召回批次{campaign.recall_no}完成，关联管控同步结束"
            action.save(update_fields=(
                "status", "ended_at", "ended_by", "end_reason",
            ))
            completed_action_ids.append(action.pk)
        linked_case.status = BatchQualityCase.Status.REMEDIATING
        linked_case.save(update_fields=("status", "updated_at"))
        QualityTraceEvent.objects.create(
            quality_case=linked_case,
            alert=linked_case.source_alert,
            event_type="RECALL_COMPLETED_BY_CANONICAL_CAMPAIGN",
            actor=user,
            actor_role_snapshot=identity.role.code,
            payload={
                "canonical_campaign_id": campaign.pk,
                "canonical_recall_no": campaign.recall_no,
                "completed_control_action_ids": completed_action_ids,
                "reason": reason,
            },
        )
    closeout = QualityRecallCloseout.objects.create(
        campaign=campaign,
        progress_snapshot=progress,
        execution_summary={
            "recall_no": campaign.recall_no,
            "device_count": campaign.device_count,
            "counts": progress.counts,
            "completion_eligible_count": progress.completion_eligible_count,
            "unreachable_approved_device_ids": list(
                campaign.exception_decisions.filter(
                    decision=QualityRecallExceptionDecision.Decision.APPROVED,
                ).values_list("recall_device__device_id", flat=True)
            ),
            "reason": reason,
            "recall_control_action_id": recall_action.pk,
        },
        incomplete_reasons=[],
        completed_by=user,
        completed_role_snapshot=identity.role.code,
        completed_at=now,
        remediation_hold_action=terminal_control,
    )
    _complete_escalation(campaign, user)
    _complete_campaign_workitems(campaign, user)
    QualityTraceEvent.objects.create(
        quality_case=campaign.quality_case,
        alert=campaign.quality_case.source_alert,
        event_type="RECALL_CAMPAIGN_COMPLETED",
        actor=user,
        actor_role_snapshot=identity.role.code,
        payload={
            "recall_campaign_id": campaign.pk,
            "closeout_id": closeout.pk,
            "progress_snapshot_id": progress.pk,
            "remediation_hold_action_id": terminal_control.pk,
        },
    )
    return closeout
