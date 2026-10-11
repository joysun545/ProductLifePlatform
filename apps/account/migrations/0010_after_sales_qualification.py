from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("account", "0009_factory_root_and_platform_invitations"),
    ]

    operations = [
        migrations.AddField(
            model_name="organization",
            name="provides_after_sales",
            field=models.BooleanField(
                default=False,
                help_text="由上级组织在邀请建立商家组织时设置。",
                verbose_name="是否提供售后服务",
            ),
        ),
        migrations.AddField(
            model_name="invitationrecord",
            name="target_organization_provides_after_sales",
            field=models.BooleanField(
                default=False,
                verbose_name="目标商家提供售后服务",
            ),
        ),
    ]
