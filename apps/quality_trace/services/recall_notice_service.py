"""Policy notices; never rebuild launch snapshots or move physical devices."""
from django.db import transaction
from account.models import UserOrganization
from account.services.retired_roles import RETIRED_ROLE_CODES
from workflow.models import UserTask, WorkItem
from workflow.services.workflow_service import publish_workitem
from .recall_access_service import (
    FACTORY_RECALL_OBSERVER_ROLES, RecallViewer, recall_campaign_queryset, visible_rows,
)
from .recall_display_service import campaign_context


def _identities(campaign, roles):
    return UserOrganization.objects.select_related("user").filter(
        organization_id=campaign.factory_organization_id, organization__is_active=True,
        role__code__in=roles, role__is_active=True, user__is_active=True, is_active=True,
    ).exclude(user__wx_openid__isnull=True).exclude(user__wx_openid="")


def _notice(campaign, *, key, title, summary, recipients, payload, actor=None, kind="OBSERVER_NOTICE"):
    if not recipients:
        return 0
    item = WorkItem.objects.select_for_update().filter(uniq_key=key).first()
    missing = set(recipients)
    if item:
        missing -= set(item.user_tasks.values_list("to_openid", flat=True))
        if not missing:
            # Refresh safe scope text without resetting read/acknowledgment state.
            if (item.title, item.summary, item.payload) != (title, summary, payload):
                item.title, item.summary, item.payload = title, summary, payload
                item.save(update_fields=("title", "summary", "payload", "updated_at"))
            return 0
    publish_workitem(
        domain="SYSTEM", type=f"QUALITY_RECALL_{kind}", biz_id=str(campaign.pk),
        biz_ref=f"QualityRecallCampaign:{campaign.pk}", uniq_key=key,
        title=title, summary=summary,
        link=f"/pages/quality_trace/recall_detail/recall_detail?campaign_id={campaign.pk}",
        recipients_openids=sorted(missing), need_action=False, action_mode="EACH_ACK",
        payload=payload, actor_openid=getattr(actor, "wx_openid", "") or "",
        mark_workitem_done_immediately=False,
    )
    return len(missing)


@transaction.atomic
def sync_recall_policy_notices(*, campaign, actor=None):
    campaign = recall_campaign_queryset().get(pk=campaign.pk)
    observer_identities = _identities(campaign, FACTORY_RECALL_OBSERVER_ROLES)
    observers = list(observer_identities.values_list("user__wx_openid", flat=True))
    current_observers = list(observer_identities.exclude(
        role__code__in=RETIRED_ROLE_CODES,
    ).values_list("user__wx_openid", flat=True))
    # An old shared work item may also contain a chief's executable task. Only
    # remove the observer's action requirement, never finish the shared work item.
    corrected = UserTask.objects.filter(
        to_openid__in=observers, workitem__type__startswith="QUALITY_RECALL_",
        workitem__biz_id=str(campaign.pk), need_action=True,
    ).update(need_action=False)
    created = _notice(
        campaign, key=f"quality-recall:{campaign.pk}:factory-observers",
        title=f"召回知晓：{campaign.recall_no}",
        summary="请知晓本厂家已发布召回事项。本岗位无需办理召回操作。",
        recipients=current_observers, payload={"recall_campaign_id": campaign.pk, "observer_only": True}, actor=actor,
    )
    for identity in _identities(campaign, {"factory_sales"}):
        viewer = RecallViewer(identity.user)
        rows = visible_rows(campaign, viewer)
        if not rows:
            continue
        context = campaign_context(campaign, rows, viewer)
        created += _notice(
            campaign, key=f"quality-recall:{campaign.pk}:sales-user:{identity.user_id}",
            title=f"渠道召回任务：{campaign.recall_no}（{len(rows)}台）",
            summary=context["scope_summary"] + "，请跟踪本渠道及下级组织的召回进度。",
            recipients=[identity.user.wx_openid],
            payload={"recall_campaign_id": campaign.pk, "recall_device_ids": [row.pk for row in rows],
                     "device_count": len(rows), "scope_user_id": identity.user_id},
            actor=actor, kind="SALES_SCOPE_NOTICE",
        )
    return {"created_recipient_count": created, "observer_action_flags_removed": corrected}


def publish_sales_batch_notice(*, operation, legs, actor):
    for identity in _identities(operation.campaign, {"factory_sales"}):
        viewer = RecallViewer(identity.user)
        rows = [leg.recall_device for leg in legs if viewer.allows(leg.recall_device)]
        if not rows:
            continue
        action = {"DISPATCH": "已发出", "RECEIVE": "已接收", "ISOLATE": "已隔离入账"}[operation.action]
        _notice(
            operation.campaign, key=f"quality-recall-batch:{operation.pk}:sales-user:{identity.user_id}",
            title=f"本渠道召回流转：{len(rows)}台{action}",
            summary=f"本渠道及下级组织相关设备{len(rows)}台{action}，请查看当前节点。",
            recipients=[identity.user.wx_openid],
            payload={"recall_campaign_id": operation.campaign_id, "batch_operation_id": operation.pk,
                     "recall_device_ids": [row.pk for row in rows], "device_count": len(rows)},
            actor=actor, kind="SALES_BATCH_NOTICE",
        )
