from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("account", "0010_after_sales_qualification"),
    ]

    operations = [
        migrations.AlterField(
            model_name="organization",
            name="provides_after_sales",
            field=models.BooleanField(
                default=False,
                help_text="由有权管理该商家组织的上级在管理下级用户时设置。",
                verbose_name="是否提供售后服务",
            ),
        ),
        migrations.AlterField(
            model_name="invitationrecord",
            name="target_organization_provides_after_sales",
            field=models.BooleanField(
                default=False,
                editable=False,
                help_text="仅为兼容历史邀请保留；新邀请流程不再使用此字段。",
                verbose_name="旧版邀请售后资格标记",
            ),
        ),
    ]
