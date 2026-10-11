from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from account.models import (
    InvitationRecord,
    InvitationScope,
    Organization,
    OrganizationRelation,
    OrganizationRelationType,
    OrganizationRole,
    OrganizationType,
    RoleInvitationRule,
    User,
    UserOrganization,
)
from account.services.auth_service import issue_access_token


class SubordinateManagementApiTests(TestCase):
    url = "/pldp/api/account/subordinates/"

    def setUp(self):
        owner_type = OrganizationType.objects.get(code="OWNER")
        merchant_type = OrganizationType.objects.get(code="MERCHANT")

        self.factory = Organization.objects.create(
            name="下级管理测试厂家",
            code="SUBORDINATE-FACTORY",
            organization_type=owner_type,
            region="重庆市",
        )
        self.merchant = Organization.objects.create(
            name="销售经理直接商家",
            code="SUBORDINATE-MERCHANT",
            organization_type=merchant_type,
            region="四川省",
        )

        self.factory_admin_role = OrganizationRole.objects.get(code="factory_admin")
        self.factory_sales_role = OrganizationRole.objects.get(code="factory_sales")
        self.factory_matching_role = OrganizationRole.objects.get(
            code="factory_matching"
        )
        self.factory_stock_role = OrganizationRole.objects.get(code="factory_stock")
        self.merchant_owner_role = OrganizationRole.objects.get(code="merchant_owner")

        self.admin = self._user_with_identity(
            "subordinate-admin",
            self.factory,
            self.factory_admin_role,
        )
        self.sales = self._user_with_identity(
            "subordinate-sales",
            self.factory,
            self.factory_sales_role,
            inviter=self.admin,
        )
        self.stock = self._user_with_identity(
            "subordinate-stock",
            self.factory,
            self.factory_stock_role,
            inviter=self.admin,
        )
        self.matching = self._user_with_identity(
            "subordinate-matching",
            self.factory,
            self.factory_matching_role,
        )
        self.merchant_owner = self._user_with_identity(
            "subordinate-merchant-owner",
            self.merchant,
            self.merchant_owner_role,
            inviter=self.sales,
        )

        self.admin_sales_invitation = self._accepted_invitation(
            self.admin,
            self.sales,
            self.factory_sales_role,
            self.factory,
        )
        self.admin_stock_invitation = self._accepted_invitation(
            self.admin,
            self.stock,
            self.factory_stock_role,
            self.factory,
        )
        self.sales_merchant_invitation = self._accepted_invitation(
            self.sales,
            self.merchant_owner,
            self.merchant_owner_role,
            self.merchant,
        )
        OrganizationRelation.objects.create(
            source_organization=self.factory,
            target_organization=self.merchant,
            relation_type=OrganizationRelationType.DISTRIBUTION,
            created_by_invitation=self.sales_merchant_invitation,
        )

    def _user_with_identity(self, username, organization, role, inviter=None):
        user = User.objects.create_user(
            username=username,
            wx_openid=f"openid-{username}",
            wx_nickname=username,
        )
        UserOrganization.objects.create(
            user=user,
            organization=organization,
            role=role,
            inviter=inviter,
        )
        return user

    def _accepted_invitation(self, inviter, invitee, target_role, target_organization):
        inviter_identity = inviter.organization_identity
        scope = (
            InvitationScope.SAME_ORGANIZATION
            if target_organization == inviter_identity.organization
            else InvitationScope.CROSS_ORGANIZATION
        )
        relation_type = (
            ""
            if scope == InvitationScope.SAME_ORGANIZATION
            else OrganizationRelationType.DISTRIBUTION
        )
        rule = RoleInvitationRule.objects.get(
            inviter_role=inviter_identity.role,
            target_role=target_role,
            scope=scope,
            relation_type=relation_type,
        )
        return InvitationRecord.objects.create(
            inviter=inviter,
            source_organization=inviter_identity.organization,
            inviter_role=inviter_identity.role,
            rule=rule,
            target_role=target_role,
            target_organization=target_organization,
            invitee=invitee,
            scope=scope,
            relation_type=relation_type,
            status=InvitationRecord.Status.ACCEPTED,
            expires_at=timezone.now() + timedelta(days=1),
            accepted_at=timezone.now(),
        )

    def _authorization(self, user):
        return f"Bearer {issue_access_token(user)}"

    def test_owner_sees_active_staff_slots_and_directly_invited_organizations(self):
        response = self.client.get(
            self.url,
            HTTP_AUTHORIZATION=self._authorization(self.admin),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)
        items = response.json()["data"]["items"]
        self.assertEqual(
            {item["role"]["code"] for item in items},
            {"factory_sales", "factory_stock", "factory_matching"},
        )
        self.assertNotIn(
            self.merchant_owner.pk,
            {item["user"]["id"] for item in items},
        )
        self.assertTrue(all(item["management_mode"] == "view_only" for item in items))

    def test_owner_sees_legacy_staff_slot_without_invitation_event(self):
        legacy_staff = self._user_with_identity(
            "subordinate-legacy-staff",
            self.factory,
            self.factory_stock_role,
            inviter=self.admin,
        )
        aftersales_role = OrganizationRole.objects.get(code="factory_aftersales")
        unlinked_staff = self._user_with_identity(
            "subordinate-unlinked-staff",
            self.factory,
            aftersales_role,
        )

        response = self.client.get(
            self.url,
            HTTP_AUTHORIZATION=self._authorization(self.admin),
        )

        items = response.json()["data"]["items"]
        legacy_item = next(
            item for item in items if item["user"]["id"] == legacy_staff.pk
        )
        self.assertIsNone(legacy_item["invitation_id"])
        self.assertTrue(legacy_item["item_key"].startswith("membership-"))
        self.assertEqual(legacy_item["relation_type_name"], "同组织邀请")
        unlinked_item = next(
            item for item in items if item["user"]["id"] == unlinked_staff.pk
        )
        self.assertEqual(unlinked_item["relation_type_name"], "本组织岗位")

    def test_factory_sales_direct_merchant_exposes_only_channel_edit_mode(self):
        response = self.client.get(
            self.url,
            HTTP_AUTHORIZATION=self._authorization(self.sales),
        )

        self.assertEqual(response.status_code, 200)
        item = response.json()["data"]["items"][0]
        self.assertEqual(item["user"]["id"], self.merchant_owner.pk)
        self.assertEqual(item["organization"]["id"], self.merchant.pk)
        self.assertEqual(item["management_mode"], "merchant_channel")
        self.assertIsNotNone(item["relation_id"])

    def test_role_without_invitation_rule_cannot_open_management_api(self):
        response = self.client.get(
            self.url,
            HTTP_AUTHORIZATION=self._authorization(self.stock),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1)
        self.assertIn("没有邀请或管理", response.json()["msg"])

    def test_non_owner_role_with_database_invitation_rule_can_open_page(self):
        response = self.client.get(
            self.url,
            HTTP_AUTHORIZATION=self._authorization(self.matching),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)
        self.assertEqual(response.json()["data"]["manager"]["role"], "factory_matching")
        self.assertEqual(response.json()["data"]["items"], [])

    def test_inactive_subordinate_identity_is_not_returned(self):
        identity = self.stock.organization_identity
        identity.is_active = False
        identity.save(update_fields=("is_active", "updated_at"))

        response = self.client.get(
            self.url,
            HTTP_AUTHORIZATION=self._authorization(self.admin),
        )

        role_codes = {
            item["role"]["code"]
            for item in response.json()["data"]["items"]
        }
        self.assertEqual(role_codes, {"factory_sales", "factory_matching"})

    def test_authentication_is_required(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], 401)

    def test_service_and_customer_chain_invitation_rules_are_available(self):
        expected = (
            ("merchant_owner", "service_owner", "cross_organization", "SERVICE"),
            ("merchant_senior_manager", "service_owner", "cross_organization", "SERVICE"),
            ("merchant_sales", "service_owner", "cross_organization", "SERVICE"),
            ("service_owner", "customer_owner", "cross_organization", "SERVICE"),
            ("customer_owner", "driver", "same_organization", ""),
        )
        for inviter, target, scope, relation_type in expected:
            self.assertTrue(
                RoleInvitationRule.objects.filter(
                    inviter_role__code=inviter,
                    target_role__code=target,
                    scope=scope,
                    relation_type=relation_type,
                    is_active=True,
                ).exists(),
                f"缺少邀请链路规则：{inviter} -> {target}",
            )

    def test_factory_inviter_repair_is_check_only_unless_apply_is_requested(self):
        aftersales_role = OrganizationRole.objects.get(code="factory_aftersales")
        aftersales = self._user_with_identity(
            "subordinate-aftersales",
            self.factory,
            aftersales_role,
        )

        check_output = StringIO()
        call_command("repair_factory_role_inviter_links", stdout=check_output)
        aftersales_identity = UserOrganization.objects.get(user=aftersales)
        self.assertIsNone(aftersales_identity.inviter_id)
        self.assertIn("CHECK_ONLY", check_output.getvalue())

        call_command("repair_factory_role_inviter_links", apply=True, stdout=StringIO())
        aftersales_identity.refresh_from_db()
        self.assertEqual(aftersales_identity.inviter_id, self.admin.pk)
