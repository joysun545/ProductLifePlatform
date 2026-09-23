"""Cross-order receivables, retained cash facts and independently actionable stock."""

import io
import json
import tempfile
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import OperationalError
from django.db.models import Sum
from django.test import TestCase
from django.utils import timezone
from PIL import Image

from account.models import Organization, OrganizationRelation, OrganizationType
from channel.models import MerchantChannelAssignment
from sales.models import (
    OrderPlan, OrderPlanCancellationAudit, OrderPlanFinance, OrderPlanMaterialPlan,
    OrderPlanPayment, OrderPlanPaymentAllocation, OrderPlanReceivableAccount,
)
from sales.private_storage import order_finance_private_storage
from sales.services.order_finance_service import (
    OrderFinanceError, cancel_order_finance_tasks, confirm_order_payment,
    initialize_order_finance, serialize_order_finance,
    submit_order_finance_statement, submit_order_payment,
)
from sales.services.order_plan_material_flow_service import (
    OrderPlanMaterialFlowError, cancel_material_flow_order_plan,
    confirm_material_flow_accounting, confirm_material_flow_merchant_receipt,
    confirm_material_flow_production, release_material_ready_for_production,
    warehouse_review_material_plan,
)
from sales import test_order_plan_material_flow as stock_fixture
from sales.test_v2_fixture import V2OrderPlanFixture
from workflow.models import UserTask, WorkItem
from workflow.services.task_action import open_task
from workflow.services.task_query import get_task_summary


