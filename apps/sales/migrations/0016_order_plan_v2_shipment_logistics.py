"""Per-shipment logistics for the material-stock V2 order flow.

Legacy logistics documents remain attached to the whole legacy order plan.
V2 may send one order through multiple production/shipping batches, so the
carrier and tracking number belong to ``OrderPlanShipment`` instead.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0015_order_plan_v2_shipment_devices"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="orderplanshipment",
            name="logistics_company",
            field=models.CharField(
                blank=True,
                default="",
                max_length=128,
                verbose_name="物流公司",
            ),
        ),
        migrations.AddField(
            model_name="orderplanshipment",
            name="tracking_no",
            field=models.CharField(
                blank=True,
                default="",
                max_length=128,
                verbose_name="物流单号",
            ),
        ),
        migrations.AddField(
            model_name="orderplanshipment",
            name="logistics_confirmed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="confirmed_order_plan_shipment_logistics_v2",
                to=settings.AUTH_USER_MODEL,
                verbose_name="物流信息确认人",
            ),
        ),
        migrations.AddField(
            model_name="orderplanshipment",
            name="logistics_confirmed_at",
            field=models.DateTimeField(
                blank=True,
                db_index=True,
                null=True,
                verbose_name="物流信息确认时间",
            ),
        ),
    ]
