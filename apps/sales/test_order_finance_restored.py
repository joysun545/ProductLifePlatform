"""Regression of independent finance, permissions, retries and private files."""

import io
import json
import tempfile
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.utils import timezone
from PIL import Image

from sales.models import OrderPlan, OrderPlanFinance, OrderPlanPayment
from sales.private_storage import order_finance_private_storage
from sales.services.order_finance_service import (
    OrderFinanceError, cancel_order_finance_tasks, confirm_order_payment,
    initialize_order_finance, serialize_order_finance, submit_order_finance_statement,
    submit_order_payment,
)
from sales.services.order_plan_material_flow_service import create_material_flow_order_plan
from sales.test_v2_fixture import V2OrderPlanFixture
from workflow.models import WorkItem


class OrderFinanceRestoredTests(V2OrderPlanFixture):
    def setUp(self):
        super().setUp()
        self.private_dir = tempfile.TemporaryDirectory(prefix="pldp-finance-test-")
        self.addCleanup(self.private_dir.cleanup)
        previous_location = order_finance_private_storage._location
        order_finance_private_storage._location = self.private_dir.name
        order_finance_private_storage.__dict__.pop("base_location", None)
        order_finance_private_storage.__dict__.pop("location", None)
        def restore_storage():
            order_finance_private_storage._location = previous_location
            order_finance_private_storage.__dict__.pop("base_location", None)
            order_finance_private_storage.__dict__.pop("location", None)
        self.addCleanup(restore_storage)
        result, _ = create_material_flow_order_plan(
            user=self.sales_user, merchant_id=self.merchant.pk, client_request_id="finance-contract",
            items=[{"category_id": self.generator_category.pk, "product_model_id": self.generator.pk,
                    "quantity": 10, "use_return_inventory": False,
                    "source_bom_id": self.generator.boms.get(version="active-v2").pk}],
        )
        self.plan = OrderPlan.objects.get(pk=result["plan"]["id"])
        self.base_url = f"/pldp/api/sales/order-plans/{self.plan.pk}/finance/"

    def image(self, color="white"):
        buffer = io.BytesIO()
        Image.new("RGB", (16, 16), color).save(buffer, format="PNG")
        return SimpleUploadedFile("receipt.png", buffer.getvalue(), content_type="image/png")

    def statement(self, amount="100.00"):
        return submit_order_finance_statement(user=self.factory_assistant_1, plan_id=self.plan.pk,
                                              receivable_amount=amount, statement_file=self.image())

    def payment(self, request_id="payment-1"):
        return submit_order_payment(user=self.merchant_owner, plan_id=self.plan.pk,
                                    client_request_id=request_id, voucher_file=self.image())

    def confirm(self, payment_id, amount, user=None):
        return confirm_order_payment(user=user or self.factory_assistant_1, plan_id=self.plan.pk,
                                     payment_id=payment_id, confirmed_amount=amount)

    def test_creation_starts_finance_before_material_or_shipment_completion(self):
        self.assertTrue(OrderPlanFinance.objects.filter(order_plan=self.plan).exists())
        work = WorkItem.objects.get(type="ORDER_PLAN_FINANCE_STATEMENT", biz_ref=f"sales.OrderPlan:{self.plan.pk}")
        self.assertEqual(work.status, "OPEN")
        self.assertSetEqual(set(work.user_tasks.values_list("to_openid", flat=True)),
                            {self.factory_assistant_1.wx_openid, self.factory_assistant_2.wx_openid})
        work.user_tasks.update(state="READ", read_at=timezone.now())
        initialize_order_finance(self.plan)
        self.assertEqual(work.user_tasks.filter(state="READ").count(), 2)
        self.plan.refresh_from_db()
        self.assertIsNone(self.plan.shipped_at)
        self.assertIsNone(self.plan.accounted_at)

    def test_partial_then_overpayment_computes_totals_and_closes_peer_tasks(self):
        self.statement()
        result = self.payment()
        first = result["payments"][0]["id"]
        result = self.confirm(first, "40.00")
        self.assertEqual(result["received_amount"], "40.00")
        self.assertEqual(result["outstanding_amount"], "60.00")
        self.assertEqual(result["overpaid_amount"], "0.00")
        payment_task = WorkItem.objects.get(type="ORDER_PLAN_PAYMENT_UPLOAD", status="OPEN")
        self.assertEqual(payment_task.payload["sequence_no"], 2)
        self.assertSetEqual(set(payment_task.user_tasks.values_list("to_openid", flat=True)), {self.merchant_owner.wx_openid})
        result = self.payment("payment-2")
        second = result["payments"][1]["id"]
        result = self.confirm(second, "70.00", self.factory_assistant_2)
        self.assertEqual(result["received_amount"], "110.00")
        self.assertEqual(result["outstanding_amount"], "0.00")
        self.assertEqual(result["overpaid_amount"], "10.00")
        self.assertFalse(WorkItem.objects.filter(type="ORDER_PLAN_PAYMENT_UPLOAD", status="OPEN").exists())
        confirm_work = WorkItem.objects.get(type="ORDER_PLAN_PAYMENT_CONFIRM", payload__payment_id=second)
        self.assertFalse(confirm_work.user_tasks.filter(need_action=True).exists())
        self.assertEqual(confirm_work.user_tasks.get(to_openid=self.factory_assistant_1.wx_openid).done_reason, "OTHER_ACTION")
        self.plan.refresh_from_db()
        self.assertIsNone(self.plan.accounted_at)

    def test_same_requests_are_idempotent_and_conflicting_retries_fail(self):
        self.statement()
        self.statement()
        with self.assertRaises(OrderFinanceError):
            self.statement("101.00")
        first = self.payment()["payments"][0]["id"]
        self.payment()
        self.assertEqual(OrderPlanPayment.objects.count(), 1)
        with self.assertRaises(OrderFinanceError):
            submit_order_payment(user=self.merchant_owner, plan_id=self.plan.pk,
                                 client_request_id="payment-1", voucher_file=self.image("black"))
        with self.assertRaises(OrderFinanceError):
            self.payment("different-request-same-pending")
        self.confirm(first, "100")
        self.confirm(first, "100.00")
        with self.assertRaises(OrderFinanceError):
            self.confirm(first, "99")
        self.assertEqual(OrderPlanPayment.objects.get(pk=first).confirmed_amount, Decimal("100.00"))
        self.assertEqual(WorkItem.objects.filter(type="ORDER_PLAN_PAYMENT_CONFIRM").count(), 1)

    def test_only_own_merchant_owner_uploads_and_only_own_assistant_confirms(self):
        self.statement()
        for user in (self.merchant_sales, self.merchant_stock_1, self.other_merchant_owner, self.factory_assistant_1):
            with self.subTest(user=user.username), self.assertRaises(OrderFinanceError):
                submit_order_payment(user=user, plan_id=self.plan.pk, client_request_id="forbidden", voucher_file=self.image())
        first = self.payment()["payments"][0]["id"]
        for user in (self.merchant_owner, self.factory_stock, self.sales_user):
            with self.subTest(user=user.username), self.assertRaises(OrderFinanceError):
                self.confirm(first, "100", user)
        original_org = self.factory_assistant_1.organization_identity.organization
        identity = self.factory_assistant_1.organization_identity
        identity.organization = self.other_merchant
        identity.save(update_fields=["organization"])
        with self.assertRaises(OrderFinanceError):
            self.confirm(first, "100")
        identity.organization = original_org
        identity.save(update_fields=["organization"])

    def test_invalid_money_and_non_images_leave_no_facts_or_extra_tasks(self):
        for value in ("0", "-1", "NaN", "Infinity", "1e2", "1.001", "1000000000000", "True", "", None):
            with self.subTest(value=value), self.assertRaises(OrderFinanceError):
                self.statement(value)
        with self.assertRaises(OrderFinanceError):
            submit_order_finance_statement(user=self.factory_assistant_1, plan_id=self.plan.pk,
                receivable_amount="100", statement_file=SimpleUploadedFile("fake.png", b"<html>hello</html>", content_type="image/png"))
        finance = OrderPlanFinance.objects.get(order_plan=self.plan)
        self.assertIsNone(finance.receivable_amount)
        self.assertFalse(finance.statement_image)
        self.assertFalse(WorkItem.objects.filter(type="ORDER_PLAN_PAYMENT_UPLOAD").exists())

    def test_cancelled_order_has_audit_but_no_new_actions(self):
        self.statement()
        first = self.payment()["payments"][0]["id"]
        self.plan.status = OrderPlan.Status.CANCELLED
        self.plan.save(update_fields=["status"])
        cancel_order_finance_tasks(self.plan, self.sales_user)
        with self.assertRaises(OrderFinanceError):
            self.confirm(first, "100")
        data = serialize_order_finance(user=self.merchant_owner, plan=self.plan)
        self.assertFalse(data["can_upload_payment"])
        self.assertEqual(len(data["payments"]), 1)
        self.assertFalse(WorkItem.objects.filter(type__in=["ORDER_PLAN_PAYMENT_UPLOAD", "ORDER_PLAN_PAYMENT_CONFIRM"], status="OPEN").exists())

    def test_authenticated_multipart_json_and_private_document_routes(self):
        response = self.client.post(self.base_url + "statement/", {"file": self.image(), "receivable_amount": "100.00"},
                                    HTTP_AUTHORIZATION=self.factory_assistant_1_authorization)
        self.assertEqual(response.json()["code"], 0, response.content)
        statement_url = response.json()["data"]["statement_url"]
        self.assertEqual(self.client.get(statement_url).status_code, 401)
        denied = self.client.get(statement_url, HTTP_AUTHORIZATION=self.merchant_sales_authorization)
        self.assertEqual(denied.status_code, 403)
        allowed = self.client.get(statement_url, HTTP_AUTHORIZATION=self.merchant_owner_authorization)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed["Cache-Control"], "private, no-store")
        self.assertEqual(allowed["Content-Type"], "image/png")
        self.assertTrue(b"".join(allowed.streaming_content).startswith(b"\x89PNG"))
        response = self.client.post(self.base_url + "payments/", {"file": self.image(), "client_request_id": "api-pay-1"},
                                    HTTP_AUTHORIZATION=self.merchant_owner_authorization)
        self.assertEqual(response.json()["code"], 0)
        payment_id = response.json()["data"]["payments"][0]["id"]
        response = self.client.post(self.base_url + f"payments/{payment_id}/confirm/", json.dumps({"confirmed_amount": "75.12"}),
                                    content_type="application/json", HTTP_AUTHORIZATION=self.factory_assistant_1_authorization)
        self.assertEqual(response.json()["code"], 0)
        self.assertEqual(response.json()["data"]["outstanding_amount"], "24.88")

    def test_repair_command_does_not_reset_read_tasks_or_touch_accounted_cancelled(self):
        work = WorkItem.objects.get(type="ORDER_PLAN_FINANCE_STATEMENT")
        work.user_tasks.update(state="READ", read_at=timezone.now())
        out = io.StringIO()
        call_command("restore_order_finance_tasks", stdout=out)
        call_command("restore_order_finance_tasks", stdout=out)
        self.assertEqual(WorkItem.objects.filter(type="ORDER_PLAN_FINANCE_STATEMENT").count(), 1)
        self.assertEqual(work.user_tasks.filter(state="READ").count(), 2)
        OrderPlanFinance.objects.filter(order_plan=self.plan).delete()
        work.delete()
        call_command("restore_order_finance_tasks", check_only=True, stdout=out)
        self.assertFalse(OrderPlanFinance.objects.filter(order_plan=self.plan).exists())
        call_command("restore_order_finance_tasks", stdout=out)
        self.assertTrue(OrderPlanFinance.objects.filter(order_plan=self.plan).exists())
        OrderPlanFinance.objects.filter(order_plan=self.plan).delete()
        WorkItem.objects.filter(type="ORDER_PLAN_FINANCE_STATEMENT").delete()
        self.plan.accounted_at = timezone.now()
        self.plan.save(update_fields=["accounted_at"])
        call_command("restore_order_finance_tasks", stdout=out)
        self.assertFalse(OrderPlanFinance.objects.filter(order_plan=self.plan).exists())
        self.plan.accounted_at = None
        self.plan.status = OrderPlan.Status.CANCELLED
        self.plan.save(update_fields=["accounted_at", "status"])
        call_command("restore_order_finance_tasks", stdout=out)
        self.assertFalse(OrderPlanFinance.objects.filter(order_plan=self.plan).exists())
