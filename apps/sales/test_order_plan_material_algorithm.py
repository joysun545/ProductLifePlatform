"""Regression tests for the explainable V2 material split planner."""

from account.models import OrganizationRole
from django.test import TestCase

from product.models import BOMItem
from sales.models import OrderPlan, OrderPlanMaterialPlan
from sales.services.order_plan_material_flow_service import (
    create_material_flow_order_plan,
    get_material_flow_bom_detail,
    get_material_flow_options,
)
from sales.test_v2_fixture import V2OrderPlanFixture
from supply_chain.models import FactoryMaterialLot, FactoryMaterialStock


class OrderPlanMaterialSplitAlgorithmTests(TestCase):
    """A split must be a complete stock-feasible production subplan."""

    _user_with_identity = V2OrderPlanFixture._user_with_identity
    _accepted_direct_relation = V2OrderPlanFixture._accepted_direct_relation
    _product_with_bom = V2OrderPlanFixture._product_with_bom
    _material_with_supplier = V2OrderPlanFixture._material_with_supplier

    def setUp(self):
        V2OrderPlanFixture.setUp(self)
        self.material_keeper = self._user_with_identity(
            "order-algorithm-material-keeper",
            "openid-order-algorithm-material-keeper",
            self.factory,
            OrganizationRole.objects.get(code="factory_material_stock"),
        )
        self.bom = self.generator.boms.get(version="active-v2")
        # The two material lines belong to the same product.  Inventory for
        # only one of them must never manufacture a false early child plan.
        BOMItem.objects.create(
            bom=self.bom,
            material=self.generator_extra_material[0],
            material_spec=self.generator_extra_material[1],
            supplier=self.supplier,
            quantity=1,
            remark="算法完整齐套校验物料",
        )
        self.primary_stock = FactoryMaterialStock.objects.create(
            factory=self.factory,
            supplier=self.supplier,
            material_spec=self.generator_material[1],
            unit=FactoryMaterialStock.Unit.PIECE,
        )
        self.extra_stock = FactoryMaterialStock.objects.create(
            factory=self.factory,
            supplier=self.supplier,
            material_spec=self.generator_extra_material[1],
            unit=FactoryMaterialStock.Unit.PIECE,
        )

    def test_one_material_lot_cannot_create_a_partial_unproducible_first_subplan(self):
        FactoryMaterialLot.objects.create(
            stock=self.primary_stock,
            batch_code="V2-ALGORITHM-PARTIAL",
            supplier_batch_code="SUP-ALGORITHM-PARTIAL",
            received_milli=5_000,
            on_hand_milli=5_000,
            received_by=self.material_keeper,
        )
        result, created = create_material_flow_order_plan(
            user=self.sales_user,
            merchant_id=self.merchant.pk,
            client_request_id="v2-algorithm-complete-bundle-only",
            items=[
                {
                    "category_id": self.generator_category.pk,
                    "product_model_id": self.generator.pk,
                    "quantity": 10,
                    "use_return_inventory": False,
                    "source_bom_id": self.bom.pk,
                }
            ],
        )
        self.assertTrue(created)
        plan = OrderPlan.objects.get(pk=result["plan"]["id"])
        material_plan = OrderPlanMaterialPlan.objects.get(order_plan=plan)
        subplans = list(material_plan.production_subplans.order_by("sequence_no"))
        self.assertEqual(len(subplans), 1)
        self.assertEqual(subplans[0].planned_total_quantity, 10)

    def _assert_unsplit_production(self, quantity):
        result, created = create_material_flow_order_plan(
            user=self.sales_user,
            merchant_id=self.merchant.pk,
            client_request_id=f"v2-algorithm-normal-production-{quantity}",
            items=[{
                "category_id": self.generator_category.pk,
                "product_model_id": self.generator.pk,
                "quantity": quantity,
                "use_return_inventory": False,
                "source_bom_id": self.bom.pk,
            }],
        )
        self.assertTrue(created)
        material_plan = OrderPlanMaterialPlan.objects.get(order_plan_id=result["plan"]["id"])
        self.assertEqual(
            list(material_plan.production_subplans.values_list("planned_total_quantity", flat=True)),
            [quantity],
        )

    def test_no_tail_clearance_keeps_seventy_five_units_as_one_plan(self):
        self._assert_unsplit_production(75)

    def test_main_plan_can_exceed_two_fifty_unit_tail_limit(self):
        self._assert_unsplit_production(101)

    def test_small_normal_production_is_allowed_below_tail_split_minimum(self):
        self._assert_unsplit_production(4)

    def test_sales_creation_bom_endpoint_defaults_to_the_same_merchant_previous_v2_choice(self):
        """The pre-plan editor needs both a safe default and editable rows."""

        first, created = create_material_flow_order_plan(
            user=self.sales_user,
            merchant_id=self.merchant.pk,
            client_request_id="v2-algorithm-previous-bom",
            items=[
                {
                    "category_id": self.generator_category.pk,
                    "product_model_id": self.generator.pk,
                    "quantity": 5,
                    "use_return_inventory": False,
                    "source_bom_id": self.bom.pk,
                }
            ],
        )
        self.assertTrue(created)
        self.assertTrue(first["plan"]["id"])

        options = get_material_flow_options(
            self.sales_user,
            merchant_id=self.merchant.pk,
        )
        generator_row = next(
            model
            for category in options["categories"]
            for model in category["models"]
            if model["id"] == self.generator.pk
        )
        self.assertEqual(generator_row["recommended_bom_id"], self.bom.pk)
        self.assertEqual(generator_row["default_bom"]["id"], self.bom.pk)
        self.assertEqual(generator_row["default_bom"]["source"], "previous_order")

        detail = get_material_flow_bom_detail(
            user=self.sales_user,
            category_id=self.generator_category.pk,
            product_model_id=self.generator.pk,
            source_bom_id=self.bom.pk,
        )
        self.assertEqual(detail["bom"]["id"], self.bom.pk)
        self.assertTrue(
            any(
                row["id"] == self.generator_material[0].pk
                and row["selected_spec_id"] == self.generator_material[1].pk
                and row["selected_supplier_id"] == self.supplier.pk
                for row in detail["materials"]
            )
        )
