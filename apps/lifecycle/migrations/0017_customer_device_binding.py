import django.db.models.deletion
from django.db import migrations, models
from django.utils import timezone


def backfill_customer_device_bindings(apps, schema_editor):
    InstallationInvitation = apps.get_model(
        "lifecycle", "InstallationInvitation"
    )
    CustomerDeviceBinding = apps.get_model(
        "lifecycle", "CustomerDeviceBinding"
    )

    invitations = InstallationInvitation.objects.filter(
        status="ACCEPTED",
        target_organization_id__isnull=False,
        invitee_id__isnull=False,
    ).order_by("accepted_at", "id")
    for invitation in invitations.iterator():
        data = invitation.customer_data or {}
        CustomerDeviceBinding.objects.update_or_create(
            device_id=invitation.device_id,
            defaults={
                "customer_organization_id": invitation.target_organization_id,
                "source_organization_id": invitation.source_organization_id,
                "invitation_id": invitation.id,
                "accepted_by_id": invitation.invitee_id,
                "acceptance_mode": "LEGACY",
                "customer_type": str(
                    data.get("customer_type") or "PERSONAL"
                ).upper(),
                "vehicle_no": str(data.get("vehicle_no") or ""),
                "accepted_at": (
                    invitation.accepted_at
                    or invitation.created_at
                    or timezone.now()
                ),
                "status": "ACTIVE",
            },
        )


class Migration(migrations.Migration):
    dependencies = [
        ("account", "0003_add_factory_production_role"),
        ("lifecycle", "0016_factory_return_material_return_facts"),
    ]

    operations = [
        migrations.CreateModel(
            name="CustomerDeviceBinding",
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
                    "acceptance_mode",
                    models.CharField(
                        choices=[
                            ("NEW_CUSTOMER", "新用户首次接收"),
                            ("EXISTING_CUSTOMER", "已有用户添加设备"),
                            ("LEGACY", "历史数据回填"),
                        ],
                        default="NEW_CUSTOMER",
                        max_length=24,
                        verbose_name="接收方式",
                    ),
                ),
                (
                    "customer_type",
                    models.CharField(
                        choices=[
                            ("PERSONAL", "个人用户"),
                            ("FLEET", "企业或车队用户"),
                        ],
                        default="PERSONAL",
                        max_length=16,
                        verbose_name="用户类型快照",
                    ),
                ),
                (
                    "vehicle_no",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=64,
                        verbose_name="车辆编号快照",
                    ),
                ),
                (
                    "accepted_at",
                    models.DateTimeField(
                        db_index=True,
                        default=timezone.now,
                        verbose_name="接收时间",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("ACTIVE", "有效"),
                            ("INACTIVE", "失效"),
                        ],
                        db_index=True,
                        default="ACTIVE",
                        max_length=16,
                        verbose_name="绑定状态",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                (
                    "accepted_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="accepted_customer_devices",
                        to="account.user",
                        verbose_name="接收账号",
                    ),
                ),
                (
                    "customer_organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="customer_device_bindings",
                        to="account.organization",
                        verbose_name="终端用户组织",
                    ),
                ),
                (
                    "device",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="customer_binding",
                        to="lifecycle.device",
                        verbose_name="设备",
                    ),
                ),
                (
                    "invitation",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="customer_device_binding",
                        to="lifecycle.installationinvitation",
                        verbose_name="安装邀请",
                    ),
                ),
                (
                    "source_organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="customer_device_sources",
                        to="account.organization",
                        verbose_name="设备来源组织",
                    ),
                ),
            ],
            options={
                "verbose_name": "终端用户设备绑定",
                "verbose_name_plural": "终端用户设备绑定",
                "ordering": ("-accepted_at", "-id"),
            },
        ),
        migrations.AddIndex(
            model_name="customerdevicebinding",
            index=models.Index(
                fields=["customer_organization", "status"],
                name="lc_cdb_org_status_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="customerdevicebinding",
            index=models.Index(
                fields=["source_organization", "accepted_at"],
                name="lc_cdb_source_idx",
            ),
        ),
        migrations.RunPython(
            backfill_customer_device_bindings,
            migrations.RunPython.noop,
        ),
    ]
