from account.models import Organization
from account.services.auth_service import issue_access_token
from product.models import BOMItem
from sales import test_order_plan_material_flow as fixture_module
from sales.models import OrderPlanMaterialLotAllocation, OrderPlanShipment
from sales.services import order_plan_material_flow_service as flow
from sales.test_v2_fixture import V2OrderPlanFixture
from supply_chain.models import FactoryMaterialLot, FactoryMaterialStock


class MaterialConsumptionStatisticsTests(V2OrderPlanFixture):
    setUp = fixture_module.OrderPlanMaterialFlowTests.setUp
    _create_v2_plan = fixture_module.OrderPlanMaterialFlowTests._create_v2_plan

    def _statistics(self, user):
        response = self.client.get(
            "/pldp/api/data-statistics/material/",
            HTTP_AUTHORIZATION="Bearer " + issue_access_token(user),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]

    def _assert_counts(self, user, *, lots, usages, devices):
        result = self._statistics(user)
        self.assertEqual(result["metrics"], {
            "material_batch_count": lots,
            "material_usage_count": usages,
            "material_covered_device_count": devices,
            "material_fault_device_count": 0,
        })
        return result["charts"]

    def _receipt_lot(self, stock, code):
        return FactoryMaterialLot.objects.create(
            stock=stock, batch_code=code, supplier_batch_code="",
            received_milli=15_000, on_hand_milli=15_000,
            received_by=self.material_keeper,
        )

    def _reserve(self, *, request_id, quantity):
        plan = self._create_v2_plan(request_id=request_id, quantity=quantity)
        flow.warehouse_review_material_plan(user=self.material_keeper, plan_id=plan.pk)
        flow.release_material_ready_for_production(user=self.material_keeper, plan_id=plan.pk)
        return plan, plan.material_plan.production_subplans.get()

    def _print(self, plan, subplan):
        shipment = OrderPlanShipment.objects.get(order_plan=plan, production_subplan=subplan)
        flow.authorize_material_flow_shipment(
            user=self.sales_user, plan_id=plan.pk, shipment_id=shipment.pk,
        )
        flow.confirm_material_flow_shipment_qrcode_print(
            user=self.factory_stock, plan_id=plan.pk, shipment_id=shipment.pk,
        )

    def test_material_statistics_follow_actual_receipt_lots_and_generated_devices(self):
        second_material, second_spec = self._material_with_supplier(
            "STATS-SECOND-MATERIAL", "统计第二种物料", self.generator_category,
        )
        BOMItem.objects.create(
            bom=self.generator_bom, material=second_material, material_spec=second_spec,
            supplier=self.supplier, quantity=1,
        )
        second_stock = FactoryMaterialStock.objects.create(
            factory=self.factory, supplier=self.supplier, material_spec=second_spec,
            unit=FactoryMaterialStock.Unit.PIECE,
        )
        first_lot = self._receipt_lot(self.material_stock, "STATS-CONSUMED-FIRST")
        second_lot = self._receipt_lot(second_stock, "STATS-CONSUMED-SECOND")
        self._receipt_lot(second_stock, "STATS-UNUSED")

        first_plan, first_subplan = self._reserve(request_id="stats-consumption-first", quantity=5)
        self.assertEqual(OrderPlanMaterialLotAllocation.objects.filter(
            requirement__production_subplan_item__production_subplan=first_subplan,
            reserved_milli__gt=0, consumed_milli=0,
        ).count(), 2)
        self._assert_counts(self.material_keeper, lots=0, usages=0, devices=0)

        flow.confirm_material_flow_production(
            user=self.factory_production, plan_id=first_plan.pk, subplan_id=first_subplan.pk,
        )
        self._assert_counts(self.material_keeper, lots=2, usages=2, devices=0)

        self._print(first_plan, first_subplan)
        charts = self._assert_counts(self.material_keeper, lots=2, usages=2, devices=5)
        self.assertEqual(charts["supplier_distribution"], [{
            "supplier_id": self.supplier.pk, "supplier_name": self.supplier.name,
            "batch_count": 2, "device_count": 5,
        }])
        self.assertEqual({
            row["material_id"]: (row["batch_count"], row["device_count"])
            for row in charts["material_distribution"]
        }, {self.generator_material[0].pk: (1, 5), second_material.pk: (1, 5)})

        second_plan, second_subplan = self._reserve(request_id="stats-consumption-second", quantity=2)
        flow.confirm_material_flow_production(
            user=self.factory_production, plan_id=second_plan.pk, subplan_id=second_subplan.pk,
        )
        self._print(second_plan, second_subplan)
        self.assertCountEqual(
            OrderPlanMaterialLotAllocation.objects.filter(consumed_milli__gt=0)
            .values_list("material_lot_id", flat=True),
            [first_lot.pk, second_lot.pk, first_lot.pk, second_lot.pk],
        )
        self._assert_counts(self.material_keeper, lots=2, usages=4, devices=7)
        self._assert_counts(self.supplier_owner, lots=2, usages=4, devices=7)

        other_factory = Organization.objects.create(
            code="STATS-OTHER-FACTORY", name="统计其他厂家",
            organization_type=self.factory.organization_type,
        )
        other_keeper = self._user_with_identity(
            "stats-other-keeper", "openid-stats-other-keeper", other_factory,
            self.factory_material_stock_role,
        )
        other_supplier = Organization.objects.create(
            code="STATS-OTHER-SUPPLIER", name="统计同链路其他供应商",
            organization_type=self.supplier.organization_type, factory_root=self.factory,
        )
        other_supplier_owner = self._user_with_identity(
            "stats-other-supplier", "openid-stats-other-supplier", other_supplier,
            self.supplier_owner_role,
        )
        self._assert_counts(other_keeper, lots=0, usages=0, devices=0)
        self._assert_counts(other_supplier_owner, lots=0, usages=0, devices=0)
