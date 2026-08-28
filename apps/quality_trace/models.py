from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class QualityAlertRule(TimeStampedModel):
    """A factory-owned, versioned alert-rule identity.

    Mutable publication state stays here. Thresholds and scope are frozen in
    QualityAlertRuleVersion so historic alerts can always be reproduced.
    """

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "草稿"
        ACTIVE = "ACTIVE", "启用"
        SUSPENDED = "SUSPENDED", "暂停"
        RETIRED = "RETIRED", "停用"

    factory_organization = models.ForeignKey(
        "account.Organization",
        on_delete=models.PROTECT,
        related_name="quality_trace_rules",
        verbose_name="规则所属厂家",
    )
    code = models.CharField(max_length=64, verbose_name="规则编码")
    name = models.CharField(max_length=128, verbose_name="规则名称")
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.DRAFT, db_index=True,
    )
    current_version = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_quality_trace_rules",
    )

    class Meta:
        ordering = ("factory_organization_id", "code")
        constraints = [
            models.UniqueConstraint(
                fields=("factory_organization", "code"),
                name="qtrace_unique_factory_rule_code",
            )
        ]

    def clean(self):
        super().clean()
        if (
            self.factory_organization_id
            and self.factory_organization.organization_type.code != "OWNER"
        ):
            raise ValidationError({"factory_organization": "质量规则必须属于厂家组织。"})

    def __str__(self):
        return f"{self.factory_organization.name}-{self.name}"


class QualityAlertRuleVersion(models.Model):
    """Immutable rule snapshot used by calculation and audit."""

    class MetricType(models.TextChoices):
        FAULT_RATE = "FAULT_RATE", "故障设备率"
        EARLY_FAULT_RATE = "EARLY_FAULT_RATE", "早期故障率"
        REPEAT_FAULT_RATE = "REPEAT_FAULT_RATE", "重复故障率"
        FAULT_COUNT = "FAULT_COUNT", "故障设备台次"
        SAFETY_EVENT = "SAFETY_EVENT", "重大安全事件"

    rule = models.ForeignKey(
        QualityAlertRule, on_delete=models.PROTECT, related_name="versions",
    )
    version = models.PositiveIntegerField()
    metric_type = models.CharField(max_length=32, choices=MetricType.choices)
    scope_snapshot = models.JSONField(default=dict)
    denominator_policy_snapshot = models.JSONField(default=dict)
    fault_policy_snapshot = models.JSONField(default=dict)
    threshold_snapshot = models.JSONField(default=dict)
    window_days = models.PositiveIntegerField(default=30)
    minimum_exposure = models.PositiveIntegerField(default=10)
    minimum_fault_devices = models.PositiveIntegerField(default=2)
    cooldown_hours = models.PositiveIntegerField(default=24)
    allow_automatic_temporary_hold = models.BooleanField(default=False)
    automatic_hold_hours = models.PositiveIntegerField(default=0)
    effective_from = models.DateTimeField()
    effective_to = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="published_quality_rule_versions",
    )
    published_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("rule_id", "-version")
        constraints = [
            models.UniqueConstraint(
                fields=("rule", "version"), name="qtrace_unique_rule_version",
            ),
            models.CheckConstraint(
                condition=Q(version__gt=0), name="qtrace_rule_version_positive",
            ),
            models.CheckConstraint(
                condition=Q(window_days__gt=0), name="qtrace_window_days_positive",
            ),
        ]

    def clean(self):
        super().clean()
        if self.effective_to and self.effective_to <= self.effective_from:
            raise ValidationError({"effective_to": "失效时间必须晚于生效时间。"})
        if self.allow_automatic_temporary_hold and not self.automatic_hold_hours:
            raise ValidationError({"automatic_hold_hours": "自动临时拦截必须设置解除时限。"})

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValueError("质量规则版本发布后不可覆盖；请发布新版本。")
        return super().save(*args, **kwargs)


