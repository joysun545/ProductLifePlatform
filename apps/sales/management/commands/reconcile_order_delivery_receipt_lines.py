"""Repair old order-delivery receipt rows without creating any new inventory."""

from __future__ import annotations

import hashlib
import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import F

from sales.models import OrderPlan, OrderPlanMaterialPlan, OrderPlanSupplyDelivery
from supply_chain.models import MaterialWarehouseDocument


class Command(BaseCommand):
    help = "Backfill delivery-line received quantities from existing order-delivery receipt documents."

    def add_arguments(self, parser):
        parser.add_argument("--plan-code", required=True)
        parser.add_argument("--apply", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        try:
            plan = OrderPlan.objects.select_for_update().get(plan_code=options["plan_code"])
        except OrderPlan.DoesNotExist as exc:
            raise CommandError("Order plan was not found.") from exc
        material_plan = OrderPlanMaterialPlan.objects.select_for_update().filter(order_plan=plan).first()
        if not material_plan:
            raise CommandError("Order material plan was not found.")

        repaired = []
        deliveries = list(
            material_plan.supply_deliveries.select_for_update().prefetch_related("lines").order_by("id")
        )
        for delivery in deliveries:
            delivery_changed = False
            for line in delivery.lines.select_for_update().order_by("id"):
                if line.confirmed_milli <= line.received_milli:
                    continue
                quantity = line.confirmed_milli - line.received_milli
                request_hash = hashlib.sha256(
                    f"order-delivery:{plan.pk}:{delivery.pk}:{line.pk}:{quantity}".encode()
                ).hexdigest()
                document = MaterialWarehouseDocument.objects.filter(
                    factory_id=plan.factory_organization_id,
                    stock__supplier_id=delivery.supplier_id,
                    stock__material_spec_id=line.material_spec_id,
                    kind=MaterialWarehouseDocument.Kind.RECEIPT,
                    request_hash=request_hash,
                ).first()
                if not document:
                    continue
                repaired.append({
                    "delivery_id": delivery.pk,
                    "line_id": line.pk,
                    "document_no": document.document_no,
                    "received_milli": line.confirmed_milli,
                })
                delivery_changed = True
                if options["apply"]:
                    line.received_milli = line.confirmed_milli
                    line.save(update_fields=("received_milli", "updated_at"))
            if not delivery_changed:
                continue
            has_unreceived = delivery.lines.filter(
                confirmed_milli__gt=0,
                received_milli__lt=F("confirmed_milli"),
            ).exists()
            next_status = (
                OrderPlanSupplyDelivery.Status.RECEIVING
                if has_unreceived
                else OrderPlanSupplyDelivery.Status.SATISFIED
            )
            if options["apply"] and delivery.status != next_status:
                delivery.status = next_status
                delivery.save(update_fields=("status", "updated_at"))

        result = {
            "plan_code": plan.plan_code,
            "mode": "apply" if options["apply"] else "check-only",
            "repaired_count": len(repaired),
            "lines": repaired,
        }
        self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
