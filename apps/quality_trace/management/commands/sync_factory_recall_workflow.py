import json

from django.core.management.base import BaseCommand, CommandError

from account.models import UserOrganization
from quality_trace.models import QualityRecallCampaign, QualityRecallTransportLeg
from quality_trace.services.recall_collaboration_service import (
    publish_factory_transport_tasks,
    publish_inventory_posting_task,
)


class Command(BaseCommand):
    help = "Reconcile factory receipt/accounting tasks for an active recall campaign."

    def add_arguments(self, parser):
        parser.add_argument("--campaign-id", type=int, required=True)

    def handle(self, *args, **options):
        campaign = QualityRecallCampaign.objects.filter(
            pk=options["campaign_id"], status=QualityRecallCampaign.Status.ACTIVE,
        ).first()
        if not campaign:
            raise CommandError("召回批次不存在或不是进行中状态。")
        actor_identity = UserOrganization.objects.select_related("user").filter(
            organization=campaign.factory_organization,
            role__code="factory_chief_engineer",
            is_active=True,
            user__is_active=True,
        ).first()
        if not actor_identity:
            raise CommandError("厂家没有有效的厂家总工程师账号。")
        results = []
        legs = campaign.transport_legs.filter(
            is_factory_destination=True,
        ).exclude(status=QualityRecallTransportLeg.Status.CANCELLED)
        for leg in legs.select_related("recall_device", "to_organization"):
            if leg.status in {
                QualityRecallTransportLeg.Status.PREPARING,
                QualityRecallTransportLeg.Status.IN_TRANSIT,
            }:
                action, notice = publish_factory_transport_tasks(
                    leg, user=actor_identity.user,
                )
                results.append({"leg_id": leg.pk, "stage": "PENDING_RECEIPT",
                                "action_workitem_id": action.pk,
                                "notice_workitem_id": notice.pk if notice else None})
            elif (
                leg.status == QualityRecallTransportLeg.Status.RECEIVED
                and not hasattr(leg, "isolation_receipt")
            ):
                action = publish_inventory_posting_task(
                    leg, user=actor_identity.user,
                )
                results.append({"leg_id": leg.pk, "stage": "PENDING_POSTING",
                                "action_workitem_id": action.pk})
        self.stdout.write(json.dumps({
            "campaign_id": campaign.pk,
            "reconciled_leg_count": len(results),
            "results": results,
        }, ensure_ascii=False))
