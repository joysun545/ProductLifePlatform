"""Regression contracts for V2 supplier delivery and independent receipts."""

from uuid import uuid4

from django.test import TestCase

from account.models import (
    Organization,
    OrganizationRelation,
    OrganizationRelationType,
    OrganizationType,
)
from product.models import BOMItem
from sales.models import (
    OrderPlan,
    OrderPlanMaterialPlan,
    OrderPlanMaterialRequirement,
    OrderPlanSupplyDelivery,
)
from sales.services.order_plan_material_flow_service import (
    confirm_supplier_material_delivery,
    create_material_flow_order_plan,
    dispatch_due_supply_deliveries,
    get_material_flow_detail,
    warehouse_review_material_plan,
)
from sales.test_v2_fixture import V2OrderPlanFixture
from supply_chain.models import FactoryMaterialLot, FactoryMaterialStock, MaterialSpecSupplier
from supply_chain.services.material_inventory_service import submit_movement
from workflow.models import WorkItem


class OrderPlanMaterialDeliveryProgressTests(TestCase):
    """Delivery line quantities must reflect the independent receipt event."""

    _user_with_identity = V2OrderPlanFixture._user_with_identity
    _accepted_direct_relation = V2OrderPlanFixture._accepted_direct_relation
    _product_with_bom = V2OrderPlanFixture._product_with_bom
    _material_with_supplier = V2OrderPlanFixture._material_with_supplier

    def setUp(self):
        V2OrderPlanFixture.setUp(self)
        self.generator_bom = self.generator.boms.get(version="active-v2")
        self.primary_material, self.primary_spec = self.generator_material
        self.primary_stock = FactoryMaterialStock.objects.create(
            factory=self.factory,
            supplier=self.supplier,
            material_spec=self.primary_spec,
            unit=FactoryMaterialStock.Unit.PIECE,
        )
        OrganizationRelation.objects.get_or_create(
            source_organization=self.factory,
            target_organization=self.supplier,
            relation_type=OrganizationRelationType.SUPPLY,
        )

    def _create_primary_lot(self, quantity_milli, suffix):
        return FactoryMaterialLot.objects.create(
            stock=self.primary_stock,
            batch_code=f"DELIVERY-LOT-{suffix}",
            supplier_batch_code=f"SUPPLIER-{suffix}",
            received_milli=quantity_milli,
            on_hand_milli=quantity_milli,
            received_by=self.factory_material_stock,
        )

    def _create_v2_plan(self, request_id):
        result, created = create_material_flow_order_plan(
            user=self.sales_user,
            merchant_id=self.merchant.pk,
            client_request_id=request_id,
            items=[
                {
                    "category_id": self.generator_category.pk,
                    "product_model_id": self.generator.pk,
                    "quantity": 10,
                    "use_return_inventory": False,
                    "source_bom_id": self.generator_bom.pk,
                }
            ],
            remark="V2配送入库进度测试",
        )
        self.assertTrue(created)
        return OrderPlan.objects.get(pk=result["plan"]["id"])

    @staticmethod
    def _timeline_state(detail, code):
        return next(row["state"] for row in detail["timeline"] if row["code"] == code)

    def _independent_receipt(self, quantity, supplier_batch_code):
        return submit_movement(
            user=self.factory_material_stock,
            data={
                "kind": "RECEIPT",
                "expected_factory_id": self.factory.pk,
                "request_id": str(uuid4()),
                "supplier_id": self.supplier.pk,
                "material_spec_id": self.primary_spec.pk,
                "unit": FactoryMaterialStock.Unit.PIECE,
                "quantity": str(quantity),
                "supplier_batch_code": supplier_batch_code,
            },
        )

    def test_confirmation_persists_line_amounts_and_receipts_only_count_new_lot_delta(self):
        """4 old reserved + 2 new receipt must show 2, never the old 4 too."""

        # This stock existed before the order and is reserved at creation.  A
        # later supplier delivery therefore covers only the six-unit gap.
        self._create_primary_lot(4_000, "OLD")
        plan = self._create_v2_plan("delivery-progress-delta")
        material_plan = OrderPlanMaterialPlan.objects.get(order_plan=plan)
        warehouse_review_material_plan(user=self.factory_material_stock, plan_id=plan.pk)
        delivery = OrderPlanSupplyDelivery.objects.get(material_plan=material_plan)
        line = delivery.lines.get(material_spec=self.primary_spec)
        self.assertEqual(line.requested_milli, 6_000)
        self.assertEqual(line.confirmed_milli, 0)

        dispatch_due_supply_deliveries(force=True)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, OrderPlanSupplyDelivery.Status.DISPATCHED)
        self.assertEqual(
            self._timeline_state(
                get_material_flow_detail(user=self.factory_material_stock, plan_id=plan.pk),
                "SUPPLY",
            ),
            "CURRENT",
        )

        confirm_supplier_material_delivery(user=self.supplier_owner, plan_id=plan.pk)
        delivery.refresh_from_db()
        line.refresh_from_db()
        self.assertEqual(delivery.status, OrderPlanSupplyDelivery.Status.SUPPLIER_CONFIRMED)
        self.assertEqual(line.confirmed_milli, 6_000)
        self.assertEqual(line.received_milli, 0)
        first_confirmed_at = delivery.supplier_confirmed_at
        first_confirmed_by = delivery.supplier_confirmed_by
        # Retries are safe: the original supplier confirmation remains the
        # immutable audit fact instead of receiving a fresh timestamp.
        confirm_supplier_material_delivery(user=self.supplier_owner, plan_id=plan.pk)
        delivery.refresh_from_db()
        self.assertEqual(delivery.supplier_confirmed_at, first_confirmed_at)
        self.assertEqual(delivery.supplier_confirmed_by, first_confirmed_by)
        self.assertEqual(
            self._timeline_state(
                get_material_flow_detail(user=self.factory_material_stock, plan_id=plan.pk),
                "SUPPLY",
            ),
            "DONE",
        )

        # The independent material-warehouse page creates a new lot and its
        # hook receives that exact lot.  It must not count the four units that
        # had already been allocated before this supplier confirmation.
        self._independent_receipt(2, "NEW-2")
        delivery.refresh_from_db()
        line.refresh_from_db()
        self.assertEqual(delivery.status, OrderPlanSupplyDelivery.Status.RECEIVING)
        self.assertEqual(line.received_milli, 2_000)
        requirement = OrderPlanMaterialRequirement.objects.get(
            production_subplan_item__production_subplan__material_plan=material_plan,
            material_spec=self.primary_spec,
        )
        self.assertEqual(requirement.reserved_milli, 6_000)

        self._independent_receipt(4, "NEW-4")
        delivery.refresh_from_db()
        line.refresh_from_db()
        requirement.refresh_from_db()
        self.assertEqual(delivery.status, OrderPlanSupplyDelivery.Status.SATISFIED)
        self.assertEqual(line.received_milli, 6_000)
        self.assertEqual(requirement.reserved_milli, requirement.required_milli)

    def test_one_supplier_delivery_can_finish_while_another_supplier_is_still_short(self):
        """Delivery A must not wait for delivery B's unrelated shortage."""

        supplier_type = OrganizationType.objects.get(code="SUPPLIER")
        secondary_supplier = Organization.objects.create(
            name="订单进度第二供应商",
            code="ORDER-PROGRESS-SUPPLIER-2",
            organization_type=supplier_type,
            region="重庆",
        )
        OrganizationRelation.objects.create(
            source_organization=self.factory,
            target_organization=secondary_supplier,
            relation_type=OrganizationRelationType.SUPPLY,
        )
        secondary_owner = self._user_with_identity(
            "order-progress-supplier-2-owner",
            "openid-order-progress-supplier-2-owner",
            secondary_supplier,
            self.supplier_owner_role,
        )
        secondary_material, secondary_spec = self._material_with_supplier(
            "ORDER-PROGRESS-SECONDARY",
            "订单进度第二物料",
            self.generator_category,
        )
        MaterialSpecSupplier.objects.create(
            material_spec=secondary_spec,
            supplier=secondary_supplier,
        )
        BOMItem.objects.create(
            bom=self.generator_bom,
            material=secondary_material,
            material_spec=secondary_spec,
            supplier=secondary_supplier,
            quantity=1,
        )

        self._create_primary_lot(4_000, "MULTI-OLD")
        plan = self._create_v2_plan("delivery-progress-per-delivery")
        material_plan = OrderPlanMaterialPlan.objects.get(order_plan=plan)
        warehouse_review_material_plan(user=self.factory_material_stock, plan_id=plan.pk)
        deliveries = {
            delivery.supplier_id: delivery
            for delivery in OrderPlanSupplyDelivery.objects.filter(material_plan=material_plan)
        }
        primary_delivery = deliveries[self.supplier.pk]
        secondary_delivery = deliveries[secondary_supplier.pk]

        dispatch_due_supply_deliveries(force=True)
        confirm_supplier_material_delivery(user=self.supplier_owner, plan_id=plan.pk)
        self._independent_receipt(6, "MULTI-NEW")

        primary_delivery.refresh_from_db()
        secondary_delivery.refresh_from_db()
        self.assertEqual(primary_delivery.status, OrderPlanSupplyDelivery.Status.SATISFIED)
        self.assertEqual(secondary_delivery.status, OrderPlanSupplyDelivery.Status.DISPATCHED)
        self.assertTrue(
            OrderPlanMaterialRequirement.objects.filter(
                production_subplan_item__production_subplan__material_plan=material_plan,
                supplier=secondary_supplier,
                reserved_milli__lt=10_000,
            ).exists()
        )
        # Supplier B is still unconfirmed, so the order's supplier phase is
        # visibly current even though supplier A's own delivery is complete.
        self.assertEqual(
            self._timeline_state(
                get_material_flow_detail(user=self.factory_material_stock, plan_id=plan.pk),
                "SUPPLY",
            ),
            "CURRENT",
        )

        confirm_supplier_material_delivery(user=secondary_owner, plan_id=plan.pk)
        self.assertEqual(
            self._timeline_state(
                get_material_flow_detail(user=self.factory_material_stock, plan_id=plan.pk),
                "SUPPLY",
            ),
            "DONE",
        )

    def test_missing_supplier_recipient_does_not_block_other_due_deliveries(self):
        """One unconfigured supplier must remain queued without rolling back peers."""

        supplier_type = OrganizationType.objects.get(code="SUPPLIER")
        unconfigured_supplier = Organization.objects.create(
            name="订单进度未配置收件人供应商",
            code="ORDER-PROGRESS-SUPPLIER-NO-RECIPIENT",
            organization_type=supplier_type,
            region="重庆",
        )
        OrganizationRelation.objects.create(
            source_organization=self.factory,
            target_organization=unconfigured_supplier,
            relation_type=OrganizationRelationType.SUPPLY,
        )
        secondary_material, secondary_spec = self._material_with_supplier(
            "ORDER-PROGRESS-NO-RECIPIENT",
            "订单进度未配置收件人物料",
            self.generator_category,
        )
        MaterialSpecSupplier.objects.create(
            material_spec=secondary_spec,
            supplier=unconfigured_supplier,
        )
        BOMItem.objects.create(
            bom=self.generator_bom,
            material=secondary_material,
            material_spec=secondary_spec,
            supplier=unconfigured_supplier,
            quantity=1,
        )

        plan = self._create_v2_plan("delivery-progress-recipient-isolation")
        material_plan = OrderPlanMaterialPlan.objects.get(order_plan=plan)
        warehouse_review_material_plan(user=self.factory_material_stock, plan_id=plan.pk)
        deliveries = {
            delivery.supplier_id: delivery
            for delivery in OrderPlanSupplyDelivery.objects.filter(material_plan=material_plan)
        }
        configured_delivery = deliveries[self.supplier.pk]
        blocked_delivery = deliveries[unconfigured_supplier.pk]

        result = dispatch_due_supply_deliveries(force=True)
        configured_delivery.refresh_from_db()
        blocked_delivery.refresh_from_db()
        self.assertEqual(result["dispatched"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(
            result["errors"],
            [
                {
                    "delivery_id": blocked_delivery.pk,
                    "supplier_id": unconfigured_supplier.pk,
                    "reason": (
                        f"供应商{unconfigured_supplier.name}尚未配置有效负责人或负责销售，"
                        "配送任务保留待下次推送。"
                    ),
                }
            ],
        )
        self.assertEqual(
            configured_delivery.status,
            OrderPlanSupplyDelivery.Status.DISPATCHED,
        )
        self.assertEqual(
            blocked_delivery.status,
            OrderPlanSupplyDelivery.Status.QUEUED,
        )
        self.assertEqual(
            WorkItem.objects.filter(
                biz_ref=f"sales.OrderPlan:{plan.pk}",
                type="ORDER_PLAN_SUPPLIER_DELIVERY",
            ).count(),
            1,
        )

        retry_result = dispatch_due_supply_deliveries(force=True)
        configured_delivery.refresh_from_db()
        blocked_delivery.refresh_from_db()
        self.assertEqual(retry_result["dispatched"], 0)
        self.assertEqual(retry_result["skipped"], 1)
        self.assertEqual(len(retry_result["errors"]), 1)
        self.assertEqual(
            configured_delivery.status,
            OrderPlanSupplyDelivery.Status.DISPATCHED,
        )
        self.assertEqual(
            blocked_delivery.status,
            OrderPlanSupplyDelivery.Status.QUEUED,
        )
        self.assertEqual(
            WorkItem.objects.filter(
                biz_ref=f"sales.OrderPlan:{plan.pk}",
                type="ORDER_PLAN_SUPPLIER_DELIVERY",
            ).count(),
            1,
        )
