from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("quality_trace", "0004_qualitygatedecision")]

    operations = [
        migrations.AlterField(
            model_name="qualitycontrolaction",
            name="status",
            field=models.CharField(
                choices=[
                    ("ACTIVE", "生效"),
                    ("COMPLETED", "已完成"),
                    ("EXPIRED", "已到期"),
                    ("REVOKED", "已撤销"),
                ],
                db_index=True,
                default="ACTIVE",
                max_length=16,
            ),
        ),
    ]