class QualityAlert(TimeStampedModel):
    class Severity(models.TextChoices):
        WARNING = "WARNING", "预警"
        URGENT = "URGENT", "紧急"
        CRITICAL = "CRITICAL", "重大"

    class Status(models.TextChoices):
        OPEN = "OPEN", "待研判"
        ASSESSING = "ASSESSING", "研判中"
        ESCALATED = "ESCALATED", "已升级案件"
        DISMISSED = "DISMISSED", "已排除"
        CLOSED = "CLOSED", "已关闭"

    rule_version = models.ForeignKey(
        QualityAlertRuleVersion, on_delete=models.PROTECT, related_name="alerts",
    )
    factory_organization = models.ForeignKey(
        "account.Organization", on_delete=models.PROTECT, related_name="quality_trace_alerts",
    )
    deduplication_key = models.CharField(max_length=255, unique=True)
    target_type = models.CharField(max_length=32, db_index=True)
    target_id = models.PositiveBigIntegerField(null=True, blank=True, db_index=True)
    target_key = models.CharField(max_length=255, db_index=True)
    target_snapshot = models.JSONField(default=dict)
    window_started_at = models.DateTimeField()
    window_ended_at = models.DateTimeField()
    exposure_device_count = models.PositiveIntegerField(default=0)
    fault_device_count = models.PositiveIntegerField(default=0)
    metric_value = models.DecimalField(max_digits=12, decimal_places=6, default=0)
    metric_snapshot = models.JSONField(default=dict)
    severity = models.CharField(max_length=16, choices=Severity.choices, db_index=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.OPEN, db_index=True,
    )
    first_triggered_at = models.DateTimeField()
    last_triggered_at = models.DateTimeField()
    trigger_count = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ("-last_triggered_at", "-id")
        indexes = [
            models.Index(fields=("factory_organization", "status", "severity"), name="qtrace_alert_queue_idx"),
            models.Index(fields=("target_type", "target_id"), name="qtrace_alert_target_idx"),
        ]


class QualityAssessment(models.Model):
    class Conclusion(models.TextChoices):
        FALSE_ALARM = "FALSE_ALARM", "排除预警"
        CONTINUE_WATCH = "CONTINUE_WATCH", "继续观察"
        SUPPLIER_REVIEW = "SUPPLIER_REVIEW", "供应商复核"
        TEMPORARY_HOLD = "TEMPORARY_HOLD", "临时拦截"
        CONFIRMED_DEFECT = "CONFIRMED_DEFECT", "确认批次缺陷"

    alert = models.ForeignKey(QualityAlert, on_delete=models.PROTECT, related_name="assessments")
    conclusion = models.CharField(max_length=32, choices=Conclusion.choices)
    reason = models.TextField()
    evidence_snapshot = models.JSONField(default=dict)
    assessed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="quality_assessments",
    )
    assessed_role_snapshot = models.CharField(max_length=64, blank=True, default="")
    assessed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("alert_id", "assessed_at", "id")


class BatchQualityCase(TimeStampedModel):
    class Status(models.TextChoices):
        OPEN = "OPEN", "调查中"
        HELD = "HELD", "已拦截"
        RECALLING = "RECALLING", "召回中"
        REMEDIATING = "REMEDIATING", "处置中"
        PENDING_RELEASE = "PENDING_RELEASE", "待解除"
        CLOSED = "CLOSED", "已结案"

    case_no = models.CharField(max_length=64, unique=True)
    factory_organization = models.ForeignKey(
        "account.Organization", on_delete=models.PROTECT, related_name="batch_quality_cases",
    )
    source_alert = models.ForeignKey(
        QualityAlert, null=True, blank=True, on_delete=models.PROTECT, related_name="quality_cases",
    )
    supplier_material_batch = models.ForeignKey(
        "supply_chain.SupplierMaterialBatch",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="quality_trace_cases",
    )
    scope_type = models.CharField(max_length=32, db_index=True)
    scope_key = models.CharField(max_length=255, db_index=True)
    scope_snapshot = models.JSONField(default=dict)
    reason = models.TextField()
    status = models.CharField(
        max_length=24, choices=Status.choices, default=Status.OPEN, db_index=True,
    )
    opened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="opened_batch_quality_cases",
    )
    opened_at = models.DateTimeField(auto_now_add=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="closed_batch_quality_cases",
    )
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-opened_at", "-id")
        indexes = [
            models.Index(fields=("factory_organization", "status"), name="qtrace_case_factory_idx"),
            models.Index(fields=("scope_type", "scope_key"), name="qtrace_case_scope_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("source_alert",),
                condition=Q(source_alert__isnull=False),
                name="qtrace_unique_case_source_alert",
            ),
        ]


