from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("lifecycle", "0021_aftersales_application_workflow"),
        ("quality_trace", "0006_recall_campaign"),
    ]

    operations = [
        migrations.CreateModel(
            name="QualityRecallReturnLeg",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sequence_no", models.PositiveIntegerField()),
                ("from_organization_snapshot", models.JSONField(default=dict)),
                ("to_organization_snapshot", models.JSONField(default=dict)),
                ("status", models.CharField(choices=[("PREPARING", "准备退回"), ("IN_TRANSIT", "退回在途"), ("UPSTREAM_RECEIVED", "上级商家已收货"), ("FACTORY_PICKED_UP", "厂家已取回"), ("FACTORY_ISOLATED", "厂家已隔离入库"), ("CANCELLED", "已取消")], db_index=True, default="PREPARING", max_length=24)),
                ("isolation_snapshot", models.JSONField(default=dict)),
                ("bound_at", models.DateTimeField(auto_now_add=True)),
                ("status_updated_at", models.DateTimeField()),
                ("bound_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="bound_quality_recall_return_legs", to=settings.AUTH_USER_MODEL)),
                ("campaign", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="return_legs", to="quality_trace.qualityrecallcampaign")),
                ("goods_transfer", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="quality_recall_return_leg", to="lifecycle.goodstransfer")),
                ("goods_transfer_item", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="quality_recall_return_leg", to="lifecycle.goodstransferitem")),
                ("recall_device", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="return_legs", to="quality_trace.qualityrecalldevice")),
            ],
            options={"ordering": ("recall_device_id", "sequence_no")},
        ),
        migrations.AddIndex(
            model_name="qualityrecallreturnleg",
            index=models.Index(fields=["campaign", "status"], name="qtrace_return_leg_state_idx"),
        ),
        migrations.AddConstraint(
            model_name="qualityrecallreturnleg",
            constraint=models.UniqueConstraint(fields=("recall_device", "sequence_no"), name="qtrace_unique_return_leg_sequence"),
        ),
    ]
