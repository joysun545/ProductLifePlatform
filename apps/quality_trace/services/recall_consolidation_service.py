from django.db import transaction
from django.utils import timezone

from quality_trace.models import (
    BatchQualityCase,
    QualityControlAction,
    QualityRecallCampaign,
    QualityRecallCampaignCaseLink,
    QualityTraceEvent,
)
from workflow.models import UserTask, WorkItem

from .case_service import RECALL_MANAGER_ROLES, _identity
from .recall_service import LIVE_CAMPAIGN_STATUSES, QualityRecallError


def _device_ids(campaign):
    return set(campaign.devices.values_list("device_id", flat=True))


def _close_campaign_tasks(*, campaign, canonical, user, now, reason):
    workitems = list(
        WorkItem.objects.select_for_update()
        .filter(uniq_key__startswith=f"quality-recall:{campaign.pk}:")
        .order_by("pk")
    )
    closed_workitem_ids = []
    closed_user_task_count = 0
    for workitem in workitems:
        if workitem.status != "OPEN":
            continue
        payload = dict(workitem.payload or {})
        payload["recall_supersession"] = {
            "source_campaign_id": campaign.pk,
            "source_recall_no": campaign.recall_no,
            "canonical_campaign_id": canonical.pk,
            "canonical_recall_no": canonical.recall_no,
            "superseded_at": now.isoformat(),
            "reason": reason,
        }
        workitem.payload = payload
        workitem.status = "CANCELLED"
        workitem.done_by_openid = str(user.wx_openid or "")
        workitem.done_at = now
        workitem.last_activity_at = now
        workitem.save(update_fields=(
            "payload", "status", "done_by_openid", "done_at",
            "last_activity_at", "updated_at",
        ))
        closed_workitem_ids.append(workitem.pk)
        closed_user_task_count += UserTask.objects.filter(
            workitem=workitem,
        ).exclude(state="DONE").update(
            state="DONE",
            need_action=False,
            done_at=now,
            done_reason="OTHER_ACTION",
            cursor_time=now,
            updated_at=now,
        )
    return closed_workitem_ids, closed_user_task_count


