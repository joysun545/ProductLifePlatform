"""Separate the material warehouse role while preserving the finished-goods role."""
from django.db import migrations


MATERIAL_ROLE_CODE = "factory_material_stock"


def add_warehouse_roles(apps, schema_editor):
    alias = schema_editor.connection.alias
    OrganizationType = apps.get_model("account", "OrganizationType")
    OrganizationRole = apps.get_model("account", "OrganizationRole")
    RoleInvitationRule = apps.get_model("account", "RoleInvitationRule")

    owner_type = OrganizationType.objects.using(alias).get(code="OWNER")
    roles = OrganizationRole.objects.using(alias)
    # Keep the existing PK, code, enabled state, memberships and task references.
    finished_role = roles.get(organization_type=owner_type, code="factory_stock")
    finished_role.name = "厂家成品库管"
    finished_role.save(using=alias, update_fields=("name", "updated_at"))

    material_role, _ = roles.update_or_create(
        organization_type=owner_type,
        code=MATERIAL_ROLE_CODE,
        defaults={
            "name": "厂家物料库管",
            "is_owner_role": False,
            "is_active": True,
            "description": "厂家物料仓库岗位，与成品库管独立。",
        },
    )
    admin_role = roles.get(organization_type=owner_type, code="factory_admin")
    RoleInvitationRule.objects.using(alias).update_or_create(
        inviter_role=admin_role,
        target_role=material_role,
        scope="same_organization",
        relation_type="",
        defaults={"is_active": True},
    )


def reverse_warehouse_roles(apps, schema_editor):
    alias = schema_editor.connection.alias
    OrganizationRole = apps.get_model("account", "OrganizationRole")
    RoleInvitationRule = apps.get_model("account", "RoleInvitationRule")
    roles = OrganizationRole.objects.using(alias)
    roles.filter(
        organization_type__code="OWNER", code="factory_stock",
        name="厂家成品库管",
    ).update(name="厂家库管")
    material_roles = roles.filter(
        organization_type__code="OWNER", code=MATERIAL_ROLE_CODE,
    )
    RoleInvitationRule.objects.using(alias).filter(
        inviter_role__organization_type__code="OWNER",
        inviter_role__code="factory_admin",
        target_role__in=material_roles,
        scope="same_organization", relation_type="",
    ).update(is_active=False)
    # Preserve IDs and accepted invitations if the new role already has users.
    material_roles.update(is_active=False)


class Migration(migrations.Migration):
    dependencies = [("account", "0005_add_factory_chief_engineer_role")]
    operations = [
        migrations.RunPython(add_warehouse_roles, reverse_warehouse_roles),
    ]
