from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("quality_trace", "0003_case_assessment_control"),
    ]

    operations = [
        migrations.CreateModel(
            name="QualityGateDecision",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("operation", models.CharField(choices=[("QRCODE_PRINT", "二维码打印"), ("STOCK_OUTBOUND", "库存出库"), ("SHIPMENT", "发货"), ("TRANSFER", "商品流转"), ("ACTIVATION", "设备激活")], db_index=True, max_length=32)),
                ("allowed", models.BooleanField(db_index=True)),
                ("decision_code", models.CharField(db_index=True, max_length=32)),
                ("reason", models.TextField(blank=True, default="")),
                ("checked_role_snapshot", models.CharField(blank=True, default="", max_length=64)),
                ("context_snapshot", models.JSONField(default=dict)),
                ("checked_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("checked_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="quality_gate_checks", to=settings.AUTH_USER_MODEL)),
                ("control_action", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="gate_decisions", to="quality_trace.qualitycontrolaction")),
                ("device", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="quality_gate_decisions", to="lifecycle.device")),
                ("factory_organization", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="quality_gate_decisions", to="account.organization")),
                ("quality_case", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="gate_decisions", to="quality_trace.batchqualitycase")),
            ],
            options={"ordering": ("-checked_at", "-id")},
        ),
        migrations.AddIndex(
            model_name="qualitygatedecision",
            index=models.Index(fields=["factory_organization", "device", "operation", "checked_at"], name="qtrace_gate_device_idx"),
        ),
    ]