class BatchQualityAffectedDevice(models.Model):
    """Immutable case membership; later scans may add rows but never remove them."""

    quality_case = models.ForeignKey(
        BatchQualityCase, on_delete=models.PROTECT, related_name="affected_devices",
    )
    device = models.ForeignKey(
        "lifecycle.Device", on_delete=models.PROTECT, related_name="quality_trace_memberships",
    )
    sn_snapshot = models.CharField(max_length=64)
    production_batch_snapshot = models.CharField(max_length=64, blank=True, default="")
    product_model_snapshot = models.JSONField(default=dict)
    location_snapshot = models.JSONField(default=dict)
    trace_evidence_snapshot = models.JSONField(default=dict)
    included_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("quality_case_id", "device_id")
        constraints = [
            models.UniqueConstraint(
                fields=("quality_case", "device"), name="qtrace_unique_case_device",
            )
        ]


class QualityControlAction(models.Model):
    class ActionType(models.TextChoices):
        WARN = "WARN", "风险提示"
        TEMPORARY_HOLD = "TEMPORARY_HOLD", "临时拦截"
        FORMAL_HOLD = "FORMAL_HOLD", "正式拦截"
        RECALL = "RECALL", "召回"
        RELEASE = "RELEASE", "解除管控"

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "生效"
        COMPLETED = "COMPLETED", "已完成"
        EXPIRED = "EXPIRED", "已到期"
        REVOKED = "REVOKED", "已撤销"

    quality_case = models.ForeignKey(
        BatchQualityCase, on_delete=models.PROTECT, related_name="control_actions",
    )
    action_type = models.CharField(max_length=24, choices=ActionType.choices)
    operation_scope = models.JSONField(default=dict)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.ACTIVE, db_index=True,
    )
    reason = models.TextField()
    effective_at = models.DateTimeField()
    expires_at = models.DateTimeField(null=True, blank=True)
    acted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="quality_control_actions",
    )
    ended_at = models.DateTimeField(null=True, blank=True)
    ended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="ended_quality_control_actions",
    )
    end_reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("quality_case_id", "-effective_at", "-id")
        indexes = [models.Index(fields=("status", "action_type"), name="qtrace_control_active_idx")]

    def clean(self):
        super().clean()
        if self.action_type == self.ActionType.TEMPORARY_HOLD and not self.expires_at:
            raise ValidationError({"expires_at": "临时拦截必须设置自动到期时间。"})
        if self.expires_at and self.expires_at <= self.effective_at:
            raise ValidationError({"expires_at": "管控到期时间必须晚于生效时间。"})


class QualityTraceEvent(models.Model):
    quality_case = models.ForeignKey(
        BatchQualityCase,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="events",
    )
    alert = models.ForeignKey(
        QualityAlert,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="events",
    )
    event_type = models.CharField(max_length=64, db_index=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="quality_trace_events",
    )
    actor_role_snapshot = models.CharField(max_length=64, blank=True, default="")
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at", "id")
        constraints = [
            models.CheckConstraint(
                condition=Q(quality_case__isnull=False) | Q(alert__isnull=False),
                name="qtrace_event_has_subject",
            )
        ]


