"""Role-aware names and scopes for product-flow dashboard metrics."""

from account.models import Organization, OrganizationRelation, OrganizationType
from data_statistics.services import get_statistics_identity
from data_statistics.services.flow_service import build_flow_statistics
from lifecycle.models import InstallationRecord, OrganizationDevice
from sales import test_order_plan_material_flow as fixture_module
from sales import test_shipment_workflow_v2 as shipment_fixture
from sales.models import OrderPlan
from sales.services import order_plan_material_flow_service as flow
from sales.test_v2_fixture import V2OrderPlanFixture


class InventoryMetricSemanticsTests(V2OrderPlanFixture):
    setUp = fixture_module.OrderPlanMaterialFlowTests.setUp
    _create_lot = fixture_module.OrderPlanMaterialFlowTests._create_lot
    _create_v2_plan = fixture_module.OrderPlanMaterialFlowTests._create_v2_plan
    _ready_production_shipment = fixture_module.OrderPlanMaterialFlowTests._ready_production_shipment
    _authorize_print_and_outbound = fixture_module.OrderPlanMaterialFlowTests._authorize_print_and_outbound
    _print = shipment_fixture.ShipmentWorkflowV2Tests._print
    _split_shipments = shipment_fixture.ShipmentWorkflowV2Tests._split_shipments
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
        self.assertTrue(data["responsibility_tree"]["has_subordinate_organizations"])
        self.assertFalse(
            data["responsibility_tree"]["children"][0]["children"][0][
                "has_subordinate_organizations"
            ]
        )

    def test_merchant_subordinate_status_does_not_depend_on_child_device_rows(self):
        self._received_device("scope-empty-subordinate")
        child = Organization.objects.create(
            name="暂无设备的下级商家",
            code="STATS-EMPTY-SUBORDINATE",
            organization_type=OrganizationType.objects.get(code="MERCHANT"),
        )
        OrganizationRelation.objects.create(
            source_organization=self.merchant,
            target_organization=child,
            relation_type="DISTRIBUTION",
        )

        data = build_flow_statistics(get_statistics_identity(self.merchant_owner))
        tree = data["responsibility_tree"]

        self.assertTrue(tree["has_subordinate_organizations"])
        self.assertEqual(tree["children"], [])

    def test_merchant_flow_tree_counts_installed_devices_across_the_chain(self):
        _, parent_device = self._received_device("scope-installed-parent")
        _, child_device = self._received_device("scope-installed-child")
        child = Organization.objects.create(
            name="安装统计下级商家",
            code="STATS-INSTALLED-CHILD",
            organization_type=OrganizationType.objects.get(code="MERCHANT"),
        )
        OrganizationRelation.objects.create(
            source_organization=self.merchant,
            target_organization=child,
            relation_type="DISTRIBUTION",
        )
        OrganizationDevice.objects.filter(device=child_device).update(
            organization=child, status="IN_STOCK",
        )
        InstallationRecord.objects.create(
            device=parent_device, organization=self.merchant,
        )
        InstallationRecord.objects.create(
            device=child_device, organization=child,
        )

        data = build_flow_statistics(get_statistics_identity(self.merchant_owner))
        tree = data["responsibility_tree"]
        child_node = tree["children"][0]["children"][0]

        self.assertEqual(tree["own"]["installed_device_count"], 1)
        self.assertEqual(tree["total"]["installed_device_count"], 2)
        self.assertEqual(child_node["total"]["installed_device_count"], 1)

    def test_factory_sales_uses_own_order_and_channel_scope(self):
        plan, _ = self._received_device("scope-sales")
        # Cancelled plan history must not inflate current order-plan devices.
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

        self.assertEqual(sales["metrics"]["order_plan_count"], 1)
        self.assertEqual(sales["metrics"]["order_plan_shipped_device_count"], 1)
        self.assertEqual(
            sales["metrics"]["order_plan_shipped_pending_accounting_device_count"],
            1,
        )
        self.assertEqual(sales["metrics"]["channel_inventory_device_count"], 1)
        self.assertNotIn("organization_inventory_device_count", sales["metrics"])
        self.assertNotIn("order_plan_count", manager["metrics"])
        self.assertEqual(manager["metrics"]["produced_device_count"], 1)
        self.assertEqual(manager["metrics"]["channel_inventory_device_count"], 1)

    def test_factory_sales_flow_statistics_follow_production_shipment_and_accounting(self):
        self._create_v2_plan(request_id="stats-stage-production-pending", quantity=3)

        ready_plan, _, _ = self._ready_production_shipment(
            request_id="stats-stage-ready", quantity=4,
        )

        outbound_plan, outbound_shipment, _ = self._split_shipments(
            request_id="stats-stage-outbounded", print_second=False,
        )
        self._batch_logistics(outbound_plan, [outbound_shipment])

        for request_id, quantity, should_receive, should_account in (
            ("stats-stage-shipped", 5, False, False),
            ("stats-stage-received", 6, True, False),
            ("stats-stage-accounted", 7, True, True),
        ):
            plan, _, shipment = self._ready_production_shipment(
                request_id=request_id, quantity=quantity,
            )
            self._authorize_print_and_outbound(plan=plan, shipment=shipment)
            self._ship(plan, shipment)
            if should_receive:
                self._receipt(plan, shipment)
            if should_account:
                flow.confirm_material_flow_accounting(
                    user=self.factory_assistant_1, plan_id=plan.pk,
                )
        OrderPlan.objects.create(
            plan_code="OP-STATS-STAGE-CANCELLED",
            client_request_id="stats-stage-cancelled",
            factory_organization=ready_plan.factory_organization,
            merchant_organization=ready_plan.merchant_organization,
            merchant_relation=ready_plan.merchant_relation,
            channel_assignment=ready_plan.channel_assignment,
            brand=ready_plan.brand,
            region_snapshot=ready_plan.region_snapshot,
            created_by=self.sales_user,
            total_quantity=11,
            status=OrderPlan.Status.CANCELLED,
        )

        data = build_flow_statistics(get_statistics_identity(self.sales_user))
        metrics = data["metrics"]
        self.assertEqual(metrics["order_plan_count"], 30)
        self.assertEqual(metrics["order_plan_pending_production_device_count"], 3)
        self.assertEqual(metrics["order_plan_produced_pending_outbound_device_count"], 7)
        self.assertEqual(metrics["order_plan_outbounded_pending_ship_device_count"], 2)
        self.assertEqual(metrics["order_plan_shipped_device_count"], 18)
        self.assertEqual(
            metrics["order_plan_shipped_pending_accounting_device_count"], 11,
        )
        self.assertEqual(metrics["order_plan_accounted_device_count"], 7)
