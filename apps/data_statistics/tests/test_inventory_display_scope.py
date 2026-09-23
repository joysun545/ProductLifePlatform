"""Role-aware names and scopes for product-flow dashboard metrics."""

from account.models import Organization, OrganizationRelation, OrganizationType
from data_statistics.services import get_statistics_identity
from data_statistics.services.flow_service import build_flow_statistics
from lifecycle.models import OrganizationDevice
from sales import test_order_plan_material_flow as fixture_module
from sales import test_shipment_workflow_v2 as shipment_fixture
from sales.models import OrderPlan
from sales.test_v2_fixture import V2OrderPlanFixture


class InventoryMetricSemanticsTests(V2OrderPlanFixture):
    setUp = fixture_module.OrderPlanMaterialFlowTests.setUp
    _create_lot = fixture_module.OrderPlanMaterialFlowTests._create_lot
    _create_v2_plan = fixture_module.OrderPlanMaterialFlowTests._create_v2_plan
    _ready_production_shipment = fixture_module.OrderPlanMaterialFlowTests._ready_production_shipment
    _authorize_print_and_outbound = fixture_module.OrderPlanMaterialFlowTests._authorize_print_and_outbound
    _print = shipment_fixture.ShipmentWorkflowV2Tests._print
    _batch_logistics = shipment_fixture.ShipmentWorkflowV2Tests._batch_logistics
    _ship = shipment_fixture.ShipmentWorkflowV2Tests._ship
    _receipt = shipment_fixture.ShipmentWorkflowV2Tests._receipt

    def _received_device(self, request_id):
        plan, _, shipment = self._ready_production_shipment(
            request_id=request_id, quantity=1,
        )
        self._authorize_print_and_outbound(plan=plan, shipment=shipment)
        self._receipt(plan, shipment)
        shipment.refresh_from_db()
        return (
            plan,
            shipment.device_allocations.select_related("device").get().device,
        )

    def test_merchant_exposes_received_chain_and_organization_inventory(self):
        _, device = self._received_device("scope-merchant")
        child = Organization.objects.create(
            name="统计下级商家",
            code="STATS-SCOPE-CHILD",
            organization_type=OrganizationType.objects.get(code="MERCHANT"),
        )
        OrganizationRelation.objects.create(
            source_organization=self.merchant,
            target_organization=child,
            relation_type="DISTRIBUTION",
        )
        OrganizationDevice.objects.filter(device=device).update(
            organization=child, status="IN_STOCK",
        )

        data = build_flow_statistics(get_statistics_identity(self.merchant_owner))
        metrics = data["metrics"]
        self.assertEqual(metrics["received_device_count"], 1)
        self.assertEqual(metrics["channel_inventory_device_count"], 1)
        self.assertEqual(metrics["organization_inventory_device_count"], 0)
        self.assertEqual(metrics["inventory_device_count"], 1)
        self.assertTrue(data["responsibility_tree"]["show_organization_inventory"])

    def test_factory_sales_uses_own_order_and_channel_scope(self):
        plan, _ = self._received_device("scope-sales")
        # The headline is a device quantity, not an order-document count.  All
        # plans created by this manager remain part of the historical total,
        # including a subsequently cancelled plan.
        OrderPlan.objects.create(
            plan_code="OP-SCOPE-SALES-CANCELLED",
            client_request_id="scope-sales-cancelled",
            factory_organization=plan.factory_organization,
            merchant_organization=plan.merchant_organization,
            merchant_relation=plan.merchant_relation,
            channel_assignment=plan.channel_assignment,
            brand=plan.brand,
            region_snapshot=plan.region_snapshot,
            created_by=self.sales_user,
            total_quantity=3,
            status=OrderPlan.Status.CANCELLED,
        )
        # A second sales manager has an order but no received devices.  It must
        # not change the first manager's order or channel figures.
        OrderPlan.objects.create(
            plan_code="OP-SCOPE-OTHER-SALES",
            client_request_id="scope-other-sales",
            factory_organization=plan.factory_organization,
            merchant_organization=plan.merchant_organization,
            merchant_relation=plan.merchant_relation,
            channel_assignment=plan.channel_assignment,
            brand=plan.brand,
            region_snapshot=plan.region_snapshot,
            created_by=self.other_sales_user,
            total_quantity=7,
        )

        sales = build_flow_statistics(get_statistics_identity(self.sales_user))
        manager = build_flow_statistics(get_statistics_identity(self.factory_manager))

        self.assertEqual(sales["metrics"]["order_plan_count"], 4)
        self.assertEqual(sales["metrics"]["channel_inventory_device_count"], 1)
        self.assertNotIn("organization_inventory_device_count", sales["metrics"])
        self.assertNotIn("order_plan_count", manager["metrics"])
        self.assertEqual(manager["metrics"]["produced_device_count"], 1)
        self.assertEqual(manager["metrics"]["channel_inventory_device_count"], 1)
