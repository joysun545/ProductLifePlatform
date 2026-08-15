from django.test import TestCase

from account.models import User
from account.services.auth_service import issue_access_token
from workflow.models import UserTask, WorkItem


class WorkflowTaskAuthenticationTests(TestCase):
    summary_url = "/pldp/api/workflow/task/summary/"
    list_url = "/pldp/api/workflow/task/list/"
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
