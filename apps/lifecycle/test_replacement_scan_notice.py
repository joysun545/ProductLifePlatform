"""Local swap task recipients match the existing service execution gate."""

from copy import copy, deepcopy
from unittest.mock import patch

from django.db import connection
from django.test.utils import CaptureQueriesContext

from account.models import OrganizationRole, SeniorManagerProfile, UserOrganization
from lifecycle.models import AfterSalesServiceStationLiaison, ReplacementFlow
from lifecycle.services import replacement_flow_service as B
from lifecycle.services.aftersales_service import AfterSalesService as S, AfterSalesError
from lifecycle.services.replacement_task_scope import resolve_scopes
from lifecycle.test_replacement_task_scope import ReplacementTaskScopeTests
from workflow.models import UserTask, WorkItem


class ReplacementScanNoticeTests(ReplacementTaskScopeTests):
    """Each selected method uses real task publishers and domain operations."""

    # Do not inherit the unrelated factory-handoff tests into this test class.
    for _name in tuple(name for name in dir(ReplacementTaskScopeTests) if name.startswith("test_")):
        locals()[_name] = None
    del _name

    def approve_local(self):
        self.call("DECIDE", self.af, decision="REPLACEMENT", replacement_flow_version=3,
                  execution_mode="LOCAL", receiving_address="厂家接收地址")
        return ReplacementFlow.objects.get(application=self.case)

    def scan_item(self, notice=False):
        return WorkItem.objects.get(uniq_key=S._task_key(
            self.case, "replacement-scan" + ("-notice" if notice else ""),
        ))

    def test_local_approval_sends_operator_action_and_owner_knowledge(self):
        flow = self.approve_local()
        action, notice = self.scan_item(), self.scan_item(True)
        self.assertEqual(set(UserTask.objects.filter(workitem=action).values_list("to_openid", flat=True)), {self.sales.wx_openid})
        self.assertTrue(UserTask.objects.get(workitem=action, to_openid=self.sales.wx_openid).need_action)
        owner = UserTask.objects.get(workitem=notice, to_openid=self.owner.wx_openid)
        self.assertFalse(owner.need_action)
        self.assertEqual(notice.status, "DONE")
        self.assertEqual(notice.title, "现场换新已获批准，服务人员待办理")
        self.assertNotIn("扫码", notice.title)
        self.assertFalse(S._is_assignee(self.case, self.owner))
        self.assertFalse(B.describe(self.case, self.owner)["can_scan_swap"])
        for item in (action, notice):
            self.assertEqual(item.payload["root_organization_id"], self.factory.pk)
            self.assertEqual(item.payload["execution_organization_id"], self.merchant.pk)
            self.assertEqual(item.payload["replacement_flow_id"], flow.pk)
            self.assertEqual(item.payload["inbox_group_key"], "aftersales:" + self.case.case_no)

    def test_service_confirmation_readiness_does_not_erase_operator_responsibility(self):
        flow = self.approve_local()
        item = self.scan_item()
        context = resolve_scopes([item], self.sales)[item.pk]
        self.assertTrue(context["responsible"])
        self.assertTrue(context["unfinished"])
        self.assertFalse(context["can_act_now"])
        self.call("CONFIRM_REPLACEMENT_SERVICE", self.sales, service_mode="SHOP")
        self.assertTrue(resolve_scopes([item], self.sales)[item.pk]["can_act_now"])
        self.assertFalse(resolve_scopes([item], self.owner)[item.pk]["responsible"])

    def test_current_valid_operator_outside_old_recipient_role_cohort_is_retained(self):
        profile = SeniorManagerProfile.objects.create(identity=self.manager_i)
        profile.managed_roles.add(OrganizationRole.objects.get(
            organization_type=self.merchant.organization_type, code="merchant_stock"))
        escalation = deepcopy(self.case.escalation)
        escalation.update(source_user_id=self.manager.pk, source_route="ORIGINAL_INSTALLER")
        self.case.escalation = escalation
        self.case.save(update_fields=["escalation"])
        flow = self.approve_local()
        self.assertTrue(S._is_assignee(self.case, self.manager))
        self.assertTrue(B._org_user(self.manager, self.merchant.pk))
        self.assertTrue(UserTask.objects.get(workitem=self.scan_item(), to_openid=self.manager.wx_openid).need_action)
        self.assertFalse(UserTask.objects.filter(workitem=self.scan_item(), to_openid=self.sales.wx_openid).exists())
        self.assertFalse(UserTask.objects.get(workitem=self.scan_item(True), to_openid=self.owner.wx_openid).need_action)

    def test_existing_valid_station_operator_permissions_are_not_removed_or_expanded(self):
        AfterSalesServiceStationLiaison.objects.create(application=self.station, identity=self.owner_i)
        flow = self.approve_local()
        self.assertTrue(S._is_assignee(self.case, self.owner))
        self.assertTrue(UserTask.objects.get(workitem=self.scan_item(), to_openid=self.owner.wx_openid).need_action)
        self.call("CONFIRM_REPLACEMENT_SERVICE", self.owner, service_mode="HOME")
        self.stock(self.merchant)
        self.call("SCAN_SWAP", self.owner, qr_text="NEW-LIFECYCLE")
        self.case.refresh_from_db()
        self.assertEqual(self.case.assigned_user_id, self.sales.pk)
        task = UserTask.objects.get(workitem=self.scan_item(), to_openid=self.sales.wx_openid)
        self.assertEqual(task.state, "DONE")

    def test_operator_completion_finishes_action_without_authorizing_owner(self):
        flow = self.approve_local()
        self.call("CONFIRM_REPLACEMENT_SERVICE", self.sales, service_mode="SHOP")
        self.stock(self.merchant)
        before = self.snapshot()
        with self.assertRaises(AfterSalesError):
            self.call("SCAN_SWAP", self.owner, qr_text="NEW-LIFECYCLE")
        self.assertEqual(before, self.snapshot())
        self.call("SCAN_SWAP", self.sales, qr_text="NEW-LIFECYCLE")
        self.assertEqual(UserTask.objects.get(workitem=self.scan_item(), to_openid=self.sales.wx_openid).state, "DONE")
        flow.refresh_from_db()
        self.assertIsNotNone(flow.swapped_at)
        before = self.snapshot()
        with self.assertRaises(AfterSalesError):
            self.call("SCAN_SWAP", self.sales, qr_text="NEW-LIFECYCLE")
        self.assertEqual(before, self.snapshot())

    def test_existing_misclassified_scan_task_gets_readonly_responsibility_context(self):
        flow = self.approve_local()
        item = self.scan_item()
        old_copy = UserTask.objects.create(workitem=item, to_openid=self.owner.wx_openid, need_action=True)
        before = self.snapshot()
        context = resolve_scopes([item], self.owner)[item.pk]
        self.assertTrue(context["observer"])
        self.assertFalse(context["responsible"])
        self.assertFalse(context["can_act_now"])
        self.assertEqual(context["action"], "scan")
        self.assertEqual(context["leg"], None)
        self.assertEqual(before, self.snapshot())
        old_copy.refresh_from_db()
        self.assertTrue(old_copy.need_action)

    def test_changed_or_inactive_operator_is_not_replaced_by_owner_authority(self):
        flow = self.approve_local()
        item = self.scan_item()
        UserOrganization.objects.filter(user=self.sales).update(is_active=False)
        context = resolve_scopes([item], self.sales)[item.pk]
        self.assertFalse(context["responsible"])
        self.assertFalse(context["can_act_now"])
        self.assertFalse(resolve_scopes([item], self.owner)[item.pk]["responsible"])
        B._notify(self.case, self.merchant.pk, "售后换新：请扫描库存新机生命周期码", "scan", self.af)
        self.assertFalse(UserTask.objects.filter(workitem=item, to_openid=self.owner.wx_openid).exists())

    def test_unknown_or_conflicting_scan_references_are_not_inferred(self):
        flow = self.approve_local()
        item = self.scan_item()
        for change in [
            {"biz_id": "99999"}, {"biz_ref": "AfterSalesApplication:99999"},
            {"uniq_key": item.uniq_key.replace("replacement-scan", "replacement-scan-unknown")},
            {"payload": {**item.payload, "application_id": 99999}},
            {"payload": {**item.payload, "replacement_flow_id": 99999}},
            {"payload": {**item.payload, "root_organization_id": self.other_factory.pk}},
            {"payload": {**item.payload, "execution_organization_id": self.other_merchant.pk}},
            {"payload": {**item.payload, "replacement_leg_id": 99999}},
            {"payload": {**item.payload, "inbox_group_key": "aftersales:OTHER"}},
        ]:
            with self.subTest(change=change):
                bad = copy(item)
                for key, value in change.items():
                    setattr(bad, key, value)
                self.assertNotIn(bad.pk, resolve_scopes([bad], self.owner))

    def test_foreign_root_cannot_become_scan_operator_or_observer(self):
        flow = self.approve_local()
        outsider, _identity = self.user("scan-foreign-owner", self.other_merchant, "merchant_owner")
        item = self.scan_item()
        context = resolve_scopes([item], outsider)[item.pk]
        self.assertFalse(context["responsible"])
        self.assertFalse(context["observer"])
        self.assertFalse(context["can_act_now"])
        flow.execution_organization = self.other_merchant
        flow.save(update_fields=["execution_organization"])
        self.assertNotIn(item.pk, resolve_scopes([item], self.owner))

    def test_scan_scope_resolution_is_readonly_and_batch_loaded(self):
        flow = self.approve_local()
        item = self.scan_item()
        before = self.snapshot()
        with CaptureQueriesContext(connection) as first:
            resolve_scopes([item], self.owner)
        copies = []
        for offset in range(30):
            row = copy(item)
            row.pk = item.pk + 1000 + offset
            copies.append(row)
        with CaptureQueriesContext(connection) as multiple:
            scopes = resolve_scopes(copies, self.owner)
        self.assertEqual(len(scopes), 30)
        self.assertEqual(len(first), len(multiple))
        self.assertLessEqual(len(multiple), 10)
        self.assertTrue(all(q["sql"].lstrip().upper().startswith("SELECT") for q in multiple))
        self.assertEqual(before, self.snapshot())

    def test_delegated_operator_scope_queries_do_not_grow_per_scan_task(self):
        profile = SeniorManagerProfile.objects.create(identity=self.manager_i)
        profile.managed_roles.add(OrganizationRole.objects.get(
            organization_type=self.merchant.organization_type, code="merchant_stock"))
        escalation = deepcopy(self.case.escalation)
        escalation.update(source_user_id=self.manager.pk, source_route="ORIGINAL_INSTALLER")
        self.case.escalation = escalation
        self.case.save(update_fields=["escalation"])
        self.approve_local()
        item = self.scan_item()
        with CaptureQueriesContext(connection) as first:
            single = resolve_scopes([item], self.manager)
        copies = []
        for offset in range(30):
            row = copy(item)
            row.pk = item.pk + 1000 + offset
            copies.append(row)
        with CaptureQueriesContext(connection) as multiple:
            scopes = resolve_scopes(copies, self.manager)
        self.assertTrue(single[item.pk]["responsible"])
        self.assertEqual(len(scopes), 30)
        self.assertEqual(len(first), len(multiple))
        self.assertTrue(all(q["sql"].lstrip().upper().startswith("SELECT") for q in multiple))

    def test_scan_action_notice_publication_is_atomic(self):
        flow = self.approve_local()
        before = self.snapshot()
        original = S._publish_task
        calls = []
        def publish(*args, **kwargs):
            calls.append(kwargs["step"])
            if len(calls) == 2:
                raise RuntimeError("injected scan knowledge failure")
            return original(*args, **kwargs)
        with patch.object(S, "_publish_task", side_effect=publish):
            with self.assertRaisesRegex(RuntimeError, "scan knowledge"):
                B._notify(self.case, self.merchant.pk, "扫码待办理", "scan", self.af)
        self.assertEqual(calls, ["replacement-scan", "replacement-scan-notice"])
        self.assertEqual(before, self.snapshot())
