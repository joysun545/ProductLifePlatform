from django.db import migrations


SAME_ORGANIZATION_RULES = (
    ("customer_owner", "driver"),
)

CROSS_ORGANIZATION_RULES = (
    ("merchant_owner", "service_owner", "SERVICE"),
    ("merchant_senior_manager", "service_owner", "SERVICE"),
    ("merchant_sales", "service_owner", "SERVICE"),
    ("service_owner", "customer_owner", "SERVICE"),
)


def add_chain_rules(apps, schema_editor):
    alias = schema_editor.connection.alias
    OrganizationRole = apps.get_model("account", "OrganizationRole")
    RoleInvitationRule = apps.get_model("account", "RoleInvitationRule")

    roles = OrganizationRole.objects.using(alias)
    for inviter_code, target_code in SAME_ORGANIZATION_RULES:
        RoleInvitationRule.objects.using(alias).update_or_create(
            inviter_role=roles.get(
                organization_type__code="CUSTOMER", code=inviter_code
            ),
            target_role=roles.get(
                organization_type__code="CUSTOMER", code=target_code
            ),
            scope="same_organization",
            relation_type="",
            defaults={"is_active": True},
        )

    for inviter_code, target_code, relation_type in CROSS_ORGANIZATION_RULES:
        inviter_type = "MERCHANT" if inviter_code.startswith("merchant_") else "SERVICE"
        target_type = "SERVICE" if target_code == "service_owner" else "CUSTOMER"
        RoleInvitationRule.objects.using(alias).update_or_create(
            inviter_role=roles.get(
                organization_type__code=inviter_type, code=inviter_code
            ),
            target_role=roles.get(
                organization_type__code=target_type, code=target_code
            ),
            scope="cross_organization",
            relation_type=relation_type,
            defaults={"is_active": True},
        )


def remove_chain_rules(apps, schema_editor):
    alias = schema_editor.connection.alias
    OrganizationRole = apps.get_model("account", "OrganizationRole")
    RoleInvitationRule = apps.get_model("account", "RoleInvitationRule")

    roles = OrganizationRole.objects.using(alias)
    rules = RoleInvitationRule.objects.using(alias)
    for inviter_code, target_code in SAME_ORGANIZATION_RULES:
        rules.filter(
            inviter_role=roles.get(
                organization_type__code="CUSTOMER", code=inviter_code
            ),
            target_role=roles.get(
                organization_type__code="CUSTOMER", code=target_code
            ),
            scope="same_organization",
            relation_type="",
        ).delete()

    for inviter_code, target_code, relation_type in CROSS_ORGANIZATION_RULES:
        inviter_type = "MERCHANT" if inviter_code.startswith("merchant_") else "SERVICE"
        target_type = "SERVICE" if target_code == "service_owner" else "CUSTOMER"
        rules.filter(
            inviter_role=roles.get(
                organization_type__code=inviter_type, code=inviter_code
            ),
            target_role=roles.get(
                organization_type__code=target_type, code=target_code
            ),
            scope="cross_organization",
            relation_type=relation_type,
        ).delete()


class Migration(migrations.Migration):
    dependencies = [("account", "0006_factory_warehouse_roles")]

    operations = [
        migrations.RunPython(add_chain_rules, remove_chain_rules),
    ]
