"""Whole-group pagination, with real recall facts and current HTTP permissions.

Generic inbox rows below are legal notification/action history fixtures; DONE
rows mirror notices acknowledged through task/open. Transitions under test use
that API. Recall fixtures launch a real campaign, and transport uses the current
service with real signed evidence uploaded through its API, without mocking its
stage or display projection.
"""
from datetime import timedelta
from unittest.mock import patch

from django.core import signing
from django.test import TestCase
from django.utils import timezone

from account.models import Organization, OrganizationType, User
from account.services.auth_service import issue_access_token
from quality_trace.tests import test_recall_batch as recall_fixture
from workflow.models import UserTask, WorkItem
from workflow.services.task_query import (
    TASK_LIST_CURSOR_MAX_AGE, TASK_LIST_CURSOR_SALT, TaskCursorError,
    _task_groups, _task_revision, get_task_list, get_task_summary,
)


def badge_counts(summary):
    return {key: summary[key] for key in ("unread_count", "action_pending_count", "attention_count")}


class TaskPaginationAPITests(TestCase):
    list_url = "/pldp/api/workflow/task/list/"
    group_url = "/pldp/api/workflow/task/group/"
    open_url = "/pldp/api/workflow/task/open/"

    def setUp(self):
        self.user = User.objects.create_user(username="pagination-owner", wx_openid="pagination-owner")
        self.other = User.objects.create_user(username="pagination-other", wx_openid="pagination-other")
        self.when = timezone.now()
        self.sequence = 0

    def _task(self, *, state="DONE", action=False, scope=None, kind="", when=None, user=None, root=None):
        self.sequence += 1
        payload = {}
        if scope:
            payload.update(inbox_group_key=scope, inbox_group_kind=kind)
        if root:
            payload["root_organization_id"] = root.pk
        item = WorkItem.objects.create(
            domain="SYSTEM", type="PAGINATION_ACTION" if action else "PAGINATION_NOTICE",
            biz_id=str(self.sequence), uniq_key=f"pagination-{self.sequence}",
            title=f"分页任务{self.sequence}", link=f"/pages/example/detail?id={self.sequence}",
            payload=payload, status="DONE" if state == "DONE" else "OPEN",
            action_mode="ANYONE_DO" if action else "EACH_ACK",
        )
        return UserTask.objects.create(
            workitem=item, to_openid=(user or self.user).wx_openid,
            state=state, need_action=action, cursor_time=when or self.when,
            read_at=self.when if state != "NEW" else None,
            done_at=self.when if state == "DONE" else None,
            done_reason="OPENED" if state == "DONE" else "",
        )

    def _response(self, *, user=None, **query):
        return self.client.get(
            self.list_url, query,
            HTTP_AUTHORIZATION="Bearer " + issue_access_token(user or self.user),
        )

    def _page(self, **query):
        response = self._response(**query)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["code"], 0, response.content)
        return response.json()["data"]

    def _assert_refresh_error(self, response, status):
        self.assertEqual(response.status_code, status, response.content)
        self.assertEqual(response.json()["code"], 1, response.content)
        self.assertEqual(response.json()["data"], {"refresh_required": True})

    def test_old_pending_action_precedes_201_new_done_cards_and_all_are_discoverable(self):
        pending = self._task(state="READ", action=True, when=self.when - timedelta(days=30))
        for index in range(201):
            self._task(when=self.when + timedelta(seconds=index))
        before = (list(UserTask.objects.order_by("pk").values()), list(WorkItem.objects.order_by("pk").values()))
        page = self._page(limit=50)
        self.assertEqual(page["items"][0]["entry_task_id"], pending.pk)
        self.assertEqual(page["group_count"], 202)
        self.assertTrue(page["has_more"])
        self.assertFalse(page["updates_available"])
        ids = [item["group_id"] for item in page["items"]]
        while page["has_more"]:
            page = self._page(limit=50, cursor=page["next_cursor"])
            ids.extend(item["group_id"] for item in page["items"])
        self.assertIsNone(page["next_cursor"])
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 202)
        self.assertEqual(badge_counts(get_task_summary(self.user.wx_openid)), {
            "unread_count": 0, "action_pending_count": 1, "attention_count": 1,
        })
        self.assertEqual(before, (list(UserTask.objects.order_by("pk").values()), list(WorkItem.objects.order_by("pk").values())))

    def test_whole_group_retains_entry_root_child_and_card_counts(self):
        root = Organization.objects.create(
            name="分页根厂家", code="PAGINATION-ROOT", organization_type=OrganizationType.objects.get(code="OWNER"),
        )
        action = self._task(state="READ", action=True, scope="order:one", kind="ORDER_PLAN", root=root,
                            when=self.when - timedelta(days=1))
        self._task(state="NEW", scope="order:one", kind="ORDER_PLAN", root=root)
        self._task(scope="order:one", kind="ORDER_PLAN", root=root)
        self._task(state="NEW", when=self.when + timedelta(days=1))
        page = self._page(limit=1)
        item = page["items"][0]
        self.assertEqual(item["entry_task_id"], action.pk)
        self.assertEqual(item["child_count"], 3)
        self.assertEqual((item["unread_count"], item["action_pending_count"]), (1, 1))
        self.assertEqual((item["child_unread_count"], item["child_action_pending_count"]), (1, 1))
        self.assertEqual(item["root_organization"], {"id": root.pk, "name": root.name})
        detail = self.client.get(self.group_url, {"group_id": item["group_id"]},
                                 HTTP_AUTHORIZATION="Bearer " + issue_access_token(self.user)).json()["data"]
        self.assertEqual(len(detail["items"]), 3)
        self.assertEqual(badge_counts(get_task_summary(self.user.wx_openid)), {
            "unread_count": 2, "action_pending_count": 1, "attention_count": 2,
        })
        second = self._page(limit=1, cursor=page["next_cursor"])
        self.assertNotEqual(second["items"][0]["group_id"], item["group_id"])

    def test_same_time_is_stable_by_group_id_with_action_then_unread_then_history(self):
        for _ in range(7):
            self._task(state="NEW")
        pending = self._task(state="READ", action=True, when=self.when - timedelta(days=1))
        done = self._task(when=self.when + timedelta(days=1))
        page = self._page(limit=2)
        items = list(page["items"])
        while page["has_more"]:
            page = self._page(limit=2, cursor=page["next_cursor"])
            items.extend(page["items"])
        self.assertEqual(items[0]["entry_task_id"], pending.pk)
        self.assertEqual(items[-1]["entry_task_id"], done.pk)
        self.assertEqual([item["group_id"] for item in items[1:-1]], sorted(item["group_id"] for item in items[1:-1]))

    def test_continuous_new_notifications_keep_cursor_and_current_groups_complete(self):
        for index in range(7):
            self._task(scope=f"order:{index}", kind="ORDER_PLAN")
        original = self._page(limit=50)["items"]
        expected = [item["group_id"] for item in original]
        target = original[-1]
        original_task = UserTask.objects.get(pk=target["entry_task_id"])
        scope = original_task.workitem.payload["inbox_group_key"]
        page = self._page(limit=2)
        seen = list(page["items"])
        self._task(state="NEW", scope=scope, kind="ORDER_PLAN")
        while page["has_more"]:
            self._task(state="NEW")
            page = self._page(limit=2, cursor=page["next_cursor"])
            self.assertTrue(page["updates_available"])
            seen.extend(page["items"])
        self.assertEqual([item["group_id"] for item in seen], expected)
        self.assertEqual(len(seen), len({item["group_id"] for item in seen}))
        live = next(item for item in seen if item["group_id"] == target["group_id"])
        self.assertEqual((live["child_count"], live["child_unread_count"]), (2, 1))
        refreshed = self._page(limit=50)
        self.assertGreater(len(refreshed["items"]), len(seen))
        self.assertEqual(refreshed["items"][0]["state"], "NEW")
        self.assertEqual(refreshed["unread_count"], get_task_summary(self.user.wx_openid)["unread_count"])

    def test_opening_an_old_row_requires_explicit_refresh_not_silent_reordering(self):
        notice = self._task(state="NEW")
        self._task()
        page = self._page(limit=1)
        opened = self.client.post(self.open_url, {"usertask_id": notice.pk},
                                  HTTP_AUTHORIZATION="Bearer " + issue_access_token(self.user))
        self.assertEqual(opened.status_code, 200)
        self._assert_refresh_error(self._response(limit=1, cursor=page["next_cursor"]), 409)
        self.assertEqual(len(self._page(limit=50)["items"]), 2)

    def test_cursor_tampering_malformed_context_and_expiry_are_controlled(self):
        self._task()
        self._task()
        token = self._page(limit=1)["next_cursor"]
        for invalid in ("bad", token + "x", "x" * 4097,
                        signing.dumps([], salt=TASK_LIST_CURSOR_SALT)):
            with self.subTest(invalid=invalid[:20]):
                self._assert_refresh_error(self._response(limit=1, cursor=invalid), 400)
        context = signing.loads(token, salt=TASK_LIST_CURSOR_SALT)
        context["after"] = [0, "invalid", "not-a-group"]
        self._assert_refresh_error(self._response(limit=1, cursor=signing.dumps(context, salt=TASK_LIST_CURSOR_SALT)), 400)
        future = timezone.now() + timedelta(seconds=TASK_LIST_CURSOR_MAX_AGE + 1)
        with patch("workflow.services.task_query.timezone.now", return_value=future):
            self._assert_refresh_error(self._response(limit=1, cursor=token), 409)

    def test_cursor_is_bound_to_recipient_limit_and_filter_and_never_grants_group_access(self):
        self._task()
        self._task()
        other_task = self._task(user=self.other)
        page = self._page(limit=1)
        cursor = page["next_cursor"]
        self._assert_refresh_error(self._response(user=self.other, limit=1, cursor=cursor), 400)
        self._assert_refresh_error(self._response(limit=2, cursor=cursor), 400)
        self._assert_refresh_error(self._response(limit=1, cursor=cursor, filter="pending"), 400)
        denied = self.client.get(self.group_url, {"group_id": page["items"][0]["group_id"]},
                                 HTTP_AUTHORIZATION="Bearer " + issue_access_token(self.other))
        self.assertEqual(denied.status_code, 404)
        own = self._page(limit=1, cursor=cursor, openid=self.other.wx_openid)
        self.assertNotIn(other_task.pk, [item["entry_task_id"] for item in own["items"]])

    def test_direct_call_limit_compatibility_and_empty_inbox(self):
        empty = get_task_list(self.user.wx_openid, 0)
        self.assertEqual(empty.pop("revision"), get_task_summary(self.user.wx_openid)["revision"])
        self.assertEqual(empty, {
            "items": [], "unread_count": 0, "group_count": 0,
            "has_more": False, "next_cursor": None, "updates_available": False,
        })
        with self.assertRaises(TaskCursorError):
            get_task_list(self.user.wx_openid, cursor={"forged": True})
        self._task(state="NEW")
        self.assertEqual(len(get_task_list(self.user.wx_openid, "bad")["items"]), 1)

    def test_same_counts_done_notice_and_workitem_text_change_revision_not_paging_context(self):
        original = self._task()
        self._task()
        first = self._page(limit=1)
        before = get_task_summary(self.user.wx_openid)
        self.assertEqual(first["revision"], before["revision"])
        self._task()
        appended = get_task_summary(self.user.wx_openid)
        self.assertEqual(badge_counts(appended), badge_counts(before))
        self.assertNotEqual(appended["revision"], before["revision"])
        original.workitem.title = "同计数标题变更"
        original.workitem.last_activity_at = timezone.now() + timedelta(seconds=1)
        original.workitem.save(update_fields=("title", "last_activity_at", "updated_at"))
        changed = get_task_summary(self.user.wx_openid)
        self.assertEqual(badge_counts(changed), badge_counts(before))
        self.assertNotEqual(changed["revision"], appended["revision"])
        continuation = self._page(limit=1, cursor=first["next_cursor"])
        self.assertTrue(continuation["updates_available"])
        self.assertEqual(continuation["revision"], changed["revision"])


