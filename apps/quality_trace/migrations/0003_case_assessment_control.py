from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("quality_trace", "0002_qualitymetricsnapshot"),
    ]

    operations = [
        migrations.CreateModel(
            name="QualityMetricSnapshotDevice",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("is_exposure", models.BooleanField(default=True)),
                ("is_fault", models.BooleanField(db_index=True, default=False)),
                ("fault_occurrence_count", models.PositiveIntegerField(default=0)),
                ("evidence_snapshot", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("device", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="quality_metric_evidence", to="lifecycle.device")),
                ("metric_snapshot", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="device_evidence", to="quality_trace.qualitymetricsnapshot")),
            ],
            options={"ordering": ("metric_snapshot_id", "device_id")},
        ),
        migrations.AddField(
            model_name="qualitycontrolaction",
            name="end_reason",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="qualitycontrolaction",
            name="ended_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="qualitycontrolaction",
            name="ended_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="ended_quality_control_actions",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddConstraint(
            model_name="batchqualitycase",
            constraint=models.UniqueConstraint(
                condition=models.Q(("source_alert__isnull", False)),
                fields=("source_alert",),
                name="qtrace_unique_case_source_alert",
            ),
        ),
        migrations.AddConstraint(
            model_name="qualitymetricsnapshotdevice",
            constraint=models.UniqueConstraint(
                fields=("metric_snapshot", "device"),
                name="qtrace_unique_metric_device",
            ),
        ),
    ]
