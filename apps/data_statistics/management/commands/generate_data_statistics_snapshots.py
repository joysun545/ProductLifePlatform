import json

from django.core.management.base import BaseCommand
from django.utils import timezone

from data_statistics.services import generate_daily_snapshots


class Command(BaseCommand):
    help = "生成数据统计组织级日快照（幂等）"

    def add_arguments(self, parser):
        parser.add_argument("--date", help="快照日期 YYYY-MM-DD，默认今天")
        parser.add_argument("--organization-id", type=int)

    def handle(self, *args, **options):
        date_text = options.get("date")
        if date_text:
            try:
                snapshot_date = timezone.datetime.strptime(date_text, "%Y-%m-%d").date()
            except ValueError as exc:
                raise ValueError("--date 必须是 YYYY-MM-DD") from exc
        else:
            snapshot_date = timezone.localdate()
        results = generate_daily_snapshots(snapshot_date, options.get("organization_id"))
        self.stdout.write(json.dumps({
            "snapshot_date": snapshot_date.isoformat(),
            "organization_count": len(results),
            "results": results,
        }, ensure_ascii=False))
