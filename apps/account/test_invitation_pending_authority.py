from datetime import timedelta
from uuid import uuid4

from django.test import TestCase
from django.utils import timezone

from account.models import (
    InvitationRecord,
    Organization,
    OrganizationRelation,
    OrganizationRole,
    OrganizationType,
    PlatformFactoryInvitation,
    RoleInvitationRule,
    User,
    UserOrganization,
)
from account.services.auth_service import issue_access_token
from account.services.invite_service import create_platform_factory_invitation


class PendingInvitationAPITests(TestCase):
    base_url = "/pldp/api/account/invitations/"

    def setUp(self):
        self.owner_type = OrganizationType.objects.get(code="OWNER")
        self.factory = Organization.objects.create(
            name="甲厂家", code="INVITE-AUTH-FACTORY", organization_type=self.owner_type,
        )
        self.inviter = User.objects.create_user(
            username="invite-sales", wx_openid="openid-invite-sales",
        )
        self.invitee = User.objects.create_user(
            username="invite-recipient", wx_openid="openid-invite-recipient",
        )
        self.identity = UserOrganization.objects.create(
            user=self.inviter,
            organization=self.factory,
            role=OrganizationRole.objects.get(code="factory_sales"),
        )
        self.rule = RoleInvitationRule.objects.get(
            inviter_role=self.identity.role, target_role__code="merchant_owner", is_active=True,
        )

    def _auth(self, user):
        return {"HTTP_AUTHORIZATION": f"Bearer {issue_access_token(user)}"}

    def _issue(self):
        response = self.client.post(
            self.base_url + "create/", {"rule_id": self.rule.pk}, **self._auth(self.inviter),
        )
        self.assertEqual(response.json()["code"], 0)
        return response.json()["data"]["token"]

    def _preview(self, token):
        return self.client.get(self.base_url + "detail/", {"token": token}, **self._auth(self.invitee))

    def _accept(self, token):
        return self.client.post(
            self.base_url + "accept/",
            {"token": token, "organization_name": "下级商家", "region": "重庆"},
            content_type="application/json",
            **self._auth(self.invitee),
        )

    def _assert_invitation_error(self, response, message):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"code": 1, "msg": message})

    def test_source_organization_type_disabled_after_preview(self):
        token = self._issue()
        self.assertTrue(self._preview(token).json()["data"]["can_accept"])
        self.owner_type.is_active = False
        self.owner_type.save(update_fields=("is_active",))

        create = self.client.post(
            self.base_url + "create/", {"rule_id": self.rule.pk}, **self._auth(self.inviter),
        )
        self.assertNotEqual(create.json()["code"], 0)
        self._assert_invitation_error(self._preview(token), "邀请人的组织身份已经失效")
        self._assert_invitation_error(self._accept(token), "邀请人的组织身份已经失效")

        self.assertFalse(UserOrganization.objects.filter(user=self.invitee).exists())
        self.assertFalse(Organization.objects.filter(name="下级商家").exists())
        self.assertFalse(OrganizationRelation.objects.filter(source_organization=self.factory).exists())

    def test_pending_invitation_is_bound_to_its_original_role_and_organization(self):
        token = self._issue()
        original_role = self.identity.role
        self.identity.role = OrganizationRole.objects.get(code="factory_admin")
        self.identity.save(update_fields=("role",))
        self._assert_invitation_error(self._accept(token), "邀请人的组织身份已经失效")

        self.identity.role = original_role
        self.identity.organization = Organization.objects.create(
            name="乙厂家", code="INVITE-AUTH-OTHER-FACTORY", organization_type=self.owner_type,
        )
        self.identity.save(update_fields=("role", "organization"))
        self._assert_invitation_error(self._preview(token), "邀请人的组织身份已经失效")
        self.assertFalse(UserOrganization.objects.filter(user=self.invitee).exists())

    def test_malformed_tokens_return_business_errors_for_preview_and_accept(self):
        self.client.raise_request_exception = False
        message = "邀请链接格式无效，请使用完整链接"
        self._assert_invitation_error(self._preview("not-a-uuid"), message)
        for token in ("not-a-uuid", 1, False, [], {"token": str(uuid4())}):
            with self.subTest(token=token):
                self._assert_invitation_error(self._accept(token), message)
        self._assert_invitation_error(self._preview(""), "邀请token缺失")
        self._assert_invitation_error(self._accept(None), "邀请token缺失")

    def test_unknown_token_is_distinct_from_expired_invitations(self):
        unknown = str(uuid4())
        self._assert_invitation_error(self._preview(unknown), "邀请链接不存在或已失效")
        self._assert_invitation_error(self._accept(unknown), "邀请链接不存在或已失效")

        expired_at = timezone.now() - timedelta(seconds=1)
        staff_token = self._issue()
        InvitationRecord.objects.filter(token=staff_token).update(expires_at=expired_at)
        admin = User.objects.create_superuser(
            username="invite-platform-admin", password="unused", wx_openid="openid-invite-platform-admin",
        )
        platform_token = create_platform_factory_invitation(
            inviter=admin, organization_name="新厂家",
        )["token"]
        PlatformFactoryInvitation.objects.filter(token=platform_token).update(expires_at=expired_at)

        for token in (staff_token, platform_token):
            self._assert_invitation_error(self._accept(token), "该邀请已过期")
            self._assert_invitation_error(self._preview(token), "该邀请已过期")

    def test_accepted_invitation_replays_for_same_invitee_after_revocation(self):
        token = self._issue()
        first = self._accept(token).json()
        self.assertEqual(first["code"], 0)
        self.owner_type.is_active = False
        self.owner_type.save(update_fields=("is_active",))

        replay = self._accept(token).json()
        self.assertEqual(replay["code"], 0)
        self.assertEqual(replay["data"]["organization_id"], first["data"]["organization_id"])
        self.assertEqual(OrganizationRelation.objects.filter(source_organization=self.factory).count(), 1)
