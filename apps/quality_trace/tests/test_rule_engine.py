from datetime import date, timedelta
from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from account.models import (
    Organization,
    OrganizationRole,
    OrganizationType,
    User,
    UserOrganization,
)
from lifecycle.models import ActivationRecord, AfterSalesApplication, Device
from manufacturing.models import ProductionBatch
from product.models import BOM, ProductCategory, ProductModel
from quality_trace.models import (
    BatchQualityCase,
    QualityAlert,
    QualityControlAction,
    QualityMetricSnapshot,
    QualityMetricSnapshotDevice,
    QualityTraceEvent,
    QualityGateDecision,
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
                organization_type=owner_type, code="factory_manager",
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
        bom = BOM.objects.create(
            product_model=cls.product_model,
            version="R131-V1",
            created_by=cls.manager,
        )
        batch = ProductionBatch.objects.create(
            batch_no="R131-PRODUCTION-BATCH",
            product_model=cls.product_model,
            bom=bom,
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
        self.assertEqual(assessment.assessed_role_snapshot, "factory_manager")
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
        self.assertTrue(quality_case.control_actions.filter(
            action_type=QualityControlAction.ActionType.FORMAL_HOLD,
            status=QualityControlAction.Status.ACTIVE,
        ).exists())
