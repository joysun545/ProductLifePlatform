"""Receipt ends a batch's merchant notice, while independent work stays live."""

import io
import tempfile
from datetime import timedelta
from unittest.mock import patch

from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.utils import timezone
from PIL import Image

from lifecycle.models import DeviceState, LifecycleEvent, OrganizationDevice
from sales import test_shipment_workflow_v2 as shipment_fixture
from sales.models import OrderPlan, OrderPlanFinance, OrderPlanPayment, OrderPlanShipment
from sales.services import order_plan_material_flow_service as flow
from sales.services.order_finance_service import (
    confirm_order_payment, submit_order_finance_statement, submit_order_payment,
)
from sales.services.shipment_receipt_notice_service import close_received_shipment_notices
from sales.test_v2_fixture import V2OrderPlanFixture
from workflow.models import UserTask, WorkItem
from workflow.services.task_query import get_task_list, get_task_summary


class ReceivedShipmentNoticeTests(V2OrderPlanFixture):
    # Reuse production-through-receipt helpers without inheriting their tests.
    setUp = shipment_fixture.ShipmentWorkflowV2Tests.setUp
    _create_lot = shipment_fixture.ShipmentWorkflowV2Tests._create_lot
    _create_v2_plan = shipment_fixture.ShipmentWorkflowV2Tests._create_v2_plan
    _ready_production_shipment = shipment_fixture.ShipmentWorkflowV2Tests._ready_production_shipment
    _printed_shipment = shipment_fixture.ShipmentWorkflowV2Tests._printed_shipment
    _print = shipment_fixture.ShipmentWorkflowV2Tests._print
    _split_shipments = shipment_fixture.ShipmentWorkflowV2Tests._split_shipments
    _logistics = shipment_fixture.ShipmentWorkflowV2Tests._logistics
    _batch_logistics = shipment_fixture.ShipmentWorkflowV2Tests._batch_logistics
    _ship = shipment_fixture.ShipmentWorkflowV2Tests._ship
    _receipt = shipment_fixture.ShipmentWorkflowV2Tests._receipt
    _workitem = shipment_fixture.ShipmentWorkflowV2Tests._workitem

    def _merchant_users(self):
        return (self.merchant_owner, self.merchant_sales,
                self.merchant_stock_1, self.merchant_stock_2)

    def _merchant_notices(self, plan, shipment):
        return self._workitem(plan, shipment, "shipped-notice").user_tasks.filter(
            to_openid__in=[user.wx_openid for user in self._merchant_users()],
        )

    def _shipped(self, request_id):
        plan, _, shipment = self._printed_shipment(request_id=request_id)
        self._logistics(plan, shipment)
        self._ship(plan, shipment)
        shipment.refresh_from_db()
        return plan, shipment

    def _legacy_received(self, request_id):
        plan, shipment = self._shipped(request_id)
        self._receipt(plan, shipment)
        shipment.refresh_from_db()
        # Reproduce historical rows created before receipt closed notices.
        self._merchant_notices(plan, shipment).update(
            state="NEW", read_at=None, done_at=None, done_reason="",
        )
        return plan, shipment

    def _snapshot(self, *models):
        return {model._meta.label: list(model.objects.order_by("pk").values())
                for model in models}

    def _assert_task_summary(self, user, expected):
        summary = get_task_summary(user.wx_openid)
        self.assertRegex(summary.pop("revision"), r"^[a-f0-9]{64}$")
        self.assertEqual(summary, expected)

    def _assert_no_attention(self, user):
        self._assert_task_summary(user, {
            "unread_count": 0, "action_pending_count": 0, "attention_count": 0,
        })

    def _partial_payment(self, plan):
        directory = tempfile.TemporaryDirectory(prefix="pldp-received-finance-")
        self.addCleanup(directory.cleanup)
        storage = FileSystemStorage(location=directory.name)
        for model, field_name in ((OrderPlanFinance, "statement_image"),
                                  (OrderPlanPayment, "voucher_image")):
            storage_patch = patch.object(model._meta.get_field(field_name), "storage", storage)
            storage_patch.start()
            self.addCleanup(storage_patch.stop)

        def uploaded_image():
            buffer = io.BytesIO()
            Image.new("RGB", (4, 4), color="white").save(buffer, format="PNG")
            return SimpleUploadedFile("payment.png", buffer.getvalue(), content_type="image/png")

        submit_order_finance_statement(
            user=self.factory_assistant_1, plan_id=plan.pk,
            receivable_amount="18900.00", statement_file=uploaded_image(),
        )
        data = submit_order_payment(
            user=self.merchant_owner, plan_id=plan.pk,
            client_request_id=f"partial-payment-{plan.pk}", voucher_file=uploaded_image(),
        )
        data = confirm_order_payment(
            user=self.factory_assistant_1, plan_id=plan.pk,
            payment_id=data["payments"][0]["id"], confirmed_amount="1500.00",
        )
        self.assertEqual(data["received_amount"], "1500.00")
        self.assertEqual(data["outstanding_amount"], "17400.00")
        return WorkItem.objects.get(
            biz_ref=f"sales.OrderPlan:{plan.pk}", type="ORDER_PLAN_PAYMENT_UPLOAD", status="OPEN",
        )

    def test_split_receipt_closes_all_merchant_roles_one_batch_at_a_time(self):
        plan, first, second = self._split_shipments(request_id="notice-split")
        self._batch_logistics(plan, [first, second])
        for shipment in (first, second):
            self._ship(plan, shipment)
            self.assertEqual(self._merchant_notices(plan, shipment).filter(state="NEW").count(), 4)

        factory_notices = UserTask.objects.filter(
            to_openid=self.sales_user.wx_openid,
            workitem__type="ORDER_PLAN_V2_SHIPPED_NOTICE",
        )
        factory_before = list(factory_notices.order_by("pk").values())
        read_at = timezone.now() - timedelta(minutes=2)
        self._merchant_notices(plan, first).filter(
            to_openid=self.merchant_stock_2.wx_openid,
        ).update(state="READ", read_at=read_at)

        self._receipt(plan, first, self.merchant_stock_1)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, OrderPlanShipment.Status.RECEIVED)
        self.assertEqual(second.status, OrderPlanShipment.Status.SHIPPED)
        self.assertEqual(self._merchant_notices(plan, first).filter(state="DONE").count(), 4)
        self.assertEqual(self._merchant_notices(plan, second).filter(state="NEW").count(), 4)
        for user in self._merchant_users():
            expected_summary = (
                {"unread_count": 1, "action_pending_count": 0, "attention_count": 1}
                if user == self.merchant_sales
                else {"unread_count": 1, "action_pending_count": 1, "attention_count": 1}
            )
            self._assert_task_summary(user, expected_summary)
        self.assertFalse(WorkItem.objects.filter(type="ORDER_PLAN_V2_ACCOUNTING_CONFIRM").exists())
        with self.assertRaises(flow.OrderPlanMaterialFlowError):
            flow.confirm_material_flow_accounting(user=self.factory_assistant_1, plan_id=plan.pk)

        self._receipt(plan, second, self.merchant_stock_1)
        for user in self._merchant_users():
            self._assert_no_attention(user)
        self.assertFalse(UserTask.objects.filter(
            workitem__type="ORDER_PLAN_V2_MERCHANT_RECEIPT",
        ).exclude(state="DONE").exists())
        first_notice_rows = self._merchant_notices(plan, first)
        self.assertEqual(first_notice_rows.get(to_openid=self.merchant_stock_2.wx_openid).read_at, read_at)
        self.assertIsNone(first_notice_rows.get(to_openid=self.merchant_owner.wx_openid).read_at)
        self.assertEqual(list(factory_notices.order_by("pk").values()), factory_before)
        self.assertEqual(factory_notices.filter(state="NEW").count(), 2)
        self.assertEqual(OrganizationDevice.objects.filter(organization=self.merchant, status="IN_STOCK").count(), 5)
        _, changed = flow.confirm_material_flow_accounting(user=self.factory_assistant_1, plan_id=plan.pk)
        self.assertTrue(changed)
        plan.refresh_from_db()
        self.assertIsNotNone(plan.accounted_at)

    def test_partial_payment_stays_owner_only_and_card_names_pending_payment(self):
        plan, shipment = self._shipped("notice-independent-finance")
        payment_work = self._partial_payment(plan)
        self.assertEqual(payment_work.payload["sequence_no"], 2)
        self.assertEqual(list(payment_work.user_tasks.values_list("to_openid", flat=True)),
                         [self.merchant_owner.wx_openid])
        payment_task = payment_work.user_tasks.get()
        payment_task.state = "READ"
        payment_task.read_at = timezone.now() - timedelta(minutes=1)
        payment_task.save(update_fields=["state", "read_at"])
        # An unread notice is newer than the real next action; it must not
        # overwrite either the grouped title or the row opened by that card.
        self._merchant_notices(plan, shipment).update(cursor_time=timezone.now() + timedelta(minutes=1))
        card = get_task_list(self.merchant_owner.wx_openid)["items"][0]
        self.assertIn("第2笔", card["title"].replace(" ", ""))
        self.assertNotIn("已发货", card["title"])
        self.assertEqual(card["entry_task_id"], payment_task.pk)

        finance_before = self._snapshot(OrderPlanFinance, OrderPlanPayment)
        payment_before = list(payment_work.user_tasks.values())
        self._receipt(plan, shipment, self.merchant_stock_2)
        self.assertEqual(self._snapshot(OrderPlanFinance, OrderPlanPayment), finance_before)
        self.assertEqual(list(payment_work.user_tasks.values()), payment_before)
        payment_work.refresh_from_db()
        self.assertEqual(payment_work.status, "OPEN")
        for user in (self.merchant_sales, self.merchant_stock_1, self.merchant_stock_2):
            self._assert_no_attention(user)
        self._assert_task_summary(self.merchant_owner, {
            "unread_count": 0, "action_pending_count": 1, "attention_count": 1,
        })
        card = get_task_list(self.merchant_owner.wx_openid)["items"][0]
        self.assertIn("第2笔", card["title"].replace(" ", ""))
        self.assertEqual(card["entry_task_id"], payment_task.pk)
        self.assertTrue(WorkItem.objects.filter(type="ORDER_PLAN_V2_ACCOUNTING_CONFIRM").exists())

    def test_backfill_default_and_check_only_leave_all_facts_unchanged(self):
        plan, shipment = self._legacy_received("notice-dry-run")
        self._partial_payment(plan)
        before = self._snapshot(
            UserTask, WorkItem, OrderPlan, OrderPlanShipment, OrderPlanFinance,
            OrderPlanPayment, DeviceState, OrganizationDevice, LifecycleEvent,
        )
        for options in ({}, {"check_only": True}, {"check_only": True, "plan_id": plan.pk}):
            with self.subTest(options=options):
                output = io.StringIO()
                call_command("close_received_shipment_notices", stdout=output, **options)
                self.assertIn("RECEIVED_NOTICE_CHECK count=4", output.getvalue())
                self.assertEqual(self._snapshot(
                    UserTask, WorkItem, OrderPlan, OrderPlanShipment, OrderPlanFinance,
                    OrderPlanPayment, DeviceState, OrganizationDevice, LifecycleEvent,
                ), before)

    def test_backfill_plan_scope_idempotence_preserves_other_order_and_finance(self):
        plan, shipment = self._legacy_received("notice-backfill-selected")
        other_plan, other_shipment = self._legacy_received("notice-backfill-other")
        transit_plan, transit_shipment = self._shipped("notice-backfill-in-transit")
        self._partial_payment(plan)
        read_at = timezone.now() - timedelta(days=1)
        self._merchant_notices(plan, shipment).filter(
            to_openid=self.merchant_sales.wx_openid,
        ).update(state="READ", read_at=read_at)
        selected_ids = list(self._merchant_notices(plan, shipment).values_list("pk", flat=True))
        unchanged_before = list(UserTask.objects.exclude(pk__in=selected_ids).order_by("pk").values())
        facts_before = self._snapshot(
            WorkItem, OrderPlan, OrderPlanShipment, OrderPlanFinance, OrderPlanPayment,
            DeviceState, OrganizationDevice, LifecycleEvent,
        )
        output = io.StringIO()
        call_command("close_received_shipment_notices", apply=True, plan_id=plan.pk, stdout=output)
        self.assertIn("RECEIVED_NOTICE_APPLIED count=4", output.getvalue())
        self.assertEqual(self._merchant_notices(plan, shipment).filter(state="DONE").count(), 4)
        self.assertEqual(self._merchant_notices(plan, shipment).get(to_openid=self.merchant_sales.wx_openid).read_at, read_at)
        self.assertIsNone(self._merchant_notices(plan, shipment).get(to_openid=self.merchant_owner.wx_openid).read_at)
        self.assertEqual(list(UserTask.objects.exclude(pk__in=selected_ids).order_by("pk").values()), unchanged_before)
        self.assertEqual(self._snapshot(
            WorkItem, OrderPlan, OrderPlanShipment, OrderPlanFinance, OrderPlanPayment,
            DeviceState, OrganizationDevice, LifecycleEvent,
        ), facts_before)

        after = self._snapshot(UserTask, WorkItem, OrderPlanShipment)
        output = io.StringIO()
        call_command("close_received_shipment_notices", apply=True, plan_id=plan.pk, stdout=output)
        self.assertIn("RECEIVED_NOTICE_APPLIED count=0", output.getvalue())
        self.assertEqual(self._snapshot(UserTask, WorkItem, OrderPlanShipment), after)
        output = io.StringIO()
        call_command("close_received_shipment_notices", apply=True, stdout=output)
        self.assertIn("RECEIVED_NOTICE_APPLIED count=4", output.getvalue())
        self.assertEqual(self._merchant_notices(other_plan, other_shipment).filter(state="DONE").count(), 4)
        self.assertEqual(self._merchant_notices(transit_plan, transit_shipment).filter(state="NEW").count(), 4)

    def test_backfill_preserves_completed_rows_actions_and_people_outside_receipt_roster(self):
        plan, shipment = self._legacy_received("notice-roster-scope")
        notices = self._merchant_notices(plan, shipment)
        completed = notices.get(to_openid=self.merchant_stock_1.wx_openid)
        old_time = timezone.now() - timedelta(hours=1)
        UserTask.objects.filter(pk=completed.pk).update(
            state="DONE", read_at=old_time, done_at=old_time, done_reason="OPENED",
        )
        action = notices.get(to_openid=self.merchant_stock_2.wx_openid)
        UserTask.objects.filter(pk=action.pk).update(need_action=True)
        notice_work = self._workitem(plan, shipment, "shipped-notice")
        outsider = UserTask.objects.create(
            workitem=notice_work, to_openid=self.other_merchant_owner.wx_openid,
            to_role_snapshot="merchant_owner", state="NEW", need_action=False,
        )
        protected_ids = [completed.pk, action.pk, outsider.pk]
        protected_before = list(UserTask.objects.filter(pk__in=protected_ids).order_by("pk").values())
        self.assertEqual(close_received_shipment_notices(shipment), 2)
        self.assertEqual(list(UserTask.objects.filter(pk__in=protected_ids).order_by("pk").values()), protected_before)
        self.assertEqual(notices.get(to_openid=self.merchant_owner.wx_openid).state, "DONE")
        self.assertEqual(notices.get(to_openid=self.merchant_sales.wx_openid).state, "DONE")
        self.assertEqual(notice_work.user_tasks.get(to_openid=self.sales_user.wx_openid).state, "NEW")

    def test_notice_backfill_requires_completed_receipt_status_and_timestamp(self):
        plan, shipment = self._shipped("notice-unreceived")
        before = self._snapshot(UserTask, WorkItem)
        self.assertEqual(close_received_shipment_notices(shipment), 0)
        # A contradictory legacy state must not be used as proof of receipt.
        OrderPlanShipment.objects.filter(pk=shipment.pk).update(status="RECEIVED", received_at=None)
        shipment.refresh_from_db()
        self.assertEqual(close_received_shipment_notices(shipment), 0)
        output = io.StringIO()
        call_command("close_received_shipment_notices", apply=True, stdout=output)
        self.assertIn("RECEIVED_NOTICE_APPLIED count=0", output.getvalue())
        self.assertEqual(self._snapshot(UserTask, WorkItem), before)

    def test_receipt_rolls_back_inventory_tasks_and_status_if_notice_closure_fails(self):
        plan, shipment = self._shipped("notice-atomic-receipt")
        models = (UserTask, WorkItem, OrderPlan, OrderPlanShipment,
                  DeviceState, OrganizationDevice, LifecycleEvent)
        before = self._snapshot(*models)
        with patch(
            "sales.services.shipment_receipt_notice_service.close_received_shipment_notices",
            side_effect=RuntimeError("simulated notice write failure"),
        ), self.assertRaisesRegex(RuntimeError, "simulated notice write failure"):
            self._receipt(plan, shipment, self.merchant_stock_1)
        self.assertEqual(self._snapshot(*models), before)
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, OrderPlanShipment.Status.SHIPPED)
        self.assertIsNone(shipment.received_at)
        self.assertFalse(OrganizationDevice.objects.filter(organization=self.merchant).exists())
        self.assertFalse(WorkItem.objects.filter(type="ORDER_PLAN_V2_ACCOUNTING_CONFIRM").exists())

