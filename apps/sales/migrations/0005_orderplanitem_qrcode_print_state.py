from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0004_alter_order_plan_status_choices"),
    ]

    operations = [
        migrations.AddField(
            model_name="orderplanitem",
            name="qrcode_generated_at",
            field=models.DateTimeField(
                blank=True,
                db_index=True,
                null=True,
                verbose_name="二维码生成完成时间",
            ),
        ),
        migrations.AddField(
            model_name="orderplanitem",
            name="qrcode_printed_at",
            field=models.DateTimeField(
                blank=True,
                db_index=True,
                null=True,
                verbose_name="二维码打印确认时间",
            ),
        ),
    ]
