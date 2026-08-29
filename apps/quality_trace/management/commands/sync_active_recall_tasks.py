import json

from django.core.management.base import BaseCommand, CommandError

from account.models import UserOrganization
from quality_trace.models import QualityRecallCampaign
from quality_trace.services.case_service import CONTROL_MANAGER_ROLES
from quality_trace.services.recall_task_service import publish_recall_tasks
from quality_trace.services.recall_handover_service import sync_recall_delivery_collaboration


def _actor_for_campaign(campaign):
    preferred_ids = [
        user.pk
        for user in (
            campaign.launched_by,
            getattr(campaign.control_action, "acted_by", None),
            campaign.created_by,
        )
        if user
    ]
    identities = UserOrganization.objects.select_related("user", "role").filter(
        organization_id=campaign.factory_organization_id,
        role__code__in=CONTROL_MANAGER_ROLES,
        role__is_active=True,
        user__is_active=True,
        is_active=True,
    )
    preferred = identities.filter(user_id__in=preferred_ids).order_by("id").first()
    fallback = preferred or identities.order_by("role__code", "id").first()
    return fallback.user if fallback else None


class Command(BaseCommand):
    help = "幂等补齐活动召回批次的组织链路任务（仅用于部署修复或运维恢复）"

    def add_arguments(self, parser):
        parser.add_argument("--campaign-id", type=int, default=None)

    def handle(self, *args, **options):
        campaigns = (
            QualityRecallCampaign.objects.select_related(
                "launched_by", "created_by", "control_action__acted_by",
            )
            .filter(status=QualityRecallCampaign.Status.ACTIVE)
            .order_by("id")
        )
        if options.get("campaign_id"):
            campaigns = campaigns.filter(pk=options["campaign_id"])
        results = []
        for campaign in campaigns:
            actor = _actor_for_campaign(campaign)
            if not actor:
                results.append({
                    "campaign_id": campaign.pk,
                    "recall_no": campaign.recall_no,
                    "status": "SKIPPED",
                    "reason": "召回批次缺少可审计的启动账号",
                })
                continue
            try:
                rows = publish_recall_tasks(
                    user=actor,
                    campaign_id=campaign.pk,
                )
                delivery_rows = sync_recall_delivery_collaboration(
                    campaign=campaign,
                    actor=actor,
                )
                results.append({
                    "campaign_id": campaign.pk,
                    "recall_no": campaign.recall_no,
                    "status": "SYNCED",
                    "actor_user_id": actor.pk,
                    "device_count": len(rows),
                    "workitem_ids": sorted({
                        workitem_id
                        for row in rows
                        for workitem_id in row.get("workitem_ids", [])
                    }),
                    "delivery_collaboration_count": len(delivery_rows),
                    "delivery_collaboration": delivery_rows,
                })
            except Exception as exc:  # command must report every campaign
                results.append({
                    "campaign_id": campaign.pk,
                    "recall_no": campaign.recall_no,
                    "status": "FAILED",
                    "reason": str(exc),
                })
        payload = {
            "evaluated_campaign_count": len(results),
            "synced_campaign_count": sum(
                item["status"] == "SYNCED" for item in results
            ),
            "failed_campaign_count": sum(
                item["status"] == "FAILED" for item in results
            ),
            "results": results,
        }
        self.stdout.write(json.dumps(payload, ensure_ascii=False))
        if payload["failed_campaign_count"]:
            raise CommandError("部分活动召回批次任务同步失败，请根据结果逐项处理。")
