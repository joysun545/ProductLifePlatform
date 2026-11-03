from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("lifecycle", "0023_after_sales_service_station_application"),
    ]

    operations = [
        migrations.AddField(
            model_name="device",
            name="physical_device_code",
            field=models.CharField(
                blank=True,
                default=None,
                help_text="设备本体二维码编码；未绑定时为空。",
                max_length=64,
                null=True,
                unique=True,
                verbose_name="设备编码",
            ),
        ),
    ]
