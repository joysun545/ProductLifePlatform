"""Idempotently close old merchant notices only for received V2 shipments."""

import json

from django.core.management.base import BaseCommand
from django.db import transaction

from sales.models import OrderPlan, OrderPlanShipment
from sales.services.shipment_receipt_notice_service import (
    close_received_shipment_notices, pending_received_shipment_notices,
)


class Command(BaseCommand):
    help = "Close superseded merchant shipped notices; keep finance and factory notices."

    def add_arguments(self, parser):
        modes = parser.add_mutually_exclusive_group()
        modes.add_argument("--check-only", action="store_true")
        modes.add_argument("--apply", action="store_true")
        parser.add_argument("--plan-id", type=int)

    def handle(self, *args, **options):
        shipments = OrderPlanShipment.objects.filter(
            order_plan__flow_version=OrderPlan.FlowVersion.MATERIAL_STOCK_V2,
            status=OrderPlanShipment.Status.RECEIVED, received_at__isnull=False,
        ).order_by("pk")
        if options["plan_id"] is not None:
            shipments = shipments.filter(order_plan_id=options["plan_id"])
        total = 0
        for shipment_id in shipments.values_list("pk", flat=True).iterator():
            with transaction.atomic():
                shipment = OrderPlanShipment.objects.select_for_update().get(pk=shipment_id)
                count = (close_received_shipment_notices(shipment) if options["apply"]
                         else pending_received_shipment_notices(shipment).count())
                if count:
                    self.stdout.write(json.dumps({"plan_id": shipment.order_plan_id,
                        "shipment_id": shipment.pk, "merchant_notice_count": count}))
                total += count
        self.stdout.write(f"RECEIVED_NOTICE_{'APPLIED' if options['apply'] else 'CHECK'} count={total}")
