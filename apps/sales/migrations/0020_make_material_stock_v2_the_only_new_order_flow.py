from django.db import migrations, models


class Migration(migrations.Migration):
    """Make every newly created order plan use the stock-aware V2 workflow.

    Existing development rows are deliberately left untouched.  They are no
    longer exposed by the V2-only API, while retaining the migration history
    keeps upgrade and rollback mechanics valid.
    """

    dependencies = [
        ("sales", "0019_repair_v2_logistics_inbox_group"),
    ]

    operations = [
        migrations.AlterField(
            model_name="orderplan",
            name="flow_version",
            field=models.CharField(
                choices=[("MATERIAL_STOCK_V2", "物料库存订单流程")],
                db_index=True,
                default="MATERIAL_STOCK_V2",
                max_length=32,
                verbose_name="订单流程版本",
            ),
        ),
    ]
