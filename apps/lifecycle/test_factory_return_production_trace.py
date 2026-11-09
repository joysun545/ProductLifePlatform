from lifecycle import test_goods_transfer as transfer_tests
from lifecycle.models import FactoryReturnInventoryItem, FactoryReturnRepairMaterialRequirement
from sales import test_order_plan_material_flow as material_flow_tests
from sales import test_v2_fixture as order_fixtures
from sales.models import OrderPlanMaterialPlan, OrderPlanShipment
from sales.services.order_plan_material_flow_service import (
    confirm_material_flow_merchant_receipt,
    confirm_material_flow_production,
    confirm_supplier_material_delivery,
    receive_all_order_delivery_materials,
    release_material_ready_for_production,
    warehouse_review_material_plan,
)
from supply_chain.models import SupplierResponsibilityScope
from supply_chain.services.supplier_responsibility_service import assign_supplier_sales_responsibilities
from workflow.models import WorkItem


class FactoryReturnProductionTraceTests(order_fixtures.V2OrderPlanFixture):
    setUp = material_flow_tests.OrderPlanMaterialFlowTests.setUp
    _create_v2_plan = material_flow_tests.OrderPlanMaterialFlowTests._create_v2_plan
    _authorize_print_and_outbound = material_flow_tests.OrderPlanMaterialFlowTests._authorize_print_and_outbound
    endpoint = transfer_tests.GoodsTransferApiTests.endpoint
    create_flow = transfer_tests.GoodsTransferApiTests.create_flow
    scan_and_submit = transfer_tests.GoodsTransferApiTests.scan_and_submit
    submit_transfer = transfer_tests.GoodsTransferApiTests.submit_transfer
    auth = transfer_tests.GoodsTransferApiTests.auth

    def test_return_repair_traces_original_shipment_material_and_delivery_recipients(self):
        plan = self._create_v2_plan(request_id="return-production-trace", quantity=5)
        warehouse_review_material_plan(user=self.material_keeper, plan_id=plan.pk)
        supply_task = WorkItem.objects.get(
            type="ORDER_PLAN_SUPPLIER_DELIVERY", biz_ref=f"sales.OrderPlan:{plan.pk}",
        )
        self.assertIn(self.supplier_sales.wx_openid, supply_task.payload["recipient_scopes"])
        confirm_supplier_material_delivery(user=self.supplier_owner, plan_id=plan.pk)
        receive_all_order_delivery_materials(user=self.material_keeper, plan_id=plan.pk)
        release_material_ready_for_production(user=self.material_keeper, plan_id=plan.pk)
        subplan = OrderPlanMaterialPlan.objects.get(order_plan=plan).production_subplans.get()
        confirm_material_flow_production(
            user=self.factory_production, plan_id=plan.pk, subplan_id=subplan.pk,
        )
        shipment = OrderPlanShipment.objects.get(order_plan=plan, production_subplan=subplan)
        self._authorize_print_and_outbound(plan=plan, shipment=shipment)
        confirm_material_flow_merchant_receipt(
            user=self.merchant_owner, plan_id=plan.pk, shipment_id=shipment.pk,
        )

        subplan_item = subplan.items.get()
        batch = subplan_item.production_batch
        order_item = subplan_item.order_plan_item
        material, _spec = self.generator_material
        bom_item = batch.bom.items.get(material=material)
        lot = subplan_item.material_requirements.get(bom_item=bom_item).lot_allocations.get(
            consumed_milli__gt=0,
        ).material_lot
        self.assertNotEqual(batch.batch_no, order_item.order_batch_code)
        new_sales = self._user_with_identity(
            "return-new-sales", "return-new-sales-openid", self.supplier, self.supplier_sales_role,
        )
        assign_supplier_sales_responsibilities(
            user=self.supplier_owner,
            sales_identity_id=new_sales.organization_identity.pk,
            scope_type=SupplierResponsibilityScope.MATERIAL,
            target_ids=[material.pk],
        )
        supply_task.refresh_from_db()
        self.assertNotIn(new_sales.wx_openid, supply_task.payload["recipient_scopes"])

        flow = self.create_flow(self.merchant_owner, {
            "flow_type": "RETURN", "client_request_id": "return-trace",
            "target_account_id": self.sales_user.pk,
        })
        device = batch.devices.order_by("pk").first()
        submitted = self.scan_and_submit(self.merchant_owner, flow["id"], device)
        self.assertEqual(submitted["status"], "PENDING_FACTORY_PICKUP")
        return_url = f"{self.endpoint}{flow['id']}/factory-return/"
        pickup = self.client.post(return_url + "pickup/", {}, **self.auth(self.factory_logistics_1)).json()
        self.assertEqual(pickup["code"], 0, pickup)
        posting = self.client.post(
            return_url + "posting/",
            {"items": [{
                "item_id": pickup["data"]["items"][0]["goods_transfer_item_id"],
                "stock_status": "PENDING_REPAIR",
            }]},
            content_type="application/json", **self.auth(self.factory_assistant_1),
        ).json()
        self.assertEqual(posting["code"], 0, posting)
        inventory = FactoryReturnInventoryItem.objects.get(pk=posting["data"]["items"][0]["id"])
        item_url = return_url + f"items/{inventory.pk}/"
        diagnosis = self.client.post(
            item_url + "production/",
            {
                "action": "REPAIR_DIAGNOSIS", "needs_replacement_material": True,
                "production_remark": "确认原配套物料需更换",
                "repair_materials": [{"bom_item_id": bom_item.pk, "note": "更换同规格物料"}],
            },
            content_type="application/json", **self.auth(self.factory_production),
        ).json()
        self.assertEqual(diagnosis["code"], 0, diagnosis)
        requirement = FactoryReturnRepairMaterialRequirement.objects.get(inventory_item=inventory)
        self.assertEqual(requirement.source_order_plan_id, plan.pk)
        self.assertEqual(requirement.source_order_plan_item_id, order_item.pk)
        self.assertEqual(requirement.source_production_batch_id, batch.pk)
        self.assertEqual(requirement.source_bom_item_id, bom_item.pk)
        self.assertEqual(requirement.trace_source, "PRODUCTION_BATCH")
        diagnosed = diagnosis["data"]["items"][0]
        self.assertEqual(diagnosed["source_order"]["shipment_id"], shipment.pk)
        self.assertEqual(diagnosed["repair_material_requirements"][0]["material_lots"][0]["factory_lot_id"], lot.pk)

        original_payload = supply_task.payload
        WorkItem.objects.filter(pk=supply_task.pk).update(
            payload={**original_payload, "recipient_scopes": None},
        )
        matching_payload = {
            "action": "REPAIR_MATERIAL_PLAN", "material_plan": "使用原规格物料",
            "matching_remark": "物料准备完成",
        }
        notices = WorkItem.objects.filter(
            type="FACTORY_RETURN_SUPPLIER_REPAIR_MATERIAL", payload__inventory_item_id=inventory.pk,
        )
        blocked = self.client.post(
            item_url + "matching/", matching_payload,
            content_type="application/json", **self.auth(self.matching_user),
        ).json()
        self.assertNotEqual(blocked["code"], 0, blocked)
        self.assertIn("配送任务或责任快照缺失", blocked["msg"])
        self.assertFalse(notices.exists())
        requirement.refresh_from_db()
        self.assertIsNone(requirement.notified_at)

        WorkItem.objects.filter(pk=supply_task.pk).update(payload=original_payload)
        matched = self.client.post(
            item_url + "matching/", matching_payload,
            content_type="application/json", **self.auth(self.matching_user),
        ).json()
        self.assertEqual(matched["code"], 0, matched)
        notice = notices.get()
        self.assertEqual(notice.payload["responsibility_source"], "ORDER_DELIVERY_TASK_SNAPSHOT")
        self.assertEqual(notice.payload["source_delivery_ids"], [original_payload["delivery_id"]])
        self.assertSetEqual(
            set(notice.user_tasks.values_list("to_openid", flat=True)),
            {self.supplier_owner.wx_openid, self.supplier_sales.wx_openid},
        )
        supplier_list_url = "/pldp/api/lifecycle/supplier-warranty-lists/"
        original_sales_list = self.client.get(supplier_list_url, **self.auth(self.supplier_sales)).json()
        new_sales_list = self.client.get(supplier_list_url, **self.auth(new_sales)).json()
        self.assertEqual(original_sales_list["code"], 0, original_sales_list)
        self.assertEqual(new_sales_list["code"], 0, new_sales_list)
        original_rows = original_sales_list["data"]["repair_material_requests"]
        self.assertEqual([row["id"] for row in original_rows], [requirement.pk])
        self.assertEqual(new_sales_list["data"]["repair_material_requests"], [])

        completed = self.client.post(
            item_url + "production/",
            {"action": "REPAIR_COMPLETE", "production_remark": "更换物料后通过检验"},
            content_type="application/json", **self.auth(self.factory_production),
        ).json()
        self.assertEqual(completed["code"], 0, completed)
        requirement.refresh_from_db()
        removed_part = requirement.returned_material_record
        self.assertEqual(removed_part.source_order_plan_id, plan.pk)
        self.assertEqual(removed_part.source_order_plan_item_id, order_item.pk)
        self.assertEqual(removed_part.material_batch_snapshot, lot.batch_code)
