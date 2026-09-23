"""Exercise rolling merchant debt through real shipment and payment endpoints."""

from decimal import Decimal
from io import StringIO
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import call_command
from django.db import OperationalError
from django.test import TestCase

from account.models import Organization, OrganizationType
from lifecycle import test_goods_transfer as _flow_fixtures
from lifecycle.models import (
    GoodsTransfer,
    GoodsTransferPayment,
    GoodsTransferPaymentAllocation,
    GoodsTransferSettlement,
    OrganizationDevice,
)
from lifecycle.private_storage import goods_transfer_private_storage
from workflow.models import UserTask, WorkItem
from workflow.services.task_action import open_task
from workflow.services.task_query import get_task_list, get_task_summary


class RollingMerchantReceivablesTests(TestCase):
    endpoint = _flow_fixtures.GoodsTransferApiTests.endpoint
    _user = classmethod(_flow_fixtures.GoodsTransferApiTests._user.__func__)
    _relation = classmethod(_flow_fixtures.GoodsTransferApiTests._relation.__func__)
    auth = _flow_fixtures.GoodsTransferApiTests.auth
    set_owner = _flow_fixtures.GoodsTransferApiTests.set_owner
    create_flow = _flow_fixtures.GoodsTransferApiTests.create_flow
    scan_and_submit = _flow_fixtures.GoodsTransferApiTests.scan_and_submit
    submit_transfer = _flow_fixtures.GoodsTransferApiTests.submit_transfer
    image_file = _flow_fixtures.GoodsTransferApiTests.image_file

    @classmethod
    def setUpTestData(cls):
        _flow_fixtures.GoodsTransferApiTests.setUpTestData.__func__(cls)
        cls.child_b_sales = cls._user(
            "rolling-child-sales", cls.child_b, cls.merchant_sales_role,
        )
        cls.other_sender = Organization.objects.create(
            name="另一发货商家", code="ROLL-OTHER-SENDER",
            organization_type=OrganizationType.objects.get(code="MERCHANT"),
        )
        cls.other_sender_owner = cls._user(
            "rolling-other-sender", cls.other_sender, cls.merchant_owner_role,
        )
        cls._relation(cls.other_sender_owner, cls.merchant_owner_role,
                      cls.child_b_owner, cls.child_b)

    def setUp(self):
        media = TemporaryDirectory(prefix="rolling-receivables-media-")
        self.addCleanup(media.cleanup)
        for attribute in ("location", "base_location"):
            storage_patch = patch.object(goods_transfer_private_storage, attribute, media.name)
            storage_patch.start()
            self.addCleanup(storage_patch.stop)

    def post(self, path, user, payload=None, *, json=False):
        response = self.client.post(
            path, payload or {}, **self.auth(user),
            **({"content_type": "application/json"} if json else {}),
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["code"], 0, response.json())
        return response.json()["data"]

    def denied(self, path, user, payload=None, *, json=False):
        response = self.client.post(
            path, payload or {}, **self.auth(user),
            **({"content_type": "application/json"} if json else {}),
        )
        data = response.json()
        self.assertNotEqual(data.get("code"), 0, data)
        return data

    def base(self, flow):
        return f"{self.endpoint}{flow['id']}/"

    def detail(self, flow, user=None):
        response = self.client.get(self.base(flow) + "settlement/", **self.auth(user or self.parent_owner))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["code"], 0, response.json())
        return response.json()["data"]

    def start(self, index, *, receiver=None, sender=None, amount=None, receipt=False, documents=True):
        sender = sender or self.parent_stock
        sender_org = sender.organization_identity.organization
        receiver = receiver or self.child_b_owner
        device = self.devices[index]
        self.set_owner(device, sender_org)
        flow = self.create_flow(sender, {
            "flow_type": "SHIPMENT", "client_request_id": f"rolling-flow-{index}",
            "target_account_id": receiver.pk,
        })
        self.scan_and_submit(sender, flow["id"], device)
        if amount is not None:
            self.statement(flow, amount, user=sender)
        if documents:
            self.post(self.base(flow) + "settlement/logistics-documents/", sender, {
                "client_request_id": f"rolling-document-{index}",
                "document_file": self.image_file(f"logistics-{index}.png"),
            })
        if receipt:
            self.post(self.base(flow) + "receipt/", receiver)
        return flow

    def statement(self, flow, amount, user=None):
        return self.post(self.base(flow) + "settlement/statement/", user or self.parent_owner, {
            "receivable_amount": amount, "statement_image": self.image_file("statement.png"),
        })

    def voucher(self, flow, request_id, user=None):
        data = self.post(self.base(flow) + "settlement/payments/", user or self.child_b_owner, {
            "client_request_id": request_id, "voucher_image": self.image_file(request_id + ".png"),
        })
        payment = GoodsTransferPayment.objects.get(client_request_id=request_id)
        return payment, data

    def confirm(self, flow, payment, amount, user=None):
        return self.post(
            self.base(flow) + f"settlement/payments/{payment.pk}/confirm/",
            user or self.parent_sales, {"confirmed_amount": amount}, json=True,
        )

    def tasks(self, flow, *, kind=None, user=None):
        query = UserTask.objects.filter(workitem__biz_ref=f"lifecycle.GoodsTransfer:{flow['id']}")
        if kind:
            query = query.filter(workitem__type=kind)
        if user:
            query = query.filter(to_openid=user.wx_openid)
        return query

    def assert_one_payment_entry(self, active_flow, user):
        tasks = UserTask.objects.filter(
            to_openid=user.wx_openid, need_action=True,
            workitem__type__in=("GOODS_TRANSFER_PAYMENT_UPLOAD", "GOODS_TRANSFER_PAYMENT_CONFIRM"),
        ).exclude(state="DONE")
        self.assertEqual(tasks.count(), 1, list(tasks.values("id", "workitem__biz_ref")))
        self.assertEqual(tasks.get().workitem.biz_ref, f"lifecycle.GoodsTransfer:{active_flow['id']}")

    def test_three_orders_roll_debt_and_allocate_cash_oldest_first_without_duplicating_charges(self):
        first = self.start(0, amount="1000.00", receipt=True)
        payment1, _ = self.voucher(first, "roll-first-payment")
        self.confirm(first, payment1, "400.00")
        second = self.start(1, amount="2000.00", receipt=True)
        second_detail = self.detail(second)
        roll = second_detail["rolling_receivables"]
        self.assertTrue(roll["enabled"])
        self.assertTrue(roll["is_current"])
        self.assertEqual(roll["active_transfer_id"], second["id"])
        self.assertEqual(roll["carry_forward_amount"], "600.00")
        self.assertEqual(roll["receivable_amount"], "3000.00")
        self.assertEqual(roll["confirmed_received_amount"], "400.00")
        self.assertEqual(roll["outstanding_amount"], "2600.00")
        self.assertEqual(second_detail["receivable_amount"], "2000.00")
        self.assertEqual(second_detail["confirmed_received_amount"], "0.00")
        self.assertEqual(second_detail["outstanding_amount"], "2000.00")
        for user in (self.child_b_owner, self.child_b_sales, self.child_b_stock):
            self.assert_one_payment_entry(second, user)
        payment2, _ = self.voucher(second, "roll-second-payment")
        self.confirm(second, payment2, "1600.00")
        self.assertEqual(self.detail(first)["outstanding_amount"], "0.00")
        self.assertEqual(self.detail(first)["confirmed_received_amount"], "1000.00")
        self.assertEqual(self.detail(second)["confirmed_received_amount"], "1000.00")
        third = self.start(2, amount="500.00", receipt=True)
        roll = self.detail(third)["rolling_receivables"]
        self.assertEqual(roll["receivable_amount"], "3500.00")
        self.assertEqual(roll["confirmed_received_amount"], "2000.00")
        self.assertEqual(roll["carry_forward_amount"], "1000.00")
        self.assertEqual(roll["outstanding_amount"], "1500.00")
        payment3, _ = self.voucher(third, "roll-third-payment")
        self.confirm(third, payment3, "1500.00")
        self.assertEqual(self.detail(third)["rolling_receivables"]["outstanding_amount"], "0.00")
        for flow, amount in ((first, "1000.00"), (second, "2000.00"), (third, "500.00")):
            detail = self.detail(flow)
            self.assertEqual(detail["receivable_amount"], amount)
            self.assertEqual(detail["confirmed_received_amount"], amount)
            self.assertEqual(detail["outstanding_amount"], "0.00")
        for payment, flow, amount in ((payment1, first, "400.00"), (payment2, second, "1600.00"), (payment3, third, "1500.00")):
            payment.refresh_from_db()
            self.assertEqual(payment.settlement.transfer_id, flow["id"])
            self.assertEqual(payment.confirmed_amount, Decimal(amount))
        self.assertEqual(GoodsTransferPayment.objects.count(), 3)
        self.assertEqual(sum(GoodsTransferSettlement.objects.values_list("receivable_amount", flat=True)), Decimal("3500.00"))

    def test_unpaid_old_card_closes_only_finance_and_preserves_collaborative_receipt(self):
        first = self.start(0, amount="1000.00")
        old_ids = set(UserTask.objects.values_list("id", flat=True))
        second = self.start(1, amount="2000.00")
        old_finance = self.tasks(first, kind="GOODS_TRANSFER_PAYMENT_UPLOAD")
        self.assertEqual(old_finance.count(), 3)
        self.assertTrue(all(row.state == "DONE" and not row.need_action and row.done_reason == "FINANCE_ROLLED_FORWARD" for row in old_finance))
        self.assertEqual(self.detail(first)["outstanding_amount"], "1000.00")
        self.assertFalse(self.detail(first, self.child_b_owner)["actions"]["can_upload_payment"])
        self.assertEqual(self.detail(second)["rolling_receivables"]["outstanding_amount"], "3000.00")
        for user in (self.child_b_owner, self.child_b_sales, self.child_b_stock):
            old_receipt = self.tasks(first, kind="GOODS_TRANSFER_RECEIPT_CONFIRM", user=user).get()
            self.assertTrue(old_receipt.need_action)
            self.assertNotEqual(old_receipt.state, "DONE")
            self.assert_one_payment_entry(second, user)
        self.post(self.base(first) + "receipt/", self.child_b_sales)
        self.assertEqual(OrganizationDevice.objects.get(device=self.devices[0]).organization_id, self.child_b.pk)
        self.assertEqual(GoodsTransfer.objects.get(pk=second["id"]).status, "PENDING_RECEIPT")
        self.assertFalse(self.tasks(first).filter(need_action=True).exclude(state="DONE").exists())
        self.assertTrue(old_ids.issubset(set(UserTask.objects.values_list("id", flat=True))))
        for user in (self.child_b_owner, self.child_b_sales, self.child_b_stock):
            self.assertEqual(get_task_summary(user.wx_openid)["action_pending_count"], 1)

    def test_uploaded_old_proof_survives_rollover_and_is_confirmed_from_latest_card(self):
        first = self.start(0, amount="1000.00", receipt=True)
        pending, _ = self.voucher(first, "proof-before-rollover")
        evidence = {key: getattr(pending, key) for key in ("settlement_id", "uploaded_by_id", "uploaded_at", "voucher_original_name")}
        evidence["voucher_image"] = pending.voucher_image.name
        second = self.start(1, amount="2000.00", receipt=True)
        details = self.detail(second)
        payment_row = next(row for row in details["payments"] if row["id"] == pending.pk)
        self.assertTrue(payment_row["can_confirm"])
        self.assertEqual(details["pending_payment_count"], 1)
        self.assertFalse(self.detail(second, self.child_b_owner)["actions"]["can_upload_payment"])
        for user in (self.parent_owner, self.parent_sales, self.parent_stock):
            self.assert_one_payment_entry(second, user)
        self.denied(self.base(second) + "settlement/payments/", self.child_b_owner, {
            "client_request_id": "second-proof-too-soon", "voucher_image": self.image_file(),
        })
        latest = self.confirm(second, pending, "1200.00")
        self.assertEqual(latest["rolling_receivables"]["outstanding_amount"], "1800.00")
        pending.refresh_from_db()
        for key, value in evidence.items():
            self.assertEqual(pending.voucher_image.name if key == "voucher_image" else getattr(pending, key), value)
        self.assertEqual(self.detail(first)["confirmed_received_amount"], "1000.00")
        self.assertEqual(self.detail(second)["confirmed_received_amount"], "200.00")
        self.assert_one_payment_entry(second, self.child_b_owner)
        file_response = self.client.get(
            self.base(first) + f"settlement/payments/{pending.pk}/voucher-file/", **self.auth(self.parent_owner),
        )
        self.assertEqual(file_response.status_code, 200)
        file_response.close()

    def test_payment_request_and_confirm_retries_are_idempotent_across_rollover(self):
        first = self.start(0, amount="1000.00", receipt=True)
        payment, _ = self.voucher(first, "same-proof-request")
        second = self.start(1, amount="2000.00", receipt=True)
        self.voucher(second, "same-proof-request")
        self.assertEqual(GoodsTransferPayment.objects.count(), 1)
        self.confirm(second, payment, "600.00")
        before = self.detail(second)
        self.confirm(second, payment, "600.00")
        self.voucher(second, "same-proof-request")
        after = self.detail(second)
        self.assertEqual(before["rolling_receivables"], after["rolling_receivables"])
        self.assertEqual(GoodsTransferPayment.objects.count(), 1)
        self.assertEqual(self.detail(first)["confirmed_received_amount"], "600.00")
        self.assertEqual(after["rolling_receivables"]["confirmed_received_amount"], "600.00")
        self.assert_one_payment_entry(second, self.child_b_owner)

    def test_stale_order_mutations_are_rejected_with_latest_entry_and_no_money_changes(self):
        first = self.start(0, amount="1000.00", receipt=True)
        payment, _ = self.voucher(first, "pending-stale-proof")
        second = self.start(1, amount="2000.00", receipt=True)
        snapshots = list(GoodsTransferPayment.objects.values())
        denied = self.denied(self.base(first) + f"settlement/payments/{payment.pk}/confirm/", self.parent_owner, {"confirmed_amount": "300.00"}, json=True)
        self.assertTrue("最新" in str(denied) or second["flow_no"] in str(denied), denied)
        denied = self.denied(self.base(first) + "settlement/payments/", self.child_b_owner, {
            "client_request_id": "new-on-stale-entry", "voucher_image": self.image_file(),
        })
        self.assertTrue("最新" in str(denied) or second["flow_no"] in str(denied), denied)
        self.assertEqual(list(GoodsTransferPayment.objects.values()), snapshots)
        self.assertEqual(self.detail(second)["rolling_receivables"]["outstanding_amount"], "3000.00")

    def test_pair_balance_guard_and_nonparticipant_authorization_preserve_pending_proof(self):
        first = self.start(0, amount="1000.00", receipt=True)
        second = self.start(1, amount="2000.00", receipt=True)
        payment, _ = self.voucher(second, "guard-proof")
        url = self.base(second) + f"settlement/payments/{payment.pk}/confirm/"
        for user, amount in ((self.parent_owner, "3000.01"), (self.child_c_owner, "300.00"), (self.child_b_owner, "300.00")):
            self.denied(url, user, {"confirmed_amount": amount}, json=True)
        payment.refresh_from_db()
        self.assertEqual(payment.status, "PENDING")
        self.assertIsNone(payment.confirmed_at)
        self.assertEqual(self.detail(first)["confirmed_received_amount"], "0.00")
        self.assertEqual(self.detail(second)["confirmed_received_amount"], "0.00")
        self.confirm(second, payment, "3000.00")
        self.assertEqual(self.detail(second)["rolling_receivables"]["outstanding_amount"], "0.00")

    def test_distinct_buyers_and_sellers_never_share_balances_or_payment_deduplication(self):
        to_b = self.start(0, amount="1000.00", receipt=True)
        to_c = self.start(1, receiver=self.child_c_owner, amount="2000.00", receipt=True)
        other_to_b = self.start(2, sender=self.other_sender_owner, amount="3000.00", receipt=True)
        for flow, amount, user in ((to_b, "1000.00", self.parent_owner), (to_c, "2000.00", self.parent_owner), (other_to_b, "3000.00", self.other_sender_owner)):
            roll = self.detail(flow, user)["rolling_receivables"]
            self.assertEqual(roll["outstanding_amount"], amount)
            self.assertEqual(roll["active_transfer_id"], flow["id"])
        self.post(self.base(to_b) + "settlement/payments/", self.child_b_owner, {"client_request_id": "same-across-pairs", "voucher_image": self.image_file()})
        self.post(self.base(to_c) + "settlement/payments/", self.child_c_owner, {"client_request_id": "same-across-pairs", "voucher_image": self.image_file()})
        self.post(self.base(other_to_b) + "settlement/payments/", self.child_b_owner, {"client_request_id": "same-across-pairs", "voucher_image": self.image_file()})
        self.assertEqual(GoodsTransferPayment.objects.filter(client_request_id="same-across-pairs").count(), 3)
        wrong_payment = GoodsTransferPayment.objects.get(settlement__transfer_id=to_c["id"])
        self.denied(self.base(to_b) + f"settlement/payments/{wrong_payment.pk}/confirm/", self.parent_owner, {"confirmed_amount": "100.00"}, json=True)
        self.assertEqual(self.detail(to_b)["rolling_receivables"]["outstanding_amount"], "1000.00")

    def test_late_old_statement_adds_charge_without_stealing_newer_shipment_entry(self):
        first = self.start(0)
        second = self.start(1, amount="2000.00")
        self.statement(first, "1000.00")
        current = self.detail(second)["rolling_receivables"]
        historical = self.detail(first)["rolling_receivables"]
        self.assertEqual(current["active_transfer_id"], second["id"])
        self.assertTrue(current["is_current"])
        self.assertFalse(historical["is_current"])
        self.assertEqual(current["outstanding_amount"], "3000.00")
        self.assertEqual(current["carry_forward_amount"], "1000.00")
        self.assert_one_payment_entry(second, self.child_b_owner)

    def test_unpriced_new_shipment_does_not_hide_existing_payment_due(self):
        first = self.start(0, amount="1000.00")
        second = self.start(1)
        self.assert_one_payment_entry(first, self.child_b_owner)
        self.assertEqual(self.detail(first)["rolling_receivables"]["active_transfer_id"], first["id"])
        self.statement(second, "2000.00")
        self.assert_one_payment_entry(second, self.child_b_owner)
        self.assertEqual(self.detail(second)["rolling_receivables"]["outstanding_amount"], "3000.00")

    def test_reading_old_card_cannot_acknowledge_latest_payment_action(self):
        first = self.start(0, amount="1000.00", receipt=True)
        old_card = get_task_list(self.child_b_owner.wx_openid)["items"][0]
        second = self.start(1, amount="2000.00", receipt=True)
        open_task(self.child_b_owner.wx_openid, old_card["entry_task_id"])
        self.assert_one_payment_entry(second, self.child_b_owner)
        cards = get_task_list(self.child_b_owner.wx_openid)["items"]
        self.assertEqual(len(cards), 2)
        self.assertEqual(sum(row["action_pending_count"] for row in cards), 1)
        self.assertEqual(get_task_summary(self.child_b_owner.wx_openid)["action_pending_count"], 1)

    def test_return_credit_reduces_rolling_debt_without_changing_invoice_or_cash(self):
        first = self.start(0, amount="1000.00", receipt=True)
        second = self.start(1, amount="2000.00", receipt=True)
        returned = self.create_flow(self.child_b_owner, {
            "flow_type": "RETURN", "client_request_id": "rolling-return-credit",
            "target_account_id": self.parent_sales.pk,
        })
        self.scan_and_submit(self.child_b_owner, returned["id"], self.devices[0])
        self.post(self.base(returned) + "receipt/", self.parent_stock)
        accounts = self.base(returned) + "accounts/"
        submitted = self.post(accounts + "RETURN_CREDIT/submit/", self.child_b_owner, {"amount": "300.00", "remark": "滚动应收退货冲抵"})
        case = submitted["cases"][0]
        confirmed = self.post(accounts + f"{case['id']}/confirm/", self.parent_owner, {"action": "CONFIRM"}, json=True)
        candidate = next(row for row in confirmed["cases"][0]["allocation_candidates"] if row["flow_no"] == first["flow_no"])
        self.post(accounts + f"{case['id']}/allocations/", self.parent_sales, {
            "target_kind": candidate["target_kind"], "target_id": candidate["target_id"], "amount": "300.00",
        }, json=True)
        current = self.detail(second)["rolling_receivables"]
        self.assertEqual(current["receivable_amount"], "3000.00")
        self.assertEqual(current["confirmed_received_amount"], "0.00")
        self.assertEqual(current["confirmed_credit_amount"], "300.00")
        self.assertEqual(current["carry_forward_amount"], "700.00")
        self.assertEqual(current["outstanding_amount"], "2700.00")
        self.assertEqual(self.detail(first)["confirmed_credit_amount"], "300.00")
        self.assert_one_payment_entry(second, self.child_b_owner)
        payment, _ = self.voucher(second, "after-credit-proof")
        self.confirm(second, payment, "2700.00")
        self.assertEqual(self.detail(first)["confirmed_received_amount"], "700.00")
        self.assertEqual(self.detail(second)["confirmed_received_amount"], "2000.00")
        self.assertEqual(self.detail(second)["rolling_receivables"]["outstanding_amount"], "0.00")
        self.assertFalse(UserTask.objects.filter(to_openid=self.child_b_owner.wx_openid, workitem__type="GOODS_TRANSFER_PAYMENT_UPLOAD", need_action=True).exclude(state="DONE").exists())

    def test_return_credit_cannot_be_allocated_against_invoice_already_paid_by_later_order(self):
        first = self.start(0, amount="1000.00", receipt=True)
        second = self.start(1, amount="2000.00", receipt=True)
        payment, _ = self.voucher(second, "cash-paid-earlier-invoice")
        self.confirm(second, payment, "1000.00")
        returned = self.create_flow(self.child_b_owner, {
            "flow_type": "RETURN", "client_request_id": "return-after-cross-invoice-cash",
            "target_account_id": self.parent_sales.pk,
        })
        self.scan_and_submit(self.child_b_owner, returned["id"], self.devices[0])
        self.post(self.base(returned) + "receipt/", self.parent_stock)
        accounts = self.base(returned) + "accounts/"
        submitted = self.post(accounts + "RETURN_CREDIT/submit/", self.child_b_owner, {"amount": "300.00"})
        case = submitted["cases"][0]
        confirmed = self.post(accounts + f"{case['id']}/confirm/", self.parent_owner, {"action": "CONFIRM"}, json=True)
        candidates = confirmed["cases"][0]["allocation_candidates"]
        self.assertNotIn(first["flow_no"], {row["flow_no"] for row in candidates})
        candidate = next(row for row in candidates if row["flow_no"] == second["flow_no"])
        old_settlement = GoodsTransferSettlement.objects.get(transfer_id=first["id"])
        self.denied(accounts + f"{case['id']}/allocations/", self.parent_sales, {
            "target_kind": candidate["target_kind"], "target_id": old_settlement.pk, "amount": "100.00",
        }, json=True)
        self.assertEqual(self.detail(second)["rolling_receivables"]["outstanding_amount"], "2000.00")

    def test_failed_allocation_rolls_back_money_and_completed_collaboration_tasks(self):
        first = self.start(0, amount="1000.00", receipt=True)
        second = self.start(1, amount="2000.00", receipt=True)
        payment, _ = self.voucher(second, "allocation-rollback-proof")
        payments_before = list(GoodsTransferPayment.objects.order_by("id").values())
        work_before = list(WorkItem.objects.order_by("id").values())
        tasks_before = list(UserTask.objects.order_by("id").values())
        with patch.object(GoodsTransferPaymentAllocation.objects, "bulk_create",
                          side_effect=RuntimeError("simulated allocation storage failure")):
            with self.assertRaisesRegex(RuntimeError, "allocation storage failure"):
                self.confirm(second, payment, "1200.00")
        self.assertEqual(list(GoodsTransferPayment.objects.order_by("id").values()), payments_before)
        self.assertEqual(list(WorkItem.objects.order_by("id").values()), work_before)
        self.assertEqual(list(UserTask.objects.order_by("id").values()), tasks_before)
        self.assertFalse(GoodsTransferPaymentAllocation.objects.exists())
        self.assertEqual(self.detail(second)["rolling_receivables"]["outstanding_amount"], "3000.00")
        self.confirm(second, payment, "1200.00")
        self.assertEqual(self.detail(first)["confirmed_received_amount"], "1000.00")
        self.assertEqual(self.detail(second)["confirmed_received_amount"], "200.00")

    def test_existing_task_reconciliation_is_readonly_by_default_and_idempotent_on_apply(self):
        first = self.start(0, amount="1000.00")
        payment, _ = self.voucher(first, "legacy-pending-reconcile")
        second = self.start(1, amount="2000.00")
        # Reconstruct the pre-upgrade state: the old proof confirmation task is
        # open and no latest-entry confirmation task has ever been generated.
        old_work = WorkItem.objects.get(
            type="GOODS_TRANSFER_PAYMENT_CONFIRM",
            biz_ref=f"lifecycle.GoodsTransfer:{first['id']}",
        )
        WorkItem.objects.filter(type="GOODS_TRANSFER_PAYMENT_CONFIRM",
                                biz_ref=f"lifecycle.GoodsTransfer:{second['id']}").delete()
        WorkItem.objects.filter(pk=old_work.pk).update(status="OPEN", done_at=None)
        old_work.user_tasks.update(state="NEW", need_action=True, done_at=None, done_reason="")
        tasks_before = list(UserTask.objects.order_by("id").values())
        work_before = list(WorkItem.objects.order_by("id").values())
        payments_before = list(GoodsTransferPayment.objects.order_by("id").values())
        receipts_before = list(UserTask.objects.filter(workitem__type="GOODS_TRANSFER_RECEIPT_CONFIRM").order_by("id").values())
        output = StringIO()
        call_command("reconcile_goods_transfer_receivables", "--check-only", stdout=output)
        self.assertIn("ROLLING_RECEIVABLES_CHECK_ONLY", output.getvalue())
        self.assertEqual(list(UserTask.objects.order_by("id").values()), tasks_before)
        self.assertEqual(list(WorkItem.objects.order_by("id").values()), work_before)
        call_command("reconcile_goods_transfer_receivables", "--apply", stdout=StringIO())
        old_work.refresh_from_db()
        self.assertEqual(old_work.status, "DONE")
        self.assertTrue(all(row.done_reason == "FINANCE_ROLLED_FORWARD" for row in old_work.user_tasks.all()))
        for user in (self.parent_owner, self.parent_sales, self.parent_stock):
            self.assert_one_payment_entry(second, user)
        applied_tasks = list(UserTask.objects.order_by("id").values())
        applied_work = list(WorkItem.objects.order_by("id").values())
        call_command("reconcile_goods_transfer_receivables", "--apply", stdout=StringIO())
        self.assertEqual(list(UserTask.objects.order_by("id").values()), applied_tasks)
        self.assertEqual(list(WorkItem.objects.order_by("id").values()), applied_work)
        self.assertEqual(list(GoodsTransferPayment.objects.order_by("id").values()), payments_before)
        self.assertEqual(list(UserTask.objects.filter(workitem__type="GOODS_TRANSFER_RECEIPT_CONFIRM").order_by("id").values()), receipts_before)
        self.assertTrue(next(row for row in self.detail(second)["payments"] if row["id"] == payment.pk)["can_confirm"])

    def test_sqlite_competing_write_error_returns_retry_message_and_rolls_back_cash(self):
        first = self.start(0, amount="1000.00", receipt=True)
        second = self.start(1, amount="2000.00", receipt=True)
        payment, _ = self.voucher(second, "concurrent-lock-proof")
        payments_before = list(GoodsTransferPayment.objects.order_by("id").values())
        tasks_before = list(UserTask.objects.order_by("id").values())
        with patch.object(GoodsTransferPaymentAllocation.objects, "bulk_create",
                          side_effect=OperationalError("database is locked")):
            failure = self.denied(self.base(second) + f"settlement/payments/{payment.pk}/confirm/",
                                  self.parent_sales, {"confirmed_amount": "1200.00"}, json=True)
        self.assertIn("重试", str(failure))
        self.assertEqual(list(GoodsTransferPayment.objects.order_by("id").values()), payments_before)
        self.assertEqual(list(UserTask.objects.order_by("id").values()), tasks_before)
        self.assertFalse(GoodsTransferPaymentAllocation.objects.exists())
        self.confirm(second, payment, "1200.00")
        self.assertEqual(self.detail(first)["confirmed_received_amount"], "1000.00")
        self.assertEqual(self.detail(second)["rolling_receivables"]["outstanding_amount"], "1800.00")
