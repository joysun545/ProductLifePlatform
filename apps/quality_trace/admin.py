from django.contrib import admin

from .models import (
    BatchQualityAffectedDevice,
    BatchQualityCase,
    QualityAlert,
    QualityAlertRule,
    QualityAlertRuleVersion,
    QualityAssessment,
    QualityControlAction,
    QualityMetricSnapshot,
    QualityMetricSnapshotDevice,
    QualityGateDecision,
    QualityTraceEvent,
)


@admin.register(QualityAlertRule)
class QualityAlertRuleAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "factory_organization", "status", "current_version")
    list_filter = ("status", "factory_organization")
    search_fields = ("code", "name", "factory_organization__name")


@admin.register(QualityAlertRuleVersion)
class QualityAlertRuleVersionAdmin(admin.ModelAdmin):
    list_display = ("rule", "version", "metric_type", "effective_from", "published_at")
    list_filter = ("metric_type", "allow_automatic_temporary_hold")


@admin.register(QualityAlert)
class QualityAlertAdmin(admin.ModelAdmin):
    list_display = ("target_key", "factory_organization", "severity", "status", "last_triggered_at")
    list_filter = ("severity", "status", "factory_organization")
    search_fields = ("deduplication_key", "target_key")


@admin.register(BatchQualityCase)
class BatchQualityCaseAdmin(admin.ModelAdmin):
    list_display = ("case_no", "factory_organization", "scope_type", "status", "opened_at")
    list_filter = ("status", "scope_type", "factory_organization")
    search_fields = ("case_no", "scope_key")


admin.site.register(QualityAssessment)
admin.site.register(BatchQualityAffectedDevice)
admin.site.register(QualityControlAction)
admin.site.register(QualityTraceEvent)
admin.site.register(QualityMetricSnapshot)
admin.site.register(QualityMetricSnapshotDevice)
admin.site.register(QualityGateDecision)
