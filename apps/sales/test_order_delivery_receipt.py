"""Regression contracts for the dedicated order-delivery receipt flow."""
import inspect

from django.test import SimpleTestCase

from sales.services import order_plan_material_flow_service as flow


class OrderDeliveryReceiptContracts(SimpleTestCase):
    def test_receipt_is_prefilled_and_atomic(self):
        source = inspect.getsource(flow.receive_all_order_delivery_materials)
        self.assertIn("FactoryMaterialLot.objects.create", source)
        self.assertIn("refresh_material_reservations_for_stock", source)
        self.assertIn("lock=True", source)
        self.assertIn('revision=F("revision") + 1', source)
        self.assertIn("当前订单没有供应商已配送、待入库的物料", source)

    def test_form_exposes_only_confirmed_unreceived_delivery_lines(self):
        source = inspect.getsource(flow._delivery_receipt_rows)
        self.assertIn("SUPPLIER_CONFIRMED", source)
        self.assertIn("received_milli__lt=F(\"confirmed_milli\")", source)
        self.assertIn("select_for_update", source)
