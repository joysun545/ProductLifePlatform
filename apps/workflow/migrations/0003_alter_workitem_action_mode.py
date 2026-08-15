from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("workflow", "0002_alter_workitem_domain"),
    ]

    operations = [
        migrations.AlterField(
            model_name="workitem",
            name="action_mode",
            field=models.CharField(
                choices=[
                    ("EACH_ACK", "每人知晓即完成"),
                    ("ANYONE_DO", "多人操作一人完成"),
                    ("ALL_DO", "全部完成"),
                    ("OWNER_OR_SALES", "负责人或全部销售经理完成"),
                ],
                default="EACH_ACK",
                max_length=16,
            ),
        ),
    ]
