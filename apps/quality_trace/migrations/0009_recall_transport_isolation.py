from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("account", "0004_add_factory_aftersales_role"),
        ("quality_trace", "0008_customer_handover"),
    ]

    operations = [
        migrations.CreateModel(
            name="QualityRecallTransportLeg",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("sequence_no", models.PositiveIntegerField()),
                ("relation_snapshot", models.JSONField(default=dict)),
                ("status", models.CharField(choices=[("PREPARING", "待发出"), ("IN_TRANSIT", "运输中"), ("RECEIVED", "已验收"), ("CANCELLED", "已取消")], db_index=True, default="PREPARING", max_length=16)),
                ("is_factory_destination", models.BooleanField(default=False)),
                ("logistics_provider", models.CharField(blank=True, default="", max_length=128)),
                ("tracking_no", models.CharField(blank=True, default="", max_length=128)),
                ("dispatch_evidence", models.JSONField(default=dict)),
                ("dispatched_at", models.DateTimeField(blank=True, null=True)),
                ("receipt_evidence", models.JSONField(default=dict)),
                ("received_at", models.DateTimeField(blank=True, null=True)),
                ("custody_snapshot", models.JSONField(default=dict)),
                ("campaign", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="transport_legs", to="quality_trace.qualityrecallcampaign")),
                ("recall_device", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="transport_legs", to="quality_trace.qualityrecalldevice")),
                ("from_organization", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="quality_recall_transport_departures", to="account.organization")),
                ("to_organization", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="quality_recall_transport_arrivals", to="account.organization")),
                ("dispatched_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="dispatched_quality_recall_transport_legs", to=settings.AUTH_USER_MODEL)),
                ("received_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="received_quality_recall_transport_legs", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("recall_device_id", "sequence_no")},
        ),
        migrations.AddIndex(model_name="qualityrecalltransportleg", index=models.Index(fields=["to_organization", "status"], name="qtrace_transport_arrival_idx")),
        migrations.AddIndex(model_name="qualityrecalltransportleg", index=models.Index(fields=["campaign", "status"], name="qtrace_transport_state_idx")),
        migrations.AddConstraint(model_name="qualityrecalltransportleg", constraint=models.UniqueConstraint(fields=("recall_device", "sequence_no"), name="qtrace_unique_transport_sequence")),
        migrations.AddConstraint(model_name="qualityrecalltransportleg", constraint=models.CheckConstraint(condition=~models.Q(from_organization=models.F("to_organization")), name="qtrace_transport_orgs_differ")),
        migrations.CreateModel(
            name="QualityRecallIsolationReceipt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("disposition", models.CharField(choices=[("PENDING_INSPECTION", "待检测"), ("PENDING_REPAIR", "待修复"), ("PENDING_DISPOSAL", "待报废处置")], max_length=24)),
                ("isolation_location", models.CharField(max_length=255)),
                ("evidence_snapshot", models.JSONField(default=dict)),
                ("isolated_at", models.DateTimeField()),
                ("source_activation_preserved", models.BooleanField(default=True)),
                ("ordinary_inventory_created", models.BooleanField(default=False)),
                ("campaign", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="isolation_receipts", to="quality_trace.qualityrecallcampaign")),
                ("factory_organization", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="quality_recall_isolation_receipts", to="account.organization")),
                ("isolated_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="isolated_quality_recall_devices", to=settings.AUTH_USER_MODEL)),
                ("recall_device", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="isolation_receipt", to="quality_trace.qualityrecalldevice")),
                ("transport_leg", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="isolation_receipt", to="quality_trace.qualityrecalltransportleg")),
            ],
            options={"ordering": ("-isolated_at", "-id")},
        ),
        migrations.AddIndex(model_name="qualityrecallisolationreceipt", index=models.Index(fields=["factory_organization", "disposition", "isolated_at"], name="qtrace_isolation_queue_idx")),
    ]
