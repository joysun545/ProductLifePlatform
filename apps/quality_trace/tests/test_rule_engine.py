from datetime import date, timedelta
from decimal import Decimal
import io
import json
from pathlib import Path
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from account.models import (
    Organization,
    OrganizationRole,
    OrganizationRelation,
    OrganizationRelationType,
    OrganizationType,
    User,
    UserOrganization,
)
from account.services.auth_service import issue_access_token
from lifecycle.models import (
    ActivationRecord,
    AfterSalesApplication,
    CustomerDeviceBinding,
    CustomerVehicle,
    Device,
    DeviceState,
    DriverVehicleBinding,
    FactoryReturnInventoryItem,
    FactoryReturnProcess,
    GoodsTransfer,
    GoodsTransferItem,
    OrganizationDevice,
)
from manufacturing.models import ProductionBatch
from product.models import BOM, ProductCategory, ProductModel
from quality_trace.models import (
    BatchQualityAffectedDevice,
    BatchQualityCase,
    QualityAlert,
    QualityAlertRule,
    QualityControlAction,
    QualityMetricSnapshot,
    QualityMetricSnapshotDevice,
    QualityTraceEvent,
    QualityGateDecision,
    QualityRecallCampaign,
    QualityRecallCampaignCaseLink,
    QualityRecallDevice,
    QualityRecallReturnLeg,
    QualityRecallCustomerHandover,
    QualityRecallHandoverEvidence,
    QualityRecallTransportLeg,
    QualityRecallIsolationReceipt,
    QualityRecallProgressSnapshot,
    QualityRecallExceptionDecision,
    QualityRecallCloseout,
)
from quality_trace.services.case_service import (
    QualityCaseError,
    assess_alert,
    expire_temporary_holds,
    open_case_from_alert,
    place_temporary_hold,
    revoke_temporary_hold,
    place_formal_hold,
    release_formal_hold,
    start_recall,
    complete_recall,
)
from quality_trace.services.gate_service import (
    QualityOperationBlocked,
    check_device_operation,
    enforce_device_operation,
    quality_gate_atomic,
)
from quality_trace.services.metric_engine import evaluate_active_rules
from quality_trace.services.rule_service import publish_rule_version
from quality_trace.services.recall_service import (
    QualityRecallError,
    create_recall_campaign,
    ensure_recall_campaign,
    launch_recall_campaign,
    transition_recall_device,
)
from quality_trace.services.recall_consolidation_service import (
    consolidate_recall_campaigns,
)
from quality_trace.services.recall_task_service import publish_recall_tasks
from quality_trace.services.recall_return_service import (
    bind_recall_return_transfer,
    synchronize_recall_return_leg,
)
from quality_trace.services.recall_handover_service import (
    accept_factory_pickup_request,
    accept_merchant_handover_by_token,
    acknowledge_customer_recall,
    add_handover_evidence,
    confirm_merchant_handover_receipt,
    initialize_customer_handover,
    schedule_customer_pickup,
    select_customer_delivery_method,
    submit_customer_direct_shipment,
    submit_customer_pickup_shipment,
)
from quality_trace.services.recall_transport_service import (
    create_next_recall_transport_leg,
    dispatch_recall_transport_leg,
    isolate_recalled_device,
    receive_recall_transport_leg,
)
from quality_trace.services.recall_progress_service import (
    calculate_recall_progress,
    complete_recall_campaign,
    decide_unreachable_device,
)
from workflow.models import WorkItem, UserTask


class QualityRuleEngineTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        owner_type = OrganizationType.objects.get(code="OWNER")
        merchant_type = OrganizationType.objects.get(code="MERCHANT")
        cls.factory = Organization.objects.create(
            name="R131测试厂家", code="R131-FACTORY", organization_type=owner_type,
        )
        cls.merchant = Organization.objects.create(
            name="R131测试商家", code="R131-MERCHANT", organization_type=merchant_type,
        )
        cls.manager = User.objects.create_user(
            username="r131-manager", wx_openid="r131-manager-openid",
        )
        cls.applicant = User.objects.create_user(
            username="r131-applicant", wx_openid="r131-applicant-openid",
        )
        cls.viewer = User.objects.create_user(
            username="r132-viewer", wx_openid="r132-viewer-openid",
        )
        UserOrganization.objects.create(
            user=cls.manager,
            organization=cls.factory,
            role=OrganizationRole.objects.get(
                organization_type=owner_type, code="factory_chief_engineer",
            ),
        )
        UserOrganization.objects.create(
            user=cls.viewer,
            organization=cls.factory,
            role=OrganizationRole.objects.get(
                organization_type=owner_type, code="factory_sales_assistant",
            ),
        )
        UserOrganization.objects.create(
            user=cls.applicant,
            organization=cls.merchant,
            role=OrganizationRole.objects.get(
                organization_type=merchant_type, code="merchant_owner",
            ),
        )
        category = ProductCategory.objects.create(name="R131品类", code="R131-CATEGORY")
        cls.product_model = ProductModel.objects.create(
            name="R131型号", code="R131-MODEL", category=category,
        )
        cls.bom = BOM.objects.create(
            product_model=cls.product_model,
            version="R131-V1",
            created_by=cls.manager,
        )
        batch = ProductionBatch.objects.create(
            batch_no="R131-PRODUCTION-BATCH",
            product_model=cls.product_model,
            bom=cls.bom,
            manufacturer=cls.factory,
            production_date=date.today(),
            quantity=3,
        )
        cls.devices = [
            Device.objects.create(
                sn=f"R131-SN-{index}", product_model=cls.product_model, batch=batch,
            )
            for index in range(1, 4)
        ]
        for device in cls.devices[:2]:
            ActivationRecord.objects.create(
                device=device,
                organization=cls.merchant,
                operator=cls.applicant,
            )

    def _payload(self, **overrides):
        data = {
            "code": "MODEL-FAULT-RATE",
            "name": "型号故障率",
            "metric_type": "FAULT_RATE",
            "scope": {
                "target_type": "PRODUCT_MODEL",
                "target_ids": [self.product_model.pk],
            },
            "denominator_policy": {"basis": "ACTIVATED_DEVICE"},
            "fault_policy": {
                "sources": ["AFTERSALES_CLOSED"],
                "final_fault_types": ["COMPONENT_SUSPECTED"],
            },
            "thresholds": {"warning": "0.40", "urgent": "0.70"},
            "window_days": 30,
            "minimum_exposure": 1,
            "minimum_fault_devices": 1,
            "cooldown_hours": 0,
        }
        data.update(overrides)
        return data

    def _closed_fault(self, device, case_no="R131-AS-1"):
        return AfterSalesApplication.objects.create(
            case_no=case_no,
            device=device,
            customer_organization=self.merchant,
            factory_organization=self.factory,
            applicant=self.applicant,
            status=AfterSalesApplication.Status.CLOSED,
            final_fault_type=AfterSalesApplication.FaultType.COMPONENT_SUSPECTED,
            closed_at=timezone.now(),
        )

    def test_publishing_creates_immutable_incrementing_versions(self):
        first = publish_rule_version(user=self.manager, data=self._payload())
        second = publish_rule_version(
            user=self.manager,
            data=self._payload(
                rule_id=first["id"],
                thresholds={"warning": "0.30", "urgent": "0.60"},
            ),
        )
        self.assertEqual(first["version"]["version"], 1)
        self.assertEqual(second["version"]["version"], 2)
        self.assertEqual(
            first["version"]["thresholds"]["warning"], "0.40",
        )

    def test_metric_uses_activated_unique_devices_and_triggers_one_alert(self):
        publish_rule_version(user=self.manager, data=self._payload())
        self._closed_fault(self.devices[0])
        result = evaluate_active_rules(
            source=QualityMetricSnapshot.Source.MANUAL,
            factory_id=self.factory.pk,
        )
        target = result[0]["targets"][0]
        self.assertEqual(target["exposure_device_count"], 2)
        self.assertEqual(target["fault_device_count"], 1)
        self.assertEqual(Decimal(target["metric_value"]), Decimal("0.500000"))
        self.assertEqual(target["severity"], QualityAlert.Severity.WARNING)
        self.assertEqual(QualityAlert.objects.count(), 1)

        evaluate_active_rules(
            source=QualityMetricSnapshot.Source.SCHEDULED,
            factory_id=self.factory.pk,
        )
        self.assertEqual(QualityAlert.objects.count(), 1)
        self.assertEqual(QualityAlert.objects.get().trigger_count, 2)
        self.assertEqual(QualityMetricSnapshot.objects.count(), 2)

    def test_realtime_close_signal_evaluates_after_commit(self):
        publish_rule_version(user=self.manager, data=self._payload())
        application = AfterSalesApplication.objects.create(
            case_no="R131-AS-REALTIME",
            device=self.devices[0],
            customer_organization=self.merchant,
            factory_organization=self.factory,
            applicant=self.applicant,
            status=AfterSalesApplication.Status.PENDING_FACTORY_CLOSE,
            final_fault_type=AfterSalesApplication.FaultType.COMPONENT_SUSPECTED,
        )
        with self.captureOnCommitCallbacks(execute=True):
            application.status = AfterSalesApplication.Status.CLOSED
            application.closed_at = timezone.now()
            application.save(update_fields=("status", "closed_at", "updated_at"))
        self.assertEqual(QualityMetricSnapshot.objects.count(), 1)
        self.assertEqual(QualityAlert.objects.count(), 1)

    def test_scheduled_command_runs_without_active_rules(self):
        call_command("evaluate_quality_alerts", factory_id=self.factory.pk)

    def _evaluated_alert(self):
        publish_rule_version(user=self.manager, data=self._payload())
        self._closed_fault(self.devices[0])
        evaluate_active_rules(
            source=QualityMetricSnapshot.Source.MANUAL,
            factory_id=self.factory.pk,
        )
        return QualityAlert.objects.get()

    def test_metric_snapshot_persists_complete_device_evidence(self):
        self._evaluated_alert()
        snapshot = QualityMetricSnapshot.objects.get()
        rows = snapshot.device_evidence.order_by("device_id")
        self.assertEqual(rows.count(), 2)
        self.assertEqual(
            list(rows.values_list("device_id", "is_fault")),
            [(self.devices[0].pk, True), (self.devices[1].pk, False)],
        )
        row = rows.first()
        row.is_fault = False
        with self.assertRaisesMessage(ValueError, "不可覆盖"):
            row.save()

    def test_assessment_and_case_freeze_full_exposure_cohort(self):
        alert = self._evaluated_alert()
        assessment = assess_alert(
            user=self.manager,
            alert_id=alert.pk,
            conclusion="CONFIRMED_DEFECT",
            reason="人工复核确认需要立案",
            evidence={"inspection": "confirmed"},
        )
        quality_case = open_case_from_alert(
            user=self.manager,
            alert_id=alert.pk,
            reason="冻结当前暴露设备进行调查",
        )
        self.assertEqual(assessment.assessed_role_snapshot, "factory_chief_engineer")
        self.assertEqual(quality_case.affected_devices.count(), 2)
        self.assertEqual(
            quality_case.affected_devices.filter(
                trace_evidence_snapshot__is_fault=True,
            ).count(),
            1,
        )
        alert.refresh_from_db()
        self.assertEqual(alert.status, QualityAlert.Status.ESCALATED)
        self.assertTrue(
            QualityTraceEvent.objects.filter(event_type="QUALITY_CASE_OPENED").exists()
        )

    def test_temporary_hold_can_be_reversed_without_side_app_writes(self):
        alert = self._evaluated_alert()
        assess_alert(
            user=self.manager, alert_id=alert.pk,
            conclusion="TEMPORARY_HOLD", reason="需要短期隔离核查",
        )
        quality_case = open_case_from_alert(
            user=self.manager, alert_id=alert.pk, reason="建立调查案件",
        )
        action = place_temporary_hold(
            user=self.manager, case_id=quality_case.pk,
            reason="等待抽检结果", duration_hours=24,
        )
        quality_case.refresh_from_db()
        self.assertEqual(quality_case.status, BatchQualityCase.Status.HELD)
        self.assertEqual(action.operation_scope["affected_device_count"], 2)
        revoke_temporary_hold(
            user=self.manager, action_id=action.pk, reason="抽检后解除",
        )
        action.refresh_from_db()
        quality_case.refresh_from_db()
        self.assertEqual(action.status, QualityControlAction.Status.REVOKED)
        self.assertEqual(quality_case.status, BatchQualityCase.Status.OPEN)
        self.assertTrue(action.ended_at)

    def test_temporary_hold_expires_and_unauthorized_role_is_rejected(self):
        alert = self._evaluated_alert()
        with self.assertRaisesMessage(QualityCaseError, "无权"):
            assess_alert(
                user=self.viewer, alert_id=alert.pk,
                conclusion="CONTINUE_WATCH", reason="越权测试",
            )
        assess_alert(
            user=self.manager, alert_id=alert.pk,
            conclusion="TEMPORARY_HOLD", reason="准备到期测试",
        )
        quality_case = open_case_from_alert(
            user=self.manager, alert_id=alert.pk, reason="到期测试案件",
        )
        action = place_temporary_hold(
            user=self.manager, case_id=quality_case.pk,
            reason="一小时临时拦截", duration_hours=1,
        )
        expired = expire_temporary_holds(at=action.expires_at + timedelta(seconds=1))
        self.assertEqual(expired, 1)
        action.refresh_from_db()
        quality_case.refresh_from_db()
        self.assertEqual(action.status, QualityControlAction.Status.EXPIRED)
        self.assertEqual(quality_case.status, BatchQualityCase.Status.OPEN)
        self.assertTrue(
            QualityTraceEvent.objects.filter(event_type="TEMPORARY_HOLD_EXPIRED").exists()
        )

    def test_active_hold_blocks_all_central_device_operations_and_audits(self):
        alert = self._evaluated_alert()
        assess_alert(
            user=self.manager, alert_id=alert.pk,
            conclusion="TEMPORARY_HOLD", reason="中央闸门测试",
        )
        quality_case = open_case_from_alert(
            user=self.manager, alert_id=alert.pk, reason="冻结闸门测试设备",
        )
        action = place_temporary_hold(
            user=self.manager, case_id=quality_case.pk,
            reason="验证五类操作拦截", duration_hours=24,
        )
        operations = [value for value, _ in QualityGateDecision.Operation.choices]
        for operation in operations:
            decision = check_device_operation(
                device_id=self.devices[0].pk,
                operation=operation,
                user=self.manager,
                context={"test": "r133"},
            )
            self.assertFalse(decision["allowed"])
            self.assertEqual(decision["control_action_id"], action.pk)
        self.assertEqual(QualityGateDecision.objects.count(), len(operations))
        with self.assertRaises(QualityOperationBlocked):
            enforce_device_operation(
                device_id=self.devices[0].pk,
                operation="ACTIVATION",
                user=self.manager,
            )
        self.assertEqual(QualityGateDecision.objects.count(), len(operations) + 1)

    def test_gate_is_device_exact_and_allows_after_hold_revocation(self):
        alert = self._evaluated_alert()
        assess_alert(
            user=self.manager, alert_id=alert.pk,
            conclusion="TEMPORARY_HOLD", reason="设备精确范围测试",
        )
        quality_case = open_case_from_alert(
            user=self.manager, alert_id=alert.pk, reason="冻结两台激活设备",
        )
        action = place_temporary_hold(
            user=self.manager, case_id=quality_case.pk,
            reason="精确设备闸门", duration_hours=24,
        )
        outside = check_device_operation(
            device_id=self.devices[2].pk,
            operation="SHIPMENT",
            user=self.manager,
        )
        self.assertTrue(outside["allowed"])
        self.assertEqual(QualityGateDecision.objects.count(), 0)
        revoke_temporary_hold(
            user=self.manager, action_id=action.pk, reason="解除后放行",
        )
        inside = check_device_operation(
            device_id=self.devices[0].pk,
            operation="SHIPMENT",
            user=self.manager,
            record_allowed=True,
        )
        self.assertTrue(inside["allowed"])
        self.assertEqual(QualityGateDecision.objects.get().decision_code, "ALLOWED")

    def test_business_atomic_rollback_preserves_block_audit(self):
        alert = self._evaluated_alert()
        assess_alert(
            user=self.manager, alert_id=alert.pk,
            conclusion="TEMPORARY_HOLD", reason="事务审计测试",
        )
        quality_case = open_case_from_alert(
            user=self.manager, alert_id=alert.pk, reason="事务审计案件",
        )
        action = place_temporary_hold(
            user=self.manager, case_id=quality_case.pk,
            reason="验证业务回滚后保留审计", duration_hours=24,
        )

        @quality_gate_atomic(RuntimeError)
        def simulated_business_write():
            enforce_device_operation(
                device_id=self.devices[0].pk,
                operation="SHIPMENT",
                user=self.manager,
                context={"source": "ATOMIC_ROLLBACK_AUDIT_TEST"},
            )

        with self.assertRaisesMessage(RuntimeError, "门禁审计"):
            simulated_business_write()
        row = QualityGateDecision.objects.get()
        self.assertFalse(row.allowed)
        self.assertEqual(row.control_action_id, action.pk)
        self.assertEqual(
            row.context_snapshot["source"], "ATOMIC_ROLLBACK_AUDIT_TEST",
        )

    def test_formal_hold_blocks_until_explicit_release(self):
        alert = self._evaluated_alert()
        assess_alert(
            user=self.manager, alert_id=alert.pk,
            conclusion="CONFIRMED_DEFECT", reason="确认正式拦截",
        )
        quality_case = open_case_from_alert(
            user=self.manager, alert_id=alert.pk, reason="正式拦截案件",
        )
        action = place_formal_hold(
            user=self.manager, case_id=quality_case.pk, reason="正式隔离设备",
        )
        self.assertFalse(check_device_operation(
            device_id=self.devices[0].pk, operation="SHIPMENT",
        )["allowed"])
        release_formal_hold(
            user=self.manager, action_id=action.pk, reason="复核后解除正式拦截",
        )
        action.refresh_from_db()
        quality_case.refresh_from_db()
        self.assertEqual(action.status, QualityControlAction.Status.REVOKED)
        self.assertEqual(quality_case.status, BatchQualityCase.Status.OPEN)
        self.assertTrue(check_device_operation(
            device_id=self.devices[0].pk, operation="SHIPMENT",
        )["allowed"])
        self.assertTrue(quality_case.control_actions.filter(
            action_type=QualityControlAction.ActionType.RELEASE,
            status=QualityControlAction.Status.COMPLETED,
        ).exists())

    def test_recall_blocks_operations_and_completes_into_remediation(self):
        alert = self._evaluated_alert()
        assess_alert(
            user=self.manager, alert_id=alert.pk,
            conclusion="CONFIRMED_DEFECT", reason="确认需要召回",
        )
        quality_case = open_case_from_alert(
            user=self.manager, alert_id=alert.pk, reason="召回案件",
        )
        action = start_recall(
            user=self.manager, case_id=quality_case.pk, reason="启动设备召回",
        )
        quality_case.refresh_from_db()
        self.assertEqual(quality_case.status, BatchQualityCase.Status.RECALLING)
        self.assertFalse(check_device_operation(
            device_id=self.devices[0].pk, operation="ACTIVATION",
        )["allowed"])
        complete_recall(
            user=self.manager, action_id=action.pk, reason="冻结集合全部完成召回",
        )
        action.refresh_from_db()
        quality_case.refresh_from_db()
        self.assertEqual(action.status, QualityControlAction.Status.COMPLETED)
        self.assertEqual(quality_case.status, BatchQualityCase.Status.REMEDIATING)
        remediation_decision = check_device_operation(
            device_id=self.devices[0].pk, operation="ACTIVATION",
        )
        self.assertFalse(remediation_decision["allowed"])
        self.assertEqual(
            remediation_decision["control_action_type"],
            "REMEDIATION_ISOLATION",
        )
        self.assertIsNone(remediation_decision["control_action_id"])
        self.assertEqual(remediation_decision["quality_case_id"], quality_case.pk)
        remediation_audit = QualityGateDecision.objects.get(
            pk=remediation_decision["decision_id"],
        )
        self.assertEqual(remediation_audit.quality_case_id, quality_case.pk)
        self.assertIsNone(remediation_audit.control_action_id)
        self.assertEqual(
            remediation_audit.context_snapshot["quality_gate_basis"],
            "REMEDIATION_ISOLATION",
        )
        self.assertFalse(quality_case.control_actions.filter(
            status=QualityControlAction.Status.ACTIVE,
        ).exists())

    def test_recall_can_be_added_while_temporary_hold_remains_active(self):
        alert = self._evaluated_alert()
        assess_alert(
            user=self.manager, alert_id=alert.pk,
            conclusion="CONFIRMED_DEFECT", reason="临时拦截后升级召回",
        )
        quality_case = open_case_from_alert(
            user=self.manager, alert_id=alert.pk, reason="递进处置测试",
        )
        temporary = place_temporary_hold(
            user=self.manager, case_id=quality_case.pk,
            reason="先阻断流转", duration_hours=24,
        )
        recall = start_recall(
            user=self.manager, case_id=quality_case.pk,
            reason="追加启动召回",
        )
        temporary.refresh_from_db()
        quality_case.refresh_from_db()
        self.assertEqual(temporary.status, QualityControlAction.Status.ACTIVE)
        self.assertEqual(recall.status, QualityControlAction.Status.ACTIVE)
        self.assertEqual(quality_case.status, BatchQualityCase.Status.RECALLING)
        with self.assertRaisesMessage(QualityCaseError, "召回进行中"):
            revoke_temporary_hold(
                user=self.manager, action_id=temporary.pk,
                reason="召回中不应允许解除",
            )
        quality_case.refresh_from_db()
        self.assertEqual(quality_case.status, BatchQualityCase.Status.RECALLING)

    def test_recall_can_be_added_while_formal_hold_remains_active(self):
        alert = self._evaluated_alert()
        assess_alert(
            user=self.manager, alert_id=alert.pk,
            conclusion="CONFIRMED_DEFECT", reason="正式拦截后升级召回",
        )
        quality_case = open_case_from_alert(
            user=self.manager, alert_id=alert.pk, reason="正式管控递进测试",
        )
        formal = place_formal_hold(
            user=self.manager, case_id=quality_case.pk, reason="正式阻断流转",
        )
        recall = start_recall(
            user=self.manager, case_id=quality_case.pk, reason="正式拦截期间启动召回",
        )
        formal.refresh_from_db()
        quality_case.refresh_from_db()
        self.assertEqual(formal.status, QualityControlAction.Status.ACTIVE)
        self.assertEqual(recall.status, QualityControlAction.Status.ACTIVE)
        self.assertEqual(quality_case.status, BatchQualityCase.Status.RECALLING)
        with self.assertRaisesMessage(QualityCaseError, "召回进行中"):
            release_formal_hold(
                user=self.manager, action_id=formal.pk,
                reason="召回中不应允许解除",
            )

    def test_temporary_hold_upgrades_to_formal_without_gate_gap(self):
        alert = self._evaluated_alert()
        assess_alert(
            user=self.manager, alert_id=alert.pk,
            conclusion="CONFIRMED_DEFECT", reason="升级正式拦截",
        )
        quality_case = open_case_from_alert(
            user=self.manager, alert_id=alert.pk, reason="无门禁空窗测试",
        )
        temporary = place_temporary_hold(
            user=self.manager, case_id=quality_case.pk,
            reason="先临时拦截", duration_hours=24,
        )
        formal = place_formal_hold(
            user=self.manager, case_id=quality_case.pk,
            reason="升级为正式拦截",
        )
        temporary.refresh_from_db()
        self.assertEqual(temporary.status, QualityControlAction.Status.COMPLETED)
        self.assertIn("升级为正式拦截", temporary.end_reason)
        self.assertEqual(formal.status, QualityControlAction.Status.ACTIVE)
        self.assertFalse(check_device_operation(
            device_id=self.devices[0].pk, operation="SHIPMENT",
        )["allowed"])
        self.assertTrue(QualityTraceEvent.objects.filter(
            quality_case=quality_case,
            event_type="TEMPORARY_HOLD_UPGRADED",
        ).exists())

    def _recall_case(self):
        alert = self._evaluated_alert()
        assess_alert(
            user=self.manager,
            alert_id=alert.pk,
            conclusion="CONFIRMED_DEFECT",
            reason="确认进入召回批次测试",
        )
        return open_case_from_alert(
            user=self.manager,
            alert_id=alert.pk,
            reason="R13.6.1召回批次基础测试",
        )

    def _recall_campaign(self, quality_case, device_ids=None):
        return create_recall_campaign(
            user=self.manager,
            case_id=quality_case.pk,
            title="R13.6.1产品召回",
            reason="重复售后故障达到召回条件",
            risk_level="HIGH",
            remedy_type="RETURN",
            deadline=timezone.localdate() + timedelta(days=10),
            device_ids=device_ids,
        )

    def test_quality_trace_test_data_reset_preserves_rules_and_business_chain(self):
        quality_case = self._recall_case()
        campaign = self._recall_campaign(quality_case)
        launch_recall_campaign(user=self.manager, campaign_id=campaign.pk)
        rule = quality_case.source_alert.rule_version.rule
        rule_version_count = rule.versions.count()
        device_ids = list(
            Device.objects.order_by("id").values_list("id", flat=True)
        )

        ordinary_workitem = WorkItem.objects.create(
            domain="SYSTEM",
            type="NON_QUALITY_TEST_NOTICE",
            biz_id="R13851-NON-QUALITY",
            title="普通业务任务应保留",
            uniq_key="r13851-non-quality-workitem",
        )

        dry_run_output = io.StringIO()
        call_command("reset_quality_trace_test_data", stdout=dry_run_output)
        self.assertIn('"mode": "DRY_RUN"', dry_run_output.getvalue())
        self.assertTrue(QualityRecallCampaign.objects.filter(pk=campaign.pk).exists())
        self.assertTrue(WorkItem.objects.filter(pk=ordinary_workitem.pk).exists())

        commit_output = io.StringIO()
        call_command(
            "reset_quality_trace_test_data",
            commit=True,
            confirm="RESET_QUALITY_TRACE",
            stdout=commit_output,
        )
        self.assertIn('"status": "COMPLETED"', commit_output.getvalue())

        rule.refresh_from_db()
        self.assertEqual(rule.status, QualityAlertRule.Status.SUSPENDED)
        self.assertEqual(rule.versions.count(), rule_version_count)
        self.assertFalse(QualityAlert.objects.exists())
        self.assertFalse(QualityMetricSnapshot.objects.exists())
        self.assertFalse(BatchQualityCase.objects.exists())
        self.assertFalse(QualityControlAction.objects.exists())
        self.assertFalse(QualityRecallCampaign.objects.exists())
        self.assertFalse(QualityTraceEvent.objects.exists())
        self.assertFalse(WorkItem.objects.filter(type__startswith="QUALITY_").exists())
        self.assertTrue(WorkItem.objects.filter(pk=ordinary_workitem.pk).exists())
        self.assertEqual(
            list(Device.objects.order_by("id").values_list("id", flat=True)),
            device_ids,
        )

    def _overlap_case(self, suffix, device_ids):
        quality_case = BatchQualityCase.objects.create(
            case_no=f"QT-R13831-{suffix}",
            factory_organization=self.factory,
            source_alert=None,
            scope_type="R13831_OVERLAP",
            scope_key=f"R13831-{suffix}",
            scope_snapshot={"source": "R13.8.3.1_TEST"},
            reason="验证重叠召回主批次接管",
            status=BatchQualityCase.Status.OPEN,
            opened_by=self.manager,
        )
        by_id = {device.pk: device for device in self.devices}
        for device_id in device_ids:
            device = by_id[device_id]
            BatchQualityAffectedDevice.objects.create(
                quality_case=quality_case,
                device=device,
                sn_snapshot=device.sn,
                production_batch_snapshot=device.batch.batch_no,
                product_model_snapshot={
                    "id": self.product_model.pk,
                    "name": self.product_model.name,
                },
                location_snapshot={},
                trace_evidence_snapshot={"source": "R13.8.3.1_TEST"},
            )
        return quality_case

    def _historical_active_campaign(self, quality_case, suffix):
        action = start_recall(
            user=self.manager,
            case_id=quality_case.pk,
            reason="模拟升级前已经存在的重叠召回",
        )
        memberships = list(
            quality_case.affected_devices.select_related("device").order_by("device_id")
        )
        now = timezone.now()
        campaign = QualityRecallCampaign.objects.create(
            recall_no=f"RC-R13831-{suffix}",
            quality_case=quality_case,
            factory_organization=self.factory,
            control_action=action,
            title="R13.8.3.1历史重叠召回",
            reason="模拟历史版本产生的重叠批次",
            deadline=timezone.localdate() + timedelta(days=10),
            status=QualityRecallCampaign.Status.ACTIVE,
            scope_snapshot={"source": "R13.8.3.1_TEST"},
            device_count=len(memberships),
            created_by=self.manager,
            launched_by=self.manager,
            launched_at=now,
        )
        QualityRecallCampaignCaseLink.objects.create(
            campaign=campaign,
            quality_case=quality_case,
            link_type=QualityRecallCampaignCaseLink.LinkType.PRIMARY,
            linked_by=self.manager,
            reason="模拟历史批次主案件关系",
        )
        QualityRecallDevice.objects.bulk_create([
            QualityRecallDevice(
                campaign=campaign,
                case_membership=membership,
                device=membership.device,
                sn_snapshot=membership.sn_snapshot,
                product_model_snapshot=membership.product_model_snapshot,
                production_batch_snapshot=membership.production_batch_snapshot,
                location_snapshot=membership.location_snapshot,
                responsibility_snapshot={},
                trace_evidence_snapshot=membership.trace_evidence_snapshot,
                status_updated_at=now,
            )
            for membership in memberships
        ])
        publish_recall_tasks(user=self.manager, campaign_id=campaign.pk)
        return campaign

    def test_recall_campaign_freezes_selected_case_device_scope(self):
        quality_case = self._recall_case()
        selected = quality_case.affected_devices.order_by("device_id").first()
        campaign = self._recall_campaign(quality_case, [selected.device_id])

        self.assertEqual(campaign.status, QualityRecallCampaign.Status.DRAFT)
        self.assertEqual(campaign.device_count, 1)
        self.assertEqual(
            list(campaign.devices.values_list("device_id", flat=True)),
            [selected.device_id],
        )
        BatchQualityAffectedDevice.objects.create(
            quality_case=quality_case,
            device=self.devices[2],
            sn_snapshot=self.devices[2].sn,
            production_batch_snapshot=self.devices[2].batch.batch_no,
            product_model_snapshot={"id": self.product_model.pk},
            location_snapshot={},
            trace_evidence_snapshot={},
        )
        campaign.refresh_from_db()
        self.assertEqual(campaign.device_count, 1)
        self.assertEqual(campaign.devices.count(), 1)

    def test_recall_campaign_creates_primary_case_link(self):
        quality_case = self._recall_case()
        campaign = self._recall_campaign(quality_case)

        link = campaign.case_links.get()
        self.assertEqual(link.quality_case_id, quality_case.pk)
        self.assertEqual(
            link.link_type,
            QualityRecallCampaignCaseLink.LinkType.PRIMARY,
        )

    def test_fully_covered_case_reuses_live_campaign_without_duplicate_task(self):
        primary_case = self._recall_case()
        campaign = self._recall_campaign(primary_case)
        launch_recall_campaign(user=self.manager, campaign_id=campaign.pk)
        selected_id = primary_case.affected_devices.order_by("device_id").first().device_id
        contributing_case = self._overlap_case("REUSE", [selected_id])
        original_workitem_ids = set(WorkItem.objects.values_list("pk", flat=True))

        reused = ensure_recall_campaign(
            user=self.manager,
            case_id=contributing_case.pk,
            reason="同一设备复用现有召回执行",
        )

        contributing_case.refresh_from_db()
        self.assertEqual(reused.pk, campaign.pk)
        self.assertEqual(contributing_case.status, BatchQualityCase.Status.RECALLING)
        self.assertTrue(QualityRecallCampaignCaseLink.objects.filter(
            campaign=campaign,
            quality_case=contributing_case,
            link_type=QualityRecallCampaignCaseLink.LinkType.CONTRIBUTING,
        ).exists())
        self.assertEqual(
            set(WorkItem.objects.values_list("pk", flat=True)),
            original_workitem_ids,
        )
        self.assertEqual(QualityRecallCampaign.objects.filter(
            status__in=("DRAFT", "ACTIVE"),
        ).count(), 1)

    def test_partial_overlap_is_blocked_until_campaigns_are_consolidated(self):
        primary_case = self._recall_case()
        memberships = list(primary_case.affected_devices.order_by("device_id"))
        campaign = self._recall_campaign(primary_case, [memberships[0].device_id])
        launch_recall_campaign(user=self.manager, campaign_id=campaign.pk)
        overlapping_case = self._overlap_case(
            "PARTIAL",
            [memberships[0].device_id, memberships[1].device_id],
        )

        with self.assertRaisesMessage(QualityRecallError, "范围重叠"):
            ensure_recall_campaign(
                user=self.manager,
                case_id=overlapping_case.pk,
                reason="部分重叠不得直接复用",
            )

        self.assertFalse(overlapping_case.recall_campaign_links.exists())
        self.assertEqual(QualityRecallCampaign.objects.count(), 1)

    def test_historical_subset_campaign_is_superseded_and_tasks_are_closed(self):
        primary_case = self._recall_case()
        canonical = self._recall_campaign(primary_case)
        launch_recall_campaign(user=self.manager, campaign_id=canonical.pk)
        selected_id = primary_case.affected_devices.order_by("device_id").first().device_id
        redundant_case = self._overlap_case("HISTORY", [selected_id])
        redundant = self._historical_active_campaign(redundant_case, "HISTORY")
        redundant_action_id = redundant.control_action_id
        redundant_workitems = list(WorkItem.objects.filter(
            uniq_key__startswith=f"quality-recall:{redundant.pk}:",
        ))
        self.assertTrue(redundant_workitems)
        preview_output = io.StringIO()
        call_command("consolidate_overlapping_recalls", stdout=preview_output)
        preview = json.loads(preview_output.getvalue())
        self.assertEqual(preview["mode"], "DRY_RUN")
        self.assertEqual(preview["overlap_group_count"], 1)
        self.assertEqual(
            preview["results"][0]["canonical_campaign_id"],
            canonical.pk,
        )

        result = consolidate_recall_campaigns(
            user=self.manager,
            canonical_campaign_id=canonical.pk,
            redundant_campaign_ids=[redundant.pk],
            reason="同一设备只保留一个活动召回执行",
        )

        redundant.refresh_from_db()
        redundant_case.refresh_from_db()
        self.assertEqual(redundant.status, QualityRecallCampaign.Status.SUPERSEDED)
        self.assertEqual(redundant.superseded_by_id, canonical.pk)
        self.assertEqual(redundant.devices.count(), 1)
        self.assertEqual(redundant_case.status, BatchQualityCase.Status.RECALLING)
        self.assertEqual(
            QualityControlAction.objects.get(pk=redundant_action_id).status,
            QualityControlAction.Status.COMPLETED,
        )
        self.assertFalse(WorkItem.objects.filter(
            pk__in=[item.pk for item in redundant_workitems],
            status="OPEN",
        ).exists())
        self.assertFalse(UserTask.objects.filter(
            workitem_id__in=[item.pk for item in redundant_workitems],
        ).exclude(state="DONE").exists())
        self.assertTrue(QualityRecallCampaignCaseLink.objects.filter(
            campaign=canonical,
            quality_case=redundant_case,
            source_campaign=redundant,
        ).exists())
        self.assertEqual(
            result["superseded_campaigns"][0]["campaign_id"],
            redundant.pk,
        )
        self.assertTrue(QualityTraceEvent.objects.filter(
            quality_case=redundant_case,
            event_type="RECALL_CAMPAIGN_SUPERSEDED",
        ).exists())
        case_detail = self.client.get(
            f"/pldp/api/quality-trace/cases/{redundant_case.pk}/",
            HTTP_AUTHORIZATION=f"Bearer {issue_access_token(self.manager)}",
        )
        self.assertEqual(case_detail.status_code, 200, case_detail.json())
        self.assertEqual(
            case_detail.json()["data"]["recall_campaign"]["id"],
            canonical.pk,
        )

    def test_recall_device_membership_keys_are_immutable(self):
        quality_case = self._recall_case()
        campaign = self._recall_campaign(quality_case)
        row = campaign.devices.order_by("device_id").first()
        row.device = self.devices[2]
        with self.assertRaisesMessage(ValueError, "不可更换"):
            row.save()

    def test_launch_recall_campaign_binds_control_action(self):
        quality_case = self._recall_case()
        campaign = self._recall_campaign(quality_case)
        launched = launch_recall_campaign(
            user=self.manager,
            campaign_id=campaign.pk,
        )
        quality_case.refresh_from_db()
        self.assertEqual(launched.status, QualityRecallCampaign.Status.ACTIVE)
        self.assertEqual(
            launched.control_action.action_type,
            QualityControlAction.ActionType.RECALL,
        )
        self.assertEqual(quality_case.status, BatchQualityCase.Status.RECALLING)
        self.assertEqual(launched.devices.count(), launched.device_count)

    def test_case_recall_endpoint_automatically_creates_and_binds_campaign(self):
        quality_case = self._recall_case()
        auth = {"HTTP_AUTHORIZATION": f"Bearer {issue_access_token(self.manager)}"}

        response = self.client.post(
            f"/pldp/api/quality-trace/cases/{quality_case.pk}/recall/",
            {"reason": "重复故障达到召回条件，立即启动召回"},
            content_type="application/json",
            **auth,
        )

        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["code"], 0)
        campaign = QualityRecallCampaign.objects.get(pk=response.json()["data"]["id"])
        self.assertEqual(campaign.status, QualityRecallCampaign.Status.ACTIVE)
        self.assertEqual(campaign.risk_level, "")
        self.assertEqual(campaign.remedy_type, "")
        self.assertIsNotNone(campaign.control_action_id)
        self.assertEqual(QualityControlAction.objects.filter(
            quality_case=quality_case,
            action_type=QualityControlAction.ActionType.RECALL,
        ).count(), 1)

        repeated = self.client.post(
            f"/pldp/api/quality-trace/cases/{quality_case.pk}/recall/",
            {"reason": "重复提交不应新建数据"},
            content_type="application/json",
            **auth,
        )
        self.assertEqual(repeated.status_code, 200, repeated.json())
        self.assertEqual(repeated.json()["data"]["id"], campaign.pk)
        self.assertEqual(QualityRecallCampaign.objects.filter(
            quality_case=quality_case,
        ).count(), 1)

    def test_case_detail_exposes_three_progressive_control_levels(self):
        quality_case = self._recall_case()
        auth = {"HTTP_AUTHORIZATION": f"Bearer {issue_access_token(self.manager)}"}
        detail_url = f"/pldp/api/quality-trace/cases/{quality_case.pk}/"

        initial = self.client.get(detail_url, **auth).json()["data"]
        self.assertTrue(initial["available_actions"]["temporary_hold"])
        self.assertTrue(initial["available_actions"]["formal_hold"])
        self.assertTrue(initial["available_actions"]["start_recall"])

        place_temporary_hold(
            user=self.manager,
            case_id=quality_case.pk,
            reason="先执行临时拦截",
            duration_hours=24,
        )
        after_temporary = self.client.get(detail_url, **auth).json()["data"]
        self.assertFalse(after_temporary["available_actions"]["temporary_hold"])
        self.assertTrue(after_temporary["available_actions"]["formal_hold"])
        self.assertTrue(after_temporary["available_actions"]["start_recall"])

        place_formal_hold(
            user=self.manager,
            case_id=quality_case.pk,
            reason="升级到正式拦截",
        )
        after_formal = self.client.get(detail_url, **auth).json()["data"]
        self.assertFalse(after_formal["available_actions"]["temporary_hold"])
        self.assertFalse(after_formal["available_actions"]["formal_hold"])
        self.assertTrue(after_formal["available_actions"]["start_recall"])

        started = self.client.post(
            f"/pldp/api/quality-trace/cases/{quality_case.pk}/recall/",
            {"reason": "升级到召回处置"},
            content_type="application/json",
            **auth,
        )
        self.assertEqual(started.status_code, 200, started.json())
        self.assertEqual(started.json()["code"], 0)
        after_recall = self.client.get(detail_url, **auth).json()["data"]
        self.assertFalse(after_recall["available_actions"]["temporary_hold"])
        self.assertFalse(after_recall["available_actions"]["formal_hold"])
        self.assertFalse(after_recall["available_actions"]["start_recall"])
        self.assertTrue(after_recall["available_actions"]["open_recall_detail"])
        self.assertIsNotNone(after_recall["recall_campaign"])
        self.assertFalse(check_device_operation(
            device_id=self.devices[0].pk,
            operation="SHIPMENT",
        )["allowed"])

    def test_case_detail_returns_current_recall_campaign_navigation(self):
        quality_case = self._recall_case()
        campaign = self._recall_campaign(quality_case)
        auth = {"HTTP_AUTHORIZATION": f"Bearer {issue_access_token(self.manager)}"}

        response = self.client.get(
            f"/pldp/api/quality-trace/cases/{quality_case.pk}/",
            **auth,
        )

        self.assertEqual(response.status_code, 200, response.json())
        data = response.json()["data"]
        self.assertEqual(data["recall_campaign"]["id"], campaign.pk)
        self.assertEqual(data["recall_campaign"]["status"], "DRAFT")
        self.assertTrue(data["recall_binding"]["is_consistent"])
        self.assertFalse(data["available_actions"]["create_recall_campaign"])
        self.assertTrue(data["available_actions"]["open_recall_detail"])
        self.assertEqual(
            data["available_actions"]["continue_recall_campaign_id"],
            campaign.pk,
        )

    def test_case_recall_endpoint_silently_backfills_legacy_action(self):
        quality_case = self._recall_case()
        orphan = start_recall(
            user=self.manager,
            case_id=quality_case.pk,
            reason="历史召回控制动作",
        )
        auth = {"HTTP_AUTHORIZATION": f"Bearer {issue_access_token(self.manager)}"}

        response = self.client.post(
            f"/pldp/api/quality-trace/cases/{quality_case.pk}/recall/",
            {},
            content_type="application/json",
            **auth,
        )

        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["code"], 0)
        campaign = QualityRecallCampaign.objects.get(pk=response.json()["data"]["id"])
        self.assertEqual(campaign.status, QualityRecallCampaign.Status.ACTIVE)
        self.assertEqual(campaign.control_action_id, orphan.pk)
        self.assertEqual(QualityControlAction.objects.filter(
            quality_case=quality_case,
            action_type=QualityControlAction.ActionType.RECALL,
        ).count(), 1)

    def test_creating_campaign_backfills_legacy_orphan_recall_action(self):
        quality_case = self._recall_case()
        orphan = start_recall(
            user=self.manager,
            case_id=quality_case.pk,
            reason="历史版本直接创建的孤立召回控制动作",
        )

        campaign = self._recall_campaign(quality_case)

        campaign.refresh_from_db()
        self.assertEqual(campaign.status, QualityRecallCampaign.Status.ACTIVE)
        self.assertEqual(campaign.control_action_id, orphan.pk)
        self.assertEqual(campaign.launched_at, orphan.effective_at)
        self.assertTrue(QualityTraceEvent.objects.filter(
            quality_case=quality_case,
            event_type="RECALL_CAMPAIGN_BACKFILLED",
            payload__control_action_id=orphan.pk,
        ).exists())
        self.assertEqual(QualityControlAction.objects.filter(
            quality_case=quality_case,
            action_type=QualityControlAction.ActionType.RECALL,
            status=QualityControlAction.Status.ACTIVE,
        ).count(), 1)

    def test_recall_device_status_uses_explicit_transition_graph(self):
        quality_case = self._recall_case()
        campaign = self._recall_campaign(quality_case)
        launch_recall_campaign(user=self.manager, campaign_id=campaign.pk)
        row = campaign.devices.order_by("device_id").first()

        with self.assertRaisesMessage(QualityRecallError, "不能从"):
            transition_recall_device(
                user=self.manager,
                recall_device_id=row.pk,
                to_status="RECEIVED",
            )
        row.refresh_from_db()
        self.assertEqual(row.status, QualityRecallDevice.Status.NOTIFIED)
        transition_recall_device(
            user=self.manager,
            recall_device_id=row.pk,
            to_status="ACKNOWLEDGED",
            reason="当前责任人已确认知晓",
        )
        row.refresh_from_db()
        self.assertEqual(row.status, QualityRecallDevice.Status.ACKNOWLEDGED)
        self.assertEqual(row.events.count(), 3)

    def test_recall_tasks_resolve_merchant_inventory_and_are_idempotent(self):
        quality_case = self._recall_case()
        membership = quality_case.affected_devices.order_by("device_id").first()
        OrganizationDevice.objects.update_or_create(
            device_id=membership.device_id,
            defaults={"organization": self.merchant, "status": "IN_STOCK"},
        )
        DeviceState.objects.update_or_create(
            device_id=membership.device_id,
            defaults={
                "current_organization": self.merchant,
                "status": "IN_STOCK",
                "activation_status": "NOT_ACTIVATED",
                "install_status": "NOT_INSTALLED",
            },
        )
        campaign = self._recall_campaign(quality_case, [membership.device_id])
        launch_recall_campaign(user=self.manager, campaign_id=campaign.pk)

        self.assertEqual(WorkItem.objects.filter(
            type="QUALITY_RECALL_MERCHANT_SCOPE",
            biz_id=str(campaign.pk),
        ).count(), 1)

        first = publish_recall_tasks(user=self.manager, campaign_id=campaign.pk)
        second = publish_recall_tasks(user=self.manager, campaign_id=campaign.pk)

        row = campaign.devices.get()
        row.refresh_from_db()
        self.assertEqual(first[0]["mode"], "MERCHANT_INVENTORY")
        self.assertEqual(second[0]["mode"], "MERCHANT_INVENTORY")
        self.assertEqual(row.status, QualityRecallDevice.Status.NOTIFIED)
        self.assertEqual(WorkItem.objects.filter(
            type="QUALITY_RECALL_MERCHANT_SCOPE",
            biz_id=str(campaign.pk),
        ).count(), 1)
        self.assertTrue(UserTask.objects.filter(
            workitem__type="QUALITY_RECALL_MERCHANT_SCOPE",
            to_openid=self.applicant.wx_openid,
            need_action=True,
        ).exists())

    def test_recall_tasks_fall_back_to_factory_manual_trace(self):
        quality_case = self._recall_case()
        membership = quality_case.affected_devices.order_by("device_id").first()
        campaign = self._recall_campaign(quality_case, [membership.device_id])
        launch_recall_campaign(user=self.manager, campaign_id=campaign.pk)

        result = publish_recall_tasks(user=self.manager, campaign_id=campaign.pk)

        row = campaign.devices.get()
        row.refresh_from_db()
        self.assertEqual(result[0]["mode"], "MANUAL_TRACE")
        self.assertEqual(
            row.responsibility_snapshot["mode"],
            "MANUAL_TRACE",
        )
        self.assertTrue(UserTask.objects.filter(
            workitem__type="QUALITY_RECALL_FACTORY_COORDINATION",
            to_openid=self.manager.wx_openid,
            need_action=True,
        ).exists())

    def test_automatic_recall_tasks_are_partitioned_by_distribution_branch(self):
        merchant_type = OrganizationType.objects.get(code="MERCHANT")
        customer_type, _ = OrganizationType.objects.update_or_create(
            code="CUSTOMER", defaults={"name": "用户", "is_active": True},
        )
        merchant_role = OrganizationRole.objects.get(
            organization_type=merchant_type, code="merchant_owner",
        )
        customer_role, _ = OrganizationRole.objects.update_or_create(
            organization_type=customer_type,
            code="customer_owner",
            defaults={"name": "用户负责人账号", "is_owner_role": True, "is_active": True},
        )
        child = Organization.objects.create(
            name="R1382下级甲", code="R1382-CHILD-A", organization_type=merchant_type,
        )
        sibling = Organization.objects.create(
            name="R1382下级乙", code="R1382-CHILD-B", organization_type=merchant_type,
        )
        customer_org = Organization.objects.create(
            name="R1382终端用户", code="R1382-CUSTOMER", organization_type=customer_type,
        )
        child_user = User.objects.create_user(
            username="r1382-child", wx_openid="r1382-child-openid",
        )
        sibling_user = User.objects.create_user(
            username="r1382-sibling", wx_openid="r1382-sibling-openid",
        )
        customer_user = User.objects.create_user(
            username="r1382-customer", wx_openid="r1382-customer-openid",
        )
        UserOrganization.objects.create(
            user=child_user, organization=child, role=merchant_role,
        )
        UserOrganization.objects.create(
            user=sibling_user, organization=sibling, role=merchant_role,
        )
        UserOrganization.objects.create(
            user=customer_user, organization=customer_org, role=customer_role,
        )
        OrganizationRelation.objects.create(
            source_organization=self.factory,
            target_organization=self.merchant,
            relation_type=OrganizationRelationType.DISTRIBUTION,
        )
        OrganizationRelation.objects.create(
            source_organization=self.merchant,
            target_organization=child,
            relation_type=OrganizationRelationType.DISTRIBUTION,
        )
        OrganizationRelation.objects.create(
            source_organization=self.merchant,
            target_organization=sibling,
            relation_type=OrganizationRelationType.DISTRIBUTION,
        )

        quality_case = self._recall_case()
        memberships = list(quality_case.affected_devices.order_by("device_id"))
        OrganizationDevice.objects.update_or_create(
            device_id=memberships[0].device_id,
            defaults={"organization": child, "status": "IN_STOCK"},
        )
        CustomerDeviceBinding.objects.create(
            device_id=memberships[1].device_id,
            customer_organization=customer_org,
            source_organization=sibling,
            accepted_by=customer_user,
            acceptance_mode=CustomerDeviceBinding.AcceptanceMode.NEW_CUSTOMER,
            customer_type=CustomerDeviceBinding.CustomerType.PERSONAL,
        )
        campaign = self._recall_campaign(quality_case)
        launch_recall_campaign(user=self.manager, campaign_id=campaign.pk)

        def task_scope(organization):
            item = WorkItem.objects.get(
                uniq_key=f"quality-recall:{campaign.pk}:org:{organization.pk}",
            )
            return item.payload["scope"]

        parent_scope = task_scope(self.merchant)
        child_scope = task_scope(child)
        sibling_scope = task_scope(sibling)
        customer_scope = task_scope(customer_org)

        self.assertEqual(parent_scope["device_count"], 2)
        self.assertEqual(parent_scope["downstream_inventory_count"], 1)
        self.assertEqual(parent_scope["activated_user_device_count"], 1)
        self.assertEqual(
            {row["device_id"] for row in child_scope["devices"]},
            {memberships[0].device_id},
        )
        self.assertEqual(
            {row["device_id"] for row in sibling_scope["devices"]},
            {memberships[1].device_id},
        )
        self.assertEqual(
            {row["device_id"] for row in customer_scope["devices"]},
            {memberships[1].device_id},
        )
        self.assertNotIn(
            memberships[0].device_id,
            {row["device_id"] for row in sibling_scope["devices"]},
        )

        response = self.client.get(
            f"/pldp/api/quality-trace/recalls/{campaign.pk}/task-scope/",
            HTTP_AUTHORIZATION=f"Bearer {issue_access_token(child_user)}",
        )
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["code"], 0)
        self.assertEqual(
            {row["device_id"] for row in response.json()["data"]["scope"]["devices"]},
            {memberships[0].device_id},
        )

    def test_customer_campaign_api_reports_only_visible_device_count(self):
        customer_type, _ = OrganizationType.objects.update_or_create(
            code="CUSTOMER", defaults={"name": "用户", "is_active": True},
        )
        customer_role, _ = OrganizationRole.objects.update_or_create(
            organization_type=customer_type,
            code="customer_owner",
            defaults={"name": "用户负责人账号", "is_owner_role": True, "is_active": True},
        )
        customer_org = Organization.objects.create(
            name="R13831用户范围", code="R13831-CUSTOMER", organization_type=customer_type,
        )
        customer = User.objects.create_user(
            username="r13831-customer", wx_openid="r13831-customer-openid",
        )
        UserOrganization.objects.create(
            user=customer, organization=customer_org, role=customer_role,
        )
        quality_case = self._recall_case()
        memberships = list(quality_case.affected_devices.order_by("device_id"))
        CustomerDeviceBinding.objects.create(
            device_id=memberships[0].device_id,
            customer_organization=customer_org,
            source_organization=self.merchant,
            accepted_by=customer,
            acceptance_mode=CustomerDeviceBinding.AcceptanceMode.NEW_CUSTOMER,
            customer_type=CustomerDeviceBinding.CustomerType.PERSONAL,
        )
        campaign = self._recall_campaign(quality_case)
        launch_recall_campaign(user=self.manager, campaign_id=campaign.pk)
        auth = {"HTTP_AUTHORIZATION": f"Bearer {issue_access_token(customer)}"}

        detail = self.client.get(
            f"/pldp/api/quality-trace/recalls/{campaign.pk}/",
            **auth,
        )
        listing = self.client.get(
            "/pldp/api/quality-trace/recalls/",
            **auth,
        )

        self.assertEqual(detail.status_code, 200, detail.json())
        self.assertEqual(detail.json()["code"], 0)
        self.assertEqual(detail.json()["data"]["device_count"], 1)
        self.assertEqual(detail.json()["data"]["scope_label"], "本账号相关设备")
        self.assertEqual(len(detail.json()["data"]["devices"]), 1)
        self.assertEqual(listing.status_code, 200, listing.json())
        self.assertEqual(listing.json()["data"]["items"][0]["device_count"], 1)
        self.assertEqual(
            listing.json()["data"]["items"][0]["scope_label"],
            "本账号相关设备",
        )

    def _merchant_inventory_recall(self):
        quality_case = self._recall_case()
        membership = quality_case.affected_devices.order_by("device_id").first()
        OrganizationDevice.objects.update_or_create(
            device_id=membership.device_id,
            defaults={"organization": self.merchant, "status": "IN_STOCK"},
        )
        DeviceState.objects.update_or_create(
            device_id=membership.device_id,
            defaults={
                "current_organization": self.merchant,
                "status": "IN_STOCK",
                "activation_status": "NOT_ACTIVATED",
                "install_status": "NOT_INSTALLED",
            },
        )
        campaign = self._recall_campaign(quality_case, [membership.device_id])
        launch_recall_campaign(user=self.manager, campaign_id=campaign.pk)
        publish_recall_tasks(user=self.manager, campaign_id=campaign.pk)
        return campaign, campaign.devices.get()

    def _return_transfer(self, row, *, target, status, suffix):
        transfer = GoodsTransfer.objects.create(
            flow_no=f"R1363-{suffix}",
            client_request_id=f"r1363-{suffix}",
            flow_type=GoodsTransfer.FlowType.RETURN,
            status=status,
            instruction_organization=self.merchant,
            from_organization=self.merchant,
            to_organization=target,
            target_account=self.manager,
            initiated_by=self.applicant,
            receipt_required=True,
        )
        item = GoodsTransferItem.objects.create(
            transfer=transfer,
            device=row.device,
            scanned_by=self.applicant,
            status=(
                GoodsTransferItem.Status.PENDING_FACTORY_PICKUP
                if target == self.factory
                else GoodsTransferItem.Status.PENDING_RECEIPT
            ),
        )
        return transfer, item

    def test_recall_return_leg_binds_existing_return_and_completes_tasks(self):
        campaign, row = self._merchant_inventory_recall()
        transfer, _item = self._return_transfer(
            row,
            target=self.factory,
            status=GoodsTransfer.Status.PENDING_FACTORY_PICKUP,
            suffix="BIND",
        )

        leg, created = bind_recall_return_transfer(
            user=self.applicant,
            recall_device_id=row.pk,
            transfer_id=transfer.pk,
        )

        row.refresh_from_db()
        self.assertTrue(created)
        self.assertEqual(leg.sequence_no, 1)
        self.assertEqual(leg.status, QualityRecallReturnLeg.Status.IN_TRANSIT)
        self.assertEqual(row.status, QualityRecallDevice.Status.RETURN_IN_PROGRESS)
        self.assertFalse(WorkItem.objects.filter(
            pk__in=row.responsibility_snapshot["workitem_ids"],
            status="OPEN",
        ).exists())
        duplicate, duplicate_created = bind_recall_return_transfer(
            user=self.applicant,
            recall_device_id=row.pk,
            transfer_id=transfer.pk,
        )
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate.pk, leg.pk)

    def test_recall_return_sync_requires_factory_isolation_before_received(self):
        _campaign, row = self._merchant_inventory_recall()
        transfer, item = self._return_transfer(
            row,
            target=self.factory,
            status=GoodsTransfer.Status.PENDING_FACTORY_PICKUP,
            suffix="ISOLATE",
        )
        leg, _created = bind_recall_return_transfer(
            user=self.applicant,
            recall_device_id=row.pk,
            transfer_id=transfer.pk,
        )
        process = FactoryReturnProcess.objects.create(
            transfer=transfer,
            phase=FactoryReturnProcess.Phase.WAITING_PICKUP,
        )
        synchronize_recall_return_leg(leg_id=leg.pk)
        row.refresh_from_db()
        self.assertEqual(row.status, QualityRecallDevice.Status.RETURN_IN_PROGRESS)

        process.phase = FactoryReturnProcess.Phase.POSTED
        process.posted_by = self.manager
        process.posted_at = timezone.now()
        process.save(update_fields=("phase", "posted_by", "posted_at", "updated_at"))
        inventory = FactoryReturnInventoryItem.objects.create(
            process=process,
            transfer_item=item,
            source_product_model=self.product_model,
            source_production_batch=row.device.batch,
            source_bom=self.bom,
            stock_status=FactoryReturnInventoryItem.StockStatus.PENDING_REPAIR,
            classified_by=self.manager,
            classified_at=timezone.now(),
        )
        transfer.status = GoodsTransfer.Status.COMPLETED
        transfer.save(update_fields=("status", "updated_at"))
        synchronize_recall_return_leg(leg_id=leg.pk)

        leg.refresh_from_db()
        row.refresh_from_db()
        self.assertEqual(leg.status, QualityRecallReturnLeg.Status.FACTORY_ISOLATED)
        self.assertEqual(row.status, QualityRecallDevice.Status.RECEIVED)
        self.assertEqual(
            leg.isolation_snapshot["factory_return_inventory_item_id"],
            inventory.pk,
        )

    def test_activated_recall_auto_handover_shared_driver_and_method_lock(self):
        customer_type, _ = OrganizationType.objects.update_or_create(
            code="CUSTOMER", defaults={"name": "用户", "is_active": True},
        )
        owner_role, _ = OrganizationRole.objects.update_or_create(
            organization_type=customer_type, code="customer_owner",
            defaults={"name": "用户负责人账号", "is_owner_role": True, "is_active": True},
        )
        driver_role, _ = OrganizationRole.objects.update_or_create(
            organization_type=customer_type, code="driver",
            defaults={"name": "共享司机", "is_owner_role": False, "is_active": True},
        )
        customer_org = Organization.objects.create(
            name="R1384协同用户", code="R1384-CUSTOMER", organization_type=customer_type,
        )
        owner = User.objects.create_user(
            username="r1384-owner", wx_openid="r1384-owner-openid",
        )
        driver = User.objects.create_user(
            username="r1384-driver", wx_openid="r1384-driver-openid",
        )
        unrelated_driver = User.objects.create_user(
            username="r1384-unrelated-driver", wx_openid="r1384-unrelated-driver-openid",
        )
        for user, role in ((owner, owner_role), (driver, driver_role), (unrelated_driver, driver_role)):
            UserOrganization.objects.create(user=user, organization=customer_org, role=role)
        vehicle = CustomerVehicle.objects.create(
            customer_organization=customer_org, vehicle_no="R1384-TEST-VEHICLE",
        )
        DriverVehicleBinding.objects.create(vehicle=vehicle, driver=driver)

        quality_case = self._recall_case()
        membership = quality_case.affected_devices.order_by("device_id").first()
        CustomerDeviceBinding.objects.create(
            device_id=membership.device_id,
            customer_organization=customer_org,
            source_organization=self.merchant,
            accepted_by=owner,
            acceptance_mode=CustomerDeviceBinding.AcceptanceMode.NEW_CUSTOMER,
            customer_type=CustomerDeviceBinding.CustomerType.PERSONAL,
            vehicle=vehicle,
        )
        campaign = self._recall_campaign(quality_case, [membership.device_id])
        launch_recall_campaign(user=self.manager, campaign_id=campaign.pk)
        row = campaign.devices.get()
        handover = QualityRecallCustomerHandover.objects.get(recall_device=row)
        self.assertEqual(
            handover.status,
            QualityRecallCustomerHandover.Status.PENDING_CUSTOMER_CHOICE,
        )
        customer_task = WorkItem.objects.get(
            uniq_key=f"quality-recall:{campaign.pk}:org:{customer_org.pk}",
        )
        self.assertEqual(
            set(customer_task.user_tasks.values_list("to_openid", flat=True)),
            {owner.wx_openid, driver.wx_openid},
        )
        self.assertFalse(customer_task.user_tasks.filter(
            to_openid=unrelated_driver.wx_openid,
        ).exists())

        select_customer_delivery_method(
            user=driver,
            handover_id=handover.pk,
            delivery_method=QualityRecallCustomerHandover.DeliveryMethod.MERCHANT_HANDOVER,
            payload={"note": "司机与用户负责人协商后选择来源商家"},
        )
        handover.refresh_from_db()
        customer_task.refresh_from_db()
        self.assertEqual(handover.method_selected_by_id, driver.pk)
        self.assertEqual(handover.method_selected_role, "driver")
        self.assertTrue(handover.merchant_transfer_token)
        self.assertEqual(customer_task.status, "DONE")
        self.assertFalse(customer_task.user_tasks.exclude(state="DONE").exists())

        # Before physical handover starts, either collaborator may change the
        # method.  Once the merchant scans and accepts, all changes are blocked.
        select_customer_delivery_method(
            user=owner,
            handover_id=handover.pk,
            delivery_method=QualityRecallCustomerHandover.DeliveryMethod.CUSTOMER_DIRECT_SHIP,
        )
        handover.refresh_from_db()
        self.assertFalse(handover.method_locked_at)
        select_customer_delivery_method(
            user=owner,
            handover_id=handover.pk,
            delivery_method=QualityRecallCustomerHandover.DeliveryMethod.MERCHANT_HANDOVER,
        )
        handover.refresh_from_db()
        accept_merchant_handover_by_token(
            user=self.applicant,
            token=handover.merchant_transfer_token,
            receipt_note="已按SN核对并接管召回设备",
        )
        handover.refresh_from_db()
        self.assertIsNotNone(handover.method_locked_at)
        self.assertTrue(WorkItem.objects.filter(
            uniq_key=(
                f"quality-recall-merchant-return:{handover.recall_device_id}:"
                f"org:{self.merchant.pk}"
            ),
            status="OPEN",
        ).exists())
        self.assertTrue(WorkItem.objects.filter(
            uniq_key=f"quality-recall-device-progress:{handover.recall_device_id}",
            status="OPEN",
            payload__stage="SOURCE_MERCHANT_ACCEPTED",
        ).exists())
        with self.assertRaisesMessage(QualityRecallError, "不能再更换"):
            select_customer_delivery_method(
                user=owner,
                handover_id=handover.pk,
                delivery_method=QualityRecallCustomerHandover.DeliveryMethod.CUSTOMER_DIRECT_SHIP,
            )
        OrganizationRelation.objects.create(
            source_organization=self.factory,
            target_organization=self.merchant,
            relation_type=OrganizationRelationType.DISTRIBUTION,
        )
        leg = create_next_recall_transport_leg(
            user=self.applicant,
            recall_device_id=handover.recall_device_id,
        )
        merchant_return = WorkItem.objects.get(
            uniq_key=(
                f"quality-recall-merchant-return:{handover.recall_device_id}:"
                f"org:{self.merchant.pk}"
            ),
        )
        progress = WorkItem.objects.get(
            uniq_key=f"quality-recall-device-progress:{handover.recall_device_id}",
        )
        self.assertEqual(merchant_return.status, "DONE")
        self.assertEqual(progress.payload["stage"], "RETURN_LEG_CREATED")
        dispatch_recall_transport_leg(
            user=self.applicant,
            leg_id=leg.pk,
            logistics_provider="召回专线",
            tracking_no="R1385-MERCHANT-001",
            evidence={"storage_key": "recall/r1385-merchant-dispatch.jpg"},
        )
        progress.refresh_from_db()
        self.assertEqual(progress.payload["stage"], "RETURN_IN_TRANSIT")
        receive_recall_transport_leg(
            user=self.manager,
            leg_id=leg.pk,
            evidence={"storage_key": "recall/r1385-factory-receipt.jpg"},
        )
        progress.refresh_from_db()
        self.assertEqual(progress.payload["stage"], "FACTORY_RECEIVED_PENDING_ISOLATION")
        isolate_recalled_device(
            user=self.manager,
            leg_id=leg.pk,
            disposition=QualityRecallIsolationReceipt.Disposition.PENDING_INSPECTION,
            isolation_location="R1385召回隔离区",
            evidence={"storage_key": "recall/r1385-isolation.jpg"},
        )
        progress.refresh_from_db()
        self.assertEqual(progress.status, "DONE")

    def test_customer_direct_ship_creates_factory_recall_transport(self):
        customer_type, _ = OrganizationType.objects.update_or_create(
            code="CUSTOMER", defaults={"name": "用户", "is_active": True},
        )
        customer_role, _ = OrganizationRole.objects.update_or_create(
            organization_type=customer_type, code="customer_owner",
            defaults={"name": "用户负责人账号", "is_owner_role": True, "is_active": True},
        )
        customer_org = Organization.objects.create(
            name="R1384直寄用户", code="R1384-DIRECT", organization_type=customer_type,
        )
        customer = User.objects.create_user(
            username="r1384-direct", wx_openid="r1384-direct-openid",
        )
        UserOrganization.objects.create(
            user=customer, organization=customer_org, role=customer_role,
        )
        owner_type = OrganizationType.objects.get(code="OWNER")
        sales = User.objects.create_user(
            username="r1385-direct-sales", wx_openid="r1385-direct-sales-openid",
        )
        logistics = User.objects.create_user(
            username="r1385-direct-logistics", wx_openid="r1385-direct-logistics-openid",
        )
        UserOrganization.objects.create(
            user=sales,
            organization=self.factory,
            role=OrganizationRole.objects.get(
                organization_type=owner_type, code="factory_sales",
            ),
        )
        UserOrganization.objects.create(
            user=logistics,
            organization=self.factory,
            role=OrganizationRole.objects.get(
                organization_type=owner_type, code="factory_logistics",
            ),
        )
        quality_case = self._recall_case()
        membership = quality_case.affected_devices.order_by("device_id").first()
        CustomerDeviceBinding.objects.create(
            device_id=membership.device_id,
            customer_organization=customer_org,
            source_organization=self.merchant,
            accepted_by=customer,
            acceptance_mode=CustomerDeviceBinding.AcceptanceMode.NEW_CUSTOMER,
            customer_type=CustomerDeviceBinding.CustomerType.PERSONAL,
        )
        campaign = self._recall_campaign(quality_case, [membership.device_id])
        launch_recall_campaign(user=self.manager, campaign_id=campaign.pk)
        handover = campaign.customer_handovers.get()
        select_customer_delivery_method(
            user=customer,
            handover_id=handover.pk,
            delivery_method=QualityRecallCustomerHandover.DeliveryMethod.CUSTOMER_DIRECT_SHIP,
        )
        handover, leg = submit_customer_direct_shipment(
            user=customer,
            handover_id=handover.pk,
            logistics_provider="测试快递",
            tracking_no="R1384-DIRECT-001",
            evidence={"storage_key": "recall/r1384-direct.jpg"},
        )
        handover.refresh_from_db()
        self.assertEqual(handover.status, QualityRecallCustomerHandover.Status.DIRECT_SHIPPED)
        self.assertIsNotNone(handover.method_locked_at)
        self.assertEqual(leg.from_organization_id, customer_org.pk)
        self.assertEqual(leg.to_organization_id, self.factory.pk)
        self.assertEqual(leg.status, QualityRecallTransportLeg.Status.IN_TRANSIT)
        self.assertTrue(leg.is_factory_destination)
        self.assertTrue(UserTask.objects.filter(
            workitem__uniq_key=f"quality-recall-transport:{leg.pk}:receive",
            to_openid=self.viewer.wx_openid,
        ).exists())
        receipt = WorkItem.objects.get(
            uniq_key=f"quality-recall-transport:{leg.pk}:receive",
        )
        self.assertEqual(
            set(receipt.user_tasks.values_list("to_openid", flat=True)),
            {
                self.manager.wx_openid,
                self.viewer.wx_openid,
                sales.wx_openid,
                logistics.wx_openid,
            },
        )
        self.assertIn(customer_org.name, receipt.title)
        self.assertIn("待收货", receipt.title)
        self.assertTrue(CustomerDeviceBinding.objects.filter(
            device_id=membership.device_id,
            status=CustomerDeviceBinding.Status.ACTIVE,
        ).exists())

    def test_factory_pickup_request_publishes_task_and_locks_on_acceptance(self):
        customer_type, _ = OrganizationType.objects.update_or_create(
            code="CUSTOMER", defaults={"name": "用户", "is_active": True},
        )
        customer_role, _ = OrganizationRole.objects.update_or_create(
            organization_type=customer_type, code="customer_owner",
            defaults={"name": "用户负责人账号", "is_owner_role": True, "is_active": True},
        )
        customer_org = Organization.objects.create(
            name="R1384取件用户", code="R1384-PICKUP", organization_type=customer_type,
        )
        customer = User.objects.create_user(
            username="r1384-pickup", wx_openid="r1384-pickup-openid",
        )
        UserOrganization.objects.create(user=customer, organization=customer_org, role=customer_role)
        owner_type = OrganizationType.objects.get(code="OWNER")
        logistics = User.objects.create_user(
            username="r1385-pickup-logistics", wx_openid="r1385-pickup-logistics-openid",
        )
        UserOrganization.objects.create(
            user=logistics,
            organization=self.factory,
            role=OrganizationRole.objects.get(
                organization_type=owner_type, code="factory_logistics",
            ),
        )
        quality_case = self._recall_case()
        membership = quality_case.affected_devices.order_by("device_id").first()
        CustomerDeviceBinding.objects.create(
            device_id=membership.device_id,
            customer_organization=customer_org,
            source_organization=self.merchant,
            accepted_by=customer,
            acceptance_mode=CustomerDeviceBinding.AcceptanceMode.NEW_CUSTOMER,
            customer_type=CustomerDeviceBinding.CustomerType.PERSONAL,
        )
        campaign = self._recall_campaign(quality_case, [membership.device_id])
        launch_recall_campaign(user=self.manager, campaign_id=campaign.pk)
        handover = campaign.customer_handovers.get()
        select_customer_delivery_method(
            user=customer,
            handover_id=handover.pk,
            delivery_method=QualityRecallCustomerHandover.DeliveryMethod.FACTORY_PICKUP,
            payload={
                "contact_name": "测试用户",
                "contact_phone": "13800000000",
                "pickup_address": "测试取件地址",
                "pickup_time_window": "2026-09-01 10:00",
            },
        )
        handover.refresh_from_db()
        self.assertEqual(handover.status, QualityRecallCustomerHandover.Status.PICKUP_REQUESTED)
        pickup_task = WorkItem.objects.get(
            uniq_key=f"quality-recall-pickup:{handover.pk}",
        )
        self.assertEqual(
            set(pickup_task.user_tasks.values_list("to_openid", flat=True)),
            {self.manager.wx_openid, logistics.wx_openid},
        )
        accepted, leg = accept_factory_pickup_request(
            user=logistics,
            handover_id=handover.pk,
            pickup_scheduled_at=timezone.now() + timedelta(days=1),
            logistics_provider="厂家委托物流",
        )
        accepted.refresh_from_db()
        self.assertEqual(accepted.status, QualityRecallCustomerHandover.Status.PICKUP_ACCEPTED)
        self.assertIsNotNone(accepted.method_locked_at)
        self.assertEqual(leg.status, QualityRecallTransportLeg.Status.PREPARING)
        self.assertEqual(leg.from_organization_id, customer_org.pk)
        self.assertEqual(leg.to_organization_id, self.factory.pk)
        customer_dispatch = WorkItem.objects.get(
            uniq_key=f"quality-recall-pickup-customer-dispatch:{handover.pk}",
        )
        self.assertEqual(
            set(customer_dispatch.user_tasks.values_list("to_openid", flat=True)),
            {customer.wx_openid},
        )

        shipped, leg = submit_customer_pickup_shipment(
            user=customer,
            handover_id=handover.pk,
            logistics_provider="厂家委托物流",
            tracking_no="R1385-PICKUP-001",
            evidence={"storage_key": "recall/r1385-pickup.jpg"},
        )
        shipped.refresh_from_db()
        customer_dispatch.refresh_from_db()
        leg.refresh_from_db()
        self.assertEqual(
            shipped.status,
            QualityRecallCustomerHandover.Status.PICKUP_SHIPPED,
        )
        self.assertEqual(leg.status, QualityRecallTransportLeg.Status.IN_TRANSIT)
        self.assertEqual(customer_dispatch.status, "DONE")
        receipt = WorkItem.objects.get(
            uniq_key=f"quality-recall-transport:{leg.pk}:receive",
        )
        self.assertEqual(
            set(receipt.user_tasks.values_list("to_openid", flat=True)),
            {self.manager.wx_openid, self.viewer.wx_openid, logistics.wx_openid},
        )

    def test_activated_device_customer_handover_preserves_activation_and_inventory_facts(self):
        customer_type, _ = OrganizationType.objects.update_or_create(
            code="CUSTOMER", defaults={"name": "用户", "is_active": True},
        )
        customer_role, _ = OrganizationRole.objects.update_or_create(
            organization_type=customer_type, code="customer_owner",
            defaults={"name": "用户负责人账号", "is_owner_role": True, "is_active": True},
        )
        customer_org = Organization.objects.create(
            name="R1364测试用户", code="R1364-CUSTOMER", organization_type=customer_type,
        )
        customer = User.objects.create_user(
            username="r1364-customer", wx_openid="r1364-customer-openid",
        )
        UserOrganization.objects.create(
            user=customer, organization=customer_org, role=customer_role,
        )
        quality_case = self._recall_case()
        membership = quality_case.affected_devices.order_by("device_id").first()
        CustomerDeviceBinding.objects.create(
            device_id=membership.device_id,
            customer_organization=customer_org,
            source_organization=self.merchant,
            accepted_by=customer,
            acceptance_mode=CustomerDeviceBinding.AcceptanceMode.NEW_CUSTOMER,
            customer_type=CustomerDeviceBinding.CustomerType.PERSONAL,
        )
        campaign = self._recall_campaign(quality_case, [membership.device_id])
        launch_recall_campaign(user=self.manager, campaign_id=campaign.pk)
        publish_recall_tasks(user=self.manager, campaign_id=campaign.pk)
        recall_device = campaign.devices.get()
        handover, created = initialize_customer_handover(
            user=self.manager, recall_device_id=recall_device.pk,
        )
        self.assertFalse(created)
        self.assertEqual(
            campaign.customer_handovers.count(), 1,
            "发布召回时应已自动建立且重复同步不得重复创建交接单",
        )
        acknowledge_customer_recall(
            user=customer, handover_id=handover.pk, note="同意商家上门取回",
        )
        schedule_customer_pickup(
            user=self.applicant, handover_id=handover.pk,
            pickup_scheduled_at=timezone.now() + timedelta(days=1),
            pickup_address="R1364测试取回地址", note="电话联系后上门",
        )
        with self.assertRaisesMessage(QualityRecallError, "验收凭证"):
            confirm_merchant_handover_receipt(
                user=self.applicant, handover_id=handover.pk, receipt_note="设备外观已核对",
            )
        add_handover_evidence(
            user=self.applicant, handover_id=handover.pk,
            kind=QualityRecallHandoverEvidence.Kind.MERCHANT_RECEIPT,
            client_request_id="r1364-receipt-1",
            evidence={
                "storage_key": "private/quality-recall/r1364-receipt.jpg",
                "original_name": "receipt.jpg", "content_type": "image/jpeg",
                "file_size": 128, "sha256": "a" * 64,
            },
        )
        confirm_merchant_handover_receipt(
            user=self.applicant, handover_id=handover.pk, receipt_note="设备外观与序列号已核对",
        )
        handover.refresh_from_db()
        recall_device.refresh_from_db()
        self.assertEqual(handover.status, QualityRecallCustomerHandover.Status.MERCHANT_RECEIVED)
        self.assertEqual(recall_device.status, QualityRecallDevice.Status.RETURN_IN_PROGRESS)
        self.assertFalse(handover.custody_snapshot["inventory_state_changed"])
        self.assertTrue(CustomerDeviceBinding.objects.filter(
            device_id=membership.device_id, status=CustomerDeviceBinding.Status.ACTIVE,
        ).exists())
        self.assertFalse(OrganizationDevice.objects.filter(
            device_id=membership.device_id, organization=self.merchant,
        ).exists())

    def test_recall_only_transport_reaches_factory_and_requires_isolation(self):
        quality_case = self._recall_case()
        membership = quality_case.affected_devices.order_by("device_id").first()
        campaign = self._recall_campaign(quality_case, [membership.device_id])
        launch_recall_campaign(user=self.manager, campaign_id=campaign.pk)
        recall_device = campaign.devices.get()
        QualityRecallCustomerHandover.objects.create(
            campaign=campaign,
            recall_device=recall_device,
            customer_organization=self.merchant,
            merchant_organization=self.merchant,
            status=QualityRecallCustomerHandover.Status.MERCHANT_RECEIVED,
            customer_snapshot={},
            merchant_snapshot={},
            custody_snapshot={"custodian_type": "RECALL_MERCHANT_CUSTODY"},
        )
        OrganizationRelation.objects.create(
            source_organization=self.factory,
            target_organization=self.merchant,
            relation_type=OrganizationRelationType.DISTRIBUTION,
        )
        leg = create_next_recall_transport_leg(
            user=self.applicant, recall_device_id=recall_device.pk,
        )
        self.assertTrue(leg.is_factory_destination)
        self.assertEqual(leg.sequence_no, 1)
        dispatch_recall_transport_leg(
            user=self.applicant,
            leg_id=leg.pk,
            logistics_provider="R1365测试物流",
            tracking_no="R1365-TRACK-001",
            evidence={"storage_key": "private/recall/r1365-dispatch.jpg"},
        )
        receive_recall_transport_leg(
            user=self.manager,
            leg_id=leg.pk,
            evidence={"storage_key": "private/recall/r1365-receipt.jpg"},
            receipt_note="设备序列号及外观已核对",
        )
        recall_device.refresh_from_db()
        leg.refresh_from_db()
        self.assertEqual(leg.status, QualityRecallTransportLeg.Status.RECEIVED)
        self.assertEqual(recall_device.status, QualityRecallDevice.Status.RETURN_IN_PROGRESS)
        self.assertFalse(leg.custody_snapshot["ordinary_inventory_created"])
        receipt = isolate_recalled_device(
            user=self.manager,
            leg_id=leg.pk,
            disposition=QualityRecallIsolationReceipt.Disposition.PENDING_INSPECTION,
            isolation_location="R1365召回隔离区-A01",
            evidence={"storage_key": "private/recall/r1365-isolation.jpg"},
        )
        recall_device.refresh_from_db()
        self.assertEqual(recall_device.status, QualityRecallDevice.Status.RECEIVED)
        self.assertTrue(receipt.source_activation_preserved)
        self.assertFalse(receipt.ordinary_inventory_created)
        self.assertFalse(OrganizationDevice.objects.filter(
            device_id=membership.device_id, organization=self.factory,
        ).exists())

    def test_recall_progress_overdue_escalation_is_deduplicated(self):
        quality_case = self._recall_case()
        campaign = self._recall_campaign(quality_case)
        launch_recall_campaign(user=self.manager, campaign_id=campaign.pk)
        campaign.deadline = timezone.localdate() - timedelta(days=1)
        campaign.save(update_fields=("deadline", "updated_at"))

        first = calculate_recall_progress(
            campaign_id=campaign.pk, source="MANUAL",
        )
        second = calculate_recall_progress(
            campaign_id=campaign.pk, source="SCHEDULED",
        )

        self.assertTrue(first.is_overdue)
        self.assertTrue(second.is_overdue)
        self.assertEqual(first.incomplete_device_ids, second.incomplete_device_ids)
        self.assertEqual(first.escalation_workitem_id, second.escalation_workitem_id)
        self.assertEqual(WorkItem.objects.filter(
            uniq_key=f"quality-recall:{campaign.pk}:overdue-escalation",
        ).count(), 1)
        self.assertEqual(QualityRecallProgressSnapshot.objects.filter(
            campaign=campaign,
        ).count(), 2)

    def test_unreachable_requires_approval_before_recall_closeout(self):
        quality_case = self._recall_case()
        campaign = self._recall_campaign(quality_case)
        launch_recall_campaign(user=self.manager, campaign_id=campaign.pk)
        rows = list(campaign.devices.order_by("device_id"))
        unreachable = rows[0]
        unreachable.status = QualityRecallDevice.Status.UNREACHABLE
        unreachable.status_updated_at = timezone.now()
        unreachable.save(update_fields=("status", "status_updated_at"))
        for row in rows[1:]:
            row.status = QualityRecallDevice.Status.RECEIVED
            row.status_updated_at = timezone.now()
            row.save(update_fields=("status", "status_updated_at"))

        with self.assertRaisesMessage(QualityRecallError, "未完成设备"):
            complete_recall_campaign(
                user=self.manager, campaign_id=campaign.pk,
                reason="尝试在无法联系审批前完成召回",
            )
        decision = decide_unreachable_device(
            user=self.manager,
            recall_device_id=unreachable.pk,
            decision=QualityRecallExceptionDecision.Decision.APPROVED,
            reason="已连续三次联系并保留记录，批准按无法联系例外结案",
        )
        closeout = complete_recall_campaign(
            user=self.manager, campaign_id=campaign.pk,
            reason="全部可召回设备已经收回，无法联系设备已履行审批",
        )

        campaign.refresh_from_db()
        self.assertEqual(decision.decision, QualityRecallExceptionDecision.Decision.APPROVED)
        self.assertEqual(campaign.status, QualityRecallCampaign.Status.COMPLETED)
        self.assertEqual(closeout.campaign_id, campaign.pk)
        self.assertEqual(closeout.incomplete_reasons, [])
        self.assertIn(
            unreachable.device_id,
            closeout.execution_summary["unreachable_approved_device_ids"],
        )
        self.assertEqual(QualityRecallCloseout.objects.filter(campaign=campaign).count(), 1)
        self.assertTrue(QualityControlAction.objects.filter(
            pk=closeout.remediation_hold_action_id,
            status=QualityControlAction.Status.COMPLETED,
        ).exists())
        self.assertFalse(QualityControlAction.objects.filter(
            quality_case=campaign.quality_case,
            status=QualityControlAction.Status.ACTIVE,
        ).exists())

    def test_recall_api_factory_lists_and_reads_campaign(self):
        quality_case = self._recall_case()
        campaign = self._recall_campaign(quality_case)
        auth = {"HTTP_AUTHORIZATION": f"Bearer {issue_access_token(self.manager)}"}

        listing = self.client.get("/pldp/api/quality-trace/recalls/", **auth)
        detail = self.client.get(
            f"/pldp/api/quality-trace/recalls/{campaign.pk}/", **auth,
        )

        self.assertEqual(listing.status_code, 200, listing.json())
        self.assertEqual(listing.json()["code"], 0)
        self.assertEqual(listing.json()["data"]["items"][0]["id"], campaign.pk)
        self.assertEqual(detail.status_code, 200, detail.json())
        self.assertEqual(detail.json()["data"]["device_count"], campaign.device_count)
        self.assertEqual(len(detail.json()["data"]["devices"]), campaign.device_count)

    def test_recall_api_responsible_merchant_reads_device_but_cannot_recalculate(self):
        campaign, row = self._merchant_inventory_recall()
        auth = {"HTTP_AUTHORIZATION": f"Bearer {issue_access_token(self.applicant)}"}

        detail = self.client.get(
            f"/pldp/api/quality-trace/recall-devices/{row.pk}/", **auth,
        )
        progress = self.client.post(
            f"/pldp/api/quality-trace/recalls/{campaign.pk}/progress/",
            {}, content_type="application/json", **auth,
        )

        self.assertEqual(detail.status_code, 200, detail.json())
        self.assertEqual(detail.json()["code"], 0)
        self.assertEqual(detail.json()["data"]["sn"], row.sn_snapshot)
        self.assertEqual(progress.status_code, 200, progress.json())
        self.assertEqual(progress.json()["code"], 1)
        self.assertIn("无权操作", progress.json()["msg"])

    def test_recall_evidence_upload_stores_private_file(self):
        auth = {"HTTP_AUTHORIZATION": f"Bearer {issue_access_token(self.manager)}"}
        with tempfile.TemporaryDirectory() as root:
            with override_settings(QUALITY_RECALL_PRIVATE_ROOT=root):
                response = self.client.post(
                    "/pldp/api/quality-trace/recall-evidence/upload/",
                    {"file": SimpleUploadedFile(
                        "receipt.jpg", b"test-image", content_type="image/jpeg",
                    )},
                    **auth,
                )
            self.assertEqual(response.status_code, 200, response.json())
            self.assertEqual(response.json()["code"], 0)
            data = response.json()["data"]
            self.assertEqual(data["original_name"], "receipt.jpg")
            self.assertTrue((Path(root) / data["storage_key"]).is_file())

    def test_factory_business_role_cannot_open_quality_console_or_recall_list(self):
        auth = {"HTTP_AUTHORIZATION": f"Bearer {issue_access_token(self.viewer)}"}
        overview = self.client.get("/pldp/api/quality-trace/overview/", **auth)
        recalls = self.client.get("/pldp/api/quality-trace/recalls/", **auth)
        self.assertEqual(overview.status_code, 200, overview.json())
        self.assertEqual(overview.json()["code"], 1)
        self.assertIn("无权访问", overview.json()["msg"])
        self.assertEqual(recalls.status_code, 200, recalls.json())
        self.assertEqual(recalls.json()["code"], 1)
        self.assertIn("无权访问", recalls.json()["msg"])
