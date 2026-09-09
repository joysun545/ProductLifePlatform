from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("quality_trace", "0009_recall_transport_isolation"),
    ]

    operations = [
        migrations.CreateModel(
            name="QualityRecallProgressSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("counts", models.JSONField(default=dict)),
                ("completion_eligible_count", models.PositiveIntegerField(default=0)),
                ("incomplete_device_ids", models.JSONField(default=list)),
                ("overdue_device_ids", models.JSONField(default=list)),
                ("is_overdue", models.BooleanField(db_index=True, default=False)),
                ("escalation_workitem_id", models.BigIntegerField(blank=True, null=True)),
                ("calculated_at", models.DateTimeField(db_index=True)),
                ("calculation_source", models.CharField(default="MANUAL", max_length=16)),
                ("campaign", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="progress_snapshots", to="quality_trace.qualityrecallcampaign")),
            ],
            options={"ordering": ("-calculated_at", "-id")},
        ),
        migrations.AddIndex(model_name="qualityrecallprogresssnapshot", index=models.Index(fields=["campaign", "calculated_at"], name="qtrace_progress_history_idx")),
        migrations.CreateModel(
            name="QualityRecallExceptionDecision",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("decision", models.CharField(choices=[("APPROVED", "批准按无法联系结案"), ("REJECTED", "驳回")], max_length=16)),
                ("reason", models.TextField()),
                ("decided_role_snapshot", models.CharField(max_length=64)),
                ("decided_at", models.DateTimeField()),
                ("campaign", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="exception_decisions", to="quality_trace.qualityrecallcampaign")),
                ("decided_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="quality_recall_exception_decisions", to=settings.AUTH_USER_MODEL)),
                ("recall_device", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="exception_decision", to="quality_trace.qualityrecalldevice")),
            ],
            options={"ordering": ("-decided_at", "-id")},
        ),
        migrations.CreateModel(
            name="QualityRecallCloseout",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("execution_summary", models.JSONField(default=dict)),
                ("incomplete_reasons", models.JSONField(default=list)),
                ("completed_role_snapshot", models.CharField(max_length=64)),
                ("completed_at", models.DateTimeField()),
                ("campaign", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="closeout", to="quality_trace.qualityrecallcampaign")),
                ("completed_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="completed_quality_recall_campaigns", to=settings.AUTH_USER_MODEL)),
                ("progress_snapshot", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="campaign_closeouts", to="quality_trace.qualityrecallprogresssnapshot")),
                ("remediation_hold_action", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="recall_closeouts", to="quality_trace.qualitycontrolaction")),
            ],
            options={"ordering": ("-completed_at", "-id")},
        ),
    ]
