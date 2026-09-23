"""Idempotently consolidate historical financial tasks after schema migration."""
import json

from django.core.management.base import BaseCommand
from django.db import transaction

from lifecycle.models import GoodsTransferSettlement
from lifecycle.services.goods_transfer_receivable_service import pair_context
from lifecycle.services.goods_transfer_receivable_task_service import reconcile_receivable_tasks


class Command(BaseCommand):
    help = "汇总已形成应收的商家发货，将历史付款待办集中到最新发货单。"

    def add_arguments(self, parser):
        modes = parser.add_mutually_exclusive_group()
        modes.add_argument("--check-only", action="store_true")
        modes.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        pairs = set()
        summaries = []
        invoices = GoodsTransferSettlement.objects.filter(
            transfer__flow_type="SHIPMENT", statement_submitted_at__isnull=False,
        ).exclude(transfer__status__in=("SCANNING", "CANCELLED")).select_related("transfer")
        for invoice in invoices.order_by("pk"):
            pair = (invoice.transfer.from_organization_id, invoice.transfer.to_organization_id)
            if pair in pairs:
                continue
            pairs.add(pair)
            with transaction.atomic():
                changes = reconcile_receivable_tasks(invoice) if options["apply"] else {}
                context = pair_context(invoice)
                active = context["active_settlement"]
                row = {
                    "seller_organization_id": pair[0], "buyer_organization_id": pair[1],
                    "active_transfer_id": active.transfer_id if active else None,
                    "invoice_count": len(context["rows"]),
                    "outstanding_amount": format(context["outstanding_amount"], ".2f"),
                    "pending_voucher_count": len(context["pending_payments"]), **changes,
                }
                summaries.append(row)
                self.stdout.write(json.dumps(row, ensure_ascii=False))
        self.stdout.write("ROLLING_RECEIVABLES_%s pairs=%s closed=%s created=%s" % (
            "APPLIED" if options["apply"] else "CHECK_ONLY", len(summaries),
            sum(row.get("closed", 0) for row in summaries),
            sum(row.get("created", 0) for row in summaries),
        ))
