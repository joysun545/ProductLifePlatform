"""Factory handoff responsibility with actual publishers and current flows."""

from contextlib import ExitStack
from copy import copy
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.files.storage import FileSystemStorage
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from account.models import OrganizationRole, SeniorManagerProfile, UserOrganization
from lifecycle.models import (
    AfterSalesApplication, AfterSalesMedia, LifecycleEvent, OrganizationDevice,
    ReplacementFlow, ReplacementLeg, ReplacementInventoryEntry,
)
from lifecycle.services import replacement_flow_service as B
from lifecycle.services.aftersales_service import AfterSalesService as S
from lifecycle.services.repair_escalation_service import execute
from lifecycle.services.replacement_task_scope import resolve_scopes
from workflow.models import UserTask, WorkItem
import lifecycle.test_replacement_flow as fixture


class ReplacementTaskScopeTests(TestCase):
    user = classmethod(fixture.ReplacementFlowTests.user.__func__)
    stock = fixture.ReplacementFlowTests.stock

    @classmethod
    def setUpTestData(cls):
        fixture.ReplacementFlowTests.setUpTestData.__func__(cls)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        media_root = self.stack.enter_context(TemporaryDirectory())
        self.stack.enter_context(patch.object(
            AfterSalesMedia._meta.get_field("file"), "storage",
            FileSystemStorage(location=media_root),
        ))

    def call(self, action, user, **data):
        result = execute(application_id=self.case.pk, user=user, data={"action": action, **data})
        self.case.refresh_from_db()
        return result

    def swapped(self):
        self.call("DECIDE", self.af, decision="REPLACEMENT", replacement_flow_version=3,
                  execution_mode="LOCAL", receiving_address="厂家验收地址")
        self.stock(self.merchant)
        self.call("CONFIRM_REPLACEMENT_SERVICE", self.sales, service_mode="SHOP")
        self.call("SCAN_SWAP", self.sales, qr_text="NEW-LIFECYCLE")
        flow = ReplacementFlow.objects.get(application=self.case)
        self.assertEqual(flow.protocol_version, 6)
        self.assertEqual(self.case.status, "PENDING_FACTORY_CLOSE")
        return flow

    def old_sent(self):
        flow = self.swapped()
        old = flow.legs.get(direction="OLD")
        image = fixture.upload_transport_proof(self, self.sales, leg=old, kind="DEVICE")
        document = fixture.upload_transport_proof(self, self.sales, leg=old, kind="LOGISTICS")
        self.call("SEND_REPLACEMENT_LEG", self.sales, leg_id=old.pk,
                  device_image_ids=[image.pk], logistics_document_ids=[document.pk],
                  logistics_company="测试物流", tracking_no="SCOPE-OLD", confirmed=True)
        old.refresh_from_db()
        self.assertEqual(old.status, "SENT")
        return flow, old

    def item(self, leg, action, *, notice=False):
        suffix = "-notice" if notice else ""
        return WorkItem.objects.get(uniq_key=S._task_key(
            self.case, f"replacement-{leg.pk}-{action}{suffix}",
        ))

    def snapshot(self):
        return {
            model.__name__: list(model.objects.order_by("pk").values())
            for model in (AfterSalesApplication, ReplacementFlow, ReplacementLeg,
                          ReplacementInventoryEntry, LifecycleEvent, OrganizationDevice,
                          WorkItem, UserTask)
        }

    def test_actual_old_send_creates_logistics_action_and_aftersales_knowledge(self):
        flow, old = self.old_sent()
        action, notice = self.item(old, "receive"), self.item(old, "receive", notice=True)
        logistics = UserTask.objects.get(workitem=action, to_openid=self.logistics.wx_openid)
        aftersales = UserTask.objects.get(workitem=notice, to_openid=self.af.wx_openid)
        self.assertTrue(logistics.need_action)
        self.assertFalse(aftersales.need_action)
        self.assertFalse(UserTask.objects.filter(workitem=action, to_openid=self.af.wx_openid).exists())
        self.assertEqual(action.status, "OPEN")
        self.assertEqual(notice.status, "DONE")
        for item in (action, notice):
            self.assertEqual(item.payload["inbox_group_key"], f"aftersales:{self.case.case_no}")
            self.assertEqual(item.payload["root_organization_id"], self.factory.pk)
            self.assertEqual(item.payload["replacement_leg_id"], old.pk)
        af = resolve_scopes([action, notice], self.af)[action.pk]
        logistics_scope = resolve_scopes([action], self.logistics)[action.pk]
        self.assertTrue(af["observer"])
        self.assertFalse(af["responsible"])
        self.assertFalse(af["can_act_now"])
        self.assertTrue(logistics_scope["responsible"])
        self.assertTrue(logistics_scope["can_act_now"])
        self.assertTrue(logistics_scope["unfinished"])
        self.assertTrue(resolve_scopes([notice], self.af)[notice.pk]["notification_only"])
        flow.refresh_from_db()
        self.assertEqual(flow.recovery["state"], "WAIT_PICKUP")

    def test_stock_keeps_responsibility_while_old_receipt_and_allocation_are_pending(self):
        flow, old = self.old_sent()
        supply = flow.legs.get(direction="SUPPLY", sequence=1)
        B._notify(self.case, self.factory.pk, "换新补发待安排", f"{supply.pk}-send", self.af, leg=supply)
        action = self.item(supply, "send")
        notice = self.item(supply, "send", notice=True)
        self.assertTrue(UserTask.objects.get(workitem=action, to_openid=self.warehouse.wx_openid).need_action)
        self.assertFalse(UserTask.objects.get(workitem=notice, to_openid=self.af.wx_openid).need_action)
        stock = resolve_scopes([action], self.warehouse)[action.pk]
        self.assertTrue(stock["responsible"])
        self.assertTrue(stock["unfinished"])
        self.assertTrue(stock["is_current_stage"])
        self.assertFalse(stock["can_act_now"])
        self.assertFalse(resolve_scopes([action], self.af)[action.pk]["responsible"])
        old.refresh_from_db()
        self.assertEqual(old.status, "SENT")
        self.assertEqual(self.case.status, "PENDING_FACTORY_CLOSE")

    def test_explicit_manager_receives_vacant_logistics_action_not_aftersales_observer_copy(self):
        UserOrganization.objects.filter(user=self.logistics).update(is_active=False)
        manager, identity = self.user("scope-logistics-manager", self.factory, "factory_senior_manager")
        profile = SeniorManagerProfile.objects.create(identity=identity)
        profile.managed_roles.add(OrganizationRole.objects.get(
            organization_type=self.factory.organization_type, code="factory_logistics"))
        flow, old = self.old_sent()
        action = self.item(old, "receive")
        self.assertTrue(UserTask.objects.get(workitem=action, to_openid=manager.wx_openid).need_action)
        self.assertFalse(UserTask.objects.filter(workitem=action, to_openid=self.af.wx_openid).exists())
        context = resolve_scopes([action], manager)[action.pk]
        self.assertTrue(context["responsible"])
        self.assertTrue(context["can_act_now"])

    def test_explicit_stock_manager_remains_responsible_before_dispatch_is_ready(self):
        UserOrganization.objects.filter(user=self.warehouse).update(is_active=False)
        profile = SeniorManagerProfile.objects.create(identity=self.fm_i)
        profile.managed_roles.add(OrganizationRole.objects.get(
            organization_type=self.factory.organization_type, code="factory_stock"))
        flow = self.swapped()
        supply = flow.legs.get(direction="SUPPLY", sequence=1)
        B._notify(self.case, self.factory.pk, "补发仍待前序交接", f"{supply.pk}-send", self.af, leg=supply)
        action = self.item(supply, "send")
        self.assertTrue(UserTask.objects.get(workitem=action, to_openid=self.fm.wx_openid).need_action)
        context = resolve_scopes([action], self.fm)[action.pk]
        self.assertTrue(context["responsible"])
        self.assertFalse(context["can_act_now"])

    def test_aftersales_manager_delegation_does_not_imply_logistics_permission(self):
        profile = SeniorManagerProfile.objects.create(identity=self.fm_i)
        profile.managed_roles.add(OrganizationRole.objects.get(
            organization_type=self.factory.organization_type, code="factory_aftersales"))
        flow, old = self.old_sent()
        action = self.item(old, "receive")
        context = resolve_scopes([action], self.fm)[action.pk]
        self.assertTrue(context["observer"])
        self.assertFalse(context["responsible"])
        self.assertFalse(context["can_act_now"])

    def test_other_factory_and_inactive_identity_get_no_scope_authority(self):
        flow, old = self.old_sent()
        action = self.item(old, "receive")
        foreign, _identity = self.user("scope-foreign-logistics", self.other_factory, "factory_logistics")
        other = resolve_scopes([action], foreign)[action.pk]
        self.assertFalse(other["responsible"])
        self.assertFalse(other["observer"])
        self.assertFalse(other["can_act_now"])
        UserOrganization.objects.filter(user=self.logistics).update(is_active=False)
        inactive = resolve_scopes([action], self.logistics)[action.pk]
        self.assertFalse(inactive["responsible"])
        self.assertFalse(inactive["observer"])

    def test_conflicting_references_and_unknown_keys_are_not_guessed_from_titles(self):
        flow, old = self.old_sent()
        action = self.item(old, "receive")
        changes = [
            {"biz_id": "999999"}, {"biz_ref": "AfterSalesApplication:999999"},
            {"payload": {**action.payload, "application_id": 999999}},
            {"payload": {**action.payload, "replacement_leg_id": 999999}},
            {"payload": {**action.payload, "root_organization_id": self.other_factory.pk}},
            {"payload": {**action.payload, "factory_root_id": self.other_factory.pk}},
            {"payload": {**action.payload, "flow_id": 999999}},
            {"payload": {**action.payload, "case_no": "ANOTHER-AS"}},
            {"payload": {**action.payload, "inbox_group_key": "aftersales:ANOTHER-AS"}},
            {"uniq_key": "unrecognized-but-the-title-matches"},
            {"uniq_key": "aftersales:9999999999999999999999999:replacement-1-receive:v1"},
        ]
        for change in changes:
            with self.subTest(change=change):
                malformed = copy(action)
                for key, value in change.items():
                    setattr(malformed, key, value)
                self.assertNotIn(malformed.pk, resolve_scopes([malformed], self.af))

    def test_dispatch_version_mismatch_is_recorded_without_erasing_current_logistics_responsibility(self):
        flow, old = self.old_sent()
        action = copy(self.item(old, "receive"))
        action.uniq_key = f"aftersales:{self.case.pk}:replacement-{old.pk}-receive:v99"
        action.payload = {**action.payload, "dispatch_version": 99}
        context = resolve_scopes([action], self.logistics)[action.pk]
        self.assertFalse(context["version_matches"])
        self.assertTrue(context["responsible"])
        self.assertTrue(context["can_act_now"])

    def test_resolution_is_read_only_and_batch_loaded(self):
        flow, old = self.old_sent()
        action = self.item(old, "receive")
        before = self.snapshot()
        with CaptureQueriesContext(connection) as queries:
            scopes = resolve_scopes([action], self.af)
        one_count = len(queries)
        copies = []
        for offset in range(30):
            item = copy(action)
            item.pk = action.pk + 1000 + offset
            copies.append(item)
        with CaptureQueriesContext(connection) as repeated_queries:
            repeated = resolve_scopes(copies, self.af)
        self.assertEqual(len(repeated), 30)
        self.assertEqual(len(repeated_queries), one_count)
        self.assertLessEqual(one_count, 8)
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") for row in repeated_queries))
        self.assertEqual(before, self.snapshot())
        self.assertEqual(scopes[action.pk]["leg"].status, "SENT")

    def test_split_publication_rolls_back_if_knowledge_publication_fails(self):
        flow = self.swapped()
        supply = flow.legs.get(direction="SUPPLY", sequence=1)
        before = self.snapshot()
        original = S._publish_task
        calls = []
        def real_first_publication(*args, **kwargs):
            calls.append(kwargs["step"])
            if len(calls) == 2:
                raise RuntimeError("injected second publication failure")
            return original(*args, **kwargs)
        with patch.object(S, "_publish_task", side_effect=real_first_publication):
            with self.assertRaisesRegex(RuntimeError, "second publication"):
                B._notify(self.case, self.factory.pk, "补发待安排", f"{supply.pk}-send", self.af, leg=supply)
        self.assertEqual(len(calls), 2)
        self.assertEqual(before, self.snapshot())

    def test_unrelated_factory_scan_keeps_original_single_action_key(self):
        flow = self.swapped()
        flow.mode = "FACTORY"
        flow.execution_organization = self.factory
        flow.save(update_fields=["mode", "execution_organization"])
        WorkItem.objects.filter(uniq_key__in=[
            S._task_key(self.case, "replacement-scan"),
            S._task_key(self.case, "replacement-scan-notice"),
        ]).delete()
        B._notify(self.case, self.factory.pk, "当前厂家扫码提醒", "scan", self.af)
        key = S._task_key(self.case, "replacement-scan")
        item = WorkItem.objects.get(uniq_key=key)
        self.assertTrue(UserTask.objects.filter(workitem=item, need_action=True).exists())
        self.assertFalse(WorkItem.objects.filter(uniq_key=S._task_key(self.case, "replacement-scan-notice")).exists())
        self.assertEqual(resolve_scopes([item], self.af), {})
