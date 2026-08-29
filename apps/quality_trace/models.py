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


class QualityRecallCampaign(TimeStampedModel):
    """A factory recall batch with an immutable device scope."""

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "待发布"
        ACTIVE = "ACTIVE", "召回中"
        COMPLETED = "COMPLETED", "已完成"
        CANCELLED = "CANCELLED", "已取消"

    class RiskLevel(models.TextChoices):
        LOW = "LOW", "一般"
        MEDIUM = "MEDIUM", "较高"
        HIGH = "HIGH", "高风险"
        CRITICAL = "CRITICAL", "重大风险"

    class RemedyType(models.TextChoices):
        INSPECTION = "INSPECTION", "检查"
        REPAIR = "REPAIR", "维修"
        REPLACE = "REPLACE", "更换"
        RETURN = "RETURN", "退回"
        REFUND = "REFUND", "退款"
        SCRAP = "SCRAP", "报废"
        OTHER = "OTHER", "其他"

    recall_no = models.CharField(max_length=64, unique=True)
    quality_case = models.ForeignKey(
        BatchQualityCase, on_delete=models.PROTECT, related_name="recall_campaigns",
    )
    factory_organization = models.ForeignKey(
        "account.Organization", on_delete=models.PROTECT, related_name="quality_recall_campaigns",
    )
    control_action = models.OneToOneField(
        QualityControlAction, null=True, blank=True, on_delete=models.PROTECT,
        related_name="recall_campaign",
    )
    title = models.CharField(max_length=160)
    reason = models.TextField()
    risk_level = models.CharField(max_length=16, choices=RiskLevel.choices)
    remedy_type = models.CharField(max_length=16, choices=RemedyType.choices)
    deadline = models.DateField()
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.DRAFT, db_index=True,
    )
    scope_snapshot = models.JSONField(default=dict)
    device_count = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_quality_recalls",
    )
    launched_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name="launched_quality_recalls",
    )
    launched_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name="completed_quality_recalls",
    )
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(
                fields=("factory_organization", "status", "deadline"),
                name="qtrace_recall_queue_idx",
            ),
            models.Index(fields=("quality_case", "status"), name="qtrace_recall_case_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(device_count__gt=0), name="qtrace_recall_device_count_positive",
            ),
        ]