class QualityMetricSnapshot(models.Model):
    """One reproducible result of evaluating one rule version and target."""

    class Source(models.TextChoices):
        REALTIME = "REALTIME", "实时事件"
        SCHEDULED = "SCHEDULED", "定时任务"
        MANUAL = "MANUAL", "人工重算"

    rule_version = models.ForeignKey(
        QualityAlertRuleVersion,
        on_delete=models.PROTECT,
        related_name="metric_snapshots",
    )
    factory_organization = models.ForeignKey(
        "account.Organization",
        on_delete=models.PROTECT,
        related_name="quality_metric_snapshots",
    )
    target_type = models.CharField(max_length=32, db_index=True)
    target_id = models.PositiveBigIntegerField(null=True, blank=True, db_index=True)
    target_key = models.CharField(max_length=255, db_index=True)
    target_snapshot = models.JSONField(default=dict)
    calculation_source = models.CharField(max_length=16, choices=Source.choices)
    window_started_at = models.DateTimeField()
    window_ended_at = models.DateTimeField()
    exposure_device_count = models.PositiveIntegerField(default=0)
    fault_device_count = models.PositiveIntegerField(default=0)
    metric_value = models.DecimalField(max_digits=12, decimal_places=6, default=0)
    threshold_met = models.BooleanField(default=False, db_index=True)
    severity = models.CharField(
        max_length=16,
        choices=QualityAlert.Severity.choices,
        blank=True,
        default="",
    )
    evidence_snapshot = models.JSONField(default=dict)
    calculated_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-calculated_at", "-id")
        indexes = [
            models.Index(
                fields=("rule_version", "target_type", "target_id", "calculated_at"),
                name="qtrace_metric_target_idx",
            ),
            models.Index(
                fields=("factory_organization", "threshold_met", "calculated_at"),
                name="qtrace_metric_factory_idx",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValueError("质量指标快照创建后不可覆盖。")
        return super().save(*args, **kwargs)


class QualityMetricSnapshotDevice(models.Model):
    """Complete device-level evidence for a metric snapshot.

    The JSON evidence remains a compact preview. This table is the complete,
    immutable membership used when a human opens a quality case.
    """

    metric_snapshot = models.ForeignKey(
        QualityMetricSnapshot,
        on_delete=models.PROTECT,
        related_name="device_evidence",
    )
    device = models.ForeignKey(
        "lifecycle.Device",
        on_delete=models.PROTECT,
        related_name="quality_metric_evidence",
    )
    is_exposure = models.BooleanField(default=True)
    is_fault = models.BooleanField(default=False, db_index=True)
    fault_occurrence_count = models.PositiveIntegerField(default=0)
    evidence_snapshot = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("metric_snapshot_id", "device_id")
        constraints = [
            models.UniqueConstraint(
                fields=("metric_snapshot", "device"),
                name="qtrace_unique_metric_device",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValueError("质量指标设备证据创建后不可覆盖。")
        return super().save(*args, **kwargs)


class QualityGateDecision(models.Model):
    """Audit record for a central quality operation-gate decision."""

    class Operation(models.TextChoices):
        QRCODE_PRINT = "QRCODE_PRINT", "二维码打印"
        STOCK_OUTBOUND = "STOCK_OUTBOUND", "库存出库"
        SHIPMENT = "SHIPMENT", "发货"
        TRANSFER = "TRANSFER", "商品流转"
        ACTIVATION = "ACTIVATION", "设备激活"

    factory_organization = models.ForeignKey(
        "account.Organization",
        on_delete=models.PROTECT,
        related_name="quality_gate_decisions",
    )
    device = models.ForeignKey(
        "lifecycle.Device",
        on_delete=models.PROTECT,
        related_name="quality_gate_decisions",
    )
    operation = models.CharField(max_length=32, choices=Operation.choices, db_index=True)
    allowed = models.BooleanField(db_index=True)
    decision_code = models.CharField(max_length=32, db_index=True)
    reason = models.TextField(blank=True, default="")
    quality_case = models.ForeignKey(
        BatchQualityCase,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="gate_decisions",
    )
    control_action = models.ForeignKey(
        QualityControlAction,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="gate_decisions",
    )
    checked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="quality_gate_checks",
    )
    checked_role_snapshot = models.CharField(max_length=64, blank=True, default="")
    context_snapshot = models.JSONField(default=dict)
    checked_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-checked_at", "-id")
        indexes = [
            models.Index(
                fields=("factory_organization", "device", "operation", "checked_at"),
                name="qtrace_gate_device_idx",
            ),
        ]
