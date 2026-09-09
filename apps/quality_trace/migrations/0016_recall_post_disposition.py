from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("quality_trace", "0015_recall_inventory_accounting_chain"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.CreateModel(
            name="QualityRecallPostDisposition",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("method", models.CharField(choices=[("DISASSEMBLE", "拆机"), ("REPAIR_NO_PARTS", "不换件维修"), ("REPAIR_WITH_PARTS", "换件维修")], max_length=24)),
                ("status", models.CharField(choices=[("PENDING", "待处置"), ("IN_PROGRESS", "处置中"), ("COMPLETED", "处置完成")], db_index=True, default="PENDING", max_length=16)),
                ("stock_result", models.CharField(choices=[("PENDING", "待处理"), ("DISASSEMBLED", "已拆机并结束生命周期"), ("READY_FOR_OUTBOUND", "已重新入库待出库")], default="PENDING", max_length=24)),
                ("decision_note", models.TextField(blank=True, default="")),
                ("completion_note", models.TextField(blank=True, default="")),
                ("replacement_parts", models.JSONField(blank=True, default=list)),
                ("decided_at", models.DateTimeField()),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("campaign", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="post_dispositions", to="quality_trace.qualityrecallcampaign")),
                ("recall_device", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="post_disposition", to="quality_trace.qualityrecalldevice")),
                ("decided_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="decided_recall_post_dispositions", to=settings.AUTH_USER_MODEL)),
                ("completed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="completed_recall_post_dispositions", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("campaign_id", "recall_device_id")},
        ),
        migrations.AddConstraint(
            model_name="qualityrecallpostdisposition",
            constraint=models.UniqueConstraint(fields=("campaign", "recall_device"), name="qtrace_unique_recall_post_disposition"),
        ),
    ]
