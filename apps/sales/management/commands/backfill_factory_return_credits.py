"""Move historically unspent return credits into one factory's ledger."""
import json
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from account.models import Organization
from lifecycle.models import GoodsTransferAccountCase
from sales.services.factory_account_service import pending_return_credit, post_return_credit
from sales.services.order_receivable_service import OrderReceivableError


class Command(BaseCommand):
    help = "Preview historical factory return credits, or apply them with --apply."

    def add_arguments(self, parser):
        parser.add_argument("--factory-id", type=int, required=True)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        factory_id = options["factory_id"]
        apply = options["apply"]
        if not Organization.objects.filter(
            pk=factory_id, factory_root_id=factory_id, organization_type__code="OWNER",
        ).exists():
            raise CommandError("指定组织不是厂家根组织。")
        cases = GoodsTransferAccountCase.objects.filter(
            upper_organization_id=factory_id, case_type=GoodsTransferAccountCase.CaseType.RETURN_CREDIT,
            status=GoodsTransferAccountCase.Status.CONFIRMED, confirmed_amount__gt=0,
        ).select_related("confirmed_by", "transfer", "upper_organization__organization_type",
                         "lower_organization__organization_type").order_by("confirmed_at", "pk")
        entries = []
        try:
            with transaction.atomic():
                for case in cases.iterator():
                    if apply:
                        funding = post_return_credit(case, case.confirmed_by)
                        amount = funding.amount if funding else None
                    else:
                        pending = pending_return_credit(case)
                        amount = pending[0] if pending else None
                    if amount is not None:
                        entries.append({"return_credit_id": case.pk, "merchant_id": case.lower_organization_id,
                                        "amount": format(amount, ".2f")})
        except OrderReceivableError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps({
            "mode": "apply" if apply else "preview", "factory_id": factory_id, "count": len(entries),
            "total": format(sum((Decimal(entry["amount"]) for entry in entries), Decimal(0)), ".2f"),
            "credits": entries,
        }, ensure_ascii=False))
