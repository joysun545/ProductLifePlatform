from django.db import migrations


ORGANIZATION_TYPES = (
    ("OWNER", "厂家"),
    ("MERCHANT", "商家"),
    ("SUPPLIER", "供应商"),
    ("SERVICE", "服务商"),
    ("CUSTOMER", "用户"),
)


ROLES = {
    "OWNER": (
        ("factory_admin", "厂家负责人账号", True),
        ("factory_manager", "厂家管理人员", False),
        ("factory_sales", "厂家销售经理", False),
        ("factory_matching", "厂家配套经理", False),
        ("factory_stock", "厂家库管", False),
        ("factory_logistics", "厂家物流", False),
        ("factory_sales_assistant", "厂家销售助理", False),
        ("factory_purchase", "厂家采购", False),
    ),
    "MERCHANT": (
        ("merchant_owner", "商家负责人账号", True),
        ("merchant_senior_manager", "商家高级经理", False),
        ("merchant_manager", "商家经理", False),
        ("merchant_sales", "商家销售经理", False),
        ("merchant_stock", "商家库管", False),
        ("merchant_assistant", "商家销售助理", False),
        ("merchant_electrician", "商家电工", False),
    ),
    "SUPPLIER": (
        ("supplier_owner", "供应商负责人账号", True),
        ("supplier_manager", "供应商经理", False),
        ("supplier_sales", "供应商销售", False),
        ("supplier_technical", "供应商技术人员", False),
    ),
    "SERVICE": (
        ("service_owner", "服务商负责人账号", True),
        ("service_electrician", "服务商电工", False),
    ),
    "CUSTOMER": (
        ("customer_owner", "用户负责人账号", True),
        ("driver", "司机", False),
    ),
}


SAME_ORGANIZATION_RULES = (
    (
        "factory_admin",
        (
            "factory_manager",
            "factory_sales",
            "factory_matching",
            "factory_stock",
            "factory_logistics",
            "factory_sales_assistant",
            "factory_purchase",
        ),
    ),
    (
        "merchant_owner",
        (
            "merchant_senior_manager",
            "merchant_manager",
            "merchant_sales",
            "merchant_stock",
            "merchant_assistant",
            "merchant_electrician",
        ),
    ),
    (
        "supplier_owner",
        (
            "supplier_manager",
            "supplier_sales",
            "supplier_technical",
        ),
    ),
    (
        "service_owner",
        ("service_electrician",),
    ),
)


CROSS_ORGANIZATION_RULES = (
    ("factory_matching", "supplier_owner", "SUPPLY"),
    ("factory_sales", "merchant_owner", "DISTRIBUTION"),
    ("merchant_owner", "merchant_owner", "DISTRIBUTION"),
    ("merchant_senior_manager", "merchant_owner", "DISTRIBUTION"),
    ("merchant_sales", "merchant_owner", "DISTRIBUTION"),
)


def seed_account_reference_data(apps, schema_editor):
    OrganizationType = apps.get_model("account", "OrganizationType")
    OrganizationRole = apps.get_model("account", "OrganizationRole")
    RoleInvitationRule = apps.get_model("account", "RoleInvitationRule")

    organization_types = {}
    roles = {}

    for code, name in ORGANIZATION_TYPES:
        organization_type, _ = OrganizationType.objects.update_or_create(
            code=code,
            defaults={
                "name": name,
                "is_active": True,
            },
        )
        organization_types[code] = organization_type

    for organization_type_code, role_items in ROLES.items():
        organization_type = organization_types[organization_type_code]

        for code, name, is_owner_role in role_items:
            role, _ = OrganizationRole.objects.update_or_create(
                organization_type=organization_type,
                code=code,
                defaults={
                    "name": name,
                    "is_owner_role": is_owner_role,
                    "is_active": True,
                },
            )
            roles[code] = role

    for inviter_role_code, target_role_codes in SAME_ORGANIZATION_RULES:
        for target_role_code in target_role_codes:
            RoleInvitationRule.objects.update_or_create(
                inviter_role=roles[inviter_role_code],
                target_role=roles[target_role_code],
                scope="same_organization",
                relation_type="",
                defaults={"is_active": True},
            )

    for (
        inviter_role_code,
        target_role_code,
        relation_type,
    ) in CROSS_ORGANIZATION_RULES:
        RoleInvitationRule.objects.update_or_create(
            inviter_role=roles[inviter_role_code],
            target_role=roles[target_role_code],
            scope="cross_organization",
            relation_type=relation_type,
            defaults={"is_active": True},
        )


def remove_account_reference_data(apps, schema_editor):
    OrganizationType = apps.get_model("account", "OrganizationType")
    OrganizationRole = apps.get_model("account", "OrganizationRole")
    RoleInvitationRule = apps.get_model("account", "RoleInvitationRule")

    role_codes = [
        role_code
        for role_items in ROLES.values()
        for role_code, _name, _is_owner_role in role_items
    ]

    RoleInvitationRule.objects.filter(
        inviter_role__code__in=role_codes,
        target_role__code__in=role_codes,
    ).delete()
    OrganizationRole.objects.filter(code__in=role_codes).delete()
    OrganizationType.objects.filter(
        code__in=[code for code, _name in ORGANIZATION_TYPES]
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("account", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(
            seed_account_reference_data,
            remove_account_reference_data,
        ),
    ]
