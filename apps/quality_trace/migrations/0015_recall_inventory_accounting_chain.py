from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("quality_trace", "0014_recall_delivery_collaboration"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="QualityRecallInventoryBatchConfirmation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("confirmed_role_snapshot", models.CharField(max_length=64)),
                ("confirmed_device_count", models.PositiveIntegerField()),
                ("confirmed_at", models.DateTimeField()),
                ("note", models.TextField(blank=True, default="")),
                ("campaign", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="inventory_batch_confirmation", to="quality_trace.qualityrecallcampaign")),
                ("confirmed_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="confirmed_quality_recall_inventory_batches", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("-confirmed_at", "-id")},
        ),
    ]