class QualityRecallDevice(models.Model):
    """Frozen per-device membership and current execution state."""

    class Status(models.TextChoices):
        PENDING_NOTICE = "PENDING_NOTICE", "待通知"
        NOTIFIED = "NOTIFIED", "已通知"
        ACKNOWLEDGED = "ACKNOWLEDGED", "已确认知晓"
        RETURN_IN_PROGRESS = "RETURN_IN_PROGRESS", "退回中"
        RECEIVED = "RECEIVED", "已收回"
        DISPOSED = "DISPOSED", "已完成处置"
        UNREACHABLE = "UNREACHABLE", "无法联系"
        EXEMPTED = "EXEMPTED", "人工豁免"

    campaign = models.ForeignKey(
        QualityRecallCampaign, on_delete=models.PROTECT, related_name="devices",
    )
    case_membership = models.ForeignKey(
        BatchQualityAffectedDevice, on_delete=models.PROTECT, related_name="recall_memberships",
    )
    device = models.ForeignKey(
        "lifecycle.Device", on_delete=models.PROTECT, related_name="quality_recall_records",
    )
    sn_snapshot = models.CharField(max_length=64)
    product_model_snapshot = models.JSONField(default=dict)
    production_batch_snapshot = models.CharField(max_length=64, blank=True, default="")
    location_snapshot = models.JSONField(default=dict)
    responsibility_snapshot = models.JSONField(default=dict)
    trace_evidence_snapshot = models.JSONField(default=dict)
    status = models.CharField(
        max_length=24, choices=Status.choices, default=Status.PENDING_NOTICE, db_index=True,
    )
    status_updated_at = models.DateTimeField()
    included_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("campaign_id", "device_id")
        indexes = [
            models.Index(fields=("campaign", "status"), name="qtrace_recall_device_state_idx"),
            models.Index(fields=("device", "status"), name="qtrace_device_recall_state_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("campaign", "device"), name="qtrace_unique_recall_device",
            ),
            models.UniqueConstraint(
                fields=("campaign", "case_membership"), name="qtrace_unique_recall_membership",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.only(
                "campaign_id", "case_membership_id", "device_id",
            ).get(pk=self.pk)
            if (
                original.campaign_id != self.campaign_id
                or original.case_membership_id != self.case_membership_id
                or original.device_id != self.device_id
            ):
                raise ValueError("召回设备名单创建后不可更换所属批次或设备。")
        return super().save(*args, **kwargs)


class QualityRecallDeviceEvent(models.Model):
    campaign = models.ForeignKey(
        QualityRecallCampaign, on_delete=models.PROTECT, related_name="device_events",
    )
    recall_device = models.ForeignKey(
        QualityRecallDevice, on_delete=models.PROTECT, related_name="events",
    )
    from_status = models.CharField(max_length=24, blank=True, default="")
    to_status = models.CharField(max_length=24, choices=QualityRecallDevice.Status.choices)
    reason = models.TextField(blank=True, default="")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="quality_recall_device_events",
    )
    actor_role_snapshot = models.CharField(max_length=64, blank=True, default="")
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at", "id")
        indexes = [
            models.Index(
                fields=("campaign", "recall_device", "created_at"),
                name="qtrace_recall_event_idx",
            ),
        ]


class QualityRecallReturnLeg(models.Model):
    """One existing GoodsTransfer RETURN leg in a multi-level recall return."""

    class Status(models.TextChoices):
        PREPARING = "PREPARING", "准备退回"
        IN_TRANSIT = "IN_TRANSIT", "退回在途"
        UPSTREAM_RECEIVED = "UPSTREAM_RECEIVED", "上级商家已收货"
        FACTORY_PICKED_UP = "FACTORY_PICKED_UP", "厂家已取回"
        FACTORY_ISOLATED = "FACTORY_ISOLATED", "厂家已隔离入库"
        CANCELLED = "CANCELLED", "已取消"

    campaign = models.ForeignKey(
        QualityRecallCampaign, on_delete=models.PROTECT, related_name="return_legs",
    )
    recall_device = models.ForeignKey(
        QualityRecallDevice, on_delete=models.PROTECT, related_name="return_legs",
    )
    sequence_no = models.PositiveIntegerField()
    goods_transfer = models.OneToOneField(
        "lifecycle.GoodsTransfer", on_delete=models.PROTECT,
        related_name="quality_recall_return_leg",
    )
    goods_transfer_item = models.OneToOneField(
        "lifecycle.GoodsTransferItem", on_delete=models.PROTECT,
        related_name="quality_recall_return_leg",
    )
    from_organization_snapshot = models.JSONField(default=dict)
    to_organization_snapshot = models.JSONField(default=dict)
    status = models.CharField(
        max_length=24, choices=Status.choices, default=Status.PREPARING, db_index=True,
    )
    isolation_snapshot = models.JSONField(default=dict)
    bound_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="bound_quality_recall_return_legs",
    )
    bound_at = models.DateTimeField(auto_now_add=True)
    status_updated_at = models.DateTimeField()

    class Meta:
        ordering = ("recall_device_id", "sequence_no")
        indexes = [
            models.Index(
                fields=("campaign", "status"), name="qtrace_return_leg_state_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("recall_device", "sequence_no"),
                name="qtrace_unique_return_leg_sequence",
            ),
        ]


