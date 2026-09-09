from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("sales", "0005_orderplanitem_qrcode_print_state"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(model_name="orderplan", name="shipped_at", field=models.DateTimeField(blank=True, null=True, verbose_name="发货时间")),
        migrations.AddField(model_name="orderplan", name="shipped_by", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="shipped_order_plans", to=settings.AUTH_USER_MODEL, verbose_name="出库操作人")),
        migrations.AddField(model_name="orderplan", name="received_at", field=models.DateTimeField(blank=True, null=True, verbose_name="商家入库时间")),
        migrations.AddField(model_name="orderplan", name="received_by", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="received_order_plans", to=settings.AUTH_USER_MODEL, verbose_name="商家入库操作人")),
        migrations.AddField(model_name="orderplan", name="accounted_at", field=models.DateTimeField(blank=True, null=True, verbose_name="入账时间")),
        migrations.AddField(model_name="orderplan", name="accounted_by", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="accounted_order_plans", to=settings.AUTH_USER_MODEL, verbose_name="入账操作人")),
    ]
