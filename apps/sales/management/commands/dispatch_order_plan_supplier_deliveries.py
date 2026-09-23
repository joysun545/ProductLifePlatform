"""Dispatch V2 supplier deliveries, with an optional safe order-plan scope."""

from __future__ import annotations

import json
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from sales.models import OrderPlanSupplyDelivery
from sales.services.order_plan_material_flow_service import (
    OrderPlanMaterialFlowError,
    dispatch_due_supply_deliveries,
)


def _parse_dispatch_time(raw_value):
    if not raw_value:
        return timezone.now()
    try:
        value = str(raw_value).strip()
        if value.endswith("Z"):
            value = f"{value[:-1]}+00:00"
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise CommandError("--at 必须是 ISO-8601 时间。") from exc
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


class Command(BaseCommand):
    help = "推送 V2 订单物料供应商配送任务；可限定到一个订单。"

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true")
        parser.add_argument("--at", default=None, metavar="ISO-8601")
        parser.add_argument(
            "--plan-code",
            default=None,
            help="仅推送此订单仍处于 QUEUED 状态的配送计划，避免影响其他订单。",
        )

    def handle(self, *args, **options):
        dispatch_time = _parse_dispatch_time(options.get("at"))
        plan_code = str(options.get("plan_code") or "").strip()
        delivery_ids = None
        if plan_code:
            delivery_ids = list(
                OrderPlanSupplyDelivery.objects.filter(
                    material_plan__order_plan__plan_code=plan_code,
                    status=OrderPlanSupplyDelivery.Status.QUEUED,
                ).values_list("pk", flat=True)
            )
            if not delivery_ids:
                raise CommandError(f"订单{plan_code}没有待推送的配送计划。")
        try:
            result = dispatch_due_supply_deliveries(
                now=dispatch_time,
                force=bool(options.get("force")),
                delivery_ids=delivery_ids,
            )
        except OrderPlanMaterialFlowError as exc:
            raise CommandError(f"订单物料配送任务推送失败：{exc}") from exc
        self.stdout.write(json.dumps({
            "dispatch_time": timezone.localtime(dispatch_time).isoformat(),
            "force": bool(options.get("force")),
            "plan_code": plan_code or None,
            **result,
        }, ensure_ascii=False, sort_keys=True))
