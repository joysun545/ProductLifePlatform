from django.db import migrations


ROLE_CODE = "factory_production"
ROLE_NAME = "厂家生产经理"


def add_factory_production_role(apps, schema_editor):
    OrganizationType = apps.get_model("account", "OrganizationType")
    OrganizationRole = apps.get_model("account", "OrganizationRole")
    RoleInvitationRule = apps.get_model("account", "RoleInvitationRule")

    owner_type = OrganizationType.objects.get(code="OWNER")
    production_role, _ = OrganizationRole.objects.update_or_create(
        organization_type=owner_type,
        code=ROLE_CODE,
        defaults={
            "name": ROLE_NAME,
            "is_owner_role": False,
            "description": "负责厂家生产组织、生产任务接收与执行协调。",
            "is_active": True,
        },
    )

    factory_admin_role = OrganizationRole.objects.get(
        organization_type=owner_type,
        code="factory_admin",
    )
    RoleInvitationRule.objects.update_or_create(
        inviter_role=factory_admin_role,
        target_role=production_role,
        scope="same_organization",
        relation_type="",
        defaults={"is_active": True},
    )


def deactivate_factory_production_role(apps, schema_editor):
    OrganizationRole = apps.get_model("account", "OrganizationRole")
    RoleInvitationRule = apps.get_model("account", "RoleInvitationRule")

    production_roles = OrganizationRole.objects.filter(
        organization_type__code="OWNER",
        code=ROLE_CODE,
    )
    RoleInvitationRule.objects.filter(
        inviter_role__organization_type__code="OWNER",
        inviter_role__code="factory_admin",
        target_role__in=production_roles,
        scope="same_organization",
        relation_type="",
    ).delete()
    production_roles.update(is_active=False)


class Migration(migrations.Migration):
    dependencies = [
        ("account", "0002_seed_account_reference_data"),
    ]

    operations = [
        migrations.RunPython(
            add_factory_production_role,
            deactivate_factory_production_role,
        ),
    ]
