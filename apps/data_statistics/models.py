from django.db import models


class MetricDefinition(models.Model):
    """统计口径字典；只描述指标，不保存业务事实。"""

    class Domain(models.TextChoices):
        MATERIAL = "MATERIAL", "物料配套"
        FLOW = "FLOW", "产品流转"
        AFTERSALES = "AFTERSALES", "售后服务"
        QUALITY = "QUALITY", "质量追溯"

    code = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=128)
    domain = models.CharField(max_length=16, choices=Domain.choices, db_index=True)
    unit = models.CharField(max_length=16, blank=True, default="")
    description = models.TextField(blank=True, default="")
    numerator_definition = models.TextField(blank=True, default="")
    denominator_definition = models.TextField(blank=True, default="")
    source_models = models.JSONField(default=list, blank=True)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("domain", "code")


class DailyMetricSnapshot(models.Model):
    """按组织、日期、指标和维度冻结的日快照。"""

    organization = models.ForeignKey(
        "account.Organization", on_delete=models.PROTECT,
        related_name="data_statistic_snapshots",
    )
    snapshot_date = models.DateField(db_index=True)
    metric = models.ForeignKey(
        MetricDefinition, on_delete=models.PROTECT, related_name="daily_snapshots",
    )
    value = models.DecimalField(max_digits=20, decimal_places=6)
    dimensions = models.JSONField(default=dict, blank=True)
    dimension_key = models.CharField(max_length=255, blank=True, default="")
    generated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-snapshot_date", "metric__code", "dimension_key")
        constraints = [
            models.UniqueConstraint(
                fields=("organization", "snapshot_date", "metric", "dimension_key"),
                name="data_stats_unique_daily_metric_dimension",
            ),
        ]
        indexes = [
            models.Index(
                fields=("organization", "metric", "snapshot_date"),
                name="data_stats_org_metric_date_idx",
            ),
        ]
