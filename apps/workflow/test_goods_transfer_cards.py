"""One personal inbox card for one physical goods flow, including its money."""

from datetime import timedelta
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from account.models import Organization, OrganizationType, User
from account.services.auth_service import issue_access_token
from lifecycle import test_goods_transfer as _flow_fixtures
from lifecycle.models import GoodsTransfer, GoodsTransferSettlement, OrganizationDevice
from lifecycle.private_storage import goods_transfer_private_storage
from workflow.models import UserTask, WorkItem
from workflow.services.task_action import TaskActionError, open_task
from workflow.services.task_query import get_task_group, get_task_list, get_task_summary


class GoodsTransferCardProjectionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="card-owner", wx_openid="card-owner-wx")
        self.peer = User.objects.create_user(username="card-peer", wx_openid="card-peer-wx")
        merchant_type = OrganizationType.objects.get(code="MERCHANT")
        self.parent = Organization.objects.create(name="卡片上家", code="CARD-PARENT", organization_type=merchant_type)
        self.child = Organization.objects.create(name="卡片下家", code="CARD-CHILD", organization_type=merchant_type)
        self.transfer = self.make_transfer("CARD-001")
        self.second = self.make_transfer("CARD-002")
        self.sequence = 0

    def make_transfer(self, suffix):
        return GoodsTransfer.objects.create(
            flow_no=suffix,
            client_request_id=suffix,
            flow_type="SHIPMENT",
            instruction_organization=self.parent,
            from_organization=self.parent,
            to_organization=self.child,
            target_account=self.peer,
            initiated_by=self.user,
            status="PENDING_RECEIPT",
        )

    def task(self, *, transfer=None, user=None, state="NEW", need_action=False,
             task_type="GOODS_TRANSFER_RECEIPT_CONFIRM", title="商品待入库",
             payload=None, biz_ref=None, link=None, uniq_key=None, biz_id=None,
             badge_group="MY"):
        self.sequence += 1
        transfer = transfer or self.transfer
        now = timezone.now() + timedelta(seconds=self.sequence)
        workitem = WorkItem.objects.create(
            domain="LOGISTICS", type=task_type,
            biz_id=transfer.flow_no if biz_id is None else biz_id,
            biz_ref=f"lifecycle.GoodsTransfer:{transfer.pk}" if biz_ref is None else biz_ref,
            payload={"goods_transfer_id": transfer.pk} if payload is None else payload,
            link=(f"/pages/stock/goods_transfer_detail/goods_transfer_detail?transfer_id={transfer.pk}"
                  if link is None else link),
            uniq_key=uniq_key or f"card-test-{self.sequence}",
            title=title, summary=f"{transfer.flow_no}｜下家", last_activity_at=now,
            action_mode="ANYONE_DO" if need_action else "EACH_ACK",
            status="DONE" if state == "DONE" else "OPEN",
        )
        return UserTask.objects.create(
            workitem=workitem, to_openid=(user or self.user).wx_openid,
            state=state, need_action=need_action, cursor_time=now,
            badge_group=badge_group,
        )

    def cards(self, user=None):
        return get_task_list((user or self.user).wx_openid)["items"]

    def test_new_events_update_stable_card_and_pending_action_wins_notice(self):
        first = self.task(state="DONE", title="货品清单和物流单据已提交")
        initial = self.cards()[0]
        pending = self.task(
            need_action=True, state="READ", task_type="GOODS_TRANSFER_PAYMENT_CONFIRM",
            title="第1笔商品收款待确认",
            link=f"/pages/stock/goods_transfer_settlement/goods_transfer_settlement?transfer_id={self.transfer.pk}",
        )
        notice = self.task(task_type="GOODS_TRANSFER_NOTICE", title="收货方已确认商品入库")
        before_tasks = list(UserTask.objects.order_by("pk").values())
        before_workitems = list(WorkItem.objects.order_by("pk").values())
        cards = self.cards()
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(card["group_kind"], "GOODS_TRANSFER")
        self.assertEqual(card["group_id"], initial["group_id"])
        self.assertEqual(card["card_id"], initial["card_id"])
        self.assertEqual(card["entry_task_id"], pending.pk)
        self.assertIn("收款待确认", card["title"])
        self.assertIn("goods_transfer_settlement", card["link"])
        self.assertEqual(card["child_count"], 3)
        self.assertEqual(card["unread_count"], 1)
        self.assertEqual(card["action_pending_count"], 1)
        details = get_task_group(self.user.wx_openid, card["group_id"])
        self.assertSetEqual({row["id"] for row in details["items"]}, {first.pk, pending.pk, notice.pk})
        self.assertEqual(list(UserTask.objects.order_by("pk").values()), before_tasks)
        self.assertEqual(list(WorkItem.objects.order_by("pk").values()), before_workitems)

    def test_two_unread_operations_count_as_one_attention_but_remain_executable(self):
        receipt = self.task(need_action=True)
        payment = self.task(need_action=True, task_type="GOODS_TRANSFER_PAYMENT_UPLOAD", title="上传付款凭证")
        summary = get_task_summary(self.user.wx_openid)
        self.assertEqual(summary["unread_count"], 1)
        self.assertEqual(summary["action_pending_count"], 1)
        self.assertEqual(summary["attention_count"], 1)
        card = self.cards()[0]
        self.assertEqual(card["child_unread_count"], 2)
        self.assertEqual(card["child_action_pending_count"], 2)
        open_task(self.user.wx_openid, receipt.pk)
        for task in (receipt, payment):
            task.refresh_from_db()
            task.workitem.refresh_from_db()
            self.assertEqual(task.state, "READ")
            self.assertTrue(task.need_action)
            self.assertIsNone(task.done_at)
            self.assertEqual(task.workitem.status, "OPEN")
        self.assertEqual(get_task_summary(self.user.wx_openid)["action_pending_count"], 1)

    def test_distinct_transfers_to_same_counterparty_stay_separate(self):
        for transfer in (self.transfer, self.second):
            self.task(transfer=transfer, need_action=True)
            self.task(transfer=transfer, task_type="GOODS_TRANSFER_PAYMENT_UPLOAD", need_action=True)
        cards = self.cards()
        self.assertEqual(len(cards), 2)
        self.assertEqual(len({card["group_id"] for card in cards}), 2)
        self.assertEqual([card["child_count"] for card in cards], [2, 2])
        self.assertEqual(get_task_summary(self.user.wx_openid)["attention_count"], 2)

    def test_historical_references_aggregate_without_rewriting_history(self):
        settlement = GoodsTransferSettlement.objects.create(transfer=self.transfer)
        rows = [self.task()]
        for kwargs in (
            {"biz_ref": f"lifecycle.GoodsTransfer:{self.transfer.pk}"},
            {"link": f"/pages/stock/goods_transfer_detail/goods_transfer_detail?transfer_id={self.transfer.pk}"},
            {"uniq_key": f"lifecycle:goods-transfer:{self.transfer.pk}:notice:legacy"},
            {"biz_id": self.transfer.flow_no},
            {"payload": {"flow_no": self.transfer.flow_no}},
            {"payload": {"settlement_id": settlement.pk}},
        ):
            values = dict(payload={}, biz_ref="", link="", biz_id="unrelated-legacy-id")
            values.update(kwargs)
            rows.append(self.task(**values))
        snapshot = list(WorkItem.objects.order_by("pk").values())
        cards = self.cards()
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["child_count"], len(rows))
        self.assertEqual(list(WorkItem.objects.order_by("pk").values()), snapshot)

    def test_conflicting_and_unresolved_references_never_merge(self):
        self.task()
        self.task(
            payload={"goods_transfer_id": self.transfer.pk},
            biz_ref=f"lifecycle.GoodsTransfer:{self.second.pk}", link="",
        )
        for unused in range(2):
            self.task(payload={}, biz_ref="", biz_id="same-unknown-counterparty", link="")
        cards = self.cards()
        self.assertEqual(len(cards), 4)
        self.assertTrue(all(card["child_count"] == 1 for card in cards))

    def test_other_domains_with_same_transfer_reference_are_not_goods_cards(self):
        self.task()
        self.task(task_type="FACTORY_RETURN_LOGISTICS_PICKUP", payload={"goods_transfer_id": self.transfer.pk, "return_flow_no": self.transfer.flow_no})
        self.task(task_type="ORDER_PLAN_MERCHANT_RECEIPT", payload={"goods_transfer_id": self.transfer.pk, "inbox_group_kind": "ORDER_PLAN", "inbox_group_key": "order-plan:test"})
        cards = self.cards()
        self.assertEqual(len(cards), 3)
        self.assertEqual(sum(card["group_kind"] == "GOODS_TRANSFER" for card in cards), 1)

    def test_opening_completed_old_card_acknowledges_only_own_flow_updates(self):
        old = self.task(state="DONE", title="货品清单和物流单据已提交")
        notice = self.task(task_type="GOODS_TRANSFER_NOTICE")
        pending = self.task(need_action=True, task_type="GOODS_TRANSFER_PAYMENT_CONFIRM",
                            link=f"/pages/stock/goods_transfer_settlement/goods_transfer_settlement?transfer_id={self.transfer.pk}")
        peer_notice = UserTask.objects.create(workitem=notice.workitem, to_openid=self.peer.wx_openid)
        peer_pending = UserTask.objects.create(workitem=pending.workitem, to_openid=self.peer.wx_openid, need_action=True)
        other_flow = self.task(transfer=self.second, need_action=True)
        hidden = self.task(badge_group="OTHER")
        untouched_ids = [peer_notice.pk, peer_pending.pk, other_flow.pk, hidden.pk]
        untouched = list(UserTask.objects.filter(pk__in=untouched_ids).order_by("pk").values())
        mother_snapshot = list(WorkItem.objects.order_by("pk").values())
        result = open_task(self.user.wx_openid, old.pk)
        notice.refresh_from_db()
        pending.refresh_from_db()
        old.refresh_from_db()
        self.assertEqual(old.state, "DONE")
        self.assertEqual(notice.state, "DONE")
        self.assertIsNotNone(notice.read_at)
        self.assertIsNotNone(notice.done_at)
        self.assertEqual(pending.state, "READ")
        self.assertTrue(pending.need_action)
        self.assertIsNone(pending.done_at)
        self.assertIn("goods_transfer_settlement", result["link"])
        card = next(card for card in self.cards() if card["group_id"] == result["group_id"])
        self.assertEqual(card["unread_count"], 0)
        self.assertEqual(card["action_pending_count"], 1)
        self.assertEqual(list(UserTask.objects.filter(pk__in=untouched_ids).order_by("pk").values()), untouched)
        self.assertEqual(list(WorkItem.objects.order_by("pk").values()), mother_snapshot)

    def test_group_and_open_api_do_not_accept_another_accounts_task(self):
        own = self.task()
        stranger = self.task(user=self.peer, transfer=self.second)
        authorization = f"Bearer {issue_access_token(self.user)}"
        response = self.client.get("/pldp/api/workflow/task/list/", {"openid": self.peer.wx_openid}, HTTP_AUTHORIZATION=authorization)
        self.assertEqual(response.status_code, 200)
        card = response.json()["data"]["items"][0]
        self.assertEqual(card["entry_task_id"], own.pk)
        group_response = self.client.get(
            "/pldp/api/workflow/task/group/", {"group_id": self.cards(self.peer)[0]["group_id"]}, HTTP_AUTHORIZATION=authorization,
        )
        self.assertEqual(group_response.status_code, 404)
        denied = self.client.post("/pldp/api/workflow/task/open/", {"usertask_id": stranger.pk, "openid": self.peer.wx_openid}, HTTP_AUTHORIZATION=authorization)
        self.assertEqual(denied.status_code, 403)
        with self.assertRaises(TaskActionError):
            open_task(self.user.wx_openid, stranger.pk)
        stranger.refresh_from_db()
        self.assertEqual(stranger.state, "NEW")