@transaction.atomic
def consolidate_recall_campaigns(
    *, user, canonical_campaign_id, redundant_campaign_ids, reason,
):
    """Move duplicate execution responsibility to one immutable canonical scope.

    Campaigns, frozen device rows and audit history are never deleted.  A
    redundant campaign may be superseded only when all of its devices are
    already present in the canonical campaign.
    """
    identity = _identity(user, RECALL_MANAGER_ROLES)
    reason = str(reason or "").strip()
    if not reason:
        raise QualityRecallError("请填写重叠召回批次合并理由。")

    canonical = (
        QualityRecallCampaign.objects.select_for_update()
        .select_related("quality_case__source_alert")
        .filter(
            pk=canonical_campaign_id,
            factory_organization=identity.organization,
            status__in=LIVE_CAMPAIGN_STATUSES,
        )
        .first()
    )
    if not canonical:
        raise QualityRecallError("主召回批次不存在、已结束或不属于当前厂家。")

    requested_ids = sorted({
        int(item) for item in redundant_campaign_ids
        if int(item) != canonical.pk
    })
    redundant = list(
        QualityRecallCampaign.objects.select_for_update()
        .select_related("quality_case__source_alert", "control_action")
        .filter(
            pk__in=requested_ids,
            factory_organization=identity.organization,
            status__in=LIVE_CAMPAIGN_STATUSES,
        )
        .order_by("pk")
    )
    if len(redundant) != len(requested_ids):
        raise QualityRecallError("部分待合并召回批次不存在、已结束或不属于当前厂家。")
    if not redundant:
        raise QualityRecallError("请选择至少一个待合并召回批次。")

    canonical_ids = _device_ids(canonical)
    for campaign in redundant:
        source_ids = _device_ids(campaign)
        if not source_ids.intersection(canonical_ids):
            raise QualityRecallError(
                f"批次{campaign.recall_no}与主批次没有重叠设备，禁止合并。"
            )
        uncovered = sorted(source_ids - canonical_ids)
        if uncovered:
            raise QualityRecallError(
                f"批次{campaign.recall_no}有{len(uncovered)}台设备不在主批次中；"
                "为避免漏召回，本次不执行合并。"
            )

    now = timezone.now()
    results = []
    canonical_case_ids = set(
        canonical.case_links.values_list("quality_case_id", flat=True)
    )
    canonical_case_ids.add(canonical.quality_case_id)
    for campaign in redundant:
        source_links = list(
            campaign.case_links.select_related("quality_case__source_alert").all()
        )
        source_cases = {campaign.quality_case_id: campaign.quality_case}
        source_cases.update({link.quality_case_id: link.quality_case for link in source_links})
        linked_case_ids = []
        for quality_case in source_cases.values():
            _link, created = QualityRecallCampaignCaseLink.objects.get_or_create(
                campaign=canonical,
                quality_case=quality_case,
                defaults={
                    "link_type": QualityRecallCampaignCaseLink.LinkType.CONTRIBUTING,
                    "source_campaign": campaign,
                    "linked_by": user,
                    "reason": reason,
                },
            )
            canonical_case_ids.add(quality_case.pk)
            linked_case_ids.append(quality_case.pk)
            if canonical.status == QualityRecallCampaign.Status.ACTIVE and (
                quality_case.status != BatchQualityCase.Status.RECALLING
            ):
                quality_case.status = BatchQualityCase.Status.RECALLING
                quality_case.save(update_fields=("status", "updated_at"))
            QualityTraceEvent.objects.create(
                quality_case=quality_case,
                alert=quality_case.source_alert,
                event_type="RECALL_CAMPAIGN_SUPERSEDED",
                actor=user,
                actor_role_snapshot=identity.role.code,
                payload={
                    "source_campaign_id": campaign.pk,
                    "source_recall_no": campaign.recall_no,
                    "canonical_campaign_id": canonical.pk,
                    "canonical_recall_no": canonical.recall_no,
                    "case_link_created": created,
                    "reason": reason,
                },
            )

        closed_workitem_ids, closed_user_task_count = _close_campaign_tasks(
            campaign=campaign,
            canonical=canonical,
            user=user,
            now=now,
            reason=reason,
        )
        completed_control_action_id = None
        action = campaign.control_action
        if action and action.status == QualityControlAction.Status.ACTIVE:
            action.status = QualityControlAction.Status.COMPLETED
            action.ended_at = now
            action.ended_by = user
            action.end_reason = (
                f"召回执行由主批次{canonical.recall_no}接管：{reason}"
            )
            action.save(update_fields=(
                "status", "ended_at", "ended_by", "end_reason",
            ))
            completed_control_action_id = action.pk

        campaign.status = QualityRecallCampaign.Status.SUPERSEDED
        campaign.superseded_by = canonical
        campaign.superseded_at = now
        campaign.superseded_by_user = user
        campaign.supersession_reason = reason
        campaign.save(update_fields=(
            "status", "superseded_by", "superseded_at",
            "superseded_by_user", "supersession_reason", "updated_at",
        ))
        results.append({
            "campaign_id": campaign.pk,
            "recall_no": campaign.recall_no,
            "device_count": campaign.device_count,
            "linked_case_ids": sorted(linked_case_ids),
            "closed_workitem_ids": closed_workitem_ids,
            "closed_user_task_count": closed_user_task_count,
            "completed_control_action_id": completed_control_action_id,
        })

    canonical_workitem_ids = []
    if canonical.status == QualityRecallCampaign.Status.ACTIVE:
        from .recall_task_service import publish_recall_tasks

        task_rows = publish_recall_tasks(user=user, campaign_id=canonical.pk)
        canonical_workitem_ids = sorted({
            workitem_id
            for row in task_rows
            for workitem_id in row.get("workitem_ids", [])
        })

    QualityTraceEvent.objects.create(
        quality_case=canonical.quality_case,
        alert=canonical.quality_case.source_alert,
        event_type="RECALL_CAMPAIGNS_CONSOLIDATED",
        actor=user,
        actor_role_snapshot=identity.role.code,
        payload={
            "canonical_campaign_id": canonical.pk,
            "canonical_recall_no": canonical.recall_no,
            "canonical_device_count": canonical.device_count,
            "redundant_campaign_ids": [item["campaign_id"] for item in results],
            "related_quality_case_ids": sorted(canonical_case_ids),
            "reason": reason,
        },
    )
    return {
        "canonical_campaign_id": canonical.pk,
        "canonical_recall_no": canonical.recall_no,
        "canonical_device_count": canonical.device_count,
        "related_quality_case_ids": sorted(canonical_case_ids),
        "canonical_workitem_ids": canonical_workitem_ids,
        "superseded_campaigns": results,
    }