class TaskPaginationRecallProjectionTests(recall_fixture.RecallEvidenceUploadMixin, TestCase):
    setUpTestData = classmethod(recall_fixture.RecallBatchTests.setUpTestData.__func__)
    make_user = staticmethod(recall_fixture.RecallBatchTests.make_user)
    run_batch = recall_fixture.RecallBatchTests.run_batch

    def _page(self, user, **query):
        return self.client.get("/pldp/api/workflow/task/list/", query,
                               HTTP_AUTHORIZATION="Bearer " + issue_access_token(user))

    def _history(self, user):
        for index in range(2):
            workitem = WorkItem.objects.create(domain="SYSTEM", type="PAGING_RECALL_HISTORY", biz_id=str(index),
                                               uniq_key=f"paging-recall-history-{index}", status="DONE")
            UserTask.objects.create(workitem=workitem, to_openid=user.wx_openid, state="DONE",
                                    cursor_time=timezone.now() - timedelta(days=1), done_at=timezone.now())

    def _acknowledge_recall(self, user):
        card = next(item for item in get_task_list(user.wx_openid)["items"] if item["group_kind"] == "QUALITY_RECALL")
        response = self.client.post("/pldp/api/workflow/task/open/", {"usertask_id": card["entry_task_id"]},
                                    HTTP_AUTHORIZATION="Bearer " + issue_access_token(user))
        self.assertEqual(response.status_code, 200, response.content)

    def test_real_dispatch_keeps_done_inbox_state_separate_from_live_recall_stage(self):
        owner = self.owners[2]
        self._history(owner)
        first = self._page(owner, limit=1).json()["data"]
        self.assertEqual(first["items"][0]["group_kind"], "QUALITY_RECALL")
        self.assertTrue(first["has_more"])
        self.run_batch(owner, "DISPATCH", self.rows[:2])
        card = next(item for item in get_task_list(owner.wx_openid)["items"] if item["group_kind"] == "QUALITY_RECALL")
        opened = self.client.post("/pldp/api/workflow/task/open/", {"usertask_id": card["entry_task_id"]},
                                  HTTP_AUTHORIZATION="Bearer " + issue_access_token(owner))
        self.assertEqual(opened.status_code, 200, opened.content)
        before = (list(UserTask.objects.order_by("pk").values()), list(WorkItem.objects.order_by("pk").values()))
        refreshed = self._page(owner, limit=1).json()["data"]["items"][0]
        self.assertEqual(refreshed["state"], "DONE")
        self.assertEqual(refreshed["display_state"], "跟踪中")
        self.assertFalse(refreshed["need_action"])
        self.assertEqual(refreshed["root_organization"]["id"], self.factory.pk)
        self.assertEqual(refreshed["recall_context"]["device_count"], 2)
        self.assertEqual(refreshed["group_id"], first["items"][0]["group_id"])
        self.assertEqual(before, (list(UserTask.objects.order_by("pk").values()), list(WorkItem.objects.order_by("pk").values())))
        stale = self._page(owner, limit=1, cursor=first["next_cursor"])
        self.assertEqual(stale.status_code, 409)
        self.assertTrue(stale.json()["data"]["refresh_required"])

    def test_current_recall_permission_projection_invalidates_old_priority_without_changing_tasks(self):
        owner = self.owners[2]
        self._history(owner)
        first = self._page(owner, limit=1).json()["data"]
        self.factory.is_active = False
        self.factory.full_clean()
        self.factory.save(update_fields=("is_active", "updated_at"))
        before = list(UserTask.objects.order_by("pk").values())
        stale = self._page(owner, limit=1, cursor=first["next_cursor"])
        self.assertEqual(stale.status_code, 409, stale.content)
        cards = self._page(owner, limit=50).json()["data"]["items"]
        recall = next(item for item in cards if item["group_kind"] == "QUALITY_RECALL")
        self.assertEqual(recall["display_state"], "历史任务")
        self.assertFalse(recall["need_action"])
        self.assertEqual(recall["link"], "")
        self.assertEqual(list(UserTask.objects.order_by("pk").values()), before)

    def test_same_badge_counts_live_recall_stage_changes_revision_without_expiring_old_cursor(self):
        owner = self.owners[2]
        self._history(owner)
        self.run_batch(owner, "DISPATCH", self.rows[:2])
        self._acknowledge_recall(owner)
        first = self._page(owner, limit=1).json()["data"]
        before = get_task_summary(owner.wx_openid)
        self.assertEqual(first["revision"], before["revision"])
        self.run_batch(self.owners[1], "RECEIVE", self.rows[:2])
        self._acknowledge_recall(owner)
        after = get_task_summary(owner.wx_openid)
        self.assertEqual(badge_counts(after), badge_counts(before))
        self.assertNotEqual(after["revision"], before["revision"])
        loaded = _task_groups(owner.wx_openid)
        with self.assertNumQueries(0):
            self.assertEqual(_task_revision(loaded), after["revision"])
        continued = self._page(owner, limit=1, cursor=first["next_cursor"])
        self.assertEqual(continued.status_code, 200, continued.content)
        current = next(item for item in get_task_list(owner.wx_openid)["items"] if item["group_kind"] == "QUALITY_RECALL")
        self.assertEqual(current["state"], "DONE")
        self.assertEqual(current["display_state"], "跟踪中")
        self.assertIn("继续退回", current["recall_context"]["stage_summary"])
