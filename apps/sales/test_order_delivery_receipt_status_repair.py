"""Regression contracts for incomplete confirmed delivery receipt visibility."""

import inspect

from django.test import SimpleTestCase

from sales.services import order_plan_material_flow_service as flow


class OrderDeliveryReceiptStatusRepairContracts(SimpleTestCase):
    def test_residual_line_remains_receiptable_even_if_delivery_status_is_satisfied(self):
        source = inspect.getsource(flow._delivery_receipt_rows)
        self.assertIn("OrderPlanSupplyDelivery.Status.SATISFIED", source)
        self.assertIn('received_milli__lt=F("confirmed_milli")', source)

    def test_delivery_completion_uses_actual_confirmed_line_receipts(self):
        helper = inspect.getsource(flow._delivery_fully_received)
        refresh = inspect.getsource(flow.refresh_material_reservations_for_stock)
        self.assertIn('confirmed_milli__gt=0', helper)
        self.assertIn('received_milli__lt=F("confirmed_milli")', helper)
        self.assertIn("if _delivery_fully_received(delivery):", refresh)
        self.assertNotIn("if _delivery_requirements_reserved(delivery):", refresh)
