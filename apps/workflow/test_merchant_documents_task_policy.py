"""The merchant documents policy changes inbox projection, not task history."""
from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from account.models import Organization, OrganizationRole, OrganizationType, User, UserOrganization
from account.services.auth_service import issue_access_token
from lifecycle.models import (
    GoodsTransfer, GoodsTransferAccountCase, GoodsTransferAccountPayment,
    GoodsTransferReceivableAccount, GoodsTransferSettlement,
)
from lifecycle.services.goods_transfer_receivable_task_service import reconcile_receivable_tasks
from workflow.models import UserTask, WorkItem
from workflow.services.task_query import (
    _group_id, _without_discontinued_merchant_finance,
    get_task_group, get_task_list, get_task_summary,
)


class MerchantDocumentsTaskPolicyTests(TestCase):
    def setUp(self):
        owner = OrganizationType.objects.get(code="OWNER")
        merchant = OrganizationType.objects.get(code="MERCHANT")
        self.root = Organization.objects.create(name="任务厂家", code="DOC-TASK-ROOT", organization_type=owner)
        self.upper = Organization.objects.create(name="直属商家", code="DOC-TASK-A", organization_type=merchant, factory_root=self.root)
        self.lower = Organization.objects.create(name="下级商家", code="DOC-TASK-B", organization_type=merchant, factory_root=self.root)
        self.peer = Organization.objects.create(name="另一商家", code="DOC-TASK-C", organization_type=merchant, factory_root=self.root)
        self.user = User.objects.create_user(username="docs-task-merchant", wx_openid="docs-task-merchant")
        UserOrganization.objects.create(user=self.user, organization=self.upper,
            role=OrganizationRole.objects.get(code="merchant_owner"))
        self.target = User.objects.create_user(username="docs-task-lower", wx_openid="docs-task-lower")
        UserOrganization.objects.create(user=self.target, organization=self.lower,
            role=OrganizationRole.objects.get(code="merchant_owner"))
        self.factory_user = User.objects.create_user(username="docs-task-factory", wx_openid="docs-task-factory")
        UserOrganization.objects.create(user=self.factory_user, organization=self.root,
            role=OrganizationRole.objects.get(code="factory_sales"))
        self.sequence = 0
        self.shipment = self.flow("SHIPMENT", self.upper, self.lower, self.upper)
        self.merchant_transfer = self.flow("TRANSFER", self.lower, self.peer, self.upper)
        self.factory_return = self.flow("RETURN", self.upper, self.root, self.root)
        self.merchant_case = self.case(self.merchant_transfer, self.upper, self.peer, "TRANSFER_IN_RECEIVABLE")
        self.factory_case = self.case(self.factory_return, self.root, self.upper, "RETURN_CREDIT")

    def flow(self, kind, from_org, to_org, instruction):
        self.sequence += 1
        return GoodsTransfer.objects.create(
            flow_no=f"DOC-TASK-FLOW-{self.sequence}", client_request_id=f"docs-task-flow-{self.sequence}",
            flow_type=kind, status="COMPLETED", instruction_organization=instruction,
            from_organization=from_org, to_organization=to_org,
            target_account=self.factory_user if to_org == self.root else self.target,
            initiated_by=self.user, submitted_at=timezone.now(), completed_at=timezone.now(),
        )

    def case(self, transfer, upper, lower, kind):
        credit = kind == "RETURN_CREDIT"
        return GoodsTransferAccountCase.objects.create(
            transfer=transfer, case_type=kind, upper_organization=upper, lower_organization=lower,
            creditor_organization=lower if credit else upper,
            debtor_organization=upper if credit else lower,
        )

    def task(self, kind, *, transfer=None, case=None, payload=None, state="READ", action=True, ref=None, user=None):
        self.sequence += 1
        data = dict(payload or {})
        if transfer:
            data.update(goods_transfer_id=transfer.pk, flow_no=transfer.flow_no)
        if case:
            data["account_case_id"] = case.pk
        work = WorkItem.objects.create(
            domain="LOGISTICS", type=kind, biz_id=transfer.flow_no if transfer else str(self.sequence),
            biz_ref=ref if ref is not None else (f"lifecycle.GoodsTransfer:{transfer.pk}" if transfer else ""),
            uniq_key=f"docs-task-work-{self.sequence}", title=f"任务{self.sequence}", payload=data,
            action_mode="ANYONE_DO" if action else "EACH_ACK", status="OPEN",
        )
        return UserTask.objects.create(workitem=work, to_openid=(user or self.user).wx_openid,
                                       state=state, need_action=action)

    def children(self, groups):
        return {row["id"] for group in groups for row in group.get("items", [])}

    def all_cards(self, *, limit=1):
        page = get_task_list(self.user.wx_openid, limit=limit)
        cards = list(page["items"])
        while page["has_more"]:
            page = get_task_list(self.user.wx_openid, limit=limit, cursor=page["next_cursor"])
            cards.extend(page["items"])
        return cards

    def test_payment_actions_disappear_but_documents_and_physical_tasks_keep_one_card(self):
        documents = self.task("GOODS_TRANSFER_DOCUMENTS_UPLOAD", transfer=self.shipment)
        receipt = self.task("GOODS_TRANSFER_RECEIPT", transfer=self.shipment)
        cash = [self.task(kind, transfer=self.shipment, state="NEW") for kind in (
            "GOODS_TRANSFER_PAYMENT_UPLOAD", "GOODS_TRANSFER_PAYMENT_CONFIRM")]
        self.task("GOODS_TRANSFER_ACCOUNT_SUBMIT", transfer=self.merchant_transfer, case=self.merchant_case, state="NEW")
        factory = self.task("GOODS_TRANSFER_ACCOUNT_SUBMIT", transfer=self.factory_return, case=self.factory_case)
        notice = self.task("SYSTEM_NOTICE", state="NEW", action=False)
        cards = self.all_cards()
        self.assertEqual(len(cards), 3)
        entry_ids = {card["entry_task_id"] for card in cards}
        self.assertTrue({factory.pk, notice.pk}.issubset(entry_ids))
        self.assertEqual(len(entry_ids & {documents.pk, receipt.pk}), 1)
        group = get_task_group(self.user.wx_openid, _group_id(f"goods-transfer:transfer:{self.shipment.pk}"))
        self.assertEqual(self.children([group]), {documents.pk, receipt.pk})
        self.assertEqual(group["action_pending_count"], 1)
        summary = get_task_summary(self.user.wx_openid)
        self.assertEqual({key: summary[key] for key in ("unread_count", "action_pending_count", "attention_count")},
                         {"unread_count": 1, "action_pending_count": 2, "attention_count": 3})
        for row in cash:
            row.refresh_from_db()
            self.assertEqual((row.state, row.need_action, row.workitem.status), ("NEW", True, "OPEN"))

    def test_cash_only_history_has_no_card_group_badge_or_attention_and_no_database_changes(self):
        self.task("GOODS_TRANSFER_PAYMENT_UPLOAD", transfer=self.shipment, state="NEW")
        self.task("GOODS_TRANSFER_ACCOUNT_CONFIRM", transfer=self.merchant_transfer, case=self.merchant_case, state="NEW")
        before = (list(WorkItem.objects.order_by("pk").values()), list(UserTask.objects.order_by("pk").values()))
        with CaptureQueriesContext(connection) as captured:
            page = get_task_list(self.user.wx_openid)
            summary = get_task_summary(self.user.wx_openid)
            group = get_task_group(self.user.wx_openid, _group_id(f"goods-transfer:transfer:{self.shipment.pk}"))
        self.assertEqual(page["items"], [])
        self.assertEqual(page["group_count"], 0)
        self.assertIsNone(group)
        self.assertEqual([summary[key] for key in ("unread_count", "action_pending_count", "attention_count")], [0, 0, 0])
        self.assertFalse(any(row["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")) for row in captured))
        self.assertEqual(before, (list(WorkItem.objects.order_by("pk").values()), list(UserTask.objects.order_by("pk").values())))

    def test_existing_factory_return_money_task_and_factory_order_money_task_are_preserved(self):
        factory_case = self.task("GOODS_TRANSFER_ACCOUNT_CONFIRM", transfer=self.factory_return, case=self.factory_case)
        factory_order = self.task("ORDER_PLAN_PAYMENT_UPLOAD", payload={"inbox_group_key": "test-order", "inbox_group_kind": "ORDER_PLAN"})
        items = get_task_list(self.user.wx_openid)["items"]
        self.assertEqual({item["entry_task_id"] for item in items}, {factory_case.pk, factory_order.pk})
        self.assertEqual(get_task_summary(self.user.wx_openid)["action_pending_count"], 2)

    def test_account_payment_reference_is_resolved_to_real_case_in_one_batch(self):
        payment = GoodsTransferAccountPayment.objects.create(
            account_case=self.merchant_case, sequence_no=1, client_request_id="docs-old-money",
            voucher_image="not-read-by-task-query.png", uploaded_by=self.user,
        )
        target = self.task("GOODS_TRANSFER_ACCOUNT_PAYMENT_CONFIRM", payload={"payment_id": payment.pk})
        unknown = self.task("GOODS_TRANSFER_ACCOUNT_SUBMIT", payload={"account_case_id": 999999})
        selected = _without_discontinued_merchant_finance([target, unknown])
        self.assertEqual([item.pk for item in selected], [unknown.pk])
        self.assertEqual({item["entry_task_id"] for item in get_task_list(self.user.wx_openid)["items"]}, {unknown.pk})

    def test_anchored_case_reference_and_real_flow_fallback_resolve_without_names_or_titles(self):
        anchored = self.task("GOODS_TRANSFER_ACCOUNT_SUBMIT", ref=f"lifecycle.GoodsTransferAccountCase:{self.merchant_case.pk}")
        fallback = self.task("GOODS_TRANSFER_ACCOUNT_SUBMIT", transfer=self.merchant_transfer)
        factory = self.task("GOODS_TRANSFER_ACCOUNT_SUBMIT", transfer=self.factory_return)
        selected = _without_discontinued_merchant_finance([anchored, fallback, factory])
        self.assertEqual([item.pk for item in selected], [factory.pk])

    def test_conflicting_case_and_transfer_reference_is_not_guessed(self):
        conflicting = self.task("GOODS_TRANSFER_ACCOUNT_SUBMIT", transfer=self.factory_return, case=self.merchant_case)
        self.assertEqual(_without_discontinued_merchant_finance([conflicting]), [conflicting])

    def test_many_case_actions_share_one_real_case_lookup_without_per_task_queries(self):
        for _ in range(25):
            self.task("GOODS_TRANSFER_ACCOUNT_SUBMIT", transfer=self.merchant_transfer, case=self.merchant_case)
        tasks = list(UserTask.objects.select_related("workitem").filter(to_openid=self.user.wx_openid))
        with self.assertNumQueries(1):
            self.assertEqual(_without_discontinued_merchant_finance(tasks), [])

    def test_filter_is_identical_for_authenticated_list_summary_and_group_endpoints(self):
        cash = self.task("GOODS_TRANSFER_PAYMENT_CONFIRM", transfer=self.shipment, state="NEW")
        docs = self.task("GOODS_TRANSFER_DOCUMENTS_UPLOAD", transfer=self.shipment)
        headers = {"HTTP_AUTHORIZATION": "Bearer " + issue_access_token(self.user)}
        page = self.client.get("/pldp/api/workflow/task/list/", {"limit": 1}, **headers).json()
        self.assertEqual(page["code"], 0)
        card = page["data"]["items"][0]
        self.assertEqual(card["entry_task_id"], docs.pk)
        summary = self.client.get("/pldp/api/workflow/task/summary/", **headers).json()["data"]
        self.assertEqual(summary["action_pending_count"], 1)
        self.assertEqual(summary["unread_count"], 0)
        detail = self.client.get("/pldp/api/workflow/task/group/", {"group_id": card["group_id"]}, **headers).json()["data"]
        self.assertEqual(self.children([detail]), {docs.pk})
        self.assertNotIn(cash.pk, self.children([detail]))

    def test_new_retired_cash_rows_do_not_disrupt_visible_pagination(self):
        for index in range(3):
            self.task("SYSTEM_NOTICE", payload={"inbox_group_key": f"docs-notice-{index}"}, action=False)
        first = get_task_list(self.user.wx_openid, limit=1)
        self.task("GOODS_TRANSFER_PAYMENT_UPLOAD", transfer=self.shipment, state="NEW")
        second = get_task_list(self.user.wx_openid, limit=1, cursor=first["next_cursor"])
        self.assertFalse(second["updates_available"])
        self.assertNotEqual(first["items"][0]["group_id"], second["items"][0]["group_id"])
        self.assertEqual(second["group_count"], 3)

    def test_retired_reconcile_entry_is_a_zero_query_no_op_even_with_existing_priced_history(self):
        settlement = GoodsTransferSettlement.objects.create(
            transfer=self.shipment, receivable_amount=Decimal("500.00"), statement_submitted_at=timezone.now())
        before = (WorkItem.objects.count(), UserTask.objects.count(), GoodsTransferReceivableAccount.objects.count())
        with self.assertNumQueries(0):
            result = reconcile_receivable_tasks(settlement, self.user.wx_openid)
        self.assertEqual((result["created"], result["closed"], result["financial_enabled"]), (0, 0, False))
        self.assertEqual(before, (WorkItem.objects.count(), UserTask.objects.count(), GoodsTransferReceivableAccount.objects.count()))