class QualityRecallCustomerHandover(TimeStampedModel):
    """Activated-device custody handover from customer to source merchant."""

    class Status(models.TextChoices):
        PENDING_CUSTOMER_ACK = "PENDING_CUSTOMER_ACK", "待用户确认"
        ACKNOWLEDGED = "ACKNOWLEDGED", "用户已确认"
        PICKUP_SCHEDULED = "PICKUP_SCHEDULED", "已预约取回"
        MERCHANT_RECEIVED = "MERCHANT_RECEIVED", "商家已验收接管"
        CANCELLED = "CANCELLED", "已取消"

    campaign = models.ForeignKey(
        QualityRecallCampaign, on_delete=models.PROTECT, related_name="customer_handovers",
    )
    recall_device = models.OneToOneField(
        QualityRecallDevice, on_delete=models.PROTECT, related_name="customer_handover",
    )
    customer_organization = models.ForeignKey(
        "account.Organization", on_delete=models.PROTECT,
        related_name="quality_recall_customer_handovers",
    )
    merchant_organization = models.ForeignKey(
        "account.Organization", on_delete=models.PROTECT,
        related_name="quality_recall_merchant_handovers",
    )
    status = models.CharField(
        max_length=32, choices=Status.choices,
        default=Status.PENDING_CUSTOMER_ACK, db_index=True,
    )
    customer_snapshot = models.JSONField(default=dict)
    merchant_snapshot = models.JSONField(default=dict)
    customer_confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name="confirmed_quality_recall_handovers",
    )
    customer_confirmed_at = models.DateTimeField(null=True, blank=True)
    customer_note = models.TextField(blank=True, default="")
    pickup_scheduled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name="scheduled_quality_recall_pickups",
    )
    pickup_scheduled_at = models.DateTimeField(null=True, blank=True)
    pickup_address = models.TextField(blank=True, default="")
    pickup_note = models.TextField(blank=True, default="")
    merchant_received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name="received_quality_recall_handovers",
    )
    merchant_received_at = models.DateTimeField(null=True, blank=True)
    receipt_note = models.TextField(blank=True, default="")
    custody_snapshot = models.JSONField(default=dict)

    class Meta:
        ordering = ("-updated_at", "-id")
        indexes = [
            models.Index(fields=("campaign", "status"), name="qtrace_handover_state_idx"),
            models.Index(fields=("merchant_organization", "status"), name="qtrace_handover_merchant_idx"),
        ]


class QualityRecallHandoverEvidence(models.Model):
    """Immutable private-evidence reference; binary storage is handled by the upload layer."""

    class Kind(models.TextChoices):
        CUSTOMER_CONFIRMATION = "CUSTOMER_CONFIRMATION", "用户确认凭证"
        PICKUP = "PICKUP", "取回凭证"
        MERCHANT_RECEIPT = "MERCHANT_RECEIPT", "商家验收凭证"

    handover = models.ForeignKey(
        QualityRecallCustomerHandover, on_delete=models.PROTECT, related_name="evidence",
    )
    kind = models.CharField(max_length=32, choices=Kind.choices)
    client_request_id = models.CharField(max_length=64)
    storage_key = models.CharField(max_length=255)
    original_name = models.CharField(max_length=255, blank=True, default="")
    content_type = models.CharField(max_length=128, blank=True, default="")
    file_size = models.PositiveBigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True, default="")
    remark = models.TextField(blank=True, default="")
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="quality_recall_handover_evidence",
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("uploaded_at", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("handover", "client_request_id"),
                name="qtrace_unique_handover_request",
            ),
        ]


class QualityRecallTransportLeg(TimeStampedModel):
    """Recall-only custody transport; never creates ordinary sellable inventory."""

    class Status(models.TextChoices):
        PREPARING = "PREPARING", "待发出"
        IN_TRANSIT = "IN_TRANSIT", "运输中"
        RECEIVED = "RECEIVED", "已验收"
        CANCELLED = "CANCELLED", "已取消"

    campaign = models.ForeignKey(
        QualityRecallCampaign, on_delete=models.PROTECT, related_name="transport_legs",
    )
    recall_device = models.ForeignKey(
        QualityRecallDevice, on_delete=models.PROTECT, related_name="transport_legs",
    )
    sequence_no = models.PositiveIntegerField()
    from_organization = models.ForeignKey(
        "account.Organization", on_delete=models.PROTECT,
        related_name="quality_recall_transport_departures",
    )
    to_organization = models.ForeignKey(
        "account.Organization", on_delete=models.PROTECT,
        related_name="quality_recall_transport_arrivals",
    )
    relation_snapshot = models.JSONField(default=dict)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PREPARING, db_index=True,
    )
    is_factory_destination = models.BooleanField(default=False)
    logistics_provider = models.CharField(max_length=128, blank=True, default="")
    tracking_no = models.CharField(max_length=128, blank=True, default="")
    dispatch_evidence = models.JSONField(default=dict)
    dispatched_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name="dispatched_quality_recall_transport_legs",
    )
    dispatched_at = models.DateTimeField(null=True, blank=True)
    receipt_evidence = models.JSONField(default=dict)
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name="received_quality_recall_transport_legs",
    )
    received_at = models.DateTimeField(null=True, blank=True)
    custody_snapshot = models.JSONField(default=dict)

    class Meta:
        ordering = ("recall_device_id", "sequence_no")
        indexes = [
            models.Index(fields=("to_organization", "status"), name="qtrace_transport_arrival_idx"),
            models.Index(fields=("campaign", "status"), name="qtrace_transport_state_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("recall_device", "sequence_no"),
                name="qtrace_unique_transport_sequence",
            ),
            models.CheckConstraint(
                condition=~Q(from_organization=models.F("to_organization")),
                name="qtrace_transport_orgs_differ",
            ),
        ]


