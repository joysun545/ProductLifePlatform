from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0003_orderplanitembomconfirmation"),
    ]

    operations = [
        migrations.AlterField(
            model_name="orderplan",
            name="status",
            field=models.CharField(
                choices=[
                    ("MATCHING_PENDING", "待物料配套"),
                    ("MATCHING_COMPLETED", "配套完成"),
                    ("PRODUCTION_PENDING", "待生产完成"),
                    ("PRODUCTION_COMPLETED", "生产完成"),
                    ("SHIPPED", "已发货"),
                    ("RECEIVED", "商家已入库"),
                    ("CANCELLED", "已取消"),
                ],
                db_index=True,
                default="MATCHING_PENDING",
                max_length=32,
                verbose_name="计划状态",
            ),
        ),
        migrations.AlterField(
            model_name="orderplanitem",
            name="status",
            field=models.CharField(
                choices=[
                    ("MATCHING_PENDING", "待物料配套"),
                    ("MATCHING_COMPLETED", "配套完成"),
                    ("PRODUCTION_PENDING", "待生产完成"),
                    ("PRODUCTION_COMPLETED", "生产完成"),
                    ("SHIPPED", "已发货"),
                    ("RECEIVED", "商家已入库"),
                    ("CANCELLED", "已取消"),
                ],
                db_index=True,
                default="MATCHING_PENDING",
                max_length=32,
                verbose_name="明细状态",
            ),
        ),
    ]