class GoodsTransferCardLifecycleTests(TestCase):
    """Reuse production fixtures, but do not inherit/re-run unrelated tests."""

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
            "goods-card-child-sales", cls.child_b, cls.merchant_sales_role,
        )

    def setUp(self):
        media = TemporaryDirectory(prefix="goods-card-test-")
        self.addCleanup(media.cleanup)
        for attribute in ("location", "base_location"):
            storage_patch = patch.object(goods_transfer_private_storage, attribute, media.name)
            storage_patch.start()
            self.addCleanup(storage_patch.stop)

    def post(self, path, user, payload=None, *, json=False):
        options = {"content_type": "application/json"} if json else {}
        response = self.client.post(path, payload or {}, **self.auth(user), **options)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["code"], 0, response.json())
        return response.json()["data"]

    def one_card(self, user, *, group_id=None):
        cards = get_task_list(user.wx_openid)["items"]
        self.assertEqual(len(cards), 1, cards)
        card = cards[0]
        self.assertEqual(card["group_kind"], "GOODS_TRANSFER")
        if group_id:
            self.assertEqual(card["group_id"], group_id)
        return card

    def start_shipment(self, device, receiver, request_id):
        self.set_owner(device, self.parent)
        flow = self.create_flow(self.parent_stock, {
            "flow_type": "SHIPMENT", "client_request_id": request_id,
            "target_account_id": receiver.pk,
        })
        self.scan_and_submit(self.parent_stock, flow["id"], device)
        return flow

    def test_two_child_shipments_keep_two_upstream_cards_and_one_per_child(self):
        first = self.start_shipment(self.devices[0], self.child_b_owner, "cards-two-b")
        second = self.start_shipment(self.devices[1], self.child_c_owner, "cards-two-c")
        first_child_group = self.one_card(self.child_b_owner)["group_id"]
        second_child_group = self.one_card(self.child_c_owner)["group_id"]
        self.assertNotEqual(first_child_group, second_child_group)
        self.post(f"{self.endpoint}{first['id']}/receipt/", self.child_b_stock)
        self.post(f"{self.endpoint}{first['id']}/settlement/statement/", self.parent_owner, {
            "receivable_amount": "1000.00", "statement_image": self.image_file(),
        })
        for user in (self.parent_owner, self.parent_sales, self.parent_stock):
            cards = get_task_list(user.wx_openid)["items"]
            self.assertEqual(len(cards), 2)
            self.assertEqual(len({card["group_id"] for card in cards}), 2)
        self.one_card(self.child_b_owner, group_id=first_child_group)
        self.one_card(self.child_c_owner, group_id=second_child_group)
        self.assertEqual(GoodsTransfer.objects.get(pk=second["id"]).status, "PENDING_RECEIPT")

    def test_receipt_and_two_payment_rounds_update_original_card_for_collaborators(self):
        flow = self.start_shipment(self.devices[0], self.child_b_owner, "cards-full-flow")
        base = f"{self.endpoint}{flow['id']}/"
        users = (self.parent_owner, self.parent_sales, self.parent_stock,
                 self.child_b_owner, self.child_b_sales, self.child_b_stock)
        groups = {user.pk: self.one_card(user)["group_id"] for user in users}
        original_receipt = UserTask.objects.get(
            to_openid=self.child_b_owner.wx_openid,
            workitem__type="GOODS_TRANSFER_RECEIPT_CONFIRM",
            workitem__biz_ref=f"lifecycle.GoodsTransfer:{flow['id']}",
        )
        self.post(base + "settlement/statement/", self.parent_owner, {
            "receivable_amount": "1000.00", "statement_image": self.image_file("statement.png"),
        })
        before_receipt = self.one_card(self.child_b_owner, group_id=groups[self.child_b_owner.pk])
        self.assertEqual(before_receipt["child_action_pending_count"], 2)
        self.assertEqual(before_receipt["action_pending_count"], 1)
        self.post(base + "settlement/logistics-documents/", self.parent_stock, {
            "client_request_id": "cards-logistics", "document_file": self.image_file("logistics.png"),
        })
        self.post(base + "receipt/", self.child_b_stock)
        self.assertEqual(OrganizationDevice.objects.get(device=self.devices[0]).organization_id, self.child_b.pk)
        # Receipt by a collaborating stock keeper ends only the receipt action.
        lower_card = self.one_card(self.child_b_owner, group_id=groups[self.child_b_owner.pk])
        self.assertEqual(lower_card["child_action_pending_count"], 1)
        self.assertEqual(lower_card["entry_task_type"], "GOODS_TRANSFER_PAYMENT_UPLOAD")
        original_receipt.refresh_from_db()
        self.assertEqual(original_receipt.state, "DONE")
        open_task(self.child_b_owner.wx_openid, original_receipt.pk)
        self.assertEqual(get_task_summary(self.child_b_owner.wx_openid)["action_pending_count"], 1)
        historical_ids = set(UserTask.objects.values_list("pk", flat=True))
        for sequence, amount in ((1, "400.00"), (2, "600.00")):
            voucher = self.post(base + "settlement/payments/", self.child_b_owner, {
                "client_request_id": f"cards-payment-{sequence}",
                "voucher_image": self.image_file(f"payment-{sequence}.png"),
            })
            payment = voucher["payments"][-1]
            for user in (self.parent_owner, self.parent_sales, self.parent_stock):
                card = self.one_card(user, group_id=groups[user.pk])
                self.assertEqual(card["entry_task_type"], "GOODS_TRANSFER_PAYMENT_CONFIRM")
                self.assertEqual(card["action_pending_count"], 1)
            confirmed = self.post(base + f"settlement/payments/{payment['id']}/confirm/", self.parent_sales,
                                  {"confirmed_amount": amount}, json=True)
            self.assertEqual(confirmed["outstanding_amount"], "600.00" if sequence == 1 else "0.00")
            for user in users:
                self.one_card(user, group_id=groups[user.pk])
        self.assertEqual(confirmed["confirmed_received_amount"], "1000.00")
        self.assertEqual(confirmed["state"], "FULLY_RECEIVED")
        self.assertTrue(historical_ids.issubset(set(UserTask.objects.values_list("pk", flat=True))))
        for user in users:
            card = self.one_card(user, group_id=groups[user.pk])
            open_task(user.wx_openid, card["entry_task_id"])
            self.assertEqual(get_task_summary(user.wx_openid)["attention_count"], 0)
            self.assertEqual(self.one_card(user, group_id=groups[user.pk])["state"], "DONE")
