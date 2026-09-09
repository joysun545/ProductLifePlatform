import django.db.models.deletion
import django.utils.timezone
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("account", "0001_initial"),
        ("lifecycle", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="InstallationRecord",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "installed_at",
                    models.DateTimeField(
                        db_index=True,
                        default=django.utils.timezone.now,
                        verbose_name="安装时间",
                    ),
                ),
                (
                    "location",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=128,
                        verbose_name="安装地点",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[("INSTALLED", "已安装"), ("CANCELLED", "已取消")],
                        db_index=True,
                        default="INSTALLED",
                        max_length=16,
                        verbose_name="记录状态",
                    ),
                ),
                (
                    "remark",
                    models.TextField(blank=True, default="", verbose_name="备注"),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                (
                    "device",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="installation",
                        to="lifecycle.device",
                        verbose_name="设备",
                    ),
                ),
                (
                    "lifecycle_event",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="installation_record",
                        to="lifecycle.lifecycleevent",
                        verbose_name="安装生命周期事件",
                    ),
                ),
                (
                    "operator",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="installation_records",
                        to="account.user",
                        verbose_name="安装操作账号",
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="installation_records",
                        to="account.organization",
                        verbose_name="安装组织",
                    ),
                ),
            ],
            options={
                "verbose_name": "设备安装记录",
                "verbose_name_plural": "设备安装记录",
                "ordering": ("-installed_at", "-id"),
            },
        ),
        migrations.CreateModel(
            name="InstallationInvitation",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "token",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        unique=True,
                        verbose_name="邀请令牌",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING", "待使用"),
                            ("ACCEPTED", "已接受"),
                            ("CANCELLED", "已取消"),
                            ("EXPIRED", "已过期"),
                        ],
                        db_index=True,
                        default="PENDING",
                        max_length=16,
                        verbose_name="邀请状态",
                    ),
                ),
                ("expires_at", models.DateTimeField(db_index=True, verbose_name="失效时间")),
                ("accepted_at", models.DateTimeField(blank=True, null=True, verbose_name="接受时间")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                (
                    "device",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="installation_invitations",
                        to="lifecycle.device",
                        verbose_name="设备",
                    ),
                ),
                (
                    "installation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="customer_invitations",
                        to="lifecycle.installationrecord",
                        verbose_name="安装记录",
                    ),
                ),
                (
                    "invitee",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="received_installation_invitations",
                        to="account.user",
                        verbose_name="接受邀请账号",
                    ),
                ),
                (
                    "inviter",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="sent_installation_invitations",
                        to="account.user",
                        verbose_name="邀请账号",
                    ),
                ),
                (
                    "source_organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="installation_invitations",
                        to="account.organization",
                        verbose_name="安装组织",
                    ),
                ),
                (
                    "target_role",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="installation_invitations",
                        to="account.organizationrole",
                        verbose_name="指定用户岗位",
                    ),
                ),
            ],
            options={
                "verbose_name": "安装用户临时邀请",
                "verbose_name_plural": "安装用户临时邀请",
                "ordering": ("-created_at", "-id"),
            },
        ),
        migrations.AddConstraint(
            model_name="installationinvitation",
            constraint=models.CheckConstraint(
                condition=(
                    ~models.Q(("status", "ACCEPTED"))
                    | (
                        models.Q(("invitee__isnull", False))
                        & models.Q(("accepted_at__isnull", False))
                    )
                ),
                name="lifecycle_install_invite_accepted_data",
            ),
        ),
        migrations.AddConstraint(
            model_name="installationinvitation",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(("invitee__isnull", True))
                    | ~models.Q(("invitee", models.F("inviter")))
                ),
                name="lifecycle_install_invite_not_self",
            ),
        ),
    ]

