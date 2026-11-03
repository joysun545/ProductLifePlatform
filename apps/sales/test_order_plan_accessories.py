from django.test import TestCase
from django.utils import timezone
from product.models import ProductModel

from sales.models import (
    OrderPlan,
    OrderPlanAccessoryCompatibility,
    OrderPlanAccessoryItem,
    OrderPlanAccessoryLotAllocation,
    OrderPlanShipmentAccessoryItem,
)
from sales.services.order_plan_material_flow_service import (
    OrderPlanMaterialFlowError,
    _create_v2_shipment,
    _receive_accessory_allocations,
    _ship_accessory_allocations,
    create_material_flow_order_plan,
    get_material_flow_options,
)
from sales.test_v2_fixture import V2OrderPlanFixture
from supply_chain.models import (
    FactoryMaterialLot,
    FactoryMaterialStock,
    MaterialWarehouseDocument,
    OrganizationMaterialLot,
)


class OrderPlanAccessoryFlowTests(V2OrderPlanFixture):
    """Accessory inventory remains separate from device production/lifecycle rows."""

    def _accessory(self, *, suffix, stock_milli):
        material, spec = self._material_with_supplier(
            f"ORDER-ACCESSORY-{suffix}",
            f"测试配件{suffix}",
            self.generator_category,
        )
        stock = FactoryMaterialStock.objects.create(
            factory=self.factory,
            supplier=self.supplier,
            material_spec=spec,
            unit=FactoryMaterialStock.Unit.PIECE,
        )
        lot = FactoryMaterialLot.objects.create(
            stock=stock,
            batch_code=f"ACCESSORY-LOT-{suffix}",
            received_milli=stock_milli,
            on_hand_milli=stock_milli,
            received_by=self.factory_material_stock,
        )
        mapping = OrderPlanAccessoryCompatibility.objects.create(
            factory_organization=self.factory,
            material_spec=spec,
            product_model=self.generator,
        )
        return material, spec, stock, lot, mapping

    def _create_plan(self, *, request_id, accessory_spec, accessory_quantity):
        result, created = create_material_flow_order_plan(
            user=self.sales_user,
            merchant_id=self.merchant.pk,
            client_request_id=request_id,
            items=[
                {
                    "category_id": self.generator_category.pk,
                    "product_model_id": self.generator.pk,
                    "quantity": 2,
                    "source_bom_id": self.generator.boms.get(version="active-v2").pk,
                }
            ],
            accessories=[
                {
                    "category_id": self.generator_category.pk,
                    "product_model_id": self.generator.pk,
                    "material_spec_id": accessory_spec.pk,
                    "quantity": accessory_quantity,
                }
            ],
        )
        self.assertTrue(created)
        return OrderPlan.objects.get(pk=result["plan"]["id"])

    def test_accessory_quantity_is_separate_and_moves_through_first_device_batch(self):
        _material, spec, _stock, lot, _mapping = self._accessory(
            suffix="MOVE", stock_milli=5_000
        )
        plan = self._create_plan(
            request_id="accessory-first-batch-move",
            accessory_spec=spec,
            accessory_quantity="2.5",
        )

        self.assertEqual(plan.total_quantity, 2)
        accessory = OrderPlanAccessoryItem.objects.get(order_plan=plan)
        self.assertEqual(accessory.quantity_milli, 2_500)
        # Reservation uses a queryset update to preserve the stock row lock;
        # refresh the fixture instance before asserting its persisted value.
        lot.refresh_from_db()
        self.assertEqual(lot.reserved_milli, 2_500)
        options = get_material_flow_options(self.sales_user)
        self.assertEqual(options["accessory_categories"][0]["specs"][0]["available_quantity"], "2.5")

        subplan = plan.material_plan.production_subplans.first()
        subplan_item = subplan.items.first()
        first, created = _create_v2_shipment(
            plan=plan,
            client_request_id="accessory-first-shipment",
            item_rows=[{"production_subplan_item_id": subplan_item.pk, "quantity": 1}],
            production_subplan=subplan,
            publish_authorization=False,
        )
        self.assertTrue(created)
        second, created = _create_v2_shipment(
            plan=plan,
            client_request_id="accessory-second-shipment",
            item_rows=[{"production_subplan_item_id": subplan_item.pk, "quantity": 1}],
            production_subplan=subplan,
            publish_authorization=False,
        )
        self.assertTrue(created)
        first_accessory = OrderPlanShipmentAccessoryItem.objects.get(shipment=first)
        self.assertEqual(first_accessory.quantity_milli, accessory.quantity_milli)
        self.assertFalse(OrderPlanShipmentAccessoryItem.objects.filter(shipment=second).exists())

        now = timezone.now()
        _ship_accessory_allocations(
            plan=plan,
            shipment=first,
            user=self.factory_stock,
            now=now,
        )
        lot.refresh_from_db()
        self.assertEqual(lot.on_hand_milli, 2_500)
        self.assertEqual(lot.reserved_milli, 0)
        self.assertEqual(
            MaterialWarehouseDocument.objects.filter(
                kind=MaterialWarehouseDocument.Kind.ORDER_SHIPMENT,
                stock_id=lot.stock_id,
            ).count(),
            1,
        )
        _receive_accessory_allocations(
            plan=plan,
            shipment=first,
            user=self.merchant_owner,
            now=now,
        )
        merchant_lot = OrganizationMaterialLot.objects.get(
            source_order_plan_id=plan.pk,
            source_shipment_id=first.pk,
        )
        self.assertEqual(merchant_lot.received_milli, 2_500)
        self.assertEqual(merchant_lot.source_batch_code_snapshot, lot.batch_code)
        accessory.refresh_from_db()
        self.assertEqual(accessory.status, OrderPlanAccessoryItem.Status.RECEIVED)
        self.assertEqual(
            OrderPlanAccessoryLotAllocation.objects.get(accessory_item=accessory).status,
            OrderPlanAccessoryLotAllocation.Status.RECEIVED,
        )

    def test_accessory_stock_shortage_rejects_the_whole_plan(self):
        _material, spec, _stock, lot, _mapping = self._accessory(
            suffix="SHORT", stock_milli=1_000
        )
        with self.assertRaisesRegex(OrderPlanMaterialFlowError, "库存不足"):
            create_material_flow_order_plan(
                user=self.sales_user,
                merchant_id=self.merchant.pk,
                client_request_id="accessory-stock-shortage-rejected",
                items=[
                    {
                        "category_id": self.generator_category.pk,
                        "product_model_id": self.generator.pk,
                        "quantity": 1,
                        "source_bom_id": self.generator.boms.get(version="active-v2").pk,
                    }
                ],
                accessories=[
                    {
                        "category_id": self.generator_category.pk,
                        "product_model_id": self.generator.pk,
                        "material_spec_id": spec.pk,
                        "quantity": "2",
                    }
                ],
            )
        self.assertFalse(
            OrderPlan.objects.filter(
                client_request_id="accessory-stock-shortage-rejected"
            ).exists()
        )
        lot.refresh_from_db()
        self.assertEqual(lot.reserved_milli, 0)

    def test_accessory_stock_shortage_aggregates_across_compatible_models(self):
        _material, spec, _stock, lot, _mapping = self._accessory(
            suffix="AGGREGATE", stock_milli=1_000
        )
        second_model = ProductModel.objects.create(
            code="ORDER-ACCESSORY-AGGREGATE-MODEL-2",
            name="第二适配型号",
            category=self.generator_category,
        )
        OrderPlanAccessoryCompatibility.objects.create(
            factory_organization=self.factory,
            material_spec=spec,
            product_model=second_model,
        )

        with self.assertRaisesRegex(OrderPlanMaterialFlowError, "库存不足"):
            create_material_flow_order_plan(
                user=self.sales_user,
                merchant_id=self.merchant.pk,
                client_request_id="accessory-aggregate-stock-shortage",
                items=[
                    {
                        "category_id": self.generator_category.pk,
                        "product_model_id": self.generator.pk,
                        "quantity": 1,
                        "source_bom_id": self.generator.boms.get(version="active-v2").pk,
                    }
                ],
                accessories=[
                    {
                        "category_id": self.generator_category.pk,
                        "product_model_id": self.generator.pk,
                        "material_spec_id": spec.pk,
                        "quantity": "0.75",
                    },
                    {
                        "category_id": self.generator_category.pk,
                        "product_model_id": second_model.pk,
                        "material_spec_id": spec.pk,
                        "quantity": "0.75",
                    },
                ],
            )

        self.assertFalse(
            OrderPlan.objects.filter(
                client_request_id="accessory-aggregate-stock-shortage"
            ).exists()
        )
        lot.refresh_from_db()
        self.assertEqual(lot.reserved_milli, 0)
