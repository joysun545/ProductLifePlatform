import json

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from quality_trace.models import (
    QualityRecallDevice,
    QualityRecallDeviceEvent,
    QualityRecallTransportLeg,
)


class Command(BaseCommand):
    help = "Reconcile recall devices whose factory transport is received but device status is stale."

    def add_arguments(self, parser):
        parser.add_argument("--campaign-id", type=int)
        parser.add_argument("--commit", action="store_true")

    def handle(self, *args, **options):
        queryset = (
            QualityRecallTransportLeg.objects
            .select_related("recall_device__campaign", "received_by")
            .filter(
                is_factory_destination=True,
                status=QualityRecallTransportLeg.Status.RECEIVED,
            )
            .exclude(recall_device__status=QualityRecallDevice.Status.RECEIVED)
            .order_by("pk")
        )
        if options.get("campaign_id"):
            queryset = queryset.filter(campaign_id=options["campaign_id"])

        rows = list(queryset)
        result = [{
            "transport_leg_id": leg.pk,
            "campaign_id": leg.campaign_id,
            "recall_device_id": leg.recall_device_id,
            "sn": leg.recall_device.sn_snapshot,
            "from_status": leg.recall_device.status,
            "to_status": QualityRecallDevice.Status.RECEIVED,
        } for leg in rows]

        if options["commit"]:
            with transaction.atomic():
                now = timezone.now()
                for leg in rows:
                    device = QualityRecallDevice.objects.select_for_update().get(
                        pk=leg.recall_device_id,
                    )
                    if device.status == QualityRecallDevice.Status.RECEIVED:
                        continue
                    old_status = device.status
                    device.status = QualityRecallDevice.Status.RECEIVED
                    device.status_updated_at = leg.received_at or now
                    device.save(update_fields=("status", "status_updated_at"))
                    QualityRecallDeviceEvent.objects.get_or_create(
                        campaign=device.campaign,
                        recall_device=device,
                        to_status=QualityRecallDevice.Status.RECEIVED,
                        reason="厂家物流经理已确认收到召回设备，待销售助理入库记账",
                        payload={"transport_leg_id": leg.pk},
                        defaults={
                            "from_status": old_status,
                            "actor": leg.received_by,
                            "actor_role_snapshot": "factory_logistics",
                        },
                    )

        self.stdout.write(json.dumps({
            "mode": "COMMIT" if options["commit"] else "DRY_RUN",
            "matched_count": len(rows),
            "results": result,
        }, ensure_ascii=False, default=str))
