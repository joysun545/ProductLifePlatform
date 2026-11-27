"""Focused contract tests for the V2 lot-aware order material flow."""

from copy import deepcopy

from account.models import (
    Organization,
    OrganizationRelation,
    OrganizationRelationType,
    OrganizationRole,
    OrganizationType,
    SeniorManagerProfile,
    UserOrganization,
)
from account.services.auth_service import issue_access_token
from channel.models import MerchantChannelAssignment
from django.db.models import Sum
from django.test import TestCase
from django.utils import timezone
from lifecycle.models import Device, DeviceState, LifecycleEvent, OrganizationDevice
from manufacturing.models import ProductionBatch
from sales.models import (
    FactoryFinishedGoodsInventoryItem,
    OrderPlan,
    OrderPlanAccessoryCompatibility,
    OrderPlanCancellationAudit,
    OrderPlanFinance,
    OrderPlanMaterialLotAllocation,
    OrderPlanMaterialPlan,
    OrderPlanMaterialRequirement,
    OrderPlanProductionSubplan,
    OrderPlanReturnInventoryAllocation,
    OrderPlanSalesBOMSelection,
    OrderPlanShipment,
    OrderPlanShipmentDevice,
    OrderPlanShipmentItem,
    OrderPlanSupplyDelivery,
)
from sales.services.order_plan_reissue_service import get_return_inventory_options
from sales.services.order_plan_material_flow_service import (
    OrderPlanMaterialFlowError,
    authorize_material_flow_shipment,
    cancel_material_flow_order_plan,
    confirm_supplier_material_delivery,
    confirm_material_flow_accounting,
    confirm_material_flow_merchant_receipt,
    create_material_flow_order_plan,
    create_material_flow_shipment,
    dispatch_due_supply_deliveries,
    confirm_material_flow_production,
    confirm_material_flow_shipment_outbound,
    confirm_material_flow_shipment_logistics,
    confirm_material_flow_shipment_qrcode_print,
    get_material_flow_detail,
    prepare_material_flow_shipment_qrcodes,
    refresh_material_reservations_for_stock,
    release_material_ready_for_production,
    warehouse_review_material_plan,
)
from sales.services.order_plan_list_service import get_order_plan_list
from sales.test_v2_fixture import V2OrderPlanFixture
from product.models import BOM, BOMItem
from supply_chain.models import (
    FactoryMaterialLot,
    FactoryMaterialStock,
    MaterialSpec,
    MaterialSpecSupplier,
    MaterialWarehouseDocument,
    SupplierResponsibilityScope,
    SupplierSalesResponsibility,
)
from supply_chain.services.supplier_responsibility_service import (
    assign_supplier_sales_responsibilities,
)
from workflow.models import WorkItem
from workflow.services.task_query import get_task_list


