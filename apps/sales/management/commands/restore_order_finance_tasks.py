"""Restore missing independent finance starts without inventing financial facts."""

from django.core.management.base import BaseCommand
from django.db import transaction

from sales.models import OrderPlan, OrderPlanFinance
from sales.services.order_finance_service import PENDING_CANCELLATION_STATUSES, initialize_order_finance
from workflow.models import WorkItem


class Command(BaseCommand):
    help = "Restore missing finance ledgers/tasks for active, not-accounted V2 orders; does not change money or documents."

    def add_arguments(self, parser):
        parser.add_argument("--check-only", action="store_true", help="Report eligible orders without writing.")
        parser.add_argument("--plan-id", type=int, help="Limit restoration to one order.")

    def handle(self, *args, **options):
        plans = OrderPlan.objects.filter(
            flow_version=OrderPlan.FlowVersion.MATERIAL_STOCK_V2, accounted_at__isnull=True,
        ).exclude(status=OrderPlan.Status.CANCELLED).exclude(
            cancellation_audit__status__in=PENDING_CANCELLATION_STATUSES,
        ).order_by("pk")
        if options["plan_id"]:
            plans = plans.filter(pk=options["plan_id"])
        if options["check_only"]:
            self.stdout.write(f"Eligible orders: {plans.count()}; missing ledgers: {plans.filter(finance__isnull=True).count()}; no changes written.")
            return
        count = skipped = 0
        ledgers_before = OrderPlanFinance.objects.count()
        tasks_before = WorkItem.objects.count()
        for plan_id in plans.values_list("pk", flat=True).iterator():
            with transaction.atomic():
                plan = OrderPlan.objects.select_for_update().get(pk=plan_id)
                if plan.accounted_at or plan.status == OrderPlan.Status.CANCELLED:
                    continue
                finance = OrderPlanFinance.objects.filter(order_plan=plan).first()
                if finance and finance.statement_submitted_at and finance.receivable_amount is None:
                    self.stderr.write(f"Skipped order {plan.pk}: submitted statement has no receivable amount; manual review required.")
                    skipped += 1
                    continue
                initialize_order_finance(plan)
                count += 1
        self.stdout.write(self.style.SUCCESS(
            f"Orders inspected: {count}; ledgers created: {OrderPlanFinance.objects.count() - ledgers_before}; "
            f"tasks created: {WorkItem.objects.count() - tasks_before}; skipped inconsistent records: {skipped}."
        ))
