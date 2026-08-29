from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("quality_trace", "0013_activated_recall_delivery_methods"),
    ]

    operations = [
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
                    ("PICKUP_SHIPPED", "用户已交付取件物流"),
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
    ]
