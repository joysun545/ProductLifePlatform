"""Receipt statistics follow physical devices, partial receipts and reissues."""

from datetime import timedelta

from django.utils import timezone

from channel.models import MerchantChannelAssignment
from data_statistics.services import build_dashboard, get_statistics_identity
from data_statistics.services.flow_service import _market_rows, build_flow_statistics
from lifecycle.models import ActivationRecord, Device, OrganizationDevice
from manufacturing.models import ProductionBatch
from sales import test_order_plan_material_flow as fixture_module
from sales import test_shipment_workflow_v2 as shipment_fixture
from sales.models import OrderPlan, OrderPlanMaterialPlan, OrderPlanShipment, OrderPlanShipmentDevice, OrderPlanShipmentItem
from sales.services import order_plan_material_flow_service as flow
from sales.test_v2_fixture import V2OrderPlanFixture


class ShipmentReceiptInventoryTests(V2OrderPlanFixture):
    setUp = fixture_module.OrderPlanMaterialFlowTests.setUp
    _create_lot = fixture_module.OrderPlanMaterialFlowTests._create_lot
    _create_v2_plan = fixture_module.OrderPlanMaterialFlowTests._create_v2_plan
    _ready_production_shipment = fixture_module.OrderPlanMaterialFlowTests._ready_production_shipment
    _authorize_print_and_outbound = fixture_module.OrderPlanMaterialFlowTests._authorize_print_and_outbound
    _print = shipment_fixture.ShipmentWorkflowV2Tests._print
    _batch_logistics = shipment_fixture.ShipmentWorkflowV2Tests._batch_logistics
    _ship = shipment_fixture.ShipmentWorkflowV2Tests._ship
    _receipt = shipment_fixture.ShipmentWorkflowV2Tests._receipt

    def _inventory(self, user):
        dashboard = build_dashboard(get_statistics_identity(user))
        return next(section for section in dashboard["sections"] if section["domain"] == "FLOW")["metrics"]["inventory_device_count"]

    def _received_device(self, request_id):
        plan, _, shipment = self._ready_production_shipment(request_id=request_id, quantity=1)
        self._authorize_print_and_outbound(plan=plan, shipment=shipment)
        self._receipt(plan, shipment)
        shipment.refresh_from_db()
        return plan, shipment, shipment.device_allocations.select_related("device").get().device

    def test_12_then_38_receipts_refresh_all_merchant_roles_before_accounting(self):
        self._create_lot(12_000, suffix="STATS-TWELVE")
        self._create_lot(38_000, suffix="STATS-THIRTY-EIGHT")
        plan = self._create_v2_plan(request_id="stats-partial-12-38", quantity=50)
        subplans = list(OrderPlanMaterialPlan.objects.get(order_plan=plan).production_subplans.order_by("sequence_no"))
        self.assertEqual([row.planned_total_quantity for row in subplans], [12, 38])
        flow.warehouse_review_material_plan(user=self.material_keeper, plan_id=plan.pk)
        flow.release_material_ready_for_production(user=self.material_keeper, plan_id=plan.pk)
        shipments = []
        for subplan in subplans:
            flow.confirm_material_flow_production(user=self.factory_production, plan_id=plan.pk, subplan_id=subplan.pk)
            shipment = OrderPlanShipment.objects.get(order_plan=plan, production_subplan=subplan)
            self._print(plan, shipment)
            shipments.append(shipment)
        self._batch_logistics(plan, shipments)
        users = (self.merchant_owner, self.merchant_stock_1, self.merchant_sales)
        for user in users:
            self.assertEqual(self._inventory(user), 0)
        for shipment in shipments:
            self._ship(plan, shipment)
        self.assertEqual(self._inventory(self.merchant_owner), 0)
        # Physical receipt is handled by the owner or stock role; sales keeps
        # inventory visibility but no longer acts as the shared receiver.
        self._receipt(plan, shipments[0], self.merchant_stock_1)
        plan.refresh_from_db()
        self.assertIsNone(plan.received_at)
        self.assertIsNone(plan.accounted_at)
        for user in users:
            with self.subTest(role=user.organization_identity.role.code, received=12):
                self.assertEqual(self._inventory(user), 12)
        self.assertEqual(self._inventory(self.sales_user), 12)
        self.assertEqual(self._inventory(self.other_merchant_owner), 0)
        self._receipt(plan, shipments[1], self.merchant_stock_1)
        plan.refresh_from_db()
        self.assertIsNotNone(plan.received_at)
        self.assertIsNone(plan.accounted_at)
        self.assertFalse(Device.objects.filter(batch__batch_no=plan.items.get().order_batch_code).exists())
        for user in users:
            with self.subTest(role=user.organization_identity.role.code, received=50):
                self.assertEqual(self._inventory(user), 50)
        self.assertEqual(self._inventory(self.sales_user), 50)
        self.assertEqual(len(_market_rows(self.factory.pk)), 50)
        self._receipt(plan, shipments[0], self.merchant_owner)
        self.assertEqual(self._inventory(self.merchant_owner), 50)

    def test_legacy_received_order_without_shipment_retains_batch_lookup(self):
        plan = self._create_v2_plan(request_id="stats-legacy-batch", quantity=1)
        OrderPlan.objects.filter(pk=plan.pk).update(
            flow_version=OrderPlan.FlowVersion.MATERIAL_STOCK_V2,
            received_at=timezone.now(), status=OrderPlan.Status.RECEIVED,
        )
        batch = ProductionBatch.objects.create(
            batch_no=plan.items.get().order_batch_code, product_model=self.generator,
            bom=self.generator_bom, manufacturer=self.factory,
            production_date=timezone.localdate(), quantity=1,
        )
        device = Device.objects.create(sn="STATS-LEGACY-ONE", product_model=self.generator, batch=batch)
        OrganizationDevice.objects.create(device=device, organization=self.merchant, status="IN_STOCK")
        self.assertEqual(self._inventory(self.merchant_owner), 1)
        self.assertEqual(self._inventory(self.sales_user), 1)

    def _snapshot(self, plan, device, *, received_at, status=OrderPlanShipment.Status.RECEIVED):
        """Historical replay fixture; the receipt boundary is exercised above."""
        shipment = OrderPlanShipment.objects.create(
            order_plan=plan, sequence_no=1, client_request_id="stats-history",
            status=status, received_at=received_at,
        )
        item = OrderPlanShipmentItem.objects.create(shipment=shipment, order_plan_item=plan.items.get(), quantity=1)
        OrderPlanShipmentDevice.objects.create(shipment=shipment, shipment_item=item, order_plan=plan, device=device)
        return shipment

    def test_reissue_counts_device_once_under_latest_received_order(self):
        original, original_shipment, device = self._received_device("stats-original-reissue")
        assignment = MerchantChannelAssignment.objects.create(
            relation=self.other_relation, brand=self.brand, region="云南省", assigned_by=self.other_sales_user,
        )
        result, _ = flow.create_material_flow_order_plan(
            user=self.other_sales_user, merchant_id=self.other_merchant.pk,
            client_request_id="stats-reissue-other", items=[{
                "category_id": self.generator_category.pk, "product_model_id": self.generator.pk,
                "quantity": 1, "use_return_inventory": False, "source_bom_id": self.generator_bom.pk,
            }],
        )
        latest = OrderPlan.objects.get(pk=result["plan"]["id"])
        self.assertEqual(latest.channel_assignment_id, assignment.pk)
        latest_shipment = self._snapshot(latest, device, received_at=None, status=OrderPlanShipment.Status.SHIPPED)
        # An outstanding reissue cannot change the last received responsibility.
        self.assertEqual(_market_rows(self.factory.pk)[0]["plan_id"], original.pk)
        latest_shipment.status = OrderPlanShipment.Status.RECEIVED
        latest_shipment.received_at = original_shipment.received_at + timedelta(hours=1)
        latest_shipment.save(update_fields=("status", "received_at"))
        OrganizationDevice.objects.filter(device=device).update(organization=self.other_merchant, status="IN_STOCK")
        # A later inserted historical row is older in business time and must
        # not replace the actual latest receipt just because it has a larger ID.
        older = self._create_v2_plan(request_id="stats-reissue-older-import", quantity=1)
        self._snapshot(older, device, received_at=original_shipment.received_at - timedelta(hours=1))
        rows = _market_rows(self.factory.pk)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["device_id"], device.pk)
        self.assertEqual(rows[0]["plan_id"], latest.pk)
        self.assertEqual(self._inventory(self.merchant_owner), 0)
        self.assertEqual(self._inventory(self.other_merchant_owner), 1)
        self.assertEqual(self._inventory(self.sales_user), 0)
        self.assertEqual(self._inventory(self.other_sales_user), 1)
        self.assertEqual(self._inventory(self.factory_manager), 1)

    def test_current_owner_and_activation_determine_current_inventory(self):
        _, _, device = self._received_device("stats-current-device-state")
        OrganizationDevice.objects.filter(device=device).update(status="RETURNED_TO_FACTORY", organization=self.factory)
        self.assertEqual(self._inventory(self.merchant_owner), 0)
        OrganizationDevice.objects.filter(device=device).update(status="IN_STOCK", organization=self.merchant)
        self.assertEqual(self._inventory(self.merchant_owner), 1)
        ActivationRecord.objects.create(device=device, organization=self.merchant, status="ACTIVATED")
        statistics = build_flow_statistics(get_statistics_identity(self.merchant_owner))
        self.assertEqual(statistics["metrics"]["inventory_device_count"], 0)
        self.assertEqual(statistics["metrics"]["activated_device_count"], 1)
        self.assertEqual(statistics["market_status_totals"]["market_device_count"], 1)
