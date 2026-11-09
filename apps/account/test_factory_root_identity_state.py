"""Root suspension uses effective identity checks, preserving historical rows.

Only the initial platform administrator is bootstrapped through the ORM. All
ordinary accounts log in through the current API (mocking the WeChat exchange),
and all organizations, identities and links come from current invitation APIs.
Root/identity status edits are valid administrator edits, not corrupt fixtures.
"""

from unittest.mock import patch

from django.conf import settings
from django.test import Client, TestCase

from account.models import (
    InvitationRecord,
    Organization,
    OrganizationRelation,
    PlatformFactoryInvitation,
    SeniorManagerProfile,
    User,
    UserOrganization,
)
from account.services.auth_service import get_active_identity, issue_access_token


class FactoryRootIdentityStateAPITests(TestCase):
    account_url = "/pldp/api/account/"

    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="root-state-platform-admin",
            password="unused",
            wx_openid="root-state-platform-admin-openid",
        )
        self.tokens = {self.admin.pk: issue_access_token(self.admin)}

    def _login(self, label):
        openid = f"root-state-{label}"
        with patch("apps.account.api.views._wx_code_to_openid", return_value=openid):
            response = self.client.post(self.account_url + "login/", {"code": label})
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual(data["code"], 0, data)
        user = User.objects.get(wx_openid=openid)
        self.tokens[user.pk] = data["access_token"]
        return user

    def _api(self, user, path, data=None, *, method="post"):
        kwargs = {"HTTP_AUTHORIZATION": f"Bearer {self.tokens[user.pk]}"}
        if method == "get":
            return self.client.get(self.account_url + path, data or {}, **kwargs)
        return self.client.post(
            self.account_url + path, data or {}, content_type="application/json", **kwargs,
        )

    def _ok(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual(data["code"], 0, data)
        return data["data"]

    def _factory(self, label):
        invitation = self._ok(self._api(
            self.admin,
            "platform/factory-invitations/create/",
            {"organization_name": f"厂家{label}", "region": "重庆"},
        ))
        owner = self._login(f"owner-{label}")
        self._ok(self._api(owner, "invitations/detail/", {"token": invitation["token"]}, method="get"))
        accepted = self._ok(self._api(owner, "invitations/accept/", {"token": invitation["token"]}))
        root = Organization.objects.get(pk=accepted["organization_id"])
        self.assertEqual(root.factory_root_id, root.pk)
        return owner, root

    def _pending(self, inviter, invitee_label, role):
        roles = self._ok(self._api(inviter, "invitation_roles/", method="get"))["items"]
        rule = next(item for item in roles if item["role"] == role)
        invitation = self._ok(self._api(inviter, "invitations/create/", {"rule_id": rule["rule_id"]}))
        invitee = self._login(invitee_label)
        preview = self._ok(self._api(
            invitee, "invitations/detail/", {"token": invitation["token"]}, method="get",
        ))
        self.assertTrue(preview["can_accept"])
        return invitee, invitation, rule

    def _join(self, inviter, invitee_label, role):
        invitee, invitation, _ = self._pending(inviter, invitee_label, role)
        accepted = self._ok(self._api(invitee, "invitations/accept/", {
            "token": invitation["token"],
            "organization_name": f"组织-{invitee_label}",
            "region": "重庆",
        }))
        return invitee, UserOrganization.objects.get(user=invitee), invitation, accepted

    def _merchant_chain(self, label):
        owner, root = self._factory(label)
        sales, _, _, _ = self._join(owner, f"sales-{label}", "factory_sales")
        merchant, identity, invitation, accepted = self._join(sales, f"merchant-{label}", "merchant_owner")
        return owner, root, sales, merchant, identity, invitation, accepted

    def _set_root_active(self, root, active):
        root.is_active = active
        root.full_clean()
        root.save(update_fields=("is_active", "updated_at"))

    def _snapshot(self):
        return {
            model.__name__: list(model.objects.order_by("pk").values())
            for model in (
                User, Organization, UserOrganization, InvitationRecord,
                PlatformFactoryInvitation, OrganizationRelation, SeniorManagerProfile,
            )
        }

    def _profile(self, user):
        return self._ok(self._api(user, "profile/", method="get"))

    def _assert_invitation_error(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["code"], 1, response.content)

    def test_normal_invitation_chains_keep_independent_roots(self):
        owner_a, root_a, _, merchant_a, identity_a, _, _ = self._merchant_chain("a")
        _, root_b, _, merchant_b, identity_b, _, _ = self._merchant_chain("b")
        for root, identity, merchant in (
            (root_a, identity_a, merchant_a), (root_b, identity_b, merchant_b),
        ):
            self.assertEqual(identity.organization.factory_root_id, root.pk)
            self.assertEqual(self._profile(merchant)["factory_root_id"], root.pk)
        self.assertNotEqual(root_a.pk, root_b.pk)
        self._set_root_active(root_a, False)
        self.assertEqual(self._profile(owner_a)["role"], "tourist")
        self.assertEqual(self._profile(merchant_a)["role"], "tourist")
        self.assertEqual(self._profile(merchant_b)["role"], "merchant_owner")
        other_child, child_identity, _, _ = self._join(merchant_b, "b-child", "merchant_owner")
        self.assertEqual(child_identity.organization.factory_root_id, root_b.pk)
        self.assertEqual(self._profile(other_child)["factory_root_id"], root_b.pk)
        for relation in OrganizationRelation.objects.select_related("source_organization", "target_organization"):
            self.assertEqual(
                relation.source_organization.get_factory_root().pk,
                relation.target_organization.get_factory_root().pk,
            )

    def test_pending_invitation_rechecks_root_after_preview_without_writes(self):
        _, root, _, merchant, _, _, _ = self._merchant_chain("pending")
        invitee, invitation, rule = self._pending(merchant, "pending-child", "merchant_owner")
        self._set_root_active(root, False)
        before = self._snapshot()

        # The original account token still authenticates, while its business
        # identity cannot list roles, create invitations, preview or accept.
        self.assertEqual(self._profile(merchant)["role"], "tourist")
        self._assert_invitation_error(self._api(merchant, "invitation_roles/", method="get"))
        self._assert_invitation_error(self._api(merchant, "invitations/create/", {"rule_id": rule["rule_id"]}))
        self._assert_invitation_error(self._api(invitee, "invitations/detail/", {"token": invitation["token"]}, method="get"))
        rejected = self._api(invitee, "invitations/accept/", {
            "token": invitation["token"], "organization_name": "不得创建", "region": "重庆",
        })
        self._assert_invitation_error(rejected)
        self.assertEqual(rejected.json()["msg"], "邀请人的组织身份已经失效")
        self.assertEqual(self._snapshot(), before)

    def test_root_reenable_reuses_active_rows_and_preserves_independent_suspension(self):
        _, root, sales, merchant, identity, _, _ = self._merchant_chain("resume")
        child, child_identity, _, _ = self._join(merchant, "resume-child", "merchant_owner")
        identity.is_active = False
        identity.full_clean()
        identity.save(update_fields=("is_active",))
        descendants_before = self._snapshot()
        self._set_root_active(root, False)
        self.assertIsNone(get_active_identity(sales))
        self.assertIsNone(get_active_identity(child))
        self._set_root_active(root, True)
        self.assertEqual(get_active_identity(sales).organization_id, root.pk)
        self.assertEqual(get_active_identity(child).pk, child_identity.pk)
        self.assertIsNone(get_active_identity(merchant))
        self.assertEqual(self._profile(merchant)["role"], "tourist")
        self.assertEqual(self._profile(child)["role"], "merchant_owner")
        after = self._snapshot()
        # Root edits may update only that root's state/timestamp; children,
        # role slots, links and invitation history are preserved byte-for-byte.
        for rows in (descendants_before["Organization"], after["Organization"]):
            rows[:] = [row for row in rows if row["id"] != root.pk]
        self.assertEqual(after, descendants_before)

    def test_accepted_replay_never_creates_or_restores_identity(self):
        _, root, _, merchant, identity, invitation, _ = self._merchant_chain("replay")
        self._set_root_active(root, False)
        before = self._snapshot()
        replay = self._ok(self._api(merchant, "invitations/accept/", {
            "token": invitation["token"], "organization_name": "不可替换", "region": "其他地区",
        }))
        self.assertEqual(replay["role"], "tourist")
        self.assertIsNone(replay["organization_id"])
        self.assertEqual(self._snapshot(), before)

        self._set_root_active(root, True)
        identity.is_active = False
        identity.full_clean()
        identity.save(update_fields=("is_active",))
        before = self._snapshot()
        replay = self._ok(self._api(merchant, "invitations/accept/", {"token": invitation["token"]}))
        self.assertEqual(replay["role"], "tourist")
        self.assertEqual(self._snapshot(), before)

    def test_existing_browser_grant_rechecks_root_on_next_request(self):
        owner, root = self._factory("web")
        browser = Client(enforce_csrf_checks=True)
        portal = "/pldp/web/portal/"

        def browser_post(action):
            browser.get(portal + "api/session/")
            return browser.post(
                portal + f"api/{action}/", {}, content_type="application/json",
                HTTP_X_CSRFTOKEN=browser.cookies[settings.CSRF_COOKIE_NAME].value,
            )

        # Only randomness is fixed. Challenge creation, bearer approval and
        # browser consumption all use the real externally callable APIs.
        token = "a" * 64
        with patch("web_portal.views.secrets.token_hex", return_value=token):
            self.assertEqual(browser_post("start").status_code, 200)
        approved = Client().post(
            portal + "api/approve/", {"token": token}, content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.tokens[owner.pk]}",
        )
        self.assertEqual(approved.status_code, 200, approved.content)
        self.assertEqual(browser_post("status").json()["data"]["status"], "ready")
        self.assertEqual(browser.get("/pldp/web/product/").status_code, 200)
        self._set_root_active(root, False)
        self.assertFalse(browser.get(portal + "api/session/").json()["data"]["logged_in"])
        self.assertEqual(browser.get("/pldp/web/product/").status_code, 403)
        self.assertEqual(self._profile(owner)["role"], "tourist")
        self._set_root_active(root, True)
        self.assertTrue(browser.get(portal + "api/session/").json()["data"]["logged_in"])
        self.assertEqual(browser.get("/pldp/web/product/").status_code, 200)

