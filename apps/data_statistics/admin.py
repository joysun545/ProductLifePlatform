from django.contrib import admin

from .models import DailyMetricSnapshot, MetricDefinition


@admin.register(MetricDefinition)
class MetricDefinitionAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "domain", "unit", "enabled")
    list_filter = ("domain", "enabled")
    search_fields = ("code", "name")


@admin.register(DailyMetricSnapshot)
class DailyMetricSnapshotAdmin(admin.ModelAdmin):
    list_display = ("snapshot_date", "organization", "metric", "value", "dimension_key")
    list_filter = ("snapshot_date", "metric__domain")
    search_fields = ("organization__name", "metric__code", "dimension_key")
