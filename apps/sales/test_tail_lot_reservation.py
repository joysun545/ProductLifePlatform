"""Tail splitting must realize its exact lot decision without changing normal lots.

Inventory is received through the real warehouse API. Existing reservations
are created by real orders, never fabricated by assigning reserved balances.
The SQLite race synchronizes request starts, before either writer is acquired.
"""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from django.apps import apps
from django.db import OperationalError, close_old_connections, connections
from django.db.models import F, Sum
from django.db.models.query import QuerySet
from django.test import Client, TestCase, TransactionTestCase

from product.models import BOMItem
from sales.models import (
    OrderPlan, OrderPlanMaterialLotAllocation, OrderPlanMaterialPlan,
    OrderPlanMaterialPlanningRun, OrderPlanMaterialRequirement,
    OrderPlanProductionSubplan,
)
from sales.services import order_plan_material_flow_service as flow
from sales.test_v2_fixture import V2OrderPlanFixture
from supply_chain.models import FactoryMaterialLot, FactoryMaterialStock


class TailLotFixture:
    _user_with_identity = V2OrderPlanFixture._user_with_identity
    _accepted_direct_relation = V2OrderPlanFixture._accepted_direct_relation
    _product_with_bom = V2OrderPlanFixture._product_with_bom
    _material_with_supplier = V2OrderPlanFixture._material_with_supplier

    def setUp(self):
        V2OrderPlanFixture.setUp(self)
        self.bom = self.generator.boms.get(version="active-v2")

    def receive(self, quantity, *, spec=None, label="stock"):
        response = self.client.post("/pldp/api/supply-chain/material-inventory/documents/", {
            "kind": "RECEIPT", "expected_factory_id": self.factory.pk,
            "request_id": str(uuid4()), "quantity": str(quantity),
            "supplier_id": self.supplier.pk,
            "material_spec_id": (spec or self.generator_material[1]).pk,
            "unit": "件", "supplier_batch_code": label,
        }, content_type="application/json", HTTP_AUTHORIZATION=self.material_stock_authorization)
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return FactoryMaterialLot.objects.get(batch_code=body["data"]["document"]["batch_code"])

    def item(self, quantity, *, product=None):
        product = product or self.generator
        return {"category_id": product.category_id, "product_model_id": product.pk,
                "quantity": quantity, "use_return_inventory": False,
                "source_bom_id": product.boms.get(version="active-v2").pk}

    def payload(self, request_id, quantity=20, *, items=None):
        return {"client_request_id": request_id, "merchant_id": self.merchant.pk,
                "items": items or [self.item(quantity)], "remark": "尾货批次预占回归"}

    def post_order(self, payload, *, client=None):
        return (client or self.client).post(
            "/pldp/api/sales/order-plans/material-flow/", payload,
            content_type="application/json", HTTP_AUTHORIZATION=self.authorization,
        )

    def create_order(self, request_id, quantity=20, *, items=None):
        response = self.post_order(self.payload(request_id, quantity, items=items))
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return OrderPlan.objects.get(pk=body["data"]["plan"]["id"])

    def subplans(self, plan):
        return list(OrderPlanMaterialPlan.objects.get(order_plan=plan).production_subplans.order_by("sequence_no"))

    def allocations(self, plan):
        return OrderPlanMaterialLotAllocation.objects.filter(
            requirement__production_subplan_item__production_subplan__material_plan__order_plan=plan,
        ).order_by("pk")

    def allocation_rows(self, subplan):
        return list(OrderPlanMaterialLotAllocation.objects.filter(
            requirement__production_subplan_item__production_subplan=subplan,
        ).order_by("requirement__material_spec_id").values_list(
            "requirement__material_spec_id", "material_lot_id", "reserved_milli",
        ))

    def old_95_then_new_20(self):
        old = self.receive(100, label="OLD-100")
        prior = self.create_order("prior-95", 95)
        before = list(self.allocations(prior).values())
        old.refresh_from_db()
        self.assertEqual((old.on_hand_milli, old.reserved_milli), (100_000, 95_000))
        new = self.receive(20, label="NEW-20")
        return old, new, prior, before

    def add_second_material(self, quantity=1):
        return BOMItem.objects.create(
            bom=self.bom, material=self.generator_extra_material[0],
            material_spec=self.generator_extra_material[1], supplier=self.supplier,
            quantity=quantity,
        )

    def snapshot(self):
        # Include frozen BOMs, finance, workflow and physical records: a failed
        # create must not leave an extra plan, subplan, task or inventory write.
        return {
            model._meta.label: list(model.objects.order_by("pk").values())
            for model in apps.get_models()
            if model._meta.app_label in {
                "sales", "product", "manufacturing", "workflow", "supply_chain", "lifecycle",
            }
        }


