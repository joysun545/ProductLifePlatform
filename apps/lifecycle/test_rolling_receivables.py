"""The live merchant workflow preserves historical records without new debt."""

from decimal import Decimal
from io import StringIO
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from lifecycle import test_goods_transfer as fixtures
from lifecycle.models import GoodsTransferPayment, GoodsTransferPaymentAllocation, GoodsTransferSettlement
from lifecycle.private_storage import goods_transfer_private_storage
from workflow.models import UserTask, WorkItem
from workflow.services.task_query import get_task_list


class MerchantReceivableHistoryTests(TestCase):
    endpoint = fixtures.GoodsTransferApiTests.endpoint
    setUpTestData = classmethod(fixtures.GoodsTransferApiTests.setUpTestData.__func__)
    _user = classmethod(fixtures.GoodsTransferApiTests._user.__func__)
    _relation = classmethod(fixtures.GoodsTransferApiTests._relation.__func__)
    auth = fixtures.GoodsTransferApiTests.auth
    set_owner = fixtures.GoodsTransferApiTests.set_owner
    create_flow = fixtures.GoodsTransferApiTests.create_flow
    scan_and_submit = fixtures.GoodsTransferApiTests.scan_and_submit
    submit_transfer = fixtures.GoodsTransferApiTests.submit_transfer
    image_file = fixtures.GoodsTransferApiTests.image_file

    def setUp(self):
        directory = TemporaryDirectory(prefix="merchant-receivables-history-")
        self.addCleanup(directory.cleanup)
        for field in ("location", "base_location"):
            storage = patch.object(goods_transfer_private_storage, field, directory.name)
            storage.start()
            self.addCleanup(storage.stop)

    def shipment(self, device, request_id):
        self.set_owner(device, self.parent)
        flow = self.create_flow(self.parent_owner, {
            "flow_type": "SHIPMENT", "client_request_id": request_id,
            "target_account_id": self.child_b_owner.pk,
        })
        self.scan_and_submit(self.parent_owner, flow["id"], device)
        return flow

    def test_old_debt_stays_out_of_new_document_only_workflow(self):
        old = self.shipment(self.devices[0], "archive-old")
        old_id = old["id"]
        settlement = GoodsTransferSettlement.objects.get(transfer_id=old_id)
        GoodsTransferSettlement.objects.filter(pk=settlement.pk).update(
            receivable_amount=Decimal("1000"), statement_submitted_at=timezone.now(),
        )
        payment = GoodsTransferPayment.objects.create(
            settlement=settlement, sequence_no=1, client_request_id="archived-proof",
            voucher_image="archive/proof.png", uploaded_by=self.child_b_owner,
            status="CONFIRMED", confirmed_amount=Decimal("400"),
        )
        workitem = WorkItem.objects.create(
            domain="LOGISTICS", type="GOODS_TRANSFER_PAYMENT_UPLOAD",
            uniq_key="archive-payment", biz_id=old["flow_no"],
            biz_ref=f"lifecycle.GoodsTransfer:{old_id}", title="历史付款待办",
        )
        task = UserTask.objects.create(
            workitem=workitem, to_openid=self.child_b_owner.wx_openid, need_action=True,
        )

        new = self.shipment(self.devices[1], "archive-new")
        response = self.client.get(
            f"{self.endpoint}{new['id']}/settlement/", **self.auth(self.child_b_owner),
        )
        self.assertEqual(response.json()["code"], 0, response.json())
        detail = response.json()["data"]
        self.assertFalse(detail["financial_enabled"])
        self.assertFalse(detail["rolling_receivables"]["enabled"])
        self.assertIsNone(detail["receivable_amount"])
        self.assertEqual(detail["payments"], [])

        output = StringIO()
        call_command("reconcile_goods_transfer_receivables", apply=True, stdout=output)
        self.assertIn("MERCHANT_DOWNSTREAM_DOCUMENTS_ONLY", output.getvalue())
        cards = get_task_list(self.child_b_owner.wx_openid)["items"]
        self.assertNotIn(task.pk, {card["entry_task_id"] for card in cards})
        self.assertEqual(WorkItem.objects.filter(type__startswith="GOODS_TRANSFER_PAYMENT_").count(), 1)
        settlement.refresh_from_db()
        payment.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(settlement.receivable_amount, Decimal("1000"))
        self.assertEqual(payment.confirmed_amount, Decimal("400"))
        self.assertEqual(task.state, "NEW")
        self.assertFalse(GoodsTransferPaymentAllocation.objects.exists())
