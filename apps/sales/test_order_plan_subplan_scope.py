"""Regression checks for V2 production-subplan scoping."""

from types import SimpleNamespace

from django.test import SimpleTestCase

from sales.services.order_plan_material_flow_service import (
    MAX_SUBPLAN_COUNT,
    MAX_TAIL_STOCK_SUBPLAN_QUANTITY,
    _production_rows_by_product_model,
    _quantity_text,
    _subplan_slice_totals_are_valid,
)


class ProductionSubplanScopeTests(SimpleTestCase):
    def _row(self, product_model_id, quantity):
        return {
            "order_item": SimpleNamespace(product_model_id=product_model_id),
            "production_quantity": quantity,
        }

    def test_two_models_keep_their_own_split_scope(self):
        """150 + 50 is valid: no model has a 50-unit normal-plan cap."""

        groups = _production_rows_by_product_model((
            self._row(22, 150),
            self._row(11, 50),
        ))
        self.assertEqual([row[0]["order_item"].product_model_id for row in groups], [11, 22])
        self.assertEqual(
            [sum(row["production_quantity"] for row in group) for group in groups],
            [50, 150],
        )
        self.assertEqual(MAX_SUBPLAN_COUNT, 2)
        self.assertEqual(MAX_TAIL_STOCK_SUBPLAN_QUANTITY, 50)

    def test_repeated_rows_of_the_same_model_remain_one_model_scope(self):
        groups = _production_rows_by_product_model((
            self._row(7, 30),
            self._row(7, 20),
            self._row(8, 50),
        ))
        self.assertEqual(len(groups), 2)
        self.assertEqual(sum(row["production_quantity"] for row in groups[0]), 50)
        self.assertEqual(sum(row["production_quantity"] for row in groups[1]), 50)

    def test_normal_large_production_is_one_valid_subplan(self):
        self.assertTrue(_subplan_slice_totals_are_valid([150]))

    def test_small_tail_can_be_split_from_an_unrestricted_main_plan(self):
        self.assertTrue(_subplan_slice_totals_are_valid([15, 135]))

    def test_tail_over_fifty_or_a_third_subplan_is_rejected(self):
        self.assertFalse(_subplan_slice_totals_are_valid([51, 99]))
        self.assertFalse(_subplan_slice_totals_are_valid([15, 60, 60]))

    def test_quantity_display_keeps_integer_trailing_zeros(self):
        self.assertEqual(_quantity_text(100_000), "100")
        self.assertEqual(_quantity_text(150_000), "150")
        self.assertEqual(_quantity_text(30_000), "30")
        self.assertEqual(_quantity_text(1_500), "1.5")
