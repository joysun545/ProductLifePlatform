from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def normalize_existing_handovers(apps, schema_editor):
    Handover = apps.get_model("quality_trace", "QualityRecallCustomerHandover")
    # Historical handovers were merchant-only.  Preserve that fact explicitly
    # instead of making them look like an unmade customer choice.
    Handover.objects.filter(
        status__in=("ACKNOWLEDGED", "PICKUP_SCHEDULED", "MERCHANT_RECEIVED"),
        delivery_method="",
    ).update(delivery_method="MERCHANT_HANDOVER")
    Handover.objects.filter(
        status="PICKUP_SCHEDULED",
        method_locked_at__isnull=True,
        pickup_scheduled_at__isnull=False,
    ).update(method_locked_at=models.F("pickup_scheduled_at"))
    Handover.objects.filter(
        status="MERCHANT_RECEIVED",
        method_locked_at__isnull=True,
        merchant_received_at__isnull=False,
    ).update(method_locked_at=models.F("merchant_received_at"))
    Handover.objects.filter(status="PENDING_CUSTOMER_ACK").update(
        status="PENDING_CUSTOMER_CHOICE",
    )


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("quality_trace", "0012_recall_campaign_consolidation"),
    ]

    operations = [
        migrations.AddField(
            model_name="qualityrecallcustomerhandover",
            name="delivery_method",
            field=models.CharField(
                blank=True,
                choices=[
                    ("MERCHANT_HANDOVER", "交给来源商家"),
                    ("CUSTOMER_DIRECT_SHIP", "用户直寄厂家"),
                    ("FACTORY_PICKUP", "厂家上门取件"),
                ],
                db_index=True,
                default="",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="qualityrecallcustomerhandover",
            name="merchant_transfer_expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="qualityrecallcustomerhandover",
            name="merchant_transfer_token",
            field=models.UUIDField(blank=True, editable=False, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="qualityrecallcustomerhandover",
            name="method_locked_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="qualityrecallcustomerhandover",
            name="method_payload",
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="qualityrecallcustomerhandover",
            name="method_selected_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="qualityrecallcustomerhandover",
            name="method_selected_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="selected_quality_recall_delivery_methods",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="qualityrecallcustomerhandover",
            name="method_selected_role",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AlterField(
            model_name="qualityrecallcustomerhandover",
            name="status",
            field=models.CharField(
                choices=[
                    ("PENDING_CUSTOMER_CHOICE", "待选择交付方式"),
                    ("METHOD_SELECTED", "已选择交付方式"),
                    ("DIRECT_SHIPPED", "用户已直寄"),
                    ("PICKUP_REQUESTED", "已申请厂家取件"),
                    ("PICKUP_ACCEPTED", "厂家已受理取件"),
                    ("PENDING_CUSTOMER_ACK", "待用户确认"),
                    ("ACKNOWLEDGED", "用户已确认"),
                    ("PICKUP_SCHEDULED", "已预约取回"),
                    ("MERCHANT_RECEIVED", "商家已验收接管"),
                    ("CANCELLED", "已取消"),
                ],
                db_index=True,
                default="PENDING_CUSTOMER_CHOICE",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="qualityrecallhandoverevidence",
            name="kind",
            field=models.CharField(
                choices=[
                    ("CUSTOMER_CONFIRMATION", "用户确认凭证"),
                    ("PICKUP", "取回凭证"),
                    ("MERCHANT_RECEIPT", "商家验收凭证"),
                    ("DIRECT_SHIPMENT", "用户直寄凭证"),
                    ("FACTORY_PICKUP", "厂家取件凭证"),
                ],
                max_length=32,
            ),
        ),
        migrations.RunPython(normalize_existing_handovers, migrations.RunPython.noop),
    ]
