from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("workflow", "0003_alter_workitem_action_mode")]

    operations = [
        migrations.AddField(
            model_name="workitem",
            name="role_delivery",
            field=models.JSONField(default=dict, blank=True, editable=False),
        ),
        migrations.AlterField(
            model_name="usertask",
            name="done_reason",
            field=models.CharField(
                max_length=32, blank=True, default="",
                choices=[
                    ("", ""), ("OPENED", "点开即完成"), ("SELF_ACTION", "本人操作完成"),
                    ("OTHER_ACTION", "他人操作导致完成"), ("ROLE_REASSIGNED", "岗位分管调整"),
                ],
            ),
        ),
    ]
