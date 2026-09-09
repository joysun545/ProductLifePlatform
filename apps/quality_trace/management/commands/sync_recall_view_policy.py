import json
from django.core.management.base import BaseCommand, CommandError
from quality_trace.models import QualityRecallCampaign
from quality_trace.services.recall_access_service import RecallViewer, recall_campaign_queryset, visible_rows
from quality_trace.services.recall_notice_service import sync_recall_policy_notices


class Command(BaseCommand):
    help = "核验召回查看范围，并可补齐厂家知晓和销售渠道任务；不改设备流转或召回快照。"

    def add_arguments(self, parser):
        parser.add_argument("--campaign-no", required=True)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        campaign = recall_campaign_queryset().filter(recall_no=options["campaign_no"]).first()
        if not campaign:
            raise CommandError("召回编号不存在，未做修改。")
        if campaign.status == QualityRecallCampaign.Status.DRAFT:
            raise CommandError("召回尚未发布，不补发任务。")
        from account.models import UserOrganization
        from quality_trace.services.recall_display_service import campaign_context
        identities = UserOrganization.objects.select_related("user", "organization", "role").filter(
            is_active=True, user__is_active=True, role__is_active=True, organization__is_active=True,
        ).order_by("organization_id", "role__code", "user_id")
        report = []
        for identity in identities:
            viewer = RecallViewer(identity.user)
            rows = visible_rows(campaign, viewer)
            if rows or viewer.metadata_access(campaign):
                report.append({"user_id": identity.user_id, "organization_id": identity.organization_id,
                    "organization": identity.organization.name, "role": identity.role.code,
                    "scope": campaign_context(campaign, rows, viewer)["scope_label"],
                    "device_count": None if viewer.observer(campaign) else len(rows),
                    "observer_only": viewer.observer(campaign),
                    "full_campaign_access": viewer.full_campaign(campaign)})
        self.stdout.write(json.dumps({"recall_no": campaign.recall_no, "roles": report}, ensure_ascii=False, indent=2))
        if options["apply"]:
            result = sync_recall_policy_notices(campaign=campaign)
            self.stdout.write("RECALL_VIEW_POLICY_SYNC_SUCCESS " + json.dumps(result, ensure_ascii=False))
        else:
            self.stdout.write("只读核验完成；未更改设备、角色或任务。")
