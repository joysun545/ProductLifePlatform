from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("account", "0004_add_factory_aftersales_role"),
        ("quality_trace", "0007_recall_return_leg"),
    ]

    operations = [
        migrations.CreateModel(
            name="QualityRecallCustomerHandover",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("status", models.CharField(choices=[("PENDING_CUSTOMER_ACK", "待用户确认"), ("ACKNOWLEDGED", "用户已确认"), ("PICKUP_SCHEDULED", "已预约取回"), ("MERCHANT_RECEIVED", "商家已验收接管"), ("CANCELLED", "已取消")], db_index=True, default="PENDING_CUSTOMER_ACK", max_length=32)),
                ("customer_snapshot", models.JSONField(default=dict)),
                ("merchant_snapshot", models.JSONField(default=dict)),
                ("customer_confirmed_at", models.DateTimeField(blank=True, null=True)),
                ("customer_note", models.TextField(blank=True, default="")),
                ("pickup_scheduled_at", models.DateTimeField(blank=True, null=True)),
                ("pickup_address", models.TextField(blank=True, default="")),
                ("pickup_note", models.TextField(blank=True, default="")),
                ("merchant_received_at", models.DateTimeField(blank=True, null=True)),
                ("receipt_note", models.TextField(blank=True, default="")),
                ("custody_snapshot", models.JSONField(default=dict)),
                ("campaign", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="customer_handovers", to="quality_trace.qualityrecallcampaign")),
                ("customer_organization", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="quality_recall_customer_handovers", to="account.organization")),
                ("merchant_organization", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="quality_recall_merchant_handovers", to="account.organization")),
                ("recall_device", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="customer_handover", to="quality_trace.qualityrecalldevice")),
                ("customer_confirmed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="confirmed_quality_recall_handovers", to=settings.AUTH_USER_MODEL)),
                ("pickup_scheduled_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="scheduled_quality_recall_pickups", to=settings.AUTH_USER_MODEL)),
                ("merchant_received_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="received_quality_recall_handovers", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("-updated_at", "-id")},
        ),
        migrations.AddIndex(model_name="qualityrecallcustomerhandover", index=models.Index(fields=["campaign", "status"], name="qtrace_handover_state_idx")),
        migrations.AddIndex(model_name="qualityrecallcustomerhandover", index=models.Index(fields=["merchant_organization", "status"], name="qtrace_handover_merchant_idx")),
        migrations.CreateModel(
            name="QualityRecallHandoverEvidence",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("kind", models.CharField(choices=[("CUSTOMER_CONFIRMATION", "用户确认凭证"), ("PICKUP", "取回凭证"), ("MERCHANT_RECEIPT", "商家验收凭证")], max_length=32)),
                ("client_request_id", models.CharField(max_length=64)),
                ("storage_key", models.CharField(max_length=255)),
                ("original_name", models.CharField(blank=True, default="", max_length=255)),
                ("content_type", models.CharField(blank=True, default="", max_length=128)),
                ("file_size", models.PositiveBigIntegerField(default=0)),
                ("sha256", models.CharField(blank=True, default="", max_length=64)),
                ("remark", models.TextField(blank=True, default="")),
                ("uploaded_at", models.DateTimeField(auto_now_add=True)),
                ("handover", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="evidence", to="quality_trace.qualityrecallcustomerhandover")),
                ("uploaded_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="quality_recall_handover_evidence", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("uploaded_at", "id")},
        ),
        migrations.AddConstraint(model_name="qualityrecallhandoverevidence", constraint=models.UniqueConstraint(fields=("handover", "client_request_id"), name="qtrace_unique_handover_request")),
    ]
