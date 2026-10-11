import json
from unittest.mock import patch

from django.test import Client, TestCase

from account.models import (
    Organization,
    OrganizationRelation,
    OrganizationRole,
    OrganizationType,
    User,
    UserOrganization,
)
from account.services.auth_service import build_user_payload, issue_access_token


class UIAppearanceTests(TestCase):
    def setUp(self):
        self.owner_type = OrganizationType.objects.get(code="OWNER")
        self.merchant_type = OrganizationType.objects.get(code="MERCHANT")
        self.factory = Organization.objects.create(
            name="主题厂家",
            code="THEME-FACTORY",
            organization_type=self.owner_type,
            ui_color_scheme="ocean",
            ui_style_scheme="order",
        )
        self.merchant = Organization.objects.create(
            name="主题商家",
            code="THEME-MERCHANT",
            organization_type=self.merchant_type,
        )
        OrganizationRelation.objects.create(
            source_organization=self.factory,
            target_organization=self.merchant,
            relation_type="DISTRIBUTION",
        )
        self.owner_role = OrganizationRole.objects.get(
            organization_type=self.owner_type, code="factory_admin"
        )
        self.sales_role = OrganizationRole.objects.get(
            organization_type=self.owner_type, code="factory_sales"
        )
        self.merchant_role = OrganizationRole.objects.get(
            organization_type=self.merchant_type, code="merchant_owner"
        )
        self.owner_user = self._user("theme-owner", self.factory, self.owner_role)
        self.sales_user = self._user("theme-sales", self.factory, self.sales_role)
        self.merchant_user = self._user(
            "theme-merchant", self.merchant, self.merchant_role
        )

    def _user(self, username, organization, role):
        user = User.objects.create_user(
            username=username, wx_openid=f"openid-{username}"
        )
        UserOrganization.objects.create(
            user=user, organization=organization, role=role
        )
        return user

    def _headers(self, user):
        return {"HTTP_AUTHORIZATION": f"Bearer {issue_access_token(user)}"}

    def test_downstream_login_payload_inherits_operator_appearance(self):
        appearance = build_user_payload(self.merchant_user)["appearance"]
        self.assertEqual(appearance["option_code"], "ocean-order")
        self.assertEqual(appearance["owner_organization_id"], self.factory.pk)

    def test_owner_can_update_one_global_selection(self):
        response = Client().post(
            "/pldp/api/account/appearance/",
            data=json.dumps(
                {"color_scheme": "amber", "style_scheme": "layer"}
            ),
            content_type="application/json",
            **self._headers(self.owner_user),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)
        self.factory.refresh_from_db()
        self.assertEqual(self.factory.ui_color_scheme, "amber")
        self.assertEqual(self.factory.ui_style_scheme, "layer")
        self.assertEqual(
            build_user_payload(self.merchant_user)["appearance"]["option_code"],
            "amber-layer",
        )

    def test_options_expose_all_nine_named_combinations(self):
        response = Client().get(
            "/pldp/api/account/appearance/", **self._headers(self.owner_user)
        )
        data = response.json()["data"]
        self.assertEqual(len(data["options"]["colors"]), 3)
        self.assertEqual(len(data["options"]["styles"]), 3)
        self.assertTrue(
            all(item["description"] for item in data["options"]["colors"])
        )
        self.assertTrue(
            all(item["description"] for item in data["options"]["styles"])
        )

    def test_non_owner_role_cannot_change_global_selection(self):
        response = Client().post(
            "/pldp/api/account/appearance/",
            {"color_scheme": "jade", "style_scheme": "flow"},
            **self._headers(self.sales_user),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1)
        self.factory.refresh_from_db()
        self.assertEqual(self.factory.ui_color_scheme, "ocean")

    @patch("apps.account.api.views._wx_code_to_openid", return_value="openid-theme-owner")
    def test_login_response_contains_resolved_appearance(self, _mock_openid):
        response = Client().post("/pldp/api/account/login/", {"code": "wx-code"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["appearance"]["option_code"], "ocean-order")