class QualityRecallIsolationReceipt(TimeStampedModel):
    """Final factory isolation fact for an activated-device recall."""

    class Disposition(models.TextChoices):
        PENDING_INSPECTION = "PENDING_INSPECTION", "待检测"
        PENDING_REPAIR = "PENDING_REPAIR", "待修复"
        PENDING_DISPOSAL = "PENDING_DISPOSAL", "待报废处置"

    campaign = models.ForeignKey(
        QualityRecallCampaign, on_delete=models.PROTECT, related_name="isolation_receipts",
    )
    recall_device = models.OneToOneField(
        QualityRecallDevice, on_delete=models.PROTECT, related_name="isolation_receipt",
    )
    transport_leg = models.OneToOneField(
        QualityRecallTransportLeg, on_delete=models.PROTECT,
        related_name="isolation_receipt",
    )
    factory_organization = models.ForeignKey(
        "account.Organization", on_delete=models.PROTECT,
        related_name="quality_recall_isolation_receipts",
    )
    disposition = models.CharField(max_length=24, choices=Disposition.choices)
    isolation_location = models.CharField(max_length=255)
    evidence_snapshot = models.JSONField(default=dict)
    isolated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="isolated_quality_recall_devices",
    )
    isolated_at = models.DateTimeField()
    source_activation_preserved = models.BooleanField(default=True)
    ordinary_inventory_created = models.BooleanField(default=False)

    class Meta:
        ordering = ("-isolated_at", "-id")
        indexes = [
            models.Index(
                fields=("factory_organization", "disposition", "isolated_at"),
                name="qtrace_isolation_queue_idx",
            ),
        ]


class QualityRecallProgressSnapshot(models.Model):
    campaign = models.ForeignKey(
        QualityRecallCampaign, on_delete=models.PROTECT, related_name="progress_snapshots",
    )
    counts = models.JSONField(default=dict)
    completion_eligible_count = models.PositiveIntegerField(default=0)
    incomplete_device_ids = models.JSONField(default=list)
    overdue_device_ids = models.JSONField(default=list)
    is_overdue = models.BooleanField(default=False, db_index=True)
    escalation_workitem_id = models.BigIntegerField(null=True, blank=True)
    calculated_at = models.DateTimeField(db_index=True)
    calculation_source = models.CharField(max_length=16, default="MANUAL")

    class Meta:
        ordering = ("-calculated_at", "-id")
        indexes = [
            models.Index(fields=("campaign", "calculated_at"), name="qtrace_progress_history_idx"),
        ]


class QualityRecallExceptionDecision(TimeStampedModel):
    class Decision(models.TextChoices):
        APPROVED = "APPROVED", "批准按无法联系结案"
        REJECTED = "REJECTED", "驳回"

    campaign = models.ForeignKey(
        QualityRecallCampaign, on_delete=models.PROTECT, related_name="exception_decisions",
    )
    recall_device = models.OneToOneField(
        QualityRecallDevice, on_delete=models.PROTECT, related_name="exception_decision",
    )
    decision = models.CharField(max_length=16, choices=Decision.choices)
    reason = models.TextField()
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="quality_recall_exception_decisions",
    )
    decided_role_snapshot = models.CharField(max_length=64)
    decided_at = models.DateTimeField()

    class Meta:
        ordering = ("-decided_at", "-id")


class QualityRecallCloseout(models.Model):
    campaign = models.OneToOneField(
        QualityRecallCampaign, on_delete=models.PROTECT, related_name="closeout",
    )
    progress_snapshot = models.ForeignKey(
        QualityRecallProgressSnapshot, on_delete=models.PROTECT,
        related_name="campaign_closeouts",
    )
    execution_summary = models.JSONField(default=dict)
    incomplete_reasons = models.JSONField(default=list)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="completed_quality_recall_campaigns",
    )
    completed_role_snapshot = models.CharField(max_length=64)
    completed_at = models.DateTimeField()
    remediation_hold_action = models.ForeignKey(
        QualityControlAction, on_delete=models.PROTECT,
        related_name="recall_closeouts",
    )

    class Meta:
        ordering = ("-completed_at", "-id")
