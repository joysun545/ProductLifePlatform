from django.db import migrations, models
import django.db.models.deletion


def seed_senior_manager_roles_and_invitation_rules(apps, schema_editor):
    alias = schema_editor.connection.alias
    OrganizationType = apps.get_model("account", "OrganizationType")
    OrganizationRole = apps.get_model("account", "OrganizationRole")
    RoleInvitationRule = apps.get_model("account", "RoleInvitationRule")

    owner_type = OrganizationType.objects.using(alias).get(code="OWNER")
    merchant_type = OrganizationType.objects.using(alias).get(code="MERCHANT")
    factory_senior, _ = OrganizationRole.objects.using(alias).update_or_create(
        organization_type=owner_type,
        code="factory_senior_manager",
        defaults={
            "name": "厂家高级经理",
            "is_owner_role": False,
            "is_active": True,
        },
    )
    merchant_senior = OrganizationRole.objects.using(alias).get(
        organization_type=merchant_type,
        code="merchant_senior_manager",
    )

    # Factory owners must be able to invite a senior manager, then choose the
    # internal roles that manager will invite.  The manager receives no role
    # invitation capability until the owner accepts/configures that invitation.
    factory_owner = OrganizationRole.objects.using(alias).get(
        organization_type=owner_type,
        code="factory_admin",
    )
    RoleInvitationRule.objects.using(alias).update_or_create(
        inviter_role=factory_owner,
        target_role=factory_senior,
        scope="same_organization",
        relation_type="",
        defaults={"is_active": True},
    )

    for manager_role in (factory_senior, merchant_senior):
        target_roles = OrganizationRole.objects.using(alias).filter(
            organization_type_id=manager_role.organization_type_id,
            is_active=True,
            is_owner_role=False,
        ).exclude(code__in=("factory_senior_manager", "merchant_senior_manager"))
        for target_role in target_roles:
            RoleInvitationRule.objects.using(alias).update_or_create(
                inviter_role=manager_role,
                target_role=target_role,
                scope="same_organization",
                relation_type="",
                defaults={"is_active": True},
            )


class Migration(migrations.Migration):
    dependencies = [
        ("account", "0011_merchant_after_sales_qualification_management"),
    ]

    operations = [
        migrations.AddField(
            model_name="invitationrecord",
            name="senior_manager_job_title",
            field=models.CharField(
                blank=True,
                default="",
                max_length=64,
                verbose_name="高级经理分管职位名称",
            ),
        ),
        migrations.AddField(
            model_name="invitationrecord",
            name="senior_manager_managed_role_codes",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="邀请创建时的授权快照；接受后写入高级经理分管岗位。",
                verbose_name="高级经理分管岗位编码快照",
            ),
        ),
        migrations.CreateModel(
            name="SeniorManagerProfile",
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
                    "job_title",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="仅供链路信息展示，不作为额外权限来源。",
                        max_length=64,
                        verbose_name="分管职位名称",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                (
                    "identity",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="senior_manager_profile",
                        to="account.userorganization",
                        verbose_name="高级经理组织身份",
                    ),
                ),
                (
                    "managed_roles",
                    models.ManyToManyField(
                        blank=True,
                        related_name="senior_manager_profiles",
                        to="account.organizationrole",
                        verbose_name="分管系统岗位",
                    ),
                ),
            ],
            options={
                "verbose_name": "高级经理分管岗位",
                "verbose_name_plural": "高级经理分管岗位",
            },
        ),
        migrations.RunPython(
            seed_senior_manager_roles_and_invitation_rules,
            migrations.RunPython.noop,
        ),
    ]
