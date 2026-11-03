from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("lifecycle", "0027_device_code_registry_web"),
    ]

    operations = [
        migrations.AlterField(
            model_name="aftersalesapplication",
            name="dispatch_route",
            field=models.CharField(
                blank=True,
                choices=[
                    ("ORIGINAL_INSTALLER", "原装车操作人"),
                    ("MERCHANT_ELECTRICIAN", "一级商家电工"),
                    ("FACTORY_AFTERSALES", "厂家售后经理"),
                    ("SERVICE_STATION", "售后服务站"),
                    ("REMOTE", "厂家销售经理远程排障"),
                    ("THIRD_PARTY", "第三方紧急维修"),
                ],
                default="",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="aftersalesapplication",
            name="preliminary_fault_materials",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
