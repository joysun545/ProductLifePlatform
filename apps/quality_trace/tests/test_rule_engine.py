from datetime import date, timedelta
from decimal import Decimal
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
    Device,
    DeviceState,
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
    QualityControlAction,
    QualityMetricSnapshot,
    QualityMetricSnapshotDevice,
    QualityTraceEvent,
    QualityGateDecision,
    QualityRecallCampaign,
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
    launch_recall_campaign,
    transition_recall_device,
)
from quality_trace.services.recall_task_service import publish_recall_tasks
from quality_trace.services.recall_return_service import (
    bind_recall_return_transfer,
    synchronize_recall_return_leg,
)
from quality_trace.services.recall_handover_service import (
    acknowledge_customer_recall,
    add_handover_evidence,
    confirm_merchant_handover_receipt,
    initialize_customer_handover,
    schedule_customer_pickup,
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
        self.assertFalse(check_device_operation(
            device_id=self.devices[0].pk, operation="ACTIVATION",
        )["allowed"])
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
        transition_recall_device(
            user=self.manager,
            recall_device_id=row.pk,
            to_status="NOTIFIED",
            reason="已向当前责任人发送召回通知",
        )
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

        first = publish_recall_tasks(user=self.manager, campaign_id=campaign.pk)
        second = publish_recall_tasks(user=self.manager, campaign_id=campaign.pk)

        row = campaign.devices.get()
        row.refresh_from_db()
        self.assertEqual(first[0]["mode"], "MERCHANT_INVENTORY")
        self.assertEqual(second[0]["mode"], "MERCHANT_INVENTORY")
        self.assertEqual(row.status, QualityRecallDevice.Status.NOTIFIED)
        self.assertEqual(WorkItem.objects.filter(
            type="QUALITY_RECALL_MERCHANT_RETURN",
            biz_id=str(campaign.pk),
        ).count(), 1)
        self.assertTrue(UserTask.objects.filter(
            workitem__type="QUALITY_RECALL_MERCHANT_RETURN",
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
            workitem__type="QUALITY_RECALL_MANUAL_TRACE",
            to_openid=self.manager.wx_openid,
            need_action=True,
        ).exists())

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
        self.assertTrue(created)
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
