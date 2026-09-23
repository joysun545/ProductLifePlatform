"""Regression checks for the development-test supplier dispatch policy."""

from datetime import timedelta
import inspect

from django.test import SimpleTestCase
from django.utils import timezone

from sales.services import order_plan_material_flow_service as flow


class SupplierDeliveryImmediateDispatchTests(SimpleTestCase):
    def test_development_policy_uses_the_current_time_not_a_clock_window(self):
        now = timezone.now()

        scheduled_for = flow._next_dispatch_window(now)

        self.assertEqual(flow.SUPPLIER_DELIVERY_DISPATCH_MODE, "IMMEDIATE_TEST")
        self.assertLessEqual(abs(scheduled_for - now), timedelta(microseconds=1))

    def test_warehouse_confirmation_invokes_the_standard_dispatcher(self):
        source = inspect.getsource(flow.warehouse_review_material_plan)

        self.assertIn("dispatch_due_supply_deliveries(now=now)", source)
        self.assertIn("订单物料配送任务已即时推送", source)
