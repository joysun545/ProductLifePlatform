from django.test import TestCase

from account.models import OrganizationRole, RoleInvitationRule


class FactoryProductionRoleReferenceDataTests(TestCase):
    def test_factory_production_role_exists_as_independent_role(self):
        role = OrganizationRole.objects.get(
            organization_type__code="OWNER",
            code="factory_production",
        )

        self.assertEqual(role.name, "厂家生产经理")
        self.assertFalse(role.is_owner_role)
        self.assertTrue(role.is_active)

    def test_factory_admin_can_invite_factory_production_in_same_org(self):
        rule = RoleInvitationRule.objects.get(
            inviter_role__organization_type__code="OWNER",
            inviter_role__code="factory_admin",
            target_role__organization_type__code="OWNER",
            target_role__code="factory_production",
            scope="same_organization",
            relation_type="",
        )

        self.assertTrue(rule.is_active)