class TailLotReservationTests(TailLotFixture, TestCase):
    def test_existing_95_reservation_is_preserved_and_tail_5_uses_exact_old_lot(self):
        old, new, prior, prior_allocations = self.old_95_then_new_20()
        plan = self.create_order("tail-5-main-15")
        tail, main = self.subplans(plan)
        self.assertEqual([tail.planned_total_quantity, main.planned_total_quantity], [5, 15])
        spec = self.generator_material[1].pk
        self.assertEqual(self.allocation_rows(tail), [(spec, old.pk, 5_000)])
        self.assertEqual(self.allocation_rows(main), [(spec, new.pk, 15_000)])
        self.assertEqual(list(self.allocations(prior).values()), prior_allocations)
        old.refresh_from_db(); new.refresh_from_db()
        self.assertEqual((old.on_hand_milli, old.reserved_milli), (100_000, 100_000))
        self.assertEqual((new.on_hand_milli, new.reserved_milli), (20_000, 15_000))
        self.assertEqual(self.allocations(plan).aggregate(n=Sum("consumed_milli"))["n"], 0)
        run = OrderPlanMaterialPlanningRun.objects.get(material_plan__order_plan=plan)
        self.assertEqual(run.output_snapshot["reserved_milli"], 20_000)
        self.assertEqual(sum(row["reserved_milli"] for row in run.output_snapshot["requirements"]), 20_000)
        self.assertEqual(run.input_snapshot["subplan_count"], 2)
        # Idempotent submission cannot reserve a second set of lots or children.
        before = self.snapshot()
        self.assertEqual(self.create_order("tail-5-main-15").pk, plan.pk)
        self.assertEqual(self.snapshot(), before)

    def test_production_consumes_only_the_frozen_tail_and_main_allocations(self):
        old, new, prior, prior_allocations = self.old_95_then_new_20()
        plan = self.create_order("tail-consume")
        tail, main = self.subplans(plan)
        flow.warehouse_review_material_plan(user=self.factory_material_stock, plan_id=plan.pk)
        flow.release_material_ready_for_production(user=self.factory_material_stock, plan_id=plan.pk)
        flow.confirm_material_flow_production(user=self.factory_production, plan_id=plan.pk, subplan_id=tail.pk)
        old.refresh_from_db(); new.refresh_from_db()
        self.assertEqual((old.on_hand_milli, old.reserved_milli), (95_000, 95_000))
        self.assertEqual((new.on_hand_milli, new.reserved_milli), (20_000, 15_000))
        flow.confirm_material_flow_production(user=self.factory_production, plan_id=plan.pk, subplan_id=main.pk)
        old.refresh_from_db(); new.refresh_from_db()
        self.assertEqual((old.on_hand_milli, old.reserved_milli), (95_000, 95_000))
        self.assertEqual((new.on_hand_milli, new.reserved_milli), (5_000, 0))
        self.assertEqual(list(self.allocations(prior).values()), prior_allocations)
        self.assertEqual(self.allocations(plan).aggregate(n=Sum("consumed_milli"))["n"], 20_000)
        self.assertEqual(old.on_hand_milli + new.on_hand_milli + 20_000, 120_000)

    def test_unsplit_order_preserves_normal_on_hand_lot_order(self):
        old, new, prior, prior_allocations = self.old_95_then_new_20()
        plan = self.create_order("normal-four", 4)
        self.assertEqual([row.planned_total_quantity for row in self.subplans(plan)], [4])
        self.assertEqual(list(self.allocations(plan).values_list("material_lot_id", "reserved_milli")), [(new.pk, 4_000)])
        old.refresh_from_db()
        self.assertEqual(old.reserved_milli, 95_000)
        self.assertEqual(list(self.allocations(prior).values()), prior_allocations)

    def test_multimaterial_tail_requires_every_material_and_pins_each_selected_lot(self):
        self.add_second_material(2)
        primary_tail = self.receive(5, label="PRIMARY-5")
        self.receive(20, label="PRIMARY-MAIN")
        extra_tail = self.receive(10, spec=self.generator_extra_material[1], label="EXTRA-10")
        self.receive(40, spec=self.generator_extra_material[1], label="EXTRA-MAIN")
        plan = self.create_order("two-materials-complete")
        tail, main = self.subplans(plan)
        self.assertEqual([tail.planned_total_quantity, main.planned_total_quantity], [5, 15])
        self.assertEqual(self.allocation_rows(tail), [
            (self.generator_material[1].pk, primary_tail.pk, 5_000),
            (self.generator_extra_material[1].pk, extra_tail.pk, 10_000),
        ])
        for lot in (primary_tail, extra_tail):
            lot.refresh_from_db()
            self.assertEqual(lot.on_hand_milli - lot.reserved_milli, 0)
        run = OrderPlanMaterialPlanningRun.objects.get(material_plan__order_plan=plan)
        self.assertEqual(run.output_snapshot["reserved_milli"], 60_000)

    def test_missing_second_material_does_not_create_a_tail_child(self):
        self.add_second_material()
        self.receive(5, label="PRIMARY-TAIL")
        self.receive(20, label="PRIMARY-MAIN")
        self.receive(4, spec=self.generator_extra_material[1], label="EXTRA-ONLY-4")
        plan = self.create_order("incomplete-kit")
        self.assertEqual([row.planned_total_quantity for row in self.subplans(plan)], [20])
        self.assertFalse(self.allocations(plan).filter(requirement__material_spec=self.generator_extra_material[1]).exists())

    def test_two_partial_lots_cannot_be_joined_to_justify_a_tail_child(self):
        self.add_second_material()
        self.receive(5, label="PRIMARY-TAIL")
        self.receive(20, label="PRIMARY-MAIN")
        self.receive(3, spec=self.generator_extra_material[1], label="EXTRA-3")
        self.receive(2, spec=self.generator_extra_material[1], label="EXTRA-2")
        plan = self.create_order("no-pieced-tail")
        self.assertEqual([row.planned_total_quantity for row in self.subplans(plan)], [20])
        self.assertFalse(self.allocations(plan).filter(requirement__material_spec=self.generator_extra_material[1]).exists())

    def test_sufficient_partial_tails_without_any_complete_lot_clear_do_not_split(self):
        self.bom.items.update(quantity=2)
        self.add_second_material(3)
        self.receive(11, label="PRIMARY-11")
        self.receive(17, spec=self.generator_extra_material[1], label="EXTRA-17")
        plan = self.create_order("no-complete-tail-clear")
        self.assertEqual([row.planned_total_quantity for row in self.subplans(plan)], [20])
        self.assertFalse(self.allocations(plan).exists())

    def test_shared_material_next_model_decides_after_actual_prior_model_reservations(self):
        self.generator_material[0].product_categories.add(self.air_category)
        self.air_conditioner.boms.get(version="active-v2").items.update(
            material=self.generator_material[0], material_spec=self.generator_material[1],
        )
        old = self.receive(5, label="SHARED-5")
        new = self.receive(35, label="SHARED-35")
        plan = self.create_order("shared-two-models", items=[
            self.item(20, product=self.air_conditioner), self.item(20),
        ])
        tail, main, second = self.subplans(plan)
        self.assertEqual([row.planned_total_quantity for row in (tail, main, second)], [5, 15, 20])
        self.assertEqual([row.items.get().order_plan_item.product_model_id for row in (tail, main, second)],
                         [self.generator.pk, self.generator.pk, self.air_conditioner.pk])
        self.assertEqual(list(self.allocations(plan).values_list("material_lot_id", "reserved_milli")),
                         [(old.pk, 5_000), (new.pk, 15_000), (new.pk, 20_000)])
        self.assertEqual(OrderPlanMaterialPlanningRun.objects.get(material_plan__order_plan=plan).output_snapshot["reserved_milli"], 40_000)

    def test_fractional_bom_uses_integer_milli_requirements_for_exact_tail(self):
        self.bom.items.update(quantity=0.3334)
        old = self.receive("1.67", label="FRACTION-TAIL")
        new = self.receive(10, label="FRACTION-MAIN")
        plan = self.create_order("fractional-tail")
        tail, main = self.subplans(plan)
        self.assertEqual([tail.planned_total_quantity, main.planned_total_quantity], [5, 15])
        self.assertEqual(list(self.allocations(plan).values_list("material_lot_id", "reserved_milli")),
                         [(old.pk, 1_670), (new.pk, 5_010)])

    def test_second_material_conditional_failure_rolls_back_every_order_fact(self):
        self.add_second_material()
        primary = self.receive(5, label="FAIL-PRIMARY")
        self.receive(5, spec=self.generator_extra_material[1], label="FAIL-EXTRA")
        before = self.snapshot()
        original = QuerySet.update
        calls = []

        def conditional_failure(queryset, **kwargs):
            if queryset.model is FactoryMaterialLot and "reserved_milli" in kwargs:
                calls.append(FactoryMaterialLot.objects.get(pk=primary.pk).reserved_milli)
                if len(calls) == 2:
                    return 0
            return original(queryset, **kwargs)

        with patch.object(QuerySet, "update", conditional_failure):
            response = self.post_order(self.payload("fail-second-material"))
        self.assertEqual(response.json()["code"], 1, response.content)
        self.assertEqual(calls, [0, 5_000])  # first strict reservation really happened
        self.assertEqual(self.snapshot(), before)

    def test_selected_lot_may_not_silently_fallback_to_a_larger_lot(self):
        old, new, _, _ = self.old_95_then_new_20()
        before = self.snapshot()
        original = flow._reserve_requirement
        selected = []

        def lose_chosen_lot(requirement, **kwargs):
            if kwargs.get("selected_lot_id") is not None:
                selected.append(kwargs["selected_lot_id"])
                return 0
            return original(requirement, **kwargs)

        with patch.object(flow, "_reserve_requirement", lose_chosen_lot):
            response = self.post_order(self.payload("tail-no-fallback"))
        self.assertEqual(response.json()["code"], 1, response.content)
        self.assertEqual(selected, [old.pk])
        self.assertNotIn(new.pk, selected)
        self.assertEqual(self.snapshot(), before)

    def test_selected_tail_must_actually_clear_after_a_reservation_is_released(self):
        _, _, prior, _ = self.old_95_then_new_20()
        before = self.snapshot()
        candidate = flow._candidate_first_subplan_decision

        def release_prior_after_decision(rows):
            decision = candidate(rows)
            self.assertIsNotNone(decision)
            # Controlled interleaving through the real cancellation service:
            # sufficient stock alone no longer means the chosen tail clears.
            flow.cancel_material_flow_order_plan(user=self.sales_user, plan_id=prior.pk,
                                                 reason="选尾后原订单取消的交错验证")
            return decision

        with patch.object(flow, "_candidate_first_subplan_decision", release_prior_after_decision):
            response = self.post_order(self.payload("tail-must-clear"))
        self.assertEqual(response.json()["code"], 1, response.content)
        self.assertIn("未能完整清尾", response.json()["msg"])
        self.assertEqual(self.snapshot(), before)

    def test_stale_stock_revision_rejects_tail_and_rolls_back(self):
        old, _, _, _ = self.old_95_then_new_20()
        before = self.snapshot()
        reserve_tail = flow._reserve_tail_subplan

        def stale_revision(**kwargs):
            FactoryMaterialStock.objects.filter(pk=old.stock_id).update(revision=F("revision") + 1)
            return reserve_tail(**kwargs)

        with patch.object(flow, "_reserve_tail_subplan", stale_revision):
            response = self.post_order(self.payload("tail-stale-revision"))
        self.assertEqual(response.json()["code"], 1, response.content)
        self.assertEqual(self.snapshot(), before)

    def test_sqlite_locked_becomes_business_retry_after_complete_rollback(self):
        self.receive(5, label="LOCK-TAIL")
        self.receive(20, label="LOCK-MAIN")
        before = self.snapshot()
        original = flow._reserve_requirement

        def locked_after_reserving(requirement, **kwargs):
            original(requirement, **kwargs)
            raise OperationalError("database table is locked")

        with patch.object(flow, "_reserve_requirement", locked_after_reserving):
            response = self.post_order(self.payload("tail-locked-retry"))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["code"], 1, response.content)
        self.assertIn("同一表单", response.json()["msg"])
        self.assertEqual(self.snapshot(), before)

    def test_non_lock_database_error_is_not_hidden_as_stock_retry(self):
        with patch.object(flow, "_create_material_flow_order_plan", side_effect=OperationalError("disk I/O error")):
            with self.assertRaisesMessage(OperationalError, "disk I/O error"):
                flow.create_material_flow_order_plan(user=self.sales_user, merchant_id=self.merchant.pk,
                    client_request_id="non-lock-error", items=[self.item(20)])


