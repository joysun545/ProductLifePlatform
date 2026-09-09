from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("workflow", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="workitem",
            name="domain",
            field=models.CharField(
                choices=[
                    ("LOGISTICS", "物流"),
                    ("AFTERSALE", "售后"),
                    ("INSTALL", "装车"),
                    ("ACTIVATE", "激活"),
                    ("PROFILE", "资料变更"),
                    ("SALES", "销售订单"),
                    ("SYSTEM", "系统通知"),
                ],
                db_index=True,
                max_length=24,
            ),
        ),
    ]
