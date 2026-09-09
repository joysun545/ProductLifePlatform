from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("lifecycle", "0021_aftersales_application_workflow"),
        ("quality_trace", "0005_control_action_completed"),
    ]

    operations = [
        migrations.CreateModel(
            name="QualityRecallCampaign",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("recall_no", models.CharField(max_length=64, unique=True)),
                ("title", models.CharField(max_length=160)),
                ("reason", models.TextField()),
                ("risk_level", models.CharField(choices=[("LOW", "一般"), ("MEDIUM", "较高"), ("HIGH", "高风险"), ("CRITICAL", "重大风险")], max_length=16)),
                ("remedy_type", models.CharField(choices=[("INSPECTION", "检查"), ("REPAIR", "维修"), ("REPLACE", "更换"), ("RETURN", "退回"), ("REFUND", "退款"), ("SCRAP", "报废"), ("OTHER", "其他")], max_length=16)),
                ("deadline", models.DateField()),
                ("status", models.CharField(choices=[("DRAFT", "待发布"), ("ACTIVE", "召回中"), ("COMPLETED", "已完成"), ("CANCELLED", "已取消")], db_index=True, default="DRAFT", max_length=16)),
                ("scope_snapshot", models.JSONField(default=dict)),
                ("device_count", models.PositiveIntegerField(default=0)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("launched_at", models.DateTimeField(blank=True, null=True)),
                ("completed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="completed_quality_recalls", to=settings.AUTH_USER_MODEL)),
                ("control_action", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="recall_campaign", to="quality_trace.qualitycontrolaction")),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="created_quality_recalls", to=settings.AUTH_USER_MODEL)),
                ("factory_organization", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="quality_recall_campaigns", to="account.organization")),
                ("launched_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="launched_quality_recalls", to=settings.AUTH_USER_MODEL)),
                ("quality_case", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="recall_campaigns", to="quality_trace.batchqualitycase")),
            ],
            options={"ordering": ("-created_at", "-id")},
        ),
        migrations.CreateModel(
            name="QualityRecallDevice",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sn_snapshot", models.CharField(max_length=64)),
                ("product_model_snapshot", models.JSONField(default=dict)),
                ("production_batch_snapshot", models.CharField(blank=True, default="", max_length=64)),
                ("location_snapshot", models.JSONField(default=dict)),
                ("responsibility_snapshot", models.JSONField(default=dict)),
                ("trace_evidence_snapshot", models.JSONField(default=dict)),
                ("status", models.CharField(choices=[("PENDING_NOTICE", "待通知"), ("NOTIFIED", "已通知"), ("ACKNOWLEDGED", "已确认知晓"), ("RETURN_IN_PROGRESS", "退回中"), ("RECEIVED", "已收回"), ("DISPOSED", "已完成处置"), ("UNREACHABLE", "无法联系"), ("EXEMPTED", "人工豁免")], db_index=True, default="PENDING_NOTICE", max_length=24)),
                ("status_updated_at", models.DateTimeField()),
                ("included_at", models.DateTimeField(auto_now_add=True)),
                ("campaign", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="devices", to="quality_trace.qualityrecallcampaign")),
                ("case_membership", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="recall_memberships", to="quality_trace.batchqualityaffecteddevice")),
                ("device", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="quality_recall_records", to="lifecycle.device")),
            ],
            options={"ordering": ("campaign_id", "device_id")},
        ),
        migrations.CreateModel(
            name="QualityRecallDeviceEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("from_status", models.CharField(blank=True, default="", max_length=24)),
                ("to_status", models.CharField(choices=[("PENDING_NOTICE", "待通知"), ("NOTIFIED", "已通知"), ("ACKNOWLEDGED", "已确认知晓"), ("RETURN_IN_PROGRESS", "退回中"), ("RECEIVED", "已收回"), ("DISPOSED", "已完成处置"), ("UNREACHABLE", "无法联系"), ("EXEMPTED", "人工豁免")], max_length=24)),
                ("reason", models.TextField(blank=True, default="")),
                ("actor_role_snapshot", models.CharField(blank=True, default="", max_length=64)),
                ("payload", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="quality_recall_device_events", to=settings.AUTH_USER_MODEL)),
                ("campaign", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="device_events", to="quality_trace.qualityrecallcampaign")),
                ("recall_device", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="events", to="quality_trace.qualityrecalldevice")),
            ],
            options={"ordering": ("created_at", "id")},
        ),
        migrations.AddIndex(model_name="qualityrecallcampaign", index=models.Index(fields=["factory_organization", "status", "deadline"], name="qtrace_recall_queue_idx")),
        migrations.AddIndex(model_name="qualityrecallcampaign", index=models.Index(fields=["quality_case", "status"], name="qtrace_recall_case_idx")),
        migrations.AddConstraint(model_name="qualityrecallcampaign", constraint=models.CheckConstraint(condition=models.Q(("device_count__gt", 0)), name="qtrace_recall_device_count_positive")),
        migrations.AddIndex(model_name="qualityrecalldevice", index=models.Index(fields=["campaign", "status"], name="qtrace_recall_device_state_idx")),
        migrations.AddIndex(model_name="qualityrecalldevice", index=models.Index(fields=["device", "status"], name="qtrace_device_recall_state_idx")),
        migrations.AddConstraint(model_name="qualityrecalldevice", constraint=models.UniqueConstraint(fields=("campaign", "device"), name="qtrace_unique_recall_device")),
        migrations.AddConstraint(model_name="qualityrecalldevice", constraint=models.UniqueConstraint(fields=("campaign", "case_membership"), name="qtrace_unique_recall_membership")),
        migrations.AddIndex(model_name="qualityrecalldeviceevent", index=models.Index(fields=["campaign", "recall_device", "created_at"], name="qtrace_recall_event_idx")),
    ]
