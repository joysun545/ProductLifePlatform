from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("account", "0002_seed_account_reference_data"),
        ("lifecycle", "0002_installationrecord_installationinvitation"),
    ]

    operations = [
        migrations.AddField(
            model_name="installationinvitation",
            name="customer_data",
            field=models.JSONField(
                blank=True,
                default=dict,
                verbose_name="用户确认资料",
            ),
        ),
        migrations.AddField(
            model_name="installationinvitation",
            name="target_organization",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="accepted_installation_invitations",
                to="account.organization",
                verbose_name="接受邀请的用户组织",
            ),
        ),
    ]
