import json

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from quality_trace.services import expire_temporary_holds


class Command(BaseCommand):
    help = "Expire reversible quality temporary holds whose deadlines have passed."

    def add_arguments(self, parser):
        parser.add_argument("--at", dest="at", default=None)

    def handle(self, *args, **options):
        at = None
        if options.get("at"):
            at = parse_datetime(options["at"])
            if not at:
                raise ValueError("--at must be an ISO-8601 datetime")
            if timezone.is_naive(at):
                at = timezone.make_aware(at)
        expired = expire_temporary_holds(at=at)
        self.stdout.write(json.dumps({
            "expired_temporary_hold_count": expired,
            "evaluated_at": (at or timezone.now()).isoformat(),
        }, ensure_ascii=False))
