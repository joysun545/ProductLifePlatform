from types import SimpleNamespace

from lifecycle.services.factory_return_service import (
    FactoryReturnProcessError,
    _resolve_material_trace,
)
from sales import test_order_plan_material_flow
from sales.models import OrderPlanShipment
from sales.services.order_plan_material_flow_service import (
    confirm_material_flow_production,
    release_material_ready_for_production,
    warehouse_review_material_plan,
)
from sales.test_v2_fixture import V2OrderPlanFixture


class FactoryReturnProductionTraceTests(V2OrderPlanFixture):
    setUp = test_order_plan_material_flow.OrderPlanMaterialFlowTests.setUp
    _create_lot = test_order_plan_material_flow.OrderPlanMaterialFlowTests._create_lot
    _create_v2_plan = test_order_plan_material_flow.OrderPlanMaterialFlowTests._create_v2_plan
    _authorize_print_and_outbound = (
        test_order_plan_material_flow.OrderPlanMaterialFlowTests._authorize_print_and_outbound
    )

    def test_trace_uses_consumed_lot_and_rejects_a_broken_production_link(self):
        consumed_lot = self._create_lot(5_000, suffix="RETURN-TRACE-CONSUMED")
        self._create_lot(8_000, suffix="RETURN-TRACE-UNUSED")
        plan = self._create_v2_plan(request_id="return-trace-consumed-lot", quantity=5)
        warehouse_review_material_plan(user=self.material_keeper, plan_id=plan.pk)
        release_material_ready_for_production(user=self.material_keeper, plan_id=plan.pk)
        subplan = plan.material_plan.production_subplans.get()
        confirm_material_flow_production(
            user=self.factory_production, plan_id=plan.pk, subplan_id=subplan.pk,
        )
        shipment = OrderPlanShipment.objects.get(order_plan=plan, production_subplan=subplan)
        self._authorize_print_and_outbound(plan=plan, shipment=shipment)
        production_item = subplan.items.get()
        source = SimpleNamespace(source_production_batch=production_item.production_batch)

        trace = _resolve_material_trace(source, self.generator_bom_line)

        self.assertEqual(trace["trace_source"], "PRODUCTION_BATCH")
        self.assertEqual(trace["supplier"], self.supplier)
        self.assertEqual(trace["material_batch"], consumed_lot.batch_code)
        self.assertEqual(
            trace["material_lots"],
            [{
                "factory_lot_id": consumed_lot.pk,
                "factory_batch_code": consumed_lot.batch_code,
                "supplier_material_batch_id": None,
                "supplier_batch_code": consumed_lot.supplier_batch_code,
                "consumed_milli": 5_000,
            }],
        )

        production_item.production_batch = None
        production_item.save(update_fields=("production_batch",))
        with self.assertRaisesMessage(FactoryReturnProcessError, "原生产批次与订单明细或冻结BOM不一致"):
            _resolve_material_trace(source, self.generator_bom_line)
