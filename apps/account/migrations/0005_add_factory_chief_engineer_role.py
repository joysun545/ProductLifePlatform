from django.db import migrations


ROLE_CODE = "factory_chief_engineer"


def add_role(apps, schema_editor):
    OrganizationType = apps.get_model("account", "OrganizationType")
    OrganizationRole = apps.get_model("account", "OrganizationRole")
    RoleInvitationRule = apps.get_model("account", "RoleInvitationRule")
    owner_type = OrganizationType.objects.get(code="OWNER")
    role, _ = OrganizationRole.objects.update_or_create(
        organization_type=owner_type,
        code=ROLE_CODE,
        defaults={
            "name": "厂家总工程师",
            "is_owner_role": False,
            "description": "负责质量预警规则、人工研判、质量管控与召回决策。",
            "is_active": True,
        },
    )
    admin = OrganizationRole.objects.get(
        organization_type=owner_type, code="factory_admin",
    )
    RoleInvitationRule.objects.update_or_create(
        inviter_role=admin,
        target_role=role,
        scope="same_organization",
        relation_type="",
        defaults={"is_active": True},
    )


def deactivate_role(apps, schema_editor):
    OrganizationRole = apps.get_model("account", "OrganizationRole")
    RoleInvitationRule = apps.get_model("account", "RoleInvitationRule")
    roles = OrganizationRole.objects.filter(
        organization_type__code="OWNER", code=ROLE_CODE,
    )
    RoleInvitationRule.objects.filter(target_role__in=roles).delete()
    roles.update(is_active=False)


class Migration(migrations.Migration):
    dependencies = [("account", "0004_add_factory_aftersales_role")]
    operations = [migrations.RunPython(add_role, deactivate_role)]
