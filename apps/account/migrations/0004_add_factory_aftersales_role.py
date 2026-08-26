from django.db import migrations


def add_role(apps, schema_editor):
    OrganizationType = apps.get_model("account", "OrganizationType")
    OrganizationRole = apps.get_model("account", "OrganizationRole")
    RoleInvitationRule = apps.get_model("account", "RoleInvitationRule")
    owner_type = OrganizationType.objects.get(code="OWNER")
    role, _ = OrganizationRole.objects.update_or_create(
        organization_type=owner_type, code="factory_aftersales",
        defaults={"name": "厂家售后经理", "is_owner_role": False,
                  "description": "负责厂家售后维修任务执行与闭环。", "is_active": True})
    admin = OrganizationRole.objects.get(organization_type=owner_type, code="factory_admin")
    RoleInvitationRule.objects.update_or_create(
        inviter_role=admin, target_role=role, scope="same_organization", relation_type="",
        defaults={"is_active": True})


class Migration(migrations.Migration):
    dependencies = [("account", "0003_add_factory_production_role")]
    operations = [migrations.RunPython(add_role, migrations.RunPython.noop)]
