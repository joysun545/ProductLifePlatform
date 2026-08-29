import json
from collections import defaultdict, deque

from django.core.management.base import BaseCommand, CommandError

from account.models import UserOrganization
from quality_trace.models import QualityRecallCampaign
from quality_trace.services.case_service import CONTROL_MANAGER_ROLES
from quality_trace.services.recall_consolidation_service import (
    consolidate_recall_campaigns,
)
from quality_trace.services.recall_service import LIVE_CAMPAIGN_STATUSES


def _actor_for_campaign(campaign, requested_user_id=None):
    rows = UserOrganization.objects.select_related("user", "role").filter(
        organization_id=campaign.factory_organization_id,
        role__code__in=CONTROL_MANAGER_ROLES,
        role__is_active=True,
        user__is_active=True,
        is_active=True,
    )
    if requested_user_id:
        row = rows.filter(user_id=requested_user_id).first()
        return row.user if row else None
    preferred_ids = [
        user.pk
        for user in (campaign.launched_by, campaign.created_by)
        if user
    ]
    row = rows.filter(user_id__in=preferred_ids).order_by("id").first()
    row = row or rows.order_by("role__code", "id").first()
    return row.user if row else None


def _components(campaigns, device_sets):
    by_device = defaultdict(set)
    for campaign in campaigns:
        for device_id in device_sets[campaign.pk]:
            by_device[device_id].add(campaign.pk)
    adjacency = defaultdict(set)
    for campaign_ids in by_device.values():
        for campaign_id in campaign_ids:
            adjacency[campaign_id].update(campaign_ids - {campaign_id})
    by_id = {campaign.pk: campaign for campaign in campaigns}
    pending = set(by_id)
    while pending:
        root = min(pending)
        queue = deque([root])
        found = set()
        while queue:
            campaign_id = queue.popleft()
            if campaign_id in found:
                continue
            found.add(campaign_id)
            queue.extend(adjacency[campaign_id] - found)
        pending -= found
        if len(found) > 1:
            yield [by_id[campaign_id] for campaign_id in sorted(found)]


class Command(BaseCommand):
    help = "预演或提交历史重叠召回批次合并，并收口冗余召回任务"

    def add_arguments(self, parser):
        parser.add_argument(
            "--commit",
            action="store_true",
            help="实际提交；未提供时仅输出预演结果",
        )
        parser.add_argument("--actor-user-id", type=int, default=None)
        parser.add_argument(
            "--reason",
            default="R13.8.3.1历史重叠召回批次合并与任务收口",
        )

    def handle(self, *args, **options):
        campaigns = list(
            QualityRecallCampaign.objects.select_related(
                "factory_organization", "launched_by", "created_by",
            )
            .filter(status__in=LIVE_CAMPAIGN_STATUSES)
            .order_by("factory_organization_id", "id")
        )
        device_sets = {
            campaign.pk: set(
                campaign.devices.values_list("device_id", flat=True)
            )
            for campaign in campaigns
        }
        factory_groups = defaultdict(list)
        for campaign in campaigns:
            factory_groups[campaign.factory_organization_id].append(campaign)

        results = []
        failure_count = 0
        conflict_count = 0
        for factory_id, rows in sorted(factory_groups.items()):
            for component in _components(rows, device_sets):
                canonical = max(
                    component,
                    key=lambda item: (
                        item.status == QualityRecallCampaign.Status.ACTIVE,
                        len(device_sets[item.pk]),
                        item.pk,
                    ),
                )
                redundant = [item for item in component if item.pk != canonical.pk]
                conflicts = [
                    {
                        "campaign_id": item.pk,
                        "recall_no": item.recall_no,
                        "uncovered_device_ids": sorted(
                            device_sets[item.pk] - device_sets[canonical.pk]
                        ),
                    }
                    for item in redundant
                    if not device_sets[item.pk].issubset(device_sets[canonical.pk])
                ]
                preview = {
                    "factory_organization_id": factory_id,
                    "canonical_campaign_id": canonical.pk,
                    "canonical_recall_no": canonical.recall_no,
                    "canonical_device_count": len(device_sets[canonical.pk]),
                    "redundant_campaigns": [
                        {
                            "campaign_id": item.pk,
                            "recall_no": item.recall_no,
                            "device_count": len(device_sets[item.pk]),
                        }
                        for item in redundant
                    ],
                    "conflicts": conflicts,
                    "status": "PREVIEW",
                }
                if conflicts:
                    preview["status"] = "CONFLICT"
                    conflict_count += 1
                    results.append(preview)
                    continue
                if options["commit"]:
                    actor = _actor_for_campaign(
                        canonical,
                        requested_user_id=options.get("actor_user_id"),
                    )
                    if not actor:
                        preview["status"] = "FAILED"
                        preview["reason"] = "主批次所属厂家缺少可审计的质量控制岗位账号"
                        failure_count += 1
                    else:
                        try:
                            preview["status"] = "COMMITTED"
                            preview["actor_user_id"] = actor.pk
                            preview["result"] = consolidate_recall_campaigns(
                                user=actor,
                                canonical_campaign_id=canonical.pk,
                                redundant_campaign_ids=[item.pk for item in redundant],
                                reason=options["reason"],
                            )
                        except Exception as exc:  # report every group in one run
                            preview["status"] = "FAILED"
                            preview["reason"] = str(exc)
                            failure_count += 1
                results.append(preview)

        payload = {
            "mode": "COMMIT" if options["commit"] else "DRY_RUN",
            "evaluated_live_campaign_count": len(campaigns),
            "overlap_group_count": len(results),
            "committed_group_count": sum(
                item["status"] == "COMMITTED" for item in results
            ),
            "conflict_group_count": conflict_count,
            "failed_group_count": failure_count,
            "results": results,
        }
        self.stdout.write(json.dumps(payload, ensure_ascii=False))
        if options["commit"] and (failure_count or conflict_count):
            raise CommandError("部分重叠召回组未完成合并，请检查JSON结果。")
