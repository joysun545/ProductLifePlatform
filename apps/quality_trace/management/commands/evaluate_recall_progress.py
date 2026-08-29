import json

from django.core.management.base import BaseCommand
from django.utils import timezone

from quality_trace.services.recall_progress_service import evaluate_active_recall_progress


class Command(BaseCommand):
    help = "Calculate active recall progress and publish deduplicated overdue escalations."

    def handle(self, *args, **options):
        results = evaluate_active_recall_progress(at=timezone.now(), source="SCHEDULED")
        self.stdout.write(json.dumps({
            "evaluated_campaign_count": len(results),
            "overdue_campaign_count": sum(1 for item in results if item["is_overdue"]),
            "results": results,
            "evaluated_at": timezone.now().isoformat(),
        }, ensure_ascii=False))