class TailLotConcurrentReservationTests(TailLotFixture, TransactionTestCase):
    serialized_rollback = True

    def test_two_sqlite_requests_reserve_shared_tail_once_and_retry_without_orphans(self):
        old = self.receive(5, label="RACE-TAIL-5")
        new = self.receive(35, label="RACE-MAIN-35")
        barrier = Barrier(2)
        payloads = [self.payload("tail-race-a"), self.payload("tail-race-b")]

        def worker(payload):
            close_old_connections()
            database = connections["default"]
            database.ensure_connection()
            connection_id = id(database.connection)
            try:
                barrier.wait(timeout=15)
                response = self.post_order(payload, client=Client())
                return connection_id, response.status_code, response.json()
            finally:
                database.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker, payload) for payload in payloads]
            outcomes = [future.result(timeout=30) for future in futures]
        self.assertEqual(len({row[0] for row in outcomes}), 2)
        for _, status, body in outcomes:
            self.assertEqual(status, 200, body)
            self.assertIn(body["code"], (0, 1), body)
            if body["code"]:
                self.assertIn("重试", body["msg"])
        # Both may succeed if SQLite naturally serializes the entire requests;
        # the shared tail must still belong to exactly one resulting order.
        # A failed SQLite writer leaves no partial order; explicit same-form
        # retry must re-evaluate the now-current real available balances.
        ids = []
        for payload in payloads:
            response = self.post_order(payload)
            self.assertEqual(response.json()["code"], 0, response.content)
            ids.append(response.json()["data"]["plan"]["id"])
        self.assertEqual(len(set(ids)), 2)
        plans = OrderPlan.objects.filter(pk__in=ids)
        split_sizes = sorted([row.planned_total_quantity for row in self.subplans(plan)] for plan in plans)
        self.assertEqual(split_sizes, [[5, 15], [20]])
        self.assertEqual(OrderPlanProductionSubplan.objects.filter(material_plan__order_plan__in=plans).count(), 3)
        old.refresh_from_db(); new.refresh_from_db()
        self.assertEqual((old.on_hand_milli, old.reserved_milli), (5_000, 5_000))
        self.assertEqual((new.on_hand_milli, new.reserved_milli), (35_000, 35_000))
        self.assertEqual(OrderPlanMaterialRequirement.objects.aggregate(n=Sum("reserved_milli"))["n"], 40_000)
        self.assertEqual(OrderPlanMaterialLotAllocation.objects.aggregate(n=Sum("reserved_milli"))["n"], 40_000)
        print("C04_SQLITE_CONCURRENT", json.dumps({
            "initial_results": [{"status": status, "code": body["code"]} for _, status, body in outcomes],
            "distinct_connections": 2, "replayed_order_count": 2,
            "subplan_quantities": split_sizes, "reserved_milli": 40_000,
            "consumed_milli": 0, "orphan_subplans": 0,
        }))
