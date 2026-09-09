import json

from django.core.management.base import BaseCommand

from quality_trace.services.recall_disposition_service import (
    sync_recall_disposition_execution_tasks,
)


class Command(BaseCommand):
    help = "Backfill role execution tasks for active recall dispositions."

    def add_arguments(self, parser):
        parser.add_argument("--campaign-id", type=int)

    def handle(self, *args, **options):
        results = sync_recall_disposition_execution_tasks(
            campaign_id=options.get("campaign_id"),
        )
        self.stdout.write(json.dumps({
            "synced_count": len(results), "results": results,
        }, ensure_ascii=False))