class OrderPlanMaterialFlowTests(TestCase):
    """V2 material flow remains internally consistent at every handoff."""

    _user_with_identity = V2OrderPlanFixture._user_with_identity
    _accepted_direct_relation = V2OrderPlanFixture._accepted_direct_relation
    _product_with_bom = V2OrderPlanFixture._product_with_bom
    _material_with_supplier = V2OrderPlanFixture._material_with_supplier

    def setUp(self):
        V2OrderPlanFixture.setUp(self)
        self.factory_material_stock_role = OrganizationRole.objects.get(
            code="factory_material_stock"
        )
        self.material_keeper = self._user_with_identity(
            "order-material-keeper",
            "openid-order-material-keeper",
            self.factory,
            self.factory_material_stock_role,
        )
        self.generator_bom = self.generator.boms.get(version="active-v2")
        self.generator_bom_line = self.generator_bom.items.get(
            material=self.generator_material[0]
        )
        self.material_stock = FactoryMaterialStock.objects.create(
            factory=self.factory,
            supplier=self.supplier,
            material_spec=self.generator_material[1],
            unit=FactoryMaterialStock.Unit.PIECE,
        )

    def _create_lot(self, quantity_milli, *, suffix):
        return FactoryMaterialLot.objects.create(
            stock=self.material_stock,
            batch_code=f"V2-LOT-{suffix}",
            supplier_batch_code=f"SUP-V2-{suffix}",
            received_milli=quantity_milli,
            on_hand_milli=quantity_milli,
            received_by=self.material_keeper,
        )

    def _create_v2_plan(self, *, request_id, quantity=10):
        result, created = create_material_flow_order_plan(
            user=self.sales_user,
            merchant_id=self.merchant.pk,
            client_request_id=request_id,
            items=[
                {
                    "category_id": self.generator_category.pk,
                    "product_model_id": self.generator.pk,
                    "quantity": quantity,
                    "use_return_inventory": False,
                    "source_bom_id": self.generator_bom.pk,
                }
            ],
            remark="V2物料库存流程测试",
        )
        self.assertTrue(created)
        plan = OrderPlan.objects.get(pk=result["plan"]["id"])
        return plan

    def _second_merchant_for_same_salesperson(self):
        merchant = Organization.objects.create(
            code="V2-RETRY-MERCHANT", name="重试测试第二商家",
            organization_type=self.merchant.organization_type,
        )
        owner = self._user_with_identity(
            "v2-retry-second-owner", "v2-retry-second-owner-openid",
            merchant, self.merchant_owner_role,
        )
        relation = self._accepted_direct_relation(self.sales_user, owner, merchant)
        MerchantChannelAssignment.objects.create(
            relation=relation, brand=self.brand, region="重庆", assigned_by=self.sales_user,
        )
        return merchant

    def test_order_creation_retry_cannot_return_another_merchant_order(self):
        second = self._second_merchant_for_same_salesperson()
        payload = {
            "client_request_id": "v2-retry-merchant-guard",
            "merchant_id": self.merchant.pk,
            "items": [{
                "category_id": self.generator_category.pk,
                "product_model_id": self.generator.pk,
                "quantity": 2,
                "source_bom_id": self.generator_bom.pk,
            }],
        }
        url = "/pldp/api/sales/order-plans/material-flow/"
        auth = {"HTTP_AUTHORIZATION": self.authorization}
        response = self.client.post(url, payload, content_type="application/json", **auth)
        first = response.json()
        self.assertEqual(first["code"], 0, first)
        first_order_id = first["data"]["plan"]["id"]
        before = (OrderPlan.objects.count(), WorkItem.objects.count(), OrderPlanFinance.objects.count())

        response = self.client.post(
            url, {**payload, "merchant_id": second.pk}, content_type="application/json", **auth,
        )
        rejected = response.json()
        self.assertEqual(rejected["code"], 1, rejected)
        self.assertIn("已用于其他商家的订单", rejected["msg"])
        self.assertEqual(before, (OrderPlan.objects.count(), WorkItem.objects.count(), OrderPlanFinance.objects.count()))

        retry = self.client.post(url, payload, content_type="application/json", **auth).json()
        self.assertEqual(retry["code"], 0, retry)
        self.assertEqual(retry["data"]["plan"]["id"], first_order_id)
        self.assertEqual(before, (OrderPlan.objects.count(), WorkItem.objects.count(), OrderPlanFinance.objects.count()))

        fresh = self.client.post(
            url, {**payload, "merchant_id": second.pk, "client_request_id": "v2-fresh-second"},
            content_type="application/json", **auth,
        ).json()
        self.assertEqual(fresh["code"], 0, fresh)
        self.assertNotEqual(fresh["data"]["plan"]["id"], first_order_id)
        self.assertEqual(OrderPlan.objects.get(pk=fresh["data"]["plan"]["id"]).merchant_organization_id, second.pk)

    def test_order_creation_retry_requires_original_content_for_the_same_merchant(self):
        payload = {
            "client_request_id": "v2-retry-content-guard",
            "merchant_id": self.merchant.pk,
            "remark": "原订单备注",
            "items": [{
                "category_id": self.generator_category.pk,
                "product_model_id": self.generator.pk,
                "quantity": 10,
                "source_bom_id": self.generator_bom.pk,
                "use_return_inventory": False,
            }],
        }
        url = "/pldp/api/sales/order-plans/material-flow/"
        auth = {"HTTP_AUTHORIZATION": self.authorization}

        def submit(data):
            return self.client.post(url, data, content_type="application/json", **auth).json()

        first = submit(payload)
        self.assertEqual(first["code"], 0, first)
        plan = OrderPlan.objects.get(pk=first["data"]["plan"]["id"])
        watched_models = (
            OrderPlan, OrderPlanFinance, OrderPlanMaterialLotAllocation, WorkItem,
        )
        before = tuple(model.objects.count() for model in watched_models)

        def expect_original(data):
            replay = submit(data)
            self.assertEqual(replay["code"], 0, replay)
            self.assertEqual(replay["data"]["plan"]["id"], plan.pk)

        def expect_conflict(data):
            response = submit(data)
            self.assertEqual(response["code"], 1, response)
            self.assertIn("当前内容与原订单不同", response["msg"])
            self.assertEqual(tuple(model.objects.count() for model in watched_models), before)

        expect_original({**payload, "accessories": []})
        for field, value in (
            ("quantity", 12),
            ("product_model_id", self.air_conditioner.pk),
            ("source_bom_id", self.air_conditioner.boms.get(version="active-v2").pk),
            ("use_return_inventory", True),
            ("materials", [{"material_id": self.generator_material[0].pk, "deleted": True}]),
        ):
            with self.subTest(field=field):
                expect_conflict({**payload, "items": [{**payload["items"][0], field: value}]})
        expect_conflict({**payload, "remark": "已改备注"})
        expect_conflict({**payload, "accessories": [{
            "category_id": self.generator_category.pk,
            "product_model_id": self.generator.pk,
            "material_spec_id": self.generator_material[1].pk,
            "quantity": "1",
        }]})

        selection = OrderPlanSalesBOMSelection.objects.get(order_plan_item__plan=plan)
        self.assertTrue(selection.selection_snapshot.pop("creation_request_fingerprint"))
        selection.save(update_fields=["selection_snapshot"])
        expect_original(payload)
        expect_conflict({**payload, "items": [{**payload["items"][0], "quantity": 12}]})
        self.assertEqual(plan.items.get().quantity, 10)

        invalid = deepcopy(payload)
        invalid["client_request_id"] = "v2-retry-never-created"
        invalid["items"][0]["quantity"] = 0
        self.assertEqual(submit(invalid)["code"], 1)
        self.assertFalse(OrderPlan.objects.filter(client_request_id=invalid["client_request_id"]).exists())
        invalid["items"][0]["quantity"] = 12
        fresh = submit(invalid)
        self.assertEqual(fresh["code"], 0, fresh)
        self.assertEqual(OrderPlan.objects.get(pk=fresh["data"]["plan"]["id"]).items.get().quantity, 12)

    def _requirements(self, plan):
        return OrderPlanMaterialRequirement.objects.filter(
            production_subplan_item__production_subplan__material_plan__order_plan=plan
        )

    def _ready_production_shipment(self, *, request_id, quantity=10):
        """Create a fully reserved V2 plan and finish its first production slice.

        This intentionally uses the public V2 commands, rather than directly
        editing a subplan status.  The downstream assertions therefore prove
        that the material keeper → production manager handoff produces a
        V2-only shipment task without falling back into the legacy chain.
        """

        self._create_lot(quantity * 1_000, suffix=f"READY-{request_id}")
        plan = self._create_v2_plan(request_id=request_id, quantity=quantity)
        warehouse_review_material_plan(user=self.material_keeper, plan_id=plan.pk)
        release_material_ready_for_production(user=self.material_keeper, plan_id=plan.pk)
        material_plan = OrderPlanMaterialPlan.objects.get(order_plan=plan)
        subplan = material_plan.production_subplans.get(sequence_no=1)
        self.assertEqual(
            subplan.status,
            OrderPlanProductionSubplan.Status.PRODUCTION_PENDING,
        )
        confirm_material_flow_production(
            user=self.factory_production,
            plan_id=plan.pk,
            subplan_id=subplan.pk,
        )
        subplan.refresh_from_db()
        shipment = OrderPlanShipment.objects.get(
            order_plan=plan,
            production_subplan=subplan,
            client_request_id=f"system-production-subplan-{subplan.pk}",
        )
        return plan, subplan, shipment

    def _authorize_print_and_outbound(self, *, plan, shipment):
        """Advance exactly one V2 shipment, leaving any sibling batch untouched."""

        authorize_material_flow_shipment(
            user=self.sales_user,
            plan_id=plan.pk,
            shipment_id=shipment.pk,
        )
        confirm_material_flow_shipment_qrcode_print(
            user=self.factory_stock,
            plan_id=plan.pk,
            shipment_id=shipment.pk,
        )
        confirm_material_flow_shipment_logistics(
            user=self.factory_logistics_1,
            plan_id=plan.pk,
            shipment_id=shipment.pk,
            logistics_company="V2测试物流",
            tracking_no=f"V2-{shipment.pk}",
        )
        confirm_material_flow_shipment_outbound(
            user=self.factory_stock,
            plan_id=plan.pk,
            shipment_id=shipment.pk,
        )

    def _timeline_node(self, detail, code):
        for row in detail["timeline"]:
            if row["code"] == code:
                return row
        self.fail(f"未找到流程节点：{code}")

    def _assert_review_dispatched_deliveries(self, plan):
        """The deployed immediate mode dispatches during the first review.

        Verify real actionable supplier tasks already exist, then prove a
        later dispatcher run neither resends nor duplicates those tasks.
        """
        deliveries = list(OrderPlanSupplyDelivery.objects.filter(material_plan__order_plan=plan))
        self.assertTrue(deliveries)
        for delivery in deliveries:
            self.assertEqual(delivery.status, OrderPlanSupplyDelivery.Status.DISPATCHED)
            task = WorkItem.objects.get(
                uniq_key=f"sales:order-plan:{plan.pk}:material-delivery:{delivery.pk}"
            )
            self.assertEqual(task.status, "OPEN")
            self.assertTrue(task.user_tasks.filter(need_action=True).exclude(state="DONE").exists())
        before = set(WorkItem.objects.values_list("pk", flat=True))
        result = dispatch_due_supply_deliveries(force=True)
        self.assertEqual(result["dispatched"], 0)
        self.assertEqual(result["errors"], [])
        self.assertSetEqual(set(WorkItem.objects.values_list("pk", flat=True)), before)

    def test_creation_freezes_sales_bom_and_reserves_existing_lot_without_legacy_matching_task(self):
        # 10台 × 单台1件 = 10,000 thousandths.  A V2 creation must reserve it
        # synchronously and create the material-keeper task, never the old
        # factory-matching action task.
        lot = self._create_lot(10_000, suffix="CREATE")
        plan = self._create_v2_plan(request_id="v2-create-and-reserve")

        self.assertEqual(plan.flow_version, OrderPlan.FlowVersion.MATERIAL_STOCK_V2)
        selection = OrderPlanSalesBOMSelection.objects.get(
            order_plan_item__plan=plan
        )
        self.assertEqual(selection.selected_by, self.sales_user)
        self.assertEqual(selection.frozen_bom, self.generator_bom)
        self.assertFalse(selection.has_changes)
        self.assertEqual(plan.items.get().bom_confirmation.confirmed_by, self.sales_user)

        requirements = self._requirements(plan)
        self.assertEqual(requirements.aggregate(value=Sum("required_milli"))["value"], 10_000)
        self.assertEqual(requirements.aggregate(value=Sum("reserved_milli"))["value"], 10_000)
        allocations = OrderPlanMaterialLotAllocation.objects.filter(
            requirement__in=requirements,
            material_lot=lot,
            status=OrderPlanMaterialLotAllocation.Status.RESERVED,
        )
        self.assertEqual(allocations.aggregate(value=Sum("reserved_milli"))["value"], 10_000)
        lot.refresh_from_db()
        self.assertEqual(lot.reserved_milli, 10_000)

        self.assertTrue(
            WorkItem.objects.filter(
                biz_ref=f"sales.OrderPlan:{plan.pk}",
                type="ORDER_PLAN_MATERIAL_REVIEW",
                link__contains="/pages/sales/order_plan_detail/order_plan_detail",
            ).exists()
        )
        self.assertFalse(
            WorkItem.objects.filter(
                biz_ref=f"sales.OrderPlan:{plan.pk}",
                type="ORDER_PLAN_MATCHING",
            ).exists()
        )
        # Receivable preparation starts at order creation, independently of
        # the physical flow; final accounting still waits for every receipt.
        self.assertTrue(OrderPlanFinance.objects.filter(order_plan=plan).exists())
        self.assertTrue(
            WorkItem.objects.filter(
                biz_ref=f"sales.OrderPlan:{plan.pk}",
                type="ORDER_PLAN_FINANCE_STATEMENT",
            ).exists()
        )
        self.assertIsNotNone(
            get_material_flow_detail(user=self.sales_user, plan_id=plan.pk)["finance"]
        )

    def test_order_choices_and_create_exclude_supplier_from_another_root(self):
        other_factory = Organization.objects.create(
            code="ORDER-OTHER-ROOT", name="另一厂家", organization_type=self.factory.organization_type,
        )
        other_supplier = Organization.objects.create(
            code="ORDER-OTHER-ROOT-SUPPLIER", name="另一链路供应商",
            organization_type=self.supplier.organization_type,
        )
        OrganizationRelation.objects.create(
            source_organization=other_factory, target_organization=other_supplier,
            relation_type=OrganizationRelationType.SUPPLY,
        )
        material, spec = self.generator_material
        MaterialSpecSupplier.objects.create(material_spec=spec, supplier=other_supplier)
        foreign_bom = BOM.objects.create(
            product_model=self.generator, version="other-root", version_no=999, is_active=True,
        )
        BOMItem.objects.create(
            bom=foreign_bom, material=material, material_spec=spec, supplier=other_supplier, quantity=1,
        )
        auth = {"HTTP_AUTHORIZATION": self.authorization}
        url = V2OrderPlanFixture.list_url + "material-flow/"
        options = self.client.get(url + "options/", {"merchant_id": self.merchant.pk}, **auth).json()
        self.assertEqual(options["code"], 0, options)
        bom_options = options["data"]["bom_options_by_product_model"][str(self.generator.pk)]
        self.assertIn(self.generator_bom.pk, {row["id"] for row in bom_options})
        self.assertNotIn(foreign_bom.pk, {row["id"] for row in bom_options})

        item = {
            "category_id": self.generator_category.pk, "product_model_id": self.generator.pk,
            "source_bom_id": foreign_bom.pk, "quantity": 5, "use_return_inventory": False,
        }
        payload = {"merchant_id": self.merchant.pk, "client_request_id": "supply-root-boundary", "items": [item]}
        rejected = self.client.post(url, payload, content_type="application/json", **auth).json()
        self.assertEqual(rejected["code"], 1, rejected)
        self.assertIn("当前厂家链路", rejected["msg"])
        self.assertFalse(OrderPlan.objects.filter(client_request_id=payload["client_request_id"]).exists())

        item["source_bom_id"] = self.generator_bom.pk
        created = self.client.post(url, payload, content_type="application/json", **auth).json()
        self.assertEqual(created["code"], 0, created)
        plan_id = created["data"]["plan"]["id"]
        warehouse_review_material_plan(user=self.material_keeper, plan_id=plan_id)
        task = WorkItem.objects.get(biz_ref=f"sales.OrderPlan:{plan_id}", type="ORDER_PLAN_SUPPLIER_DELIVERY")
        self.assertEqual(task.payload["factory_id"], self.factory.pk)
        self.assertTrue(task.user_tasks.filter(to_openid=self.supplier_owner.wx_openid).exists())
        self.assertEqual(task.payload["supplier_id"], self.supplier.pk)

    def test_dispatch_rechecks_supplier_relation_after_order_creation(self):
        plan = self._create_v2_plan(request_id="supply-relation-revoked-before-dispatch")
        relation = OrganizationRelation.objects.get(source_organization=self.factory, target_organization=self.supplier)
        relation.is_active = False
        relation.save(update_fields=["is_active"])

        warehouse_review_material_plan(user=self.material_keeper, plan_id=plan.pk)
        delivery = OrderPlanSupplyDelivery.objects.get(material_plan__order_plan=plan)
        self.assertEqual(delivery.status, OrderPlanSupplyDelivery.Status.QUEUED)
        result = dispatch_due_supply_deliveries(force=True, delivery_ids=[delivery.pk])
        self.assertEqual(result["dispatched"], 0)
        self.assertIn("没有有效供货关系", result["errors"][0]["reason"])
        self.assertFalse(WorkItem.objects.filter(
            biz_ref=f"sales.OrderPlan:{plan.pk}", type="ORDER_PLAN_SUPPLIER_DELIVERY",
        ).exists())

        relation.is_active = True
        relation.save(update_fields=["is_active"])
        self.assertEqual(dispatch_due_supply_deliveries(force=True, delivery_ids=[delivery.pk])["dispatched"], 1)
        relation.is_active = False
        relation.save(update_fields=["is_active"])
        with self.assertRaisesMessage(OrderPlanMaterialFlowError, "有权参与的厂家链路"):
            confirm_supplier_material_delivery(user=self.supplier_owner, plan_id=plan.pk, delivery_id=delivery.pk)
        self.assertNotIn(plan.pk, {row["id"] for row in get_order_plan_list(self.supplier_owner)["items"]})
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, OrderPlanSupplyDelivery.Status.DISPATCHED)

    def test_timeline_lights_only_current_actor_or_awareness_recipient(self):
        """Global progress must never turn into every viewer's green dot."""

        plan = self._create_v2_plan(request_id="v2-timeline-ownership")

        sales_detail = get_material_flow_detail(user=self.sales_user, plan_id=plan.pk)
        sales_review = self._timeline_node(sales_detail, "MATERIAL_REVIEW")
        self.assertEqual(sales_review["state"], "CURRENT")
        self.assertFalse(sales_review["active"])

        keeper_detail = get_material_flow_detail(user=self.material_keeper, plan_id=plan.pk)
        keeper_review = self._timeline_node(keeper_detail, "MATERIAL_REVIEW")
        self.assertTrue(keeper_review["active"])
        self.assertTrue(
            any(
                action["code"] == "warehouse-review"
                for action in keeper_detail["available_actions"]
            )
        )

        # The matching role is deliberately awareness-only in V2.  Its
        # material-created notice may light the review node, but it receives
        # no review button.
        matching_detail = get_material_flow_detail(user=self.matching_user, plan_id=plan.pk)
        self.assertTrue(
            self._timeline_node(matching_detail, "MATERIAL_REVIEW")["active"]
        )
        self.assertFalse(
            any(
                action["code"] == "warehouse-review"
                for action in matching_detail["available_actions"]
            )
        )

        warehouse_review_material_plan(user=self.material_keeper, plan_id=plan.pk)
        self._assert_review_dispatched_deliveries(plan)
        supplier_detail = get_material_flow_detail(user=self.supplier_owner, plan_id=plan.pk)
        self.assertTrue(self._timeline_node(supplier_detail, "SUPPLY")["active"])
        self.assertTrue(
            any(
                action["code"] == "supplier-confirm"
                for action in supplier_detail["available_actions"]
            )
        )

    def test_order_delivery_receipt_updates_only_its_own_order(self):
        first = self._create_v2_plan(request_id="receipt-first", quantity=5)
        second = self._create_v2_plan(request_id="receipt-second", quantity=5)
        for plan in (first, second):
            warehouse_review_material_plan(user=self.material_keeper, plan_id=plan.pk)
            confirm_supplier_material_delivery(user=self.supplier_owner, plan_id=plan.pk)
        first_delivery = OrderPlanSupplyDelivery.objects.get(material_plan__order_plan=first)
        second_delivery = OrderPlanSupplyDelivery.objects.get(material_plan__order_plan=second)
        auth = {"HTTP_AUTHORIZATION": self.material_stock_authorization}
        first_url = V2OrderPlanFixture.list_url + f"{first.pk}/material-flow/order-delivery-receipt/"
        second_url = V2OrderPlanFixture.list_url + f"{second.pk}/material-flow/order-delivery-receipt/"

        result = self.client.post(second_url, {}, content_type="application/json", **auth).json()
        self.assertEqual(result["code"], 0, result)
        first_delivery.refresh_from_db()
        second_delivery.refresh_from_db()
        self.assertEqual(first_delivery.lines.get().received_milli, 0)
        self.assertEqual(first_delivery.status, OrderPlanSupplyDelivery.Status.SUPPLIER_CONFIRMED)
        self.assertEqual(second_delivery.lines.get().received_milli, 5_000)
        self.assertEqual(second_delivery.status, OrderPlanSupplyDelivery.Status.SATISFIED)
        self.assertTrue(self.client.get(first_url, **auth).json()["data"]["can_submit"])
        self.assertFalse(self.client.get(second_url, **auth).json()["data"]["can_submit"])

        # Shared stock still reserves oldest demand first. That reservation is
        # not a receipt for the first order or production stock for the second.
        lot = FactoryMaterialLot.objects.get(stock=self.material_stock)
        self.assertEqual(lot.received_milli, 5_000)
        self.assertEqual(lot.reserved_milli, 5_000)
        self.assertEqual(MaterialWarehouseDocument.objects.get(stock=self.material_stock).lot_id, lot.pk)
        allocation = OrderPlanMaterialLotAllocation.objects.get(material_lot=lot)
        self.assertEqual(allocation.requirement_id, self._requirements(first).get().pk)
        self.assertEqual(allocation.reserved_milli, lot.received_milli)
        self.assertEqual(self._requirements(second).get().reserved_milli, 0)
        with self.assertRaisesMessage(OrderPlanMaterialFlowError, "所需物料尚未全部到位"):
            release_material_ready_for_production(user=self.material_keeper, plan_id=second.pk)

        repeated = self.client.post(second_url, {}, content_type="application/json", **auth).json()
        self.assertNotEqual(repeated["code"], 0, repeated)
        self.assertEqual(FactoryMaterialLot.objects.filter(stock=self.material_stock).count(), 1)
        self.assertEqual(MaterialWarehouseDocument.objects.filter(stock=self.material_stock).count(), 1)

        result = self.client.post(first_url, {}, content_type="application/json", **auth).json()
        self.assertEqual(result["code"], 0, result)
        first_delivery.refresh_from_db()
        self.assertEqual(first_delivery.lines.get().received_milli, 5_000)
        self.assertEqual(first_delivery.status, OrderPlanSupplyDelivery.Status.SATISFIED)
        self.assertEqual(second_delivery.lines.get().received_milli, 5_000)
        self.assertEqual(FactoryMaterialLot.objects.filter(stock=self.material_stock).count(), 2)
        self.assertEqual(MaterialWarehouseDocument.objects.filter(stock=self.material_stock).count(), 2)
        self.assertEqual(self._requirements(second).get().reserved_milli, 5_000)

    def test_warehouse_dispatch_then_receipt_hook_reserves_without_advancing_and_ready_pushes_production(self):
        plan = self._create_v2_plan(request_id="v2-review-receipt-ready")
        material_plan = OrderPlanMaterialPlan.objects.get(order_plan=plan)
        self.assertEqual(
            material_plan.status,
            OrderPlanMaterialPlan.Status.WAITING_WAREHOUSE_REVIEW,
        )

        warehouse_review_material_plan(user=self.material_keeper, plan_id=plan.pk)
        material_plan.refresh_from_db()
        self.assertEqual(
            material_plan.status,
            OrderPlanMaterialPlan.Status.WAITING_SUPPLIER_DISPATCH,
        )
        delivery = OrderPlanSupplyDelivery.objects.get(material_plan=material_plan)
        self.assertEqual(delivery.status, OrderPlanSupplyDelivery.Status.DISPATCHED)
        self.assertEqual(delivery.lines.get().requested_milli, 10_000)

        # The independent receipt hook replenishes only the reservation.  It
        # must not mark a plan ready or silently issue a production task.
        self._create_lot(10_000, suffix="RECEIPT")
        result = refresh_material_reservations_for_stock(
            stock=self.material_stock,
            actor=self.material_keeper,
        )
        self.assertEqual(result["reserved_milli"], 10_000)
        material_plan.refresh_from_db()
        self.assertEqual(
            material_plan.status,
            OrderPlanMaterialPlan.Status.WAITING_SUPPLIER_DISPATCH,
        )
        requirements = self._requirements(plan)
        self.assertEqual(
            requirements.aggregate(value=Sum("reserved_milli"))["value"],
            requirements.aggregate(value=Sum("required_milli"))["value"],
        )
        self.assertFalse(
            WorkItem.objects.filter(
                biz_ref=f"sales.OrderPlan:{plan.pk}",
                type="ORDER_PLAN_V2_PRODUCTION_CONFIRM",
            ).exists()
        )
        detail_before_supplier_confirmation = get_material_flow_detail(
            user=self.material_keeper,
            plan_id=plan.pk,
        )
        self.assertFalse(
            any(
                action["code"] == "material-ready"
                for action in detail_before_supplier_confirmation["available_actions"]
            )
        )

        # Stock receipt alone cannot skip the committed supplier delivery
        # confirmation.  It may reserve the material, but the material keeper
        # must wait for the supplier-side task to close before releasing
        # production.
        with self.assertRaisesRegex(OrderPlanMaterialFlowError, "供应商配送尚未确认"):
            release_material_ready_for_production(
                user=self.material_keeper,
                plan_id=plan.pk,
            )
        self._assert_review_dispatched_deliveries(plan)
        confirm_supplier_material_delivery(
            user=self.supplier_owner,
            plan_id=plan.pk,
        )
        detail_after_supplier_confirmation = get_material_flow_detail(
            user=self.material_keeper,
            plan_id=plan.pk,
        )
        self.assertTrue(
            any(
                action["code"] == "material-ready"
                for action in detail_after_supplier_confirmation["available_actions"]
            )
        )

        release_material_ready_for_production(
            user=self.material_keeper,
            plan_id=plan.pk,
        )
        material_plan.refresh_from_db()
        self.assertEqual(
            material_plan.status,
            OrderPlanMaterialPlan.Status.READY_FOR_PRODUCTION,
        )
        subplan = material_plan.production_subplans.get(sequence_no=1)
        self.assertEqual(
            subplan.status,
            OrderPlanProductionSubplan.Status.PRODUCTION_PENDING,
        )
        self.assertTrue(
            WorkItem.objects.filter(
                biz_ref=f"sales.OrderPlan:{plan.pk}",
                type="ORDER_PLAN_V2_PRODUCTION_CONFIRM",
                payload__subplan_id=subplan.pk,
            ).exists()
        )

    def test_material_ready_requires_first_warehouse_review_even_when_all_stock_was_reserved_at_creation(self):
        """Creation-time auto-reservation is not a substitute for the review task."""

        self._create_lot(10_000, suffix="READY-REVIEW-GATE")
        plan = self._create_v2_plan(request_id="v2-ready-must-review")
        notice = WorkItem.objects.get(
            uniq_key=f"sales:order-plan:{plan.pk}:material-created:matching",
        )
        self.assertFalse(notice.user_tasks.get(to_openid=self.matching_user.wx_openid).need_action)
        material_plan = OrderPlanMaterialPlan.objects.get(order_plan=plan)
        self.assertFalse(material_plan.warehouse_reviewed_at)
        self.assertEqual(
            material_plan.status,
            OrderPlanMaterialPlan.Status.WAITING_WAREHOUSE_REVIEW,
        )
        self.assertTrue(
            all(
                requirement.reserved_milli == requirement.required_milli
                for requirement in self._requirements(plan)
            )
        )

        with self.assertRaisesRegex(OrderPlanMaterialFlowError, "先完成物料库管核对"):
            release_material_ready_for_production(
                user=self.material_keeper,
                plan_id=plan.pk,
            )

        warehouse_review_material_plan(user=self.material_keeper, plan_id=plan.pk)
        material_plan.refresh_from_db()
        self.assertEqual(
            material_plan.status,
            OrderPlanMaterialPlan.Status.WAITING_RECEIPT,
        )
        self.assertFalse(material_plan.supply_deliveries.exists())
        release_material_ready_for_production(
            user=self.material_keeper,
            plan_id=plan.pk,
        )
        material_plan.refresh_from_db()
        self.assertEqual(
            material_plan.status,
            OrderPlanMaterialPlan.Status.READY_FOR_PRODUCTION,
        )

    def test_repeated_warehouse_review_is_idempotent_and_does_not_replan_dispatched_delivery(self):
        """Only the receipt hook may react to stock arriving after first review."""

        plan = self._create_v2_plan(request_id="v2-review-idempotent")
        material_plan = OrderPlanMaterialPlan.objects.get(order_plan=plan)
        warehouse_review_material_plan(user=self.material_keeper, plan_id=plan.pk)
        material_plan.refresh_from_db()
        delivery = OrderPlanSupplyDelivery.objects.get(material_plan=material_plan)
        line = delivery.lines.get()
        first_reviewed_at = material_plan.warehouse_reviewed_at
        requested_milli = line.requested_milli
        queue_notice_count = WorkItem.objects.filter(
            biz_ref=f"sales.OrderPlan:{plan.pk}",
            type="ORDER_PLAN_SUPPLY_QUEUE_NOTICE",
        ).count()

        self._assert_review_dispatched_deliveries(plan)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, OrderPlanSupplyDelivery.Status.DISPATCHED)

        # This lot deliberately arrives outside the receipt hook.  A replay of
        # the review must not reserve it or rewrite the dispatched commitment.
        late_lot = self._create_lot(10_000, suffix="REVIEW-IDEMPOTENT-LATE")
        warehouse_review_material_plan(user=self.material_keeper, plan_id=plan.pk)

        material_plan.refresh_from_db()
        delivery.refresh_from_db()
        line.refresh_from_db()
        late_lot.refresh_from_db()
        self.assertEqual(material_plan.warehouse_reviewed_at, first_reviewed_at)
        self.assertEqual(delivery.status, OrderPlanSupplyDelivery.Status.DISPATCHED)
        self.assertEqual(line.requested_milli, requested_milli)
        self.assertEqual(late_lot.reserved_milli, 0)
        self.assertEqual(
            self._requirements(plan).aggregate(value=Sum("reserved_milli"))["value"],
            0,
        )
        self.assertEqual(
            WorkItem.objects.filter(
                biz_ref=f"sales.OrderPlan:{plan.pk}",
                type="ORDER_PLAN_SUPPLY_QUEUE_NOTICE",
            ).count(),
            queue_notice_count,
        )

    def test_sales_cancellation_releases_lot_reservation_before_production(self):
        lot = self._create_lot(10_000, suffix="CANCEL")
        plan = self._create_v2_plan(request_id="v2-cancel-release")
        requirements = self._requirements(plan)
        allocations = OrderPlanMaterialLotAllocation.objects.filter(
            requirement__in=requirements,
            material_lot=lot,
        )
        self.assertEqual(allocations.aggregate(value=Sum("reserved_milli"))["value"], 10_000)

        result = cancel_material_flow_order_plan(
            user=self.sales_user,
            plan_id=plan.pk,
            reason="客户取消，重新创建数量调整后的订单",
        )
        self.assertEqual(
            result["cancellation_status"],
            OrderPlanCancellationAudit.Status.CANCELLED,
        )
        plan.refresh_from_db()
        lot.refresh_from_db()
        allocations = list(allocations.order_by("pk"))
        self.assertEqual(plan.status, OrderPlan.Status.CANCELLED)
        self.assertEqual(lot.reserved_milli, 0)
        self.assertTrue(
            all(
                allocation.status == OrderPlanMaterialLotAllocation.Status.RELEASED
                for allocation in allocations
            )
        )
        self.assertEqual(requirements.aggregate(value=Sum("reserved_milli"))["value"], 0)
        self.assertTrue(
            OrderPlanCancellationAudit.objects.filter(
                order_plan=plan,
                requested_by=self.sales_user,
                released_reservations_at__isnull=False,
            ).exists()
        )

    def test_sales_can_cancel_after_material_ready_when_offline_assembly_did_not_start(self):
        """A released production task is not itself proof of assembly.

        The sales manager must explicitly submit the offline fact.  An explicit
        false result closes the pending production task and frees only the
        still-reserved material; no completed-production path is fabricated.
        """

        lot = self._create_lot(10_000, suffix="CANCEL-NO-ASSEMBLY")
        plan = self._create_v2_plan(request_id="v2-cancel-after-ready-false")
        warehouse_review_material_plan(user=self.material_keeper, plan_id=plan.pk)
        release_material_ready_for_production(user=self.material_keeper, plan_id=plan.pk)

        detail = get_material_flow_detail(user=self.sales_user, plan_id=plan.pk)
        action_payloads = {
            action["label"]: action.get("payload", {})
            for action in detail["available_actions"]
        }
        self.assertEqual(
            action_payloads["确认未开始装配并取消"],
            {"assembly_started_offline": False},
        )
        self.assertEqual(
            action_payloads["已开始装配，登记取消等待"],
            {"assembly_started_offline": True},
        )

        result = cancel_material_flow_order_plan(
            user=self.sales_user,
            plan_id=plan.pk,
            assembly_started_offline=False,
            reason="线下核实尚未开始装配",
        )
        self.assertEqual(result["cancellation_status"], OrderPlanCancellationAudit.Status.CANCELLED)
        plan.refresh_from_db()
        lot.refresh_from_db()
        self.assertEqual(plan.status, OrderPlan.Status.CANCELLED)
        self.assertEqual(lot.reserved_milli, 0)
        self.assertFalse(
            WorkItem.objects.filter(
                biz_ref=f"sales.OrderPlan:{plan.pk}",
                type="ORDER_PLAN_V2_PRODUCTION_CONFIRM",
                status="OPEN",
            ).exists()
        )

    def test_started_assembly_cancellation_blocks_stale_shipment_api_paths(self):
        """The cancellation waiting state is a hard shipment-path boundary."""

        self._create_lot(10_000, suffix="CANCEL-SHIPMENT-BLOCK")
        plan = self._create_v2_plan(request_id="v2-cancel-block-stale-shipment")
        warehouse_review_material_plan(user=self.material_keeper, plan_id=plan.pk)
        release_material_ready_for_production(user=self.material_keeper, plan_id=plan.pk)
        # Model a stale, non-physical planned batch that was visible just
        # before the offline assembly fact was recorded.  The cancellation
        # closes it; direct API retries must not revive it.
        shipment = OrderPlanShipment.objects.create(
            order_plan=plan,
            sequence_no=1,
            client_request_id="test-stale-planned-shipment",
        )
        OrderPlanShipmentItem.objects.create(
            shipment=shipment,
            order_plan_item=plan.items.get(),
            quantity=1,
        )
        waiting = cancel_material_flow_order_plan(
            user=self.sales_user,
            plan_id=plan.pk,
            assembly_started_offline=True,
            reason="客户取消，线下已开始装配",
        )
        self.assertEqual(
            waiting["cancellation_status"],
            OrderPlanCancellationAudit.Status.WAITING_PRODUCTION_COMPLETION,
        )
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, OrderPlanShipment.Status.CANCELLED)

        with self.assertRaisesRegex(OrderPlanMaterialFlowError, "订单已登记取消"):
            create_material_flow_shipment(
                user=self.sales_user,
                plan_id=plan.pk,
                client_request_id="test-cancelled-new-shipment",
                shipment_id=shipment.pk,
                items=[{"order_plan_item_id": plan.items.get().pk, "quantity": 1}],
            )
        with self.assertRaisesRegex(OrderPlanMaterialFlowError, "订单已登记取消"):
            authorize_material_flow_shipment(
                user=self.sales_user,
                plan_id=plan.pk,
                shipment_id=shipment.pk,
            )
        with self.assertRaisesRegex(OrderPlanMaterialFlowError, "订单已登记取消"):
            confirm_material_flow_shipment_qrcode_print(
                user=self.factory_stock,
                plan_id=plan.pk,
                shipment_id=shipment.pk,
            )
        self.assertEqual(
            get_material_flow_detail(user=self.sales_user, plan_id=plan.pk)["shipments"][0][
                "available_actions"
            ],
            [],
        )

    def test_cancellation_keeps_supplier_confirmed_delivery_as_normal_inventory_fact(self):
        """Cancellation must not rewrite an already committed delivery.

        Its eventual receipt is ordinary material stock, not a cancelled-order
        artifact.  Only queued/dispatched instructions are safe to withdraw.
        """

        plan = self._create_v2_plan(request_id="v2-cancel-keep-confirmed-delivery")
        warehouse_review_material_plan(user=self.material_keeper, plan_id=plan.pk)
        delivery = OrderPlanSupplyDelivery.objects.get(material_plan__order_plan=plan)
        delivery.status = OrderPlanSupplyDelivery.Status.SUPPLIER_CONFIRMED
        delivery.save(update_fields=("status", "updated_at"))

        cancel_material_flow_order_plan(user=self.sales_user, plan_id=plan.pk)
        delivery.refresh_from_db()
        self.assertEqual(
            delivery.status,
            OrderPlanSupplyDelivery.Status.SUPPLIER_CONFIRMED,
        )

    def test_started_assembly_cancel_waits_for_production_then_reuses_finished_goods(self):
        """Completed cancelled goods become first-priority reusable stock.

        This exercises the complete safe path: sales records an actual offline
        assembly start, production finishes without a shipment being created,
        then the sales manager finalizes cancellation and a later V2 plan
        reserves the newly-created neutral finished-inventory records.
        """

        lot = self._create_lot(10_000, suffix="CANCEL-AFTER-ASSEMBLY")
        plan = self._create_v2_plan(request_id="v2-cancel-after-assembly", quantity=10)
        warehouse_review_material_plan(user=self.material_keeper, plan_id=plan.pk)
        release_material_ready_for_production(user=self.material_keeper, plan_id=plan.pk)

        waiting = cancel_material_flow_order_plan(
            user=self.sales_user,
            plan_id=plan.pk,
            assembly_started_offline=True,
            reason="客户取消，线下已开始装配",
        )
        self.assertEqual(
            waiting["cancellation_status"],
            OrderPlanCancellationAudit.Status.WAITING_PRODUCTION_COMPLETION,
        )
        material_plan = OrderPlanMaterialPlan.objects.get(order_plan=plan)
        subplan = material_plan.production_subplans.get(sequence_no=1)
        confirm_material_flow_production(
            user=self.factory_production,
            plan_id=plan.pk,
            subplan_id=subplan.pk,
        )
        material_plan.refresh_from_db()
        audit = OrderPlanCancellationAudit.objects.get(order_plan=plan)
        self.assertEqual(
            material_plan.status,
            OrderPlanMaterialPlan.Status.PRODUCTION_COMPLETED,
        )
        self.assertEqual(
            audit.status,
            OrderPlanCancellationAudit.Status.WAITING_FINISHED_STOCK,
        )
        self.assertFalse(OrderPlanShipment.objects.filter(order_plan=plan).exists())

        detail = get_material_flow_detail(user=self.sales_user, plan_id=plan.pk)
        self.assertTrue(
            any(
                action["label"] == "确认生产完成并取消订单"
                and action.get("payload") == {"assembly_started_offline": True}
                for action in detail["available_actions"]
            )
        )
        final = cancel_material_flow_order_plan(
            user=self.sales_user,
            plan_id=plan.pk,
            assembly_started_offline=True,
        )
        self.assertEqual(final["cancellation_status"], OrderPlanCancellationAudit.Status.CANCELLED)
        self.assertEqual(final["finished_goods_created"], 10)
        plan.refresh_from_db()
        lot.refresh_from_db()
        self.assertEqual(plan.status, OrderPlan.Status.CANCELLED)
        self.assertEqual(lot.on_hand_milli, 0)
        self.assertEqual(lot.reserved_milli, 0)
        finished_items = FactoryFinishedGoodsInventoryItem.objects.filter(
            source_order_plan=plan,
            stock_status=FactoryFinishedGoodsInventoryItem.StockStatus.PENDING_REISSUE,
        )
        self.assertEqual(finished_items.count(), 10)
        self.assertEqual(
            get_return_inventory_options(
                factory_organization=self.factory,
                merchant_organization=self.merchant,
                product_model=self.generator,
            )["available_count"],
            10,
        )

        next_plan_result, created = create_material_flow_order_plan(
            user=self.sales_user,
            merchant_id=self.merchant.pk,
            client_request_id="v2-reuse-cancelled-finished-goods",
            items=[
                {
                    "category_id": self.generator_category.pk,
                    "product_model_id": self.generator.pk,
                    "quantity": 9,
                    "use_return_inventory": True,
                    "source_bom_id": self.generator_bom.pk,
                }
            ],
            remark="调用已取消订单的成品库存",
        )
        self.assertTrue(created)
        next_plan = OrderPlan.objects.get(pk=next_plan_result["plan"]["id"])
        reused_allocations = OrderPlanReturnInventoryAllocation.objects.filter(
            order_plan_item__plan=next_plan,
            finished_inventory_item__isnull=False,
        )
        self.assertEqual(reused_allocations.count(), 9)
        self.assertTrue(
            reused_allocations.filter(
                inventory_item__isnull=True,
                recall_inventory__isnull=True,
            ).exists()
        )
        self.assertEqual(
            FactoryFinishedGoodsInventoryItem.objects.filter(
                source_order_plan=plan,
                stock_status=FactoryFinishedGoodsInventoryItem.StockStatus.RESERVED_FOR_ORDER,
            ).count(),
            9,
        )
        self.assertFalse(WorkItem.objects.filter(
            uniq_key=f"sales:order-plan:{next_plan.pk}:material-created:matching",
        ).exists())

        spec = self.generator_material[1]
        self._create_lot(1_000, suffix="REUSED-WITH-ACCESSORY")
        OrderPlanAccessoryCompatibility.objects.create(
            factory_organization=self.factory, product_model=self.generator, material_spec=spec,
        )
        result, created = create_material_flow_order_plan(
            user=self.sales_user, merchant_id=self.merchant.pk,
            client_request_id="reuse-last-with-accessory",
            items=[{
                "category_id": self.generator_category.pk, "product_model_id": self.generator.pk,
                "quantity": 1, "use_return_inventory": True, "source_bom_id": self.generator_bom.pk,
            }],
            accessories=[{
                "category_id": self.generator_category.pk, "product_model_id": self.generator.pk,
                "material_spec_id": spec.pk, "quantity": "1",
            }],
        )
        self.assertTrue(created)
        accessory_plan = OrderPlan.objects.get(pk=result["plan"]["id"])
        self.assertFalse(accessory_plan.material_plan.production_subplans.exists())
        notice = WorkItem.objects.get(
            uniq_key=f"sales:order-plan:{accessory_plan.pk}:material-created:matching",
        )
        self.assertFalse(notice.user_tasks.get(to_openid=self.matching_user.wx_openid).need_action)

    def test_v2_subplan_shipment_lifecycle_uses_exact_devices_and_v2_tasks(self):
        """One completed subplan has its own QR/ship/receipt/accounting chain.

        This is the critical regression contract for the new path: generated
        devices are assigned to the V2 shipment before QR print; only those
        devices move at outbound and receipt; and no plan-wide legacy task is
        synthesized as a side effect.
        """

        plan, subplan, shipment = self._ready_production_shipment(
            request_id="v2-downstream-full",
            quantity=5,
        )
        subplan_item = subplan.items.get()
        self.assertEqual(shipment.status, OrderPlanShipment.Status.PLANNED)
        self.assertEqual(shipment.items.get().quantity, 5)
        self.assertTrue(
            WorkItem.objects.filter(
                type="ORDER_PLAN_V2_SHIPMENT_AUTHORIZATION",
                uniq_key=(
                    f"sales:order-plan:{plan.pk}:shipment:{shipment.pk}:authorization"
                ),
                user_tasks__to_openid=self.sales_user.wx_openid,
                user_tasks__need_action=True,
            ).exists()
        )

        authorize_material_flow_shipment(
            user=self.sales_user,
            plan_id=plan.pk,
            shipment_id=shipment.pk,
        )
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, OrderPlanShipment.Status.AUTHORIZED)
        self.assertEqual(shipment.shipment_authorized_by, self.sales_user)
        self.assertTrue(
            WorkItem.objects.filter(
                type="ORDER_PLAN_V2_QRCODE_PRINT",
                uniq_key=(
                    f"sales:order-plan:{plan.pk}:shipment:{shipment.pk}:qrcode-print"
                ),
                user_tasks__to_openid=self.factory_stock.wx_openid,
                user_tasks__need_action=True,
            ).exists()
        )

        confirm_material_flow_shipment_qrcode_print(
            user=self.factory_stock,
            plan_id=plan.pk,
            shipment_id=shipment.pk,
        )
        shipment.refresh_from_db()
        subplan.refresh_from_db()
        subplan_item.refresh_from_db()
        self.assertIsNotNone(shipment.qrcode_printed_at)
        self.assertEqual(
            subplan.status,
            OrderPlanProductionSubplan.Status.QRCODE_COMPLETED,
        )
        self.assertIsNotNone(subplan.qrcode_completed_at)
        self.assertIsNotNone(subplan_item.production_batch_id)
        self.assertIsNotNone(subplan_item.qrcode_generated_at)
        batch = ProductionBatch.objects.get(pk=subplan_item.production_batch_id)
        self.assertEqual(batch.batch_no, subplan_item.production_batch_code)
        self.assertEqual(batch.quantity, 5)
        mappings = list(
            OrderPlanShipmentDevice.objects.filter(shipment=shipment).order_by("id")
        )
        device_ids = {row.device_id for row in mappings}
        self.assertEqual(len(mappings), 5)
        self.assertEqual(len(device_ids), 5)
        self.assertEqual(
            Device.objects.filter(pk__in=device_ids, batch=batch, qrcode__isnull=False).count(),
            5,
        )
        self.assertEqual(
            OrderPlanShipmentDevice.objects.filter(
                shipment=shipment, qrcode_printed_at__isnull=False
            ).count(),
            5,
        )
        self.assertTrue(
            WorkItem.objects.filter(
                type="ORDER_PLAN_V2_SHIPMENT_LOGISTICS_CONFIRM",
                uniq_key=(
                    f"sales:order-plan:{plan.pk}:shipment:{shipment.pk}:logistics-confirm"
                ),
                user_tasks__to_openid=self.factory_logistics_1.wx_openid,
                user_tasks__need_action=True,
            ).exists()
        )
        with self.assertRaisesRegex(OrderPlanMaterialFlowError, "物流经理"):
            confirm_material_flow_shipment_outbound(
                user=self.factory_stock,
                plan_id=plan.pk,
                shipment_id=shipment.pk,
            )
        logistics_result, logistics_changed = confirm_material_flow_shipment_logistics(
            user=self.factory_logistics_1,
            plan_id=plan.pk,
            shipment_id=shipment.pk,
            logistics_company="测试承运公司",
            tracking_no="V2-LOGISTICS-005",
        )
        self.assertTrue(logistics_changed)
        self.assertEqual(logistics_result["shipment"]["logistics_company"], "测试承运公司")
        self.assertEqual(logistics_result["shipment"]["tracking_no"], "V2-LOGISTICS-005")
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, OrderPlanShipment.Status.OUTBOUNDED)
        self.assertEqual(shipment.logistics_company, "测试承运公司")
        self.assertEqual(shipment.tracking_no, "V2-LOGISTICS-005")
        self.assertIsNotNone(shipment.logistics_confirmed_at)
        self.assertEqual(shipment.logistics_confirmed_by, self.factory_logistics_1)
        self.assertTrue(
            WorkItem.objects.filter(
                type="ORDER_PLAN_V2_STOCK_OUTBOUND",
                uniq_key=(
                    f"sales:order-plan:{plan.pk}:shipment:{shipment.pk}:stock-outbound"
                ),
                user_tasks__to_openid=self.factory_stock.wx_openid,
                user_tasks__need_action=True,
            ).exists()
        )

        confirm_material_flow_shipment_outbound(
            user=self.factory_stock,
            plan_id=plan.pk,
            shipment_id=shipment.pk,
        )
        shipment.refresh_from_db()
        plan.refresh_from_db()
        subplan.refresh_from_db()
        self.assertEqual(shipment.status, OrderPlanShipment.Status.SHIPPED)
        self.assertEqual(plan.status, OrderPlan.Status.SHIPPED)
        self.assertEqual(subplan.status, OrderPlanProductionSubplan.Status.SHIPPED)
        self.assertEqual(
            DeviceState.objects.filter(
                device_id__in=device_ids,
                current_organization=self.factory,
                status="SHIPPED",
            ).count(),
            5,
        )
        self.assertEqual(
            OrganizationDevice.objects.filter(
                device_id__in=device_ids,
                organization=self.factory,
                status="TRANSFERRED",
            ).count(),
            5,
        )
        shipped_events = [
            event
            for event in LifecycleEvent.objects.filter(
                device_id__in=device_ids,
                event_type="SHIP",
            )
            if (event.data or {}).get("shipment_id") == shipment.pk
        ]
        self.assertEqual(len(shipped_events), 5)
        self.assertTrue(
            all(
                (event.data or {}).get("logistics_company") == "测试承运公司"
                and (event.data or {}).get("tracking_no") == "V2-LOGISTICS-005"
                for event in shipped_events
            )
        )
        self.assertTrue(
            WorkItem.objects.filter(
                type="ORDER_PLAN_V2_MERCHANT_RECEIPT",
                uniq_key=(
                    f"sales:order-plan:{plan.pk}:shipment:{shipment.pk}:merchant-receipt"
                ),
                user_tasks__to_openid=self.merchant_owner.wx_openid,
                user_tasks__need_action=True,
            ).exists()
        )

        confirm_material_flow_merchant_receipt(
            user=self.merchant_owner,
            plan_id=plan.pk,
            shipment_id=shipment.pk,
        )
        shipment.refresh_from_db()
        plan.refresh_from_db()
        subplan.refresh_from_db()
        self.assertEqual(shipment.status, OrderPlanShipment.Status.RECEIVED)
        self.assertEqual(plan.status, OrderPlan.Status.RECEIVED)
        self.assertEqual(subplan.status, OrderPlanProductionSubplan.Status.RECEIVED)
        self.assertEqual(
            DeviceState.objects.filter(
                device_id__in=device_ids,
                current_organization=self.merchant,
                status="IN_STOCK",
            ).count(),
            5,
        )
        self.assertEqual(
            OrganizationDevice.objects.filter(
                device_id__in=device_ids,
                organization=self.merchant,
                status="IN_STOCK",
            ).count(),
            5,
        )
        received_events = [
            event
            for event in LifecycleEvent.objects.filter(
                device_id__in=device_ids,
                event_type="RECEIVE",
            )
            if (event.data or {}).get("shipment_id") == shipment.pk
        ]
        self.assertEqual(len(received_events), 5)
        self.assertTrue(
            WorkItem.objects.filter(
                type="ORDER_PLAN_V2_ACCOUNTING_CONFIRM",
                uniq_key=f"sales:order-plan:{plan.pk}:accounting-confirm",
                user_tasks__to_openid=self.factory_assistant_1.wx_openid,
                user_tasks__need_action=True,
            ).exists()
        )

        result, changed = confirm_material_flow_accounting(
            user=self.factory_assistant_1,
            plan_id=plan.pk,
        )
        self.assertTrue(changed)
        self.assertEqual(result["accounted_by_name"], self.factory_assistant_1.wx_nickname)
        plan.refresh_from_db()
        self.assertEqual(plan.accounted_by, self.factory_assistant_1)
        self.assertIsNotNone(plan.accounted_at)

        # The audit firewall must hold even after the V2 plan reaches the
        # shape that used to trigger legacy whole-plan production/shipping.
        self.assertFalse(
            WorkItem.objects.filter(
                biz_ref=f"sales.OrderPlan:{plan.pk}",
                type__in={
                    "ORDER_PLAN_PRODUCTION_CONFIRM",
                    "ORDER_PLAN_SHIPMENT_AUTHORIZATION",
                    "ORDER_PLAN_STOCK_OUTBOUND",
                    "ORDER_PLAN_MERCHANT_RECEIPT",
                    "ORDER_PLAN_ACCOUNTING_CONFIRM",
                },
            ).exists()
        )

    def test_accounting_timeline_lights_only_sales_assistant_after_all_receipts(self):
        plan, _subplan, shipment = self._ready_production_shipment(
            request_id="v2-accounting-timeline",
            quantity=5,
        )
        self._authorize_print_and_outbound(plan=plan, shipment=shipment)
        confirm_material_flow_merchant_receipt(
            user=self.merchant_owner,
            plan_id=plan.pk,
            shipment_id=shipment.pk,
        )

        assistant_detail = get_material_flow_detail(
            user=self.factory_assistant_1,
            plan_id=plan.pk,
        )
        assistant_accounting = self._timeline_node(assistant_detail, "ACCOUNTING")
        self.assertEqual(assistant_accounting["state"], "CURRENT")
        self.assertTrue(assistant_accounting["active"])
        self.assertTrue(
            any(
                action["code"] == "accounting-confirm"
                for action in assistant_detail["available_actions"]
            )
        )

        # Sales receives the completion notice and may see every dynamic node,
        # but accounting is exclusively the sales-assistant task.
        sales_detail = get_material_flow_detail(user=self.sales_user, plan_id=plan.pk)
        self.assertFalse(self._timeline_node(sales_detail, "ACCOUNTING")["active"])
        self.assertFalse(
            any(
                action["code"] == "accounting-confirm"
                for action in sales_detail["available_actions"]
            )
        )

    def test_vacant_order_roles_can_complete_shared_task_handoffs(self):
        manager = self._user_with_identity(
            "vacant-order-factory-manager",
            "openid-vacant-order-factory-manager",
            self.factory,
            OrganizationRole.objects.get(code="factory_senior_manager"),
        )
        factory_roles = (
            self.factory_material_stock_role,
            self.factory_production_role,
            self.factory_stock_role,
            self.factory_logistics_role,
            self.factory_sales_assistant_role,
        )
        SeniorManagerProfile.objects.create(
            identity=manager.organization_identity,
        ).managed_roles.add(*factory_roles)
        UserOrganization.objects.filter(
            organization=self.factory, role__in=factory_roles,
        ).update(is_active=False)
        merchant_manager = self.merchant_senior_manager
        SeniorManagerProfile.objects.create(
            identity=merchant_manager.organization_identity,
        ).managed_roles.add(self.merchant_stock_role)
        UserOrganization.objects.filter(
            organization=self.merchant, role=self.merchant_stock_role,
        ).update(is_active=False)

        plan = self._create_v2_plan(request_id="vacant-role-order-handoffs", quantity=5)

        def assert_task_for(work_type, actor):
            work = WorkItem.objects.get(
                biz_ref=f"sales.OrderPlan:{plan.pk}", type=work_type,
            )
            self.assertEqual(work.status, "OPEN")
            self.assertEqual(
                list(work.user_tasks.filter(need_action=True).values_list("to_openid", flat=True)),
                [actor.wx_openid],
            )
            self.assertEqual(work.payload["factory_id"], self.factory.pk)

        assert_task_for("ORDER_PLAN_MATERIAL_REVIEW", manager)
        detail = get_material_flow_detail(user=manager, plan_id=plan.pk)
        self.assertIn("warehouse-review", {row["code"] for row in detail["available_actions"]})
        warehouse_review_material_plan(user=manager, plan_id=plan.pk)
        confirm_supplier_material_delivery(user=self.supplier_owner, plan_id=plan.pk)
        receipt_url = (
            f"/pldp/api/sales/order-plans/{plan.pk}/material-flow/order-delivery-receipt/"
        )
        auth = {"HTTP_AUTHORIZATION": f"Bearer {issue_access_token(manager)}"}
        form = self.client.get(receipt_url, **auth).json()
        self.assertEqual(form["code"], 0, form)
        self.assertTrue(form["data"]["can_submit"])
        receipt = self.client.post(receipt_url, {}, content_type="application/json", **auth).json()
        self.assertEqual(receipt["code"], 0, receipt)

        release_material_ready_for_production(user=manager, plan_id=plan.pk)
        assert_task_for("ORDER_PLAN_V2_PRODUCTION_CONFIRM", manager)
        material_plan = OrderPlanMaterialPlan.objects.get(order_plan=plan)
        self.assertEqual(material_plan.warehouse_reviewed_by_id, manager.pk)
        self.assertEqual(material_plan.material_ready_by_id, manager.pk)
        subplan = material_plan.production_subplans.get()
        confirm_material_flow_production(user=manager, plan_id=plan.pk, subplan_id=subplan.pk)
        subplan.refresh_from_db()
        self.assertEqual(subplan.production_completed_by_id, manager.pk)

        shipment = OrderPlanShipment.objects.get(order_plan=plan)
        assert_task_for("ORDER_PLAN_V2_SHIPMENT_AUTHORIZATION", self.sales_user)
        authorize_material_flow_shipment(user=self.sales_user, plan_id=plan.pk, shipment_id=shipment.pk)
        assert_task_for("ORDER_PLAN_V2_QRCODE_PRINT", manager)
        print_batch = prepare_material_flow_shipment_qrcodes(
            user=manager, plan_id=plan.pk, shipment_id=shipment.pk,
        )
        self.assertEqual(len(print_batch["qrcodes"]), 5)
        confirm_material_flow_shipment_qrcode_print(
            user=manager, plan_id=plan.pk, shipment_id=shipment.pk,
        )
        assert_task_for("ORDER_PLAN_V2_SHIPMENT_LOGISTICS_CONFIRM", manager)
        confirm_material_flow_shipment_logistics(
            user=manager, plan_id=plan.pk, shipment_id=shipment.pk,
            logistics_company="缺岗交接测试", tracking_no="VACANT-ROLE-ORDER",
        )
        assert_task_for("ORDER_PLAN_V2_STOCK_OUTBOUND", manager)
        confirm_material_flow_shipment_outbound(
            user=manager, plan_id=plan.pk, shipment_id=shipment.pk,
        )
        assert_task_for("ORDER_PLAN_V2_MERCHANT_RECEIPT", merchant_manager)
        confirm_material_flow_merchant_receipt(
            user=merchant_manager, plan_id=plan.pk, shipment_id=shipment.pk,
        )
        assert_task_for("ORDER_PLAN_V2_ACCOUNTING_CONFIRM", manager)
        confirm_material_flow_accounting(user=manager, plan_id=plan.pk)

        shipment.refresh_from_db()
        plan.refresh_from_db()
        self.assertEqual(shipment.logistics_confirmed_by_id, manager.pk)
        self.assertEqual(shipment.shipped_by_id, manager.pk)
        self.assertEqual(shipment.received_by_id, merchant_manager.pk)
        self.assertEqual(plan.accounted_by_id, manager.pk)

    def test_stock_manager_cannot_prepare_staffed_print_task_without_copy(self):
        manager = self._user_with_identity(
            "unassigned-stock-manager", "openid-unassigned-stock-manager", self.factory,
            OrganizationRole.objects.get(code="factory_senior_manager"),
        )
        SeniorManagerProfile.objects.create(
            identity=manager.organization_identity,
        ).managed_roles.add(self.factory_stock_role)
        plan, _subplan, shipment = self._ready_production_shipment(
            request_id="stock-manager-without-print-task", quantity=5,
        )
        authorize_material_flow_shipment(user=self.sales_user, plan_id=plan.pk, shipment_id=shipment.pk)
        work = WorkItem.objects.get(type="ORDER_PLAN_V2_QRCODE_PRINT")
        task = work.user_tasks.get(to_openid=manager.wx_openid)
        self.assertTrue(task.need_action)
        task.delete()
        with self.assertRaisesMessage(OrderPlanMaterialFlowError, "不是本批打码任务的处理人"):
            prepare_material_flow_shipment_qrcodes(user=manager, plan_id=plan.pk, shipment_id=shipment.pk)
        self.assertFalse(shipment.device_allocations.exists())
        prepared = prepare_material_flow_shipment_qrcodes(
            user=self.factory_stock, plan_id=plan.pk, shipment_id=shipment.pk,
        )
        self.assertEqual(len(prepared["qrcodes"]), 5)

    def test_v2_stock_senior_manager_receives_pending_receipt_after_allocation(self):
        plan, _subplan, shipment = self._ready_production_shipment(
            request_id="v2-senior-proactive-receipt",
            quantity=5,
        )
        self._authorize_print_and_outbound(plan=plan, shipment=shipment)

        profile = SeniorManagerProfile.objects.create(
            identity=self.merchant_senior_manager.organization_identity
        )
        profile.managed_roles.add(self.merchant_stock_role)

        senior_detail = get_material_flow_detail(
            user=self.merchant_senior_manager,
            plan_id=plan.pk,
        )
        self.assertTrue(
            self._timeline_node(senior_detail, "MERCHANT_RECEIPT")["active"]
        )
        self.assertTrue(
            any(
                action["code"] == "shipment-receipt"
                for row in senior_detail["shipments"]
                for action in row["available_actions"]
            )
        )
        owner_detail = get_material_flow_detail(
            user=self.merchant_owner,
            plan_id=plan.pk,
        )
        self.assertFalse(
            any(
                action["code"] == "shipment-receipt"
                for row in owner_detail["shipments"]
                for action in row["available_actions"]
            )
        )
        self.assertTrue(
            WorkItem.objects.filter(
                type="ORDER_PLAN_V2_MERCHANT_RECEIPT",
                user_tasks__to_openid=self.merchant_senior_manager.wx_openid,
            ).exists()
        )
        _result, changed = confirm_material_flow_merchant_receipt(
            user=self.merchant_senior_manager,
            plan_id=plan.pk,
            shipment_id=shipment.pk,
        )
        self.assertTrue(changed)
        shipment.refresh_from_db()
        self.assertEqual(shipment.received_by_id, self.merchant_senior_manager.pk)

    def test_v2_sales_can_split_pending_subplan_shipment_and_preserve_exact_device_sets(self):
        """A sales-side urgent split must not duplicate devices or ship the remainder.

        The initial system shipment is merely a pending default.  Before it is
        authorized, sales can make a 2+3 split.  The first receipt must not
        close the order or permit accounting; the second batch completes the
        same single plan without an extra order plan or legacy batch.
        """

        plan, subplan, first_shipment = self._ready_production_shipment(
            request_id="v2-downstream-split",
            quantity=5,
        )
        subplan_item = subplan.items.get()
        split_result, changed = create_material_flow_shipment(
            user=self.sales_user,
            plan_id=plan.pk,
            client_request_id="v2-downstream-split-2-and-3",
            production_subplan_id=subplan.pk,
            items=[
                {
                    "production_subplan_item_id": subplan_item.pk,
                    "quantity": 2,
                }
            ],
        )
        self.assertTrue(changed)
        self.assertTrue(split_result["changed"])
        first_shipment.refresh_from_db()
        second_shipment = OrderPlanShipment.objects.get(
            order_plan=plan,
            client_request_id="v2-downstream-split-2-and-3",
        )
        self.assertEqual(first_shipment.status, OrderPlanShipment.Status.PLANNED)
        self.assertEqual(second_shipment.status, OrderPlanShipment.Status.PLANNED)
        self.assertEqual(first_shipment.items.get().quantity, 2)
        self.assertEqual(second_shipment.items.get().quantity, 3)
        self.assertEqual(
            sum(
                OrderPlanShipment.objects.filter(order_plan=plan).values_list(
                    "items__quantity", flat=True
                )
            ),
            5,
        )

        self._authorize_print_and_outbound(plan=plan, shipment=first_shipment)
        first_shipment.refresh_from_db()
        plan.refresh_from_db()
        self.assertEqual(first_shipment.status, OrderPlanShipment.Status.SHIPPED)
        self.assertNotEqual(plan.status, OrderPlan.Status.SHIPPED)
        partial_detail = get_material_flow_detail(user=self.sales_user, plan_id=plan.pk)
        self.assertEqual(partial_detail["plan"]["status_name"], "分批履约中")
        first_devices = set(
            OrderPlanShipmentDevice.objects.filter(shipment=first_shipment).values_list(
                "device_id", flat=True
            )
        )
        self.assertEqual(len(first_devices), 2)

        confirm_material_flow_merchant_receipt(
            user=self.merchant_owner,
            plan_id=plan.pk,
            shipment_id=first_shipment.pk,
        )
        plan.refresh_from_db()
        self.assertNotEqual(plan.status, OrderPlan.Status.RECEIVED)
        with self.assertRaisesRegex(OrderPlanMaterialFlowError, "全部批次入库"):
            confirm_material_flow_accounting(
                user=self.factory_assistant_1,
                plan_id=plan.pk,
            )

        self._authorize_print_and_outbound(plan=plan, shipment=second_shipment)
        second_shipment.refresh_from_db()
        second_devices = set(
            OrderPlanShipmentDevice.objects.filter(shipment=second_shipment).values_list(
                "device_id", flat=True
            )
        )
        self.assertEqual(len(second_devices), 3)
        self.assertSetEqual(first_devices & second_devices, set())
        self.assertEqual(
            Device.objects.filter(pk__in=first_devices | second_devices).count(),
            5,
        )

        confirm_material_flow_merchant_receipt(
            user=self.merchant_owner,
            plan_id=plan.pk,
            shipment_id=second_shipment.pk,
        )
        plan.refresh_from_db()
        subplan.refresh_from_db()
        self.assertEqual(plan.status, OrderPlan.Status.RECEIVED)
        self.assertEqual(subplan.status, OrderPlanProductionSubplan.Status.RECEIVED)
        _result, changed = confirm_material_flow_accounting(
            user=self.factory_assistant_1,
            plan_id=plan.pk,
        )
        self.assertTrue(changed)
        plan.refresh_from_db()
        self.assertEqual(plan.accounted_by, self.factory_assistant_1)

    def test_v2_awareness_roles_get_v2_only_cards_and_new_status_names(self):
        """Leadership and matching can follow V2, but do not gain actions."""

        plan = self._create_v2_plan(request_id="v2-awareness-list")
        admin = self._user_with_identity(
            "order-factory-admin-awareness",
            "openid-order-factory-admin-awareness",
            self.factory,
            OrganizationRole.objects.get(code="factory_admin"),
        )

        for viewer in (
            admin,
            self.factory_manager,
            self.factory_chief_engineer,
        ):
            listing = get_order_plan_list(viewer)
            self.assertEqual(listing["visibility"], "factory_v2")
            self.assertEqual([row["id"] for row in listing["items"]], [plan.pk])
            card = listing["items"][0]
            self.assertTrue(card["is_material_flow"])
            self.assertEqual(card["material_flow_version"], 2)
            self.assertEqual(card["status_name"], "待物料库管核对")
            self.assertNotEqual(card["status_name"], "待物料配套")
            self.assertIn("order_plan_detail", card["detail_link"])
            self.assertNotIn("production_confirmation", card)
            self.assertNotIn("shipment_authorization", card)
            self.assertNotIn("supplier_confirmations", card)
            detail = get_material_flow_detail(user=viewer, plan_id=plan.pk)
            self.assertEqual(detail["available_actions"], [])

        matching_list = get_order_plan_list(self.matching_user)
        matching_v2_card = next(
            row for row in matching_list["items"] if row["id"] == plan.pk
        )
        self.assertTrue(matching_v2_card["is_material_flow"])
        self.assertEqual(matching_v2_card["status_name"], "待物料库管核对")
        self.assertNotIn("shipment_authorization", matching_v2_card)
        self.assertEqual(
            get_material_flow_detail(user=self.matching_user, plan_id=plan.pk)[
                "available_actions"
            ],
            [],
        )

    def test_v2_supplier_sales_sees_only_responsible_material_lines_and_all_sales_complete(self):
        """V2 scopes task, BOM, requirements, and delivery rows identically."""

        # The sensor material remains assigned to supplier_sales. Transfer
        # the controller's unique material responsibility to second_sales.
        BOMItem.objects.create(
            bom=self.generator_bom,
            material=self.generator_extra_material[0],
            material_spec=self.generator_extra_material[1],
            supplier=self.supplier,
            quantity=1,
            remark="V2供应商责任范围测试传感器",
        )
        second_sales = self._user_with_identity(
            "order-v2-supplier-sales-second",
            "openid-order-v2-supplier-sales-second",
            self.supplier,
            self.supplier_sales_role,
        )
        SupplierSalesResponsibility.objects.update_or_create(
            supplier=self.supplier,
            material=self.generator_material[0],
            defaults={
                "sales_identity": second_sales.organization_identity,
                "scope_type": SupplierResponsibilityScope.MATERIAL,
                "assigned_by": self.supplier_owner,
            },
        )

        plan = self._create_v2_plan(
            request_id="v2-supplier-responsibility-scope",
        )
        warehouse_review_material_plan(user=self.material_keeper, plan_id=plan.pk)
        self._assert_review_dispatched_deliveries(plan)
        delivery = OrderPlanSupplyDelivery.objects.get(
            material_plan__order_plan=plan,
            supplier=self.supplier,
        )
        workitem = WorkItem.objects.get(
            uniq_key=f"sales:order-plan:{plan.pk}:material-delivery:{delivery.pk}"
        )
        self.assertSetEqual(
            set(workitem.user_tasks.values_list("to_openid", flat=True)),
            {
                self.supplier_owner.wx_openid,
                self.supplier_sales.wx_openid,
                second_sales.wx_openid,
            },
        )

        first_task = workitem.user_tasks.get(
            to_openid=self.supplier_sales.wx_openid
        )
        second_task = workitem.user_tasks.get(to_openid=second_sales.wx_openid)
        self.assertIn("1项负责物料", first_task.display_subtitle_snapshot)
        self.assertIn("1项负责物料", second_task.display_subtitle_snapshot)
        first_inbox = next(
            row
            for row in get_task_list(self.supplier_sales.wx_openid)["items"]
            if row["entry_task_type"] == "ORDER_PLAN_SUPPLIER_DELIVERY"
        )
        self.assertTrue(first_inbox["collaboration"]["can_confirm"])
        self.assertEqual(first_inbox["collaboration"]["material_count"], 1)
        self.assertEqual(
            first_inbox["collaboration"]["scope_material_ids"],
            [self.generator_extra_material[0].pk],
        )
        supplier_card = next(
            row
            for row in get_order_plan_list(self.supplier_sales)["items"]
            if row["id"] == plan.pk
        )
        self.assertEqual(supplier_card["status_name"], "供应商配送确认中")
        self.assertNotIn("supplier_confirmation", supplier_card)

        def visible_material_ids(detail):
            return {
                row["material_id"]
                for selection in detail["bom_selections"]
                for row in selection["materials"]
            }

        first_detail = get_material_flow_detail(
            user=self.supplier_sales,
            plan_id=plan.pk,
        )
        second_detail = get_material_flow_detail(user=second_sales, plan_id=plan.pk)
        owner_detail = get_material_flow_detail(user=self.supplier_owner, plan_id=plan.pk)
        self.assertSetEqual(
            visible_material_ids(first_detail),
            {self.generator_extra_material[0].pk},
        )
        self.assertSetEqual(
            visible_material_ids(second_detail),
            {self.generator_material[0].pk},
        )
        self.assertSetEqual(
            visible_material_ids(owner_detail),
            {self.generator_material[0].pk, self.generator_extra_material[0].pk},
        )
        for detail, expected_ids in (
            (first_detail, {self.generator_extra_material[0].pk}),
            (second_detail, {self.generator_material[0].pk}),
        ):
            self.assertSetEqual(
                {row["material_id"] for row in detail["material_requirements"]},
                expected_ids,
            )
            self.assertSetEqual(
                {
                    line["material_id"]
                    for row in detail["supply_deliveries"]
                    for line in row["lines"]
                },
                expected_ids,
            )

        # The first responsible sales account is done but must not be blocked
        # by the owner task.  Completion happens when the second responsible
        # sales account finishes its own scoped line.
        confirm_supplier_material_delivery(
            user=self.supplier_sales,
            plan_id=plan.pk,
        )
        workitem.refresh_from_db()
        delivery.refresh_from_db()
        self.assertEqual(workitem.status, "OPEN")
        self.assertEqual(delivery.status, OrderPlanSupplyDelivery.Status.DISPATCHED)
        confirm_supplier_material_delivery(user=second_sales, plan_id=plan.pk)
        workitem.refresh_from_db()
        delivery.refresh_from_db()
        self.assertEqual(workitem.status, "DONE")
        self.assertEqual(
            delivery.status,
            OrderPlanSupplyDelivery.Status.SUPPLIER_CONFIRMED,
        )
        self.assertEqual(
            workitem.user_tasks.get(
                to_openid=self.supplier_owner.wx_openid
            ).done_reason,
            "OTHER_ACTION",
        )

    def test_v2_open_supplier_task_uses_immutable_snapshot_after_reassignment(self):
        """A reassignment cannot replace a dispatched V2 supplier task.

        The task recipient is the original sales manager.  After the supplier
        owner changes the live material responsibility to another manager, the
        original manager must retain the V2 list/detail/action through the
        task snapshot, while the new manager must not see or submit the open
        historical task.  The public responsibility service is used here so
        the regression also proves its legacy-only task synchronizer does not
        rewrite V2 ``recipient_scopes``.
        """

        second_sales = self._user_with_identity(
            "order-v2-supplier-sales-reassigned",
            "openid-order-v2-supplier-sales-reassigned",
            self.supplier,
            self.supplier_sales_role,
        )
        plan = self._create_v2_plan(
            request_id="v2-supplier-delivery-snapshot-reassignment",
        )
        warehouse_review_material_plan(user=self.material_keeper, plan_id=plan.pk)
        self._assert_review_dispatched_deliveries(plan)
        delivery = OrderPlanSupplyDelivery.objects.get(
            material_plan__order_plan=plan,
            supplier=self.supplier,
        )
        workitem = WorkItem.objects.get(
            uniq_key=f"sales:order-plan:{plan.pk}:material-delivery:{delivery.pk}"
        )
        original_scopes = deepcopy(workitem.payload["recipient_scopes"])
        original_task_recipients = set(
            workitem.user_tasks.values_list("to_openid", flat=True)
        )
        self.assertIn(self.supplier_sales.wx_openid, original_task_recipients)
        self.assertNotIn(second_sales.wx_openid, original_task_recipients)

        # Reassign the controller's material responsibility without touching
        # the historical V2 task recipient snapshot.
        assignment_result = assign_supplier_sales_responsibilities(
            user=self.supplier_owner,
            sales_identity_id=second_sales.organization_identity.pk,
            scope_type=SupplierResponsibilityScope.MATERIAL,
            target_ids=[self.generator_material[0].pk],
        )
        self.assertEqual(assignment_result["open_task_sync"]["workitems"], 0)
        workitem.refresh_from_db()
        self.assertEqual(workitem.status, "OPEN")
        self.assertEqual(workitem.payload["recipient_scopes"], original_scopes)
        self.assertSetEqual(
            set(workitem.user_tasks.values_list("to_openid", flat=True)),
            original_task_recipients,
        )

        original_listing = get_order_plan_list(self.supplier_sales)
        self.assertEqual(
            [row["id"] for row in original_listing["items"]], [plan.pk]
        )
        new_listing = get_order_plan_list(second_sales)
        self.assertNotIn(
            plan.pk,
            {row["id"] for row in new_listing["items"]},
        )

        original_detail = get_material_flow_detail(
            user=self.supplier_sales,
            plan_id=plan.pk,
        )
        self.assertSetEqual(
            {
                row["material_id"]
                for row in original_detail["material_requirements"]
            },
            {self.generator_material[0].pk},
        )
        supplier_actions = [
            action
            for action in original_detail["available_actions"]
            if action["code"] == "supplier-confirm"
        ]
        self.assertEqual(len(supplier_actions), 1)
        self.assertEqual(
            supplier_actions[0]["payload"],
            {"supplier_id": self.supplier.pk, "delivery_id": delivery.pk},
        )
        with self.assertRaisesRegex(OrderPlanMaterialFlowError, "无权查看"):
            get_material_flow_detail(user=second_sales, plan_id=plan.pk)
        with self.assertRaisesRegex(
            OrderPlanMaterialFlowError,
            "当前账号不是这项供应配送任务的处理人",
        ):
            confirm_supplier_material_delivery(
                user=second_sales,
                plan_id=plan.pk,
                supplier_id=self.supplier.pk,
                delivery_id=delivery.pk,
            )

        # The old recipient can still complete the task.  The service returns
        # detail after closing the work item, so this also protects the
        # completed task snapshot from disappearing mid-response.
        result = confirm_supplier_material_delivery(
            user=self.supplier_sales,
            plan_id=plan.pk,
            supplier_id=self.supplier.pk,
            delivery_id=delivery.pk,
        )
        self.assertEqual(result["plan"]["id"], plan.pk)
        workitem.refresh_from_db()
        delivery.refresh_from_db()
        self.assertEqual(workitem.status, "DONE")
        self.assertEqual(
            delivery.status,
            OrderPlanSupplyDelivery.Status.SUPPLIER_CONFIRMED,
        )

    def test_supplier_sales_never_sees_same_material_from_another_supplier(self):
        """Supplier scope is part of the requirement visibility key.

        Two product models may use the same material in different specs from
        different suppliers.  A sales manager responsible for the material at
        supplier A must not infer supplier B's specification, quantity, lot,
        or delivery just because the material id is shared.
        """

        supplier_type = OrganizationType.objects.get(code="SUPPLIER")
        other_supplier = Organization.objects.create(
            name="V2同物料第二供应商",
            code="V2-SAME-MATERIAL-SECOND-SUPPLIER",
            organization_type=supplier_type,
            region="重庆",
        )
        OrganizationRelation.objects.create(
            source_organization=self.factory,
            target_organization=other_supplier,
            relation_type=OrganizationRelationType.SUPPLY,
        )
        same_material = self.generator_material[0]
        other_spec = MaterialSpec.objects.create(
            material=same_material,
            code="V2-SAME-MATERIAL-SECOND-SPEC",
            name="第二供应商规格",
        )
        MaterialSpecSupplier.objects.create(
            material_spec=other_spec,
            supplier=other_supplier,
        )
        other_product = self._product_with_bom(
            "V2-SAME-MATERIAL-SECOND-PRODUCT",
            "V2同物料第二产品",
            self.brand,
            self.generator_category,
        )
        other_bom = other_product.boms.get(version="active-v2")
        BOMItem.objects.create(
            bom=other_bom,
            material=same_material,
            material_spec=other_spec,
            supplier=other_supplier,
            quantity=1,
        )

        result, created = create_material_flow_order_plan(
            user=self.sales_user,
            merchant_id=self.merchant.pk,
            client_request_id="v2-supplier-scope-by-supplier-and-material",
            items=[
                {
                    "category_id": self.generator_category.pk,
                    "product_model_id": self.generator.pk,
                    "quantity": 5,
                    "use_return_inventory": False,
                    "source_bom_id": self.generator_bom.pk,
                },
                {
                    "category_id": self.generator_category.pk,
                    "product_model_id": other_product.pk,
                    "quantity": 5,
                    "use_return_inventory": False,
                    "source_bom_id": other_bom.pk,
                },
            ],
            remark="V2供应商规格隔离测试",
        )
        self.assertTrue(created)
        plan = OrderPlan.objects.get(pk=result["plan"]["id"])
        warehouse_review_material_plan(user=self.material_keeper, plan_id=plan.pk)

        detail = get_material_flow_detail(user=self.supplier_sales, plan_id=plan.pk)
        self.assertEqual(
            {row["supplier_id"] for row in detail["material_requirements"]},
            {self.supplier.pk},
        )
        self.assertEqual(
            {row["material_spec_id"] for row in detail["material_requirements"]},
            {self.generator_material[1].pk},
        )
        self.assertEqual(
            {row["supplier_id"] for row in detail["supply_deliveries"]},
            {self.supplier.pk},
        )
        self.assertEqual(
            {
                line["material_spec_id"]
                for delivery in detail["supply_deliveries"]
                for line in delivery["lines"]
            },
            {self.generator_material[1].pk},
        )
