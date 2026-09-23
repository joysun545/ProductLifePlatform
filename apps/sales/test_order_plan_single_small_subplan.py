from django.test import SimpleTestCase

from sales.services.order_plan_material_flow_service import _subplan_slice_totals_are_valid


class SingleSmallProductionSubplanPolicyTests(SimpleTestCase):
    def test_one_to_four_units_are_valid_when_not_split(self):
        for quantity in range(1, 5):
            self.assertTrue(_subplan_slice_totals_are_valid([quantity]))

    def test_tail_split_policy_still_requires_five_to_fifty_units(self):
        self.assertTrue(_subplan_slice_totals_are_valid([5, 1]))
        self.assertTrue(_subplan_slice_totals_are_valid([50, 500]))
        self.assertFalse(_subplan_slice_totals_are_valid([4, 6]))
        self.assertFalse(_subplan_slice_totals_are_valid([51, 6]))

    def test_zero_quantity_is_invalid(self):
        self.assertFalse(_subplan_slice_totals_are_valid([0]))
