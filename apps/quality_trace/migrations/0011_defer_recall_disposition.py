from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("quality_trace", "0010_recall_progress_closeout"),
    ]

    operations = [
        migrations.AlterField(
            model_name="qualityrecallcampaign",
            name="risk_level",
            field=models.CharField(
                blank=True,
                choices=[
                    ("LOW", "一般"),
                    ("MEDIUM", "较高"),
                    ("HIGH", "高风险"),
                    ("CRITICAL", "重大风险"),
                ],
                default="",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="qualityrecallcampaign",
            name="remedy_type",
            field=models.CharField(
                blank=True,
                choices=[
                    ("INSPECTION", "检查"),
                    ("REPAIR", "维修"),
                    ("REPLACE", "更换"),
                    ("RETURN", "退回"),
                    ("REFUND", "退款"),
                    ("SCRAP", "报废"),
                    ("OTHER", "其他"),
                ],
                default="",
                max_length=16,
            ),
        ),
    ]
