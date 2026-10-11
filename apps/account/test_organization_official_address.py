import json

from django.test import TestCase

from account.models import Organization, OrganizationRole, OrganizationType, User, UserOrganization
from account.services.auth_service import issue_access_token


class OrganizationOfficialAddressProfileTests(TestCase):
    profile_url = "/pldp/api/account/profile/"

    def setUp(self):
        factory_type, _ = OrganizationType.objects.get_or_create(
            code="OWNER", defaults={"name": "厂家"}
        )
        merchant_type, _ = OrganizationType.objects.get_or_create(
            code="MERCHANT", defaults={"name": "商家"}
        )
        factory_role, _ = OrganizationRole.objects.get_or_create(
            organization_type=factory_type,
            code="factory_admin",
            defaults={"name": "厂家负责人账号", "is_owner_role": True},
        )
        merchant_owner_role, _ = OrganizationRole.objects.get_or_create(
            organization_type=merchant_type,
            code="merchant_owner",
            defaults={"name": "商家负责人账号", "is_owner_role": True},
        )
        self.factory = Organization.objects.create(
            code="ADDRESS-TEST-FACTORY",
            name="地址测试厂家",
            organization_type=factory_type,
        )
        self.merchant = Organization.objects.create(
            code="ADDRESS-TEST-MERCHANT",
            name="地址测试商家",
            organization_type=merchant_type,
            factory_root=self.factory,
            address="重庆市原组织官方地址",
        )
        self.owner = User.objects.create_user(
            username="address_test_merchant_owner",
            wx_openid="address-test-merchant-owner",
            wx_nickname="商家负责人",
        )
        UserOrganization.objects.create(
            user=self.owner,
            organization=self.merchant,
            role=merchant_owner_role,
        )
        self.owner_token = issue_access_token(self.owner)

        self.factory_user = User.objects.create_user(
            username="address_test_factory_owner",
            wx_openid="address-test-factory-owner",
            wx_nickname="厂家负责人",
        )
        UserOrganization.objects.create(
            user=self.factory_user,
            organization=self.factory,
            role=factory_role,
        )
        self.factory_token = issue_access_token(self.factory_user)

    @staticmethod
    def _auth(token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def test_merchant_owner_reads_and_updates_organization_address_from_profile(self):
        response = self.client.get(
            self.profile_url,
            **self._auth(self.owner_token),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["data"]["can_edit_organization_address"])
        self.assertEqual(
            response.json()["data"]["organization_address"],
            "重庆市原组织官方地址",
        )

        response = self.client.post(
            self.profile_url,
            data=json.dumps(
                {
                    "nickname": "商家负责人",
                    "phone": "",
                    "organization_address": "重庆市新组织官方地址",
                }
            ),
            content_type="application/json",
            **self._auth(self.owner_token),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)
        self.merchant.refresh_from_db()
        self.assertEqual(self.merchant.address, "重庆市新组织官方地址")
        self.assertEqual(
            response.json()["data"]["organization_address"],
            "重庆市新组织官方地址",
        )

    def test_non_merchant_owner_cannot_change_an_organization_address(self):
        response = self.client.post(
            self.profile_url,
            data=json.dumps(
                {
                    "nickname": "厂家负责人",
                    "phone": "",
                    "organization_address": "越权修改地址",
                }
            ),
            content_type="application/json",
            **self._auth(self.factory_token),
        )

        self.assertEqual(response.json()["code"], 1)
        self.factory.refresh_from_db()
        self.assertEqual(self.factory.address, "")

    def test_organization_address_length_is_validated(self):
        response = self.client.post(
            self.profile_url,
            data=json.dumps(
                {
                    "nickname": "商家负责人",
                    "phone": "",
                    "organization_address": "地" * 256,
                }
            ),
            content_type="application/json",
            **self._auth(self.owner_token),
        )

        self.assertEqual(response.json()["code"], 1)
        self.merchant.refresh_from_db()
        self.assertEqual(self.merchant.address, "重庆市原组织官方地址")