class OrderRollingReceivablesTests(TestCase):
    # Reuse setup/helpers only; importing an existing TestCase class at module
    # scope or subclassing it would silently rerun unrelated test methods.
    _user_with_identity = V2OrderPlanFixture._user_with_identity
    _accepted_direct_relation = V2OrderPlanFixture._accepted_direct_relation
    _product_with_bom = V2OrderPlanFixture._product_with_bom
    _material_with_supplier = V2OrderPlanFixture._material_with_supplier
    _create_lot = stock_fixture.OrderPlanMaterialFlowTests._create_lot
    _create_v2_plan = stock_fixture.OrderPlanMaterialFlowTests._create_v2_plan
    _ready_production_shipment = stock_fixture.OrderPlanMaterialFlowTests._ready_production_shipment
    _authorize_print_and_outbound = stock_fixture.OrderPlanMaterialFlowTests._authorize_print_and_outbound

    def setUp(self):
        stock_fixture.OrderPlanMaterialFlowTests.setUp(self)
        private = tempfile.TemporaryDirectory(prefix="pldp-order-rolling-private-")
        self.addCleanup(private.cleanup)
        original = order_finance_private_storage._location
        order_finance_private_storage._location = private.name
        order_finance_private_storage.__dict__.pop("base_location", None)
        order_finance_private_storage.__dict__.pop("location", None)

        def restore():
            order_finance_private_storage._location = original
            order_finance_private_storage.__dict__.pop("base_location", None)
            order_finance_private_storage.__dict__.pop("location", None)
        self.addCleanup(restore)
        self.counter = 0
        self.start_time = timezone.now() - timedelta(days=1)

    def image(self, color="white"):
        buffer = io.BytesIO()
        Image.new("RGB", (16, 16), color).save(buffer, format="PNG")
        return SimpleUploadedFile("receipt.png", buffer.getvalue(), content_type="image/png")

    def order(self, amount=None):
        self.counter += 1
        plan = self._create_v2_plan(request_id=f"rolling-order-{self.counter}", quantity=5)
        plan.submitted_at = self.start_time + timedelta(minutes=self.counter)
        plan.save(update_fields=["submitted_at"])
        if amount is not None:
            self.statement(plan, amount)
        return plan

    def statement(self, plan, amount):
        return submit_order_finance_statement(
            user=self.factory_assistant_1, plan_id=plan.pk,
            receivable_amount=amount, statement_file=self.image())

    def upload(self, plan, request_id=None, *, user=None, color="white"):
        submit_order_payment(user=user or self.merchant_owner, plan_id=plan.pk,
            client_request_id=request_id or f"proof-{plan.pk}", voucher_file=self.image(color))
        return OrderPlanPayment.objects.get(client_request_id=request_id or f"proof-{plan.pk}")

    def confirm(self, plan, payment, amount, user=None):
        return confirm_order_payment(user=user or self.factory_assistant_1,
            plan_id=plan.pk, payment_id=payment.pk, confirmed_amount=amount)

    def details(self, plan, user=None):
        plan.refresh_from_db()
        return serialize_order_finance(user=user or self.merchant_owner, plan=plan)

    def rolling(self, plan):
        return self.details(plan)["rolling_receivables"]

    def finance_tasks(self, plan=None, status="OPEN"):
        qs = WorkItem.objects.filter(type__in=("ORDER_PLAN_PAYMENT_UPLOAD", "ORDER_PLAN_PAYMENT_CONFIRM"))
        if plan is not None:
            qs = qs.filter(biz_ref=f"sales.OrderPlan:{plan.pk}")
        if status is not None:
            qs = qs.filter(status=status)
        return qs

    def active_allocations(self, payment):
        return dict(OrderPlanPaymentAllocation.objects.filter(payment=payment, released_at__isnull=True)
                    .values_list("finance__order_plan_id", "amount"))

    def all_snapshot(self):
        return {
            "payments": list(OrderPlanPayment.objects.order_by("pk").values()),
            "allocations": list(OrderPlanPaymentAllocation.objects.order_by("pk").values()),
            "works": list(WorkItem.objects.order_by("pk").values()),
            "tasks": list(UserTask.objects.order_by("pk").values()),
        }

    def test_partial_balance_moves_to_latest_task_and_new_cash_pays_oldest_invoice_first(self):
        first = self.order("1000")
        paid = self.upload(first)
        self.confirm(first, paid, "400")
        second = self.order("2000")
        self.assertEqual(self.rolling(second)["outstanding_amount"], "2600.00")
        self.assertEqual(self.rolling(second)["carry_forward_amount"], "600.00")
        self.assertFalse(self.finance_tasks(first).exists())
        self.assertEqual(self.finance_tasks(second).filter(type="ORDER_PLAN_PAYMENT_UPLOAD").count(), 1)
        self.assertFalse(self.details(first)["can_upload_payment"])
        receipt = self.upload(second)
        result = self.confirm(second, receipt, "700", self.factory_assistant_2)
        self.assertEqual(result["rolling_receivables"]["outstanding_amount"], "1900.00")
        self.assertEqual(self.active_allocations(receipt), {first.pk: Decimal("600"), second.pk: Decimal("100")})
        receipt.refresh_from_db()
        self.assertEqual(receipt.finance.order_plan_id, second.pk)
        self.assertEqual(receipt.confirmed_amount, Decimal("700"))
        self.assertEqual(OrderPlanFinance.objects.get(order_plan=first).receivable_amount, Decimal("1000"))
        self.assertEqual(OrderPlanFinance.objects.get(order_plan=second).receivable_amount, Decimal("2000"))
        self.assertEqual(self.details(first)["outstanding_amount"], "0.00")
        self.assertEqual(self.details(second)["outstanding_amount"], "1900.00")

    def test_three_orders_fifo_and_cash_conservation(self):
        first, second, third = self.order("100"), self.order("200"), self.order("300")
        receipt = self.upload(third)
        self.confirm(third, receipt, "450")
        self.assertEqual(self.active_allocations(receipt), {
            first.pk: Decimal("100"), second.pk: Decimal("200"), third.pk: Decimal("150")})
        self.assertEqual(self.rolling(third)["outstanding_amount"], "150.00")
        total = OrderPlanPaymentAllocation.objects.filter(released_at__isnull=True).aggregate(value=Sum("amount"))["value"]
        self.assertEqual(total, Decimal("450"))
        self.assertEqual(OrderPlanPayment.objects.get(pk=receipt.pk).confirmed_amount, Decimal("450"))

    def test_unpriced_order_does_not_move_task_and_late_old_statement_does_not_steal_entry(self):
        oldest = self.order()
        middle = self.order("100")
        newest = self.order()
        self.assertEqual(self.rolling(middle)["active_order_plan_id"], middle.pk)
        self.assertEqual(self.finance_tasks(middle).count(), 1)
        self.statement(newest, "200")
        self.statement(oldest, "50")
        data = self.rolling(newest)
        self.assertEqual(data["active_order_plan_id"], newest.pk)
        self.assertEqual(data["outstanding_amount"], "350.00")
        self.assertEqual(data["carry_forward_amount"], "150.00")
        self.assertFalse(self.finance_tasks(oldest).exists())
        self.assertFalse(self.finance_tasks(middle).exists())

    def test_pending_old_voucher_is_confirmed_on_current_order_and_source_document_stays_private(self):
        first = self.order("1000")
        payment = self.upload(first)
        source_file = payment.voucher_image.name
        second = self.order("2000")
        current = self.details(second, self.factory_assistant_1)
        row = next(p for p in current["payments"] if p["id"] == payment.pk)
        self.assertEqual(row["source_order_plan_id"], first.pk)
        self.assertIn(f"order-plans/{first.pk}/", row["voucher_url"])
        self.assertFalse(self.finance_tasks(first).exists())
        self.assertTrue(self.finance_tasks(second).filter(payload__payment_id=payment.pk).exists())
        url = f"/pldp/api/sales/order-plans/{second.pk}/finance/payments/{payment.pk}/confirm/"
        response = self.client.post(url, json.dumps({"confirmed_amount": "1200"}),
            content_type="application/json", HTTP_AUTHORIZATION=self.factory_assistant_2_authorization)
        self.assertEqual(response.json()["code"], 0, response.content)
        self.assertEqual(response.json()["data"]["rolling_receivables"]["outstanding_amount"], "1800.00")
        payment.refresh_from_db()
        self.assertEqual(payment.finance.order_plan_id, first.pk)
        self.assertEqual(payment.voucher_image.name, source_file)
        self.assertEqual(self.active_allocations(payment), {first.pk: Decimal("1000"), second.pk: Decimal("200")})
        self.assertEqual(self.client.get(row["voucher_url"]).status_code, 401)
        self.assertEqual(self.client.get(row["voucher_url"], HTTP_AUTHORIZATION=self.merchant_sales_authorization).status_code, 403)

    def test_two_legacy_pending_vouchers_with_same_sequence_complete_exact_payment(self):
        first = self.order("100")
        old = self.upload(first)
        second = self.order("200")
        # Previous per-order releases could have one pending proof on each order.
        fresh = OrderPlanPayment.objects.create(finance=OrderPlanFinance.objects.get(order_plan=second),
            sequence_no=1, client_request_id=old.client_request_id, voucher_image=self.image("black"),
            uploaded_by=self.merchant_owner)
        # Old uniqueness was per invoice: the same request ID can name two
        # different vouchers. A replay must match its original invoice first.
        snapshot = self.all_snapshot()
        submit_order_payment(user=self.merchant_owner, plan_id=first.pk,
            client_request_id=old.client_request_id, voucher_file=self.image())
        submit_order_payment(user=self.merchant_owner, plan_id=second.pk,
            client_request_id=fresh.client_request_id, voucher_file=self.image("black"))
        self.assertEqual(self.all_snapshot(), snapshot)
        with self.assertRaises(OrderFinanceError):
            submit_order_payment(user=self.merchant_owner, plan_id=first.pk,
                client_request_id=old.client_request_id, voucher_file=self.image("black"))
        self.assertEqual(self.all_snapshot(), snapshot)
        call_command("reconcile_order_receivables", apply=True, stdout=io.StringIO())
        confirms = self.finance_tasks(second).filter(type="ORDER_PLAN_PAYMENT_CONFIRM")
        self.assertSetEqual(set(confirms.values_list("payload__payment_id", flat=True)), {old.pk, fresh.pk})
        self.confirm(second, fresh, "50")
        old.refresh_from_db()
        self.assertEqual(old.status, "PENDING")
        self.assertTrue(self.finance_tasks(second).filter(payload__payment_id=old.pk).exists())
        self.assertFalse(self.finance_tasks(second).filter(payload__payment_id=fresh.pk).exists())
        self.confirm(second, old, "100")
        self.assertEqual(self.rolling(second)["outstanding_amount"], "150.00")
        self.assertFalse(self.finance_tasks(second).filter(type="ORDER_PLAN_PAYMENT_CONFIRM").exists())

    def test_cross_order_retries_preserve_voucher_and_amount_and_reject_conflicts(self):
        first = self.order("100")
        payment = self.upload(first, "shared-request")
        second = self.order("200")
        original = self.all_snapshot()
        self.upload(second, "shared-request")
        self.assertEqual(self.all_snapshot(), original)
        with self.assertRaises(OrderFinanceError):
            self.upload(second, "shared-request", color="black")
        with self.assertRaises(OrderFinanceError):
            self.upload(first, "stale-new-action")
        with self.assertRaises(OrderFinanceError):
            self.confirm(first, payment, "100")
        self.confirm(second, payment, "100")
        completed = self.all_snapshot()
        self.confirm(first, payment, "100.00")
        self.assertEqual(self.all_snapshot(), completed)
        with self.assertRaises(OrderFinanceError):
            self.confirm(second, payment, "99")
        self.assertEqual(OrderPlanPayment.objects.count(), 1)

    def test_only_owner_uploads_and_assistant_peers_close_cooperation(self):
        first, second = self.order("100"), self.order("200")
        for user in (self.merchant_sales, self.merchant_stock_1, self.other_merchant_owner, self.sales_user):
            with self.subTest(user=user.pk), self.assertRaises(OrderFinanceError):
                self.upload(second, f"forbidden-{user.pk}", user=user)
        payment = self.upload(second)
        for user in (self.merchant_owner, self.sales_user, self.factory_stock):
            with self.subTest(user=user.pk), self.assertRaises(OrderFinanceError):
                self.confirm(second, payment, "100", user)
        self.confirm(second, payment, "100", self.factory_assistant_2)
        work = self.finance_tasks(second, status="DONE").get(type="ORDER_PLAN_PAYMENT_CONFIRM", payload__payment_id=payment.pk)
        self.assertEqual(work.user_tasks.get(to_openid=self.factory_assistant_1.wx_openid).done_reason, "OTHER_ACTION")
        self.assertFalse(work.user_tasks.filter(need_action=True).exists())

    def test_other_sales_creator_cannot_read_pair_details_or_private_other_order_documents(self):
        first = self.order("100")
        payment = self.upload(first)
        second = self.order("200")
        OrderPlan.objects.filter(pk=second.pk).update(created_by=self.other_sales_user)
        self.assertFalse(self.details(first, self.sales_user).get("rolling_receivables", {}).get("enabled", False))
        visible = self.details(second, self.other_sales_user)
        self.assertFalse(visible.get("rolling_receivables", {}).get("enabled", False))
        self.assertNotIn(payment.pk, [p["id"] for p in visible.get("payments", [])])
        # A creator's source order remains accessible, another creator's does not.
        denied = self.client.get(f"/pldp/api/sales/order-plans/{first.pk}/finance/",
            HTTP_AUTHORIZATION=self.other_sales_authorization)
        self.assertNotEqual(denied.json()["code"], 0)
        voucher = self.details(first, self.factory_assistant_1)["payments"][0]["voucher_url"]
        self.assertEqual(self.client.get(voucher, HTTP_AUTHORIZATION=self.other_sales_authorization).status_code, 403)

    def clone_pair_order(self, source, *, seller=None, buyer=None, creator=None):
        seller, buyer, creator = seller or self.factory, buyer or self.merchant, creator or self.sales_user
        relation, _ = OrganizationRelation.objects.get_or_create(source_organization=seller,
            target_organization=buyer, relation_type="DISTRIBUTION")
        assignment = MerchantChannelAssignment.objects.create(relation=relation,
            brand=self.brand, region="四川省", assigned_by=creator)
        data = {field.attname: getattr(source, field.attname) for field in OrderPlan._meta.concrete_fields
                if not field.primary_key and field.name not in ("created_at", "updated_at")}
        data.update(factory_organization_id=seller.pk, merchant_organization_id=buyer.pk,
            merchant_relation_id=relation.pk, channel_assignment_id=assignment.pk, created_by_id=creator.pk,
            plan_code=f"PAIR-{seller.pk}-{buyer.pk}", client_request_id=f"pair-{seller.pk}-{buyer.pk}")
        plan = OrderPlan.objects.create(**data)
        initialize_order_finance(plan)
        return plan

    def test_both_factory_and_merchant_boundaries_isolate_balances_and_payment_ids(self):
        first = self.order("100")
        payment = self.upload(first)
        other_buyer = self.clone_pair_order(first, buyer=self.other_merchant)
        self.statement(other_buyer, "500")
        other_factory = Organization.objects.create(code="ROLLING-OTHER-FACTORY", name="另一厂家",
            organization_type=OrganizationType.objects.get(code="OWNER"))
        peer = self._user_with_identity("other-factory-assistant", "other-factory-assistant-wx", other_factory,
            self.factory_sales_assistant_role)
        other_pair = self.clone_pair_order(first, seller=other_factory)
        submit_order_finance_statement(user=peer, plan_id=other_pair.pk, receivable_amount="700", statement_file=self.image())
        self.assertEqual(self.rolling(first)["outstanding_amount"], "100.00")
        self.assertEqual(self.details(other_buyer, self.factory_assistant_1)["rolling_receivables"]["outstanding_amount"], "500.00")
        self.assertEqual(self.rolling(other_pair)["outstanding_amount"], "700.00")
        with self.assertRaises(OrderFinanceError):
            self.confirm(other_buyer, payment, "100")
        with self.assertRaises(OrderFinanceError):
            self.confirm(other_pair, payment, "100", peer)
        self.assertEqual(OrderPlanReceivableAccount.objects.count(), 3)

    def test_overcollection_stays_cash_credit_and_offsets_later_new_invoice(self):
        first = self.order("100")
        payment = self.upload(first)
        result = self.confirm(first, payment, "110")
        self.assertEqual(result["received_amount"], "110.00")
        self.assertEqual(result["overpaid_amount"], "10.00")
        self.assertEqual(self.active_allocations(payment), {first.pk: Decimal("100")})
        second = self.order("50")
        self.assertEqual(self.rolling(second)["outstanding_amount"], "40.00")
        self.assertEqual(self.active_allocations(payment), {first.pk: Decimal("100"), second.pk: Decimal("10")})
        payment.refresh_from_db()
        self.assertEqual(payment.confirmed_amount, Decimal("110"))
        self.assertEqual(payment.finance.order_plan_id, first.pk)

    def test_cancel_current_invoice_restores_prior_task_without_rewriting_done_history(self):
        first = self.order("100")
        old_task = self.finance_tasks(first).get()
        second = self.order("200")
        old_task.refresh_from_db()
        history = list(old_task.user_tasks.values())
        second.status = OrderPlan.Status.CANCELLED
        second.save(update_fields=["status"])
        cancel_order_finance_tasks(second, self.sales_user)
        self.assertEqual(self.rolling(first)["active_order_plan_id"], first.pk)
        self.assertEqual(self.rolling(first)["outstanding_amount"], "100.00")
        self.assertTrue(self.details(first)["can_upload_payment"])
        self.assertEqual(self.finance_tasks(first).filter(type="ORDER_PLAN_PAYMENT_UPLOAD").count(), 1)
        self.assertEqual(list(old_task.user_tasks.values()), history)
        self.assertFalse(self.finance_tasks(second).exists())
        snapshot = self.all_snapshot()
        cancel_order_finance_tasks(second, self.sales_user)
        self.assertEqual(self.all_snapshot(), snapshot)

    def test_cancelled_source_cash_is_held_but_other_origin_allocation_is_released_and_reusable(self):
        first = self.order("100")
        cash = self.upload(first)
        self.confirm(first, cash, "150")
        second = self.order("100")
        self.assertEqual(self.active_allocations(cash), {first.pk: Decimal("100"), second.pk: Decimal("50")})
        second.status = OrderPlan.Status.CANCELLED
        second.save(update_fields=["status"])
        cancel_order_finance_tasks(second, self.sales_user)
        released = OrderPlanPaymentAllocation.objects.get(payment=cash, finance__order_plan=second)
        self.assertIsNotNone(released.released_at)
        self.assertTrue(released.release_reason)
        third = self.order("200")
        self.assertEqual(self.rolling(third)["outstanding_amount"], "150.00")
        # Cancelling the original cash-bearing order now holds its unused cash;
        # a later order cannot silently consume funds from that cancelled source.
        first.status = OrderPlan.Status.CANCELLED
        first.save(update_fields=["status"])
        cancel_order_finance_tasks(first, self.sales_user)
        fourth = self.order("100")
        self.assertEqual(self.rolling(fourth)["outstanding_amount"], "250.00")
        cash.refresh_from_db()
        self.assertEqual(cash.confirmed_amount, Decimal("150"))
        self.assertEqual(OrderPlanFinance.objects.get(order_plan=first).receivable_amount, Decimal("100"))

    def test_reallocated_increment_retains_previous_allocation_amount_and_timestamp(self):
        first, cancelled, current = self.order("100"), self.order("100"), self.order("300")
        cash = self.upload(current)
        self.confirm(current, cash, "450")
        prior = OrderPlanPaymentAllocation.objects.get(payment=cash, finance__order_plan=current)
        previous_amount, previous_at = prior.amount, prior.allocated_at
        self.assertEqual(previous_amount, Decimal("250"))
        cancelled.status = OrderPlan.Status.CANCELLED
        cancelled.save(update_fields=["status"])
        cancel_order_finance_tasks(cancelled, self.sales_user)
        prior.refresh_from_db()
        self.assertEqual(prior.amount, previous_amount)
        self.assertEqual(prior.allocated_at, previous_at)
        self.assertIsNotNone(prior.released_at)
        self.assertEqual(prior.release_reason, "REALLOCATED_INCREMENT")
        self.assertEqual(self.active_allocations(cash), {first.pk: Decimal("100"), current.pk: Decimal("300")})
        self.assertEqual(self.rolling(current)["overpaid_amount"], "50.00")
        self.assertFalse(self.finance_tasks(current).exists())

    def test_real_cancellation_before_assembly_resynchronizes_finance(self):
        first, second = self.order("100"), self.order("200")
        result = cancel_material_flow_order_plan(user=self.sales_user, plan_id=second.pk, reason="客户取消")
        self.assertEqual(result["cancellation_status"], "CANCELLED")
        self.assertEqual(self.rolling(first)["outstanding_amount"], "100.00")
        self.assertTrue(self.details(first)["can_upload_payment"])
        self.assertFalse(self.finance_tasks(second).exists())

    def test_real_pending_then_final_physical_cancellation_keeps_previous_finance_entry(self):
        first = self.order("100")
        self._create_lot(10000, suffix="ROLLING-PENDING-CANCEL")
        # First order's reservation was planned before this stock existed.
        second = self._create_v2_plan(request_id="rolling-pending-cancel", quantity=10)
        self.statement(second, "200")
        warehouse_review_material_plan(user=self.material_keeper, plan_id=second.pk)
        release_material_ready_for_production(user=self.material_keeper, plan_id=second.pk)
        result = cancel_material_flow_order_plan(user=self.sales_user, plan_id=second.pk, assembly_started_offline=True)
        self.assertEqual(result["cancellation_status"], "WAITING_PRODUCTION_COMPLETION")
        self.assertEqual(self.rolling(first)["active_order_plan_id"], first.pk)
        self.assertEqual(self.rolling(first)["outstanding_amount"], "100.00")
        self.assertFalse(self.finance_tasks(second).exists())
        with self.assertRaises(OrderFinanceError):
            self.upload(second)
        subplan = OrderPlanMaterialPlan.objects.get(order_plan=second).production_subplans.get(sequence_no=1)
        confirm_material_flow_production(user=self.factory_production, plan_id=second.pk, subplan_id=subplan.pk)
        self.assertEqual(OrderPlanCancellationAudit.objects.get(order_plan=second).status, "WAITING_FINISHED_STOCK")
        final = cancel_material_flow_order_plan(user=self.sales_user, plan_id=second.pk, assembly_started_offline=True)
        self.assertEqual(final["cancellation_status"], "CANCELLED")
        self.assertEqual(self.rolling(first)["outstanding_amount"], "100.00")
        self.assertEqual(self.finance_tasks(first).filter(type="ORDER_PLAN_PAYMENT_UPLOAD").count(), 1)

    def test_receipt_and_accounting_are_independent_of_cash_and_accounted_debt_keeps_rolling(self):
        first, _subplan, shipment = self._ready_production_shipment(request_id="rolling-accounted", quantity=5)
        self.statement(first, "100")
        with self.assertRaises(OrderPlanMaterialFlowError):
            confirm_material_flow_accounting(user=self.factory_assistant_1, plan_id=first.pk)
        self._authorize_print_and_outbound(plan=first, shipment=shipment)
        confirm_material_flow_merchant_receipt(user=self.merchant_stock_1, plan_id=first.pk, shipment_id=shipment.pk)
        result, changed = confirm_material_flow_accounting(user=self.factory_assistant_1, plan_id=first.pk)
        self.assertTrue(changed)
        first.refresh_from_db()
        self.assertIsNotNone(first.accounted_at)
        self.assertEqual(self.details(first)["outstanding_amount"], "100.00")
        payment = self.upload(first)
        self.confirm(first, payment, "40")
        second = self.order("200")
        second.submitted_at = first.submitted_at + timedelta(minutes=1)
        second.save(update_fields=["submitted_at"])
        call_command("reconcile_order_receivables", apply=True, stdout=io.StringIO())
        self.assertEqual(self.rolling(second)["outstanding_amount"], "260.00")
        payment2 = self.upload(second)
        self.confirm(second, payment2, "260")
        self.assertEqual(self.details(first)["outstanding_amount"], "0.00")
        self.assertEqual(self.rolling(second)["outstanding_amount"], "0.00")

    def test_reading_old_order_card_does_not_complete_new_payment_operation(self):
        first = self.order("100")
        old_task = self.finance_tasks(first).get().user_tasks.get(to_openid=self.merchant_owner.wx_openid)
        second = self.order("200")
        pending = self.finance_tasks(second).get().user_tasks.get(to_openid=self.merchant_owner.wx_openid)
        open_task(self.merchant_owner.wx_openid, old_task.pk)
        pending.refresh_from_db()
        self.assertTrue(pending.need_action)
        self.assertNotEqual(pending.state, "DONE")
        cards = get_task_summary(self.merchant_owner.wx_openid)
        self.assertEqual(cards["action_pending_count"], 1)

    def test_reconcile_command_is_read_only_then_idempotent_preserving_task_history(self):
        first = self.order("100")
        first_task = self.finance_tasks(first).get()
        second = self.order("200")
        second_task = self.finance_tasks(second).get()
        second_task.user_tasks.update(state="READ", read_at=timezone.now())
        before = self.all_snapshot()
        output = io.StringIO()
        call_command("reconcile_order_receivables", check_only=True, stdout=output)
        self.assertEqual(self.all_snapshot(), before)
        call_command("reconcile_order_receivables", apply=True, stdout=output)
        applied = self.all_snapshot()
        call_command("reconcile_order_receivables", apply=True, stdout=output)
        self.assertEqual(self.all_snapshot(), applied)
        self.assertEqual(list(first_task.user_tasks.values()), [row for row in before["tasks"] if row["workitem_id"] == first_task.pk])
        self.assertEqual(second_task.user_tasks.get().state, "READ")

    def test_allocation_failure_and_sqlite_contention_roll_back_cash_and_task_changes(self):
        first, second = self.order("100"), self.order("200")
        payment = self.upload(second)
        before = self.all_snapshot()
        from sales.services.order_receivable_service import OrderReceivableError
        # The failure occurs after confirmation has tentatively closed inboxes;
        # the outer command must restore cash status and every affected task.
        for failure in (OrderReceivableError("allocation integrity test"), OperationalError("database is locked")):
            with self.subTest(error=type(failure).__name__):
                with patch("sales.services.order_finance_service.allocate_payment_fifo", side_effect=failure):
                    with self.assertRaises(OrderFinanceError):
                        self.confirm(second, payment, "150")
                self.assertEqual(self.all_snapshot(), before)
        self.confirm(second, payment, "150")
        self.assertEqual(self.rolling(second)["outstanding_amount"], "150.00")
