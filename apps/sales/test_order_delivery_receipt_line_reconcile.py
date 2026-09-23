"""Regression contracts for order-delivery receipt detail reconciliation."""

import inspect

from django.test import SimpleTestCase

from sales.services import order_plan_material_flow_service as flow


class OrderDeliveryReceiptLineReconcileContracts(SimpleTestCase):
    def test_receipt_marks_the_exact_confirmed_delivery_line_received(self):
        source = inspect.getsource(flow.receive_all_order_delivery_materials)
        self.assertIn('line.received_milli = line.confirmed_milli', source)
        self.assertIn('line.save(update_fields=("received_milli", "updated_at"))', source)

    def test_parent_delivery_status_is_reconciled_from_line_receipts(self):
        source = inspect.getsource(flow.receive_all_order_delivery_materials)
        self.assertIn('has_unreceived_line = delivery.lines.filter(', source)
        self.assertIn('received_milli__lt=F("confirmed_milli")', source)
        self.assertIn('else OrderPlanSupplyDelivery.Status.SATISFIED', source)
