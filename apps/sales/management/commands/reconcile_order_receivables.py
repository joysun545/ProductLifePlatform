"""Reconcile existing order-plan receivables without changing source documents."""
import json

from django.core.management.base import BaseCommand, CommandError

from sales.models import OrderPlanFinance
from sales.services.order_receivable_service import OrderReceivableError, pair_context
from sales.services.order_receivable_task_service import reconcile_order_receivable_tasks


class Command(BaseCommand):
    help = "Inspect order receivables (default) or allocate available confirmed cash and consolidate finance tasks."

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--check-only", action="store_true")
        mode.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        pairs = OrderPlanFinance.objects.order_by().values_list(
            "order_plan__factory_organization_id", "order_plan__merchant_organization_id",
        ).distinct()
        try:
            for factory_id, merchant_id in list(pairs):
                finance = OrderPlanFinance.objects.select_related("order_plan").filter(
                    order_plan__factory_organization_id=factory_id,
                    order_plan__merchant_organization_id=merchant_id,
                ).order_by("pk").first()
                changes = reconcile_order_receivable_tasks(finance) if options["apply"] else {}
                context = pair_context(finance)
                active = context["active_finance"]
                self.stdout.write(json.dumps({
                    "factory_organization_id": factory_id, "merchant_organization_id": merchant_id,
                    "active_order_plan_id": active.order_plan_id if active else None,
                    "order_count": len(context["rows"]),
                    "outstanding_amount": format(context["outstanding_amount"], ".2f"),
                    "overpaid_amount": format(context["overpaid_amount"], ".2f"),
                    "pending_payment_count": len(context["pending_payments"]), **changes,
                }, ensure_ascii=False))
        except OrderReceivableError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write("ORDER_RECEIVABLES_APPLIED" if options["apply"] else "ORDER_RECEIVABLES_CHECK_ONLY")
