import json

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from quality_trace.models import QualityMetricSnapshot
from quality_trace.services.metric_engine import evaluate_active_rules


class Command(BaseCommand):
    help = "Evaluate all currently active quality alert-rule versions."

    def add_arguments(self, parser):
        parser.add_argument("--factory-id", type=int)
        parser.add_argument("--rule-id", type=int)
        parser.add_argument("--at")

    def handle(self, *args, **options):
        at = timezone.now()
        if options.get("at"):
            at = parse_datetime(options["at"])
            if not at:
                raise CommandError("--at must be an ISO-8601 datetime")
            if timezone.is_naive(at):
                at = timezone.make_aware(at)
        results = evaluate_active_rules(
            source=QualityMetricSnapshot.Source.SCHEDULED,
            factory_id=options.get("factory_id"),
            rule_id=options.get("rule_id"),
            at=at,
        )
        target_count = sum(len(row["targets"]) for row in results)
        alert_count = sum(
            1 for row in results for target in row["targets"] if target["alert_id"]
        )
        self.stdout.write(json.dumps({
            "evaluated_rule_count": len(results),
            "evaluated_target_count": target_count,
            "triggered_or_existing_alert_count": alert_count,
            "evaluated_at": at.isoformat(),
        }, ensure_ascii=False))
