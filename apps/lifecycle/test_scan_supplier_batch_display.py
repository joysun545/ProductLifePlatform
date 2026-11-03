from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from sales.models import OrderPlanMaterialLotAllocation
from lifecycle.services.scan_service import ScanService


class ScanSupplierBatchDisplayTests(SimpleTestCase):
    def setUp(self):
        self.bom_item = SimpleNamespace(
            pk=31,
            material=SimpleNamespace(code="MAT-1", name="测试物料"),
            material_spec=SimpleNamespace(code="SPEC-1", name="测试规格"),
            supplier=SimpleNamespace(id=8, name="测试供应商"),
            supplier_id=8,
            material_id=4,
            material_spec_id=12,
            quantity=1,
            remark="",
        )
        bom_items = MagicMock()
        bom_items.select_related.return_value.order_by.return_value = [self.bom_item]
        bom = SimpleNamespace(
            pk=9,
            version="V2",
            version_no=2,
            items=bom_items,
        )
        self.device = SimpleNamespace(
            batch=SimpleNamespace(bom=bom),
            batch_id=456,
        )

    def _trace(self, usages):
        requirements = MagicMock()
        requirements.prefetch_related.return_value.order_by.return_value = []
        supplier_rows = MagicMock()
        supplier_rows.select_related.return_value.order_by.return_value = usages
        empty_allocations = (
            OrderPlanMaterialLotAllocation.objects.none()
            .select_related("material_lot")
            .order_by("material_lot__received_at", "id")
        )

        with (
            patch(
                "sales.models.OrderPlanMaterialRequirement.objects.filter",
                return_value=requirements,
            ),
            patch(
                "sales.models.OrderPlanMaterialLotAllocation.objects.filter",
                return_value=empty_allocations,
            ),
            patch(
                "sales.models.OrderPlanItemMaterialBatch.objects.filter",
                return_value=supplier_rows,
            ) as supplier_filter,
        ):
            result = ScanService._traceability(self.device)

        supplier_filter.assert_called_once_with(
            order_plan_item__production_subplan_items__production_batch_id=456,
            bom_item_id__in=[31],
        )
        return result["items"][0]

    def test_returns_the_supplier_lot_for_this_device_production_batch(self):
        supplier_batch = SimpleNamespace(
            batch_code="SUP-LOT-2026-01",
            supplier_id=8,
            material_id=4,
            material_spec_id=12,
        )
        usage = SimpleNamespace(
            bom_item_id=31,
            supplier_material_batch=supplier_batch,
        )

        row = self._trace([usage])

        self.assertEqual(
            row["supplier_production_batch_codes"], ["SUP-LOT-2026-01"]
        )
        self.assertEqual(
            row["supplier_production_batch_code_text"], "SUP-LOT-2026-01"
        )

    def test_omits_a_supplier_lot_linked_to_a_different_bom_supplier(self):
        supplier_batch = SimpleNamespace(
            batch_code="WRONG-SUPPLIER-LOT",
            supplier_id=99,
            material_id=4,
            material_spec_id=12,
        )
        usage = SimpleNamespace(
            bom_item_id=31,
            supplier_material_batch=supplier_batch,
        )

        row = self._trace([usage])

        self.assertEqual(row["supplier_production_batch_codes"], [])
        self.assertEqual(row["supplier_production_batch_code_text"], "")

    def test_returns_empty_supplier_lot_when_no_supplier_batch_is_registered(self):
        row = self._trace([])

        self.assertEqual(row["supplier_production_batch_codes"], [])
        self.assertEqual(row["supplier_production_batch_code_text"], "")
