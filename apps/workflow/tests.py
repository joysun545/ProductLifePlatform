from django.test import TestCase

from account.models import User
from account.services.auth_service import issue_access_token
from workflow.models import UserTask, WorkItem


class WorkflowTaskAuthenticationTests(TestCase):
    summary_url = "/pldp/api/workflow/task/summary/"
    list_url = "/pldp/api/workflow/task/list/"
    group_url = "/pldp/api/workflow/task/group/"
    open_url = "/pldp/api/workflow/task/open/"

    def setUp(self):
        self.user = User.objects.create_user(
            username="wx_task_owner",
            wx_openid="openid-task-owner",
        )
        self.other_user = User.objects.create_user(
            username="wx_task_other",
            wx_openid="openid-task-other",
        )
        self.authorization = f"Bearer {issue_access_token(self.user)}"
        self.other_authorization = f"Bearer {issue_access_token(self.other_user)}"

        self.workitem = WorkItem.objects.create(
            domain="SYSTEM",
            type="TEST_NOTICE",
            biz_id="test-notice-1",
            title="测试通知",
            summary="用于验证个人中心任务",
            link="",
            action_mode="EACH_ACK",
            uniq_key="test-notice-1-owner",
        )
        self.task = UserTask.objects.create(
            workitem=self.workitem,
            to_openid=self.user.wx_openid,
            state="NEW",
            need_action=False,
        )

        other_workitem = WorkItem.objects.create(
            domain="SYSTEM",
            type="OTHER_NOTICE",
            biz_id="test-notice-2",
            title="其他账号通知",
            action_mode="EACH_ACK",
            uniq_key="test-notice-2-other",
        )
        self.other_task = UserTask.objects.create(
            workitem=other_workitem,
            to_openid=self.other_user.wx_openid,
            state="NEW",
            need_action=False,
        )

    def test_task_endpoints_require_access_token(self):
        response = self.client.get(self.summary_url)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], 401)

    def test_list_uses_token_identity_and_ignores_spoofed_openid(self):
        response = self.client.get(
            self.list_url,
            {"openid": self.other_user.wx_openid},
            HTTP_AUTHORIZATION=self.authorization,
        )

        self.assertEqual(response.status_code, 200)
        items = response.json()["data"]["items"]
        self.assertEqual([item["id"] for item in items], [self.task.id])

    def test_owner_can_open_own_notice_task(self):
        response = self.client.post(
            self.open_url,
            {"usertask_id": self.task.id, "openid": self.other_user.wx_openid},
            HTTP_AUTHORIZATION=self.authorization,
        )

        self.assertEqual(response.status_code, 200)
        self.task.refresh_from_db()
        self.assertEqual(self.task.state, "DONE")
        self.assertEqual(self.task.done_reason, "OPENED")

    def test_other_account_cannot_open_task(self):
        response = self.client.post(
            self.open_url,
            {"usertask_id": self.task.id},
            HTTP_AUTHORIZATION=self.other_authorization,
        )

        self.assertEqual(response.status_code, 403)
        self.task.refresh_from_db()
        self.assertEqual(self.task.state, "NEW")

    def test_opening_after_sales_card_acknowledges_notices_but_not_actions(self):
        group_key = "aftersales:AS-TEST-001"
        tasks = []
        for index, need_action in enumerate((False, False, True), start=1):
            workitem = WorkItem.objects.create(
                domain="AFTERSALE",
                type=f"AFTERSALES_TEST_{index}",
                biz_id="91",
                biz_ref="AfterSalesApplication:91",
                title=f"售后更新{index}",
                action_mode="EACH_ACK",
                uniq_key=f"aftersales-test-{index}",
                payload={
                    "inbox_group_key": group_key,
                    "inbox_group_kind": "AFTERSALES",
                },
            )
            tasks.append(UserTask.objects.create(
                workitem=workitem,
                to_openid=self.user.wx_openid,
                state="NEW",
                need_action=need_action,
            ))

        response = self.client.post(
            self.open_url,
            {"usertask_id": tasks[0].id},
            HTTP_AUTHORIZATION=self.authorization,
        )

        self.assertEqual(response.status_code, 200)
        states = list(
            UserTask.objects.filter(pk__in=[item.pk for item in tasks])
            .order_by("pk")
            .values_list("state", "done_reason")
        )
        self.assertEqual(
            states,
            [("DONE", "GROUP_OPENED"), ("DONE", "GROUP_OPENED"), ("READ", "")],
        )

    def test_summary_attention_count_is_union_without_double_count(self):
        cases = (
            ("new-action", "NEW", True),
            ("read-action", "READ", True),
            ("read-notice", "READ", False),
            ("done-action", "DONE", True),
        )
        for suffix, state, need_action in cases:
            workitem = WorkItem.objects.create(
                domain="SYSTEM",
                type="TEST_ATTENTION",
                biz_id=f"test-attention-{suffix}",
                title=f"关注计数-{suffix}",
                action_mode="EACH_ACK",
                uniq_key=f"test-attention-{suffix}-owner",
            )
            UserTask.objects.create(
                workitem=workitem,
                to_openid=self.user.wx_openid,
                state=state,
                need_action=need_action,
            )

        response = self.client.get(
            self.summary_url,
            HTTP_AUTHORIZATION=self.authorization,
        )

        self.assertEqual(response.status_code, 200)
        summary = response.json()["data"]
        # setUp 的普通未读通知 + 本测试的未读操作任务。
        self.assertEqual(summary["unread_count"], 2)
        # 未读操作任务 + 已读但未处理的操作任务。
        self.assertEqual(summary["action_pending_count"], 2)
        # 两类任务取并集，未读操作任务不能重复计数。
        self.assertEqual(summary["attention_count"], 3)

    def test_factory_return_tasks_share_one_card_but_keep_real_children(self):
        task_ids = []
        for index, (domain, task_type, title) in enumerate((
            (
                "LOGISTICS",
                "FACTORY_RETURN_DISASSEMBLY_PRODUCTION",
                "退货设备待制定拆机处理",
            ),
            (
                "AFTERSALE",
                "FACTORY_RETURN_SUPPLIER_WARRANTY",
                "厂家已推送拆机退回物料及自动质保结果",
            ),
            (
                "SUPPLY_CHAIN",
                "FACTORY_RETURN_SUPPLIER_REPAIR_MATERIAL",
                "厂家退货修复需要原配套物料",
            ),
        ), start=1):
            # WorkItem currently accepts deployed domain strings even when a
            # newer business area is not yet included in display choices.
            workitem = WorkItem.objects.create(
                domain=domain,
                type=task_type,
                biz_id=f"return-child-{index}",
                biz_ref=f"return-child:{index}",
                title=title,
                summary=f"第{index}项｜GF-TEST-GROUP-001",
                link=(
                    "/pages/stock/factory_return_process/"
                    "factory_return_process?transfer_id=901"
                ),
                action_mode="EACH_ACK",
                uniq_key=f"return-child-{index}-owner",
                payload={"return_flow_no": "GF-TEST-GROUP-001"},
            )
            task_ids.append(
                UserTask.objects.create(
                    workitem=workitem,
                    to_openid=self.user.wx_openid,
                    state="NEW",
                    need_action=False,
                ).pk
            )

        response = self.client.get(
            self.list_url,
            HTTP_AUTHORIZATION=self.authorization,
        )

        self.assertEqual(response.status_code, 200)
        groups = [
            item for item in response.json()["data"]["items"]
            if item["grouped"]
        ]
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group["child_count"], 3)
        self.assertEqual(group["unread_count"], 3)
        self.assertIn("factory_return_process?transfer_id=901", group["link"])
        self.assertIn(f"task_group_id={group['group_id']}", group["link"])
        self.assertIn(f"task_id={group['entry_task_id']}", group["link"])
        self.assertIn(group["entry_task_id"], task_ids)
        self.assertTrue(group["title"].endswith("……"))

        detail_response = self.client.get(
            self.group_url,
            {"group_id": group["group_id"]},
            HTTP_AUTHORIZATION=self.authorization,
        )
        self.assertEqual(detail_response.status_code, 200)
        detail = detail_response.json()["data"]
        self.assertEqual(
            {item["id"] for item in detail["items"]},
            set(task_ids),
        )
        # Opening the projected parent/detail must not acknowledge children.
        self.assertEqual(
            UserTask.objects.filter(pk__in=task_ids, state="NEW").count(),
            3,
        )

        open_response = self.client.post(
            self.open_url,
            {"usertask_id": task_ids[0]},
            HTTP_AUTHORIZATION=self.authorization,
        )
        self.assertEqual(open_response.status_code, 200)
        self.assertEqual(
            UserTask.objects.filter(pk__in=task_ids, state="NEW").count(),
            2,
        )

        updated_detail = self.client.get(
            self.group_url,
            {"group_id": group["group_id"]},
            HTTP_AUTHORIZATION=self.authorization,
        ).json()["data"]
        self.assertEqual(updated_detail["unread_count"], 2)

    def test_quality_recall_tasks_share_campaign_card_and_open_focused_detail(self):
        self.task.state = "DONE"
        self.task.need_action = False
        self.task.save(update_fields=("state", "need_action", "updated_at"))
        task_ids = []
        rows = (
            ("QUALITY_RECALL_FACTORY_COORDINATION", None, "BATCH_TRACKING"),
            ("QUALITY_RECALL_FACTORY_RECEIPT", 81, "FACTORY_PENDING_RECEIPT"),
            ("QUALITY_RECALL_FACTORY_RECEIPT_NOTICE", 81, "RETURN_IN_TRANSIT"),
        )
        for index, (task_type, device_id, stage) in enumerate(rows, start=1):
            item = WorkItem.objects.create(
                domain="SYSTEM",
                type=task_type,
                biz_id="44",
                title=f"召回更新{index}",
                summary=f"召回设备节点{index}",
                link=(
                    "/pages/quality_trace/recall_task_detail/recall_task_detail"
                    "?campaign_id=44"
                ),
                action_mode="EACH_ACK",
                uniq_key=f"quality-recall-group-test-{index}",
                payload={
                    "recall_campaign_id": 44,
                    "recall_device_id": device_id,
                    "stage": stage,
                    "inbox_group_key": f"legacy-org-key-{index}",
                    "inbox_group_kind": "QUALITY_RECALL",
                },
            )
            task_ids.append(UserTask.objects.create(
                workitem=item,
                to_openid=self.user.wx_openid,
                state="NEW",
                need_action=index == 2,
            ).pk)

        response = self.client.get(
            self.list_url,
            HTTP_AUTHORIZATION=self.authorization,
        )
        groups = [
            row for row in response.json()["data"]["items"]
            if row.get("group_kind") == "QUALITY_RECALL"
        ]
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group["child_count"], 3)
        self.assertEqual(group["group_label"], "召回任务")
        self.assertEqual(group["unread_count"], 1)
        self.assertEqual(group["child_unread_count"], 3)
        summary = self.client.get(
            self.summary_url,
            HTTP_AUTHORIZATION=self.authorization,
        ).json()["data"]
        self.assertEqual(summary["unread_count"], 1)
        self.assertEqual(summary["action_pending_count"], 1)
        self.assertEqual(summary["attention_count"], 1)
        self.assertIn(
            "/pages/quality_trace/recall_detail/recall_detail?campaign_id=44",
            group["link"],
        )
        self.assertIn("recall_device_id=81", group["link"])
        self.assertIn("focus_stage=", group["link"])
        self.assertNotIn("recall_task_detail", group["link"])
        self.assertIn(group["entry_task_id"], task_ids)
        self.assertEqual(group["entry_task_type"], "QUALITY_RECALL_FACTORY_RECEIPT")
        self.assertTrue(group["need_action"])

        opened = self.client.post(
            self.open_url,
            {"usertask_id": group["entry_task_id"]},
            HTTP_AUTHORIZATION=self.authorization,
        )
        self.assertEqual(opened.status_code, 200)
        self.assertEqual(
            UserTask.objects.filter(pk__in=task_ids, need_action=False, state="DONE").count(),
            2,
        )
        action_task = UserTask.objects.get(pk__in=task_ids, need_action=True)
        self.assertEqual(action_task.state, "READ")
        summary = self.client.get(
            self.summary_url,
            HTTP_AUTHORIZATION=self.authorization,
        ).json()["data"]
        self.assertEqual(summary["unread_count"], 0)
        self.assertEqual(summary["action_pending_count"], 1)
        self.assertEqual(summary["attention_count"], 1)

    def test_task_group_cannot_be_read_by_another_account(self):
        response = self.client.get(
            self.list_url,
            HTTP_AUTHORIZATION=self.authorization,
        )
        group_id = response.json()["data"]["items"][0]["group_id"]

        forbidden = self.client.get(
            self.group_url,
            {"group_id": group_id},
            HTTP_AUTHORIZATION=self.other_authorization,
        )

        self.assertEqual(forbidden.status_code, 404)
