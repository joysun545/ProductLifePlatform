from django.test import TestCase

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
from account.services.invite_service import (
    accept_staff_invitation,
    create_staff_invitation,
    get_invitation_detail,
)
from channel.models import MerchantProductCategoryScope, MerchantProductModelScope
from product.models import Brand, ProductCategory, ProductModel


class MerchantAfterSalesQualificationManagementTests(TestCase):
    create_invitation_url = "/pldp/api/account/invitations/create/"
    subordinates_url = "/pldp/api/account/subordinates/"
    qualification_url = "/pldp/api/account/subordinates/after-sales-qualification/"

    def setUp(self):
        self.factory_type, _ = OrganizationType.objects.get_or_create(
            code="OWNER", defaults={"name": "厂家"}
        )
        self.merchant_type, _ = OrganizationType.objects.get_or_create(
            code="MERCHANT", defaults={"name": "商家"}
        )
        self.factory_sales, _ = OrganizationRole.objects.get_or_create(
            organization_type=self.factory_type,
            code="factory_sales",
            defaults={"name": "厂家销售经理"},
        )
        self.merchant_owner, _ = OrganizationRole.objects.get_or_create(
            organization_type=self.merchant_type,
            code="merchant_owner",
            defaults={"name": "商家负责人账号", "is_owner_role": True},
        )
        self.factory = Organization.objects.create(
            code="FACTORY-QUAL-TEST",
            name="测试厂家",
            organization_type=self.factory_type,
        )
        self.inviter = User.objects.create_user(
            username="after_sales_inviter",
            wx_openid="openid-after-sales-inviter",
        )
        self.invitee = User.objects.create_user(
            username="after_sales_invitee",
            wx_openid="openid-after-sales-invitee",
        )
        UserOrganization.objects.create(
            user=self.inviter,
            organization=self.factory,
            role=self.factory_sales,
        )
        self.rule, _ = RoleInvitationRule.objects.get_or_create(
            inviter_role=self.factory_sales,
            target_role=self.merchant_owner,
            scope=InvitationScope.CROSS_ORGANIZATION,
            relation_type=OrganizationRelationType.DISTRIBUTION,
            defaults={"is_active": True},
        )

    def _authorization(self, user):
        return f"Bearer {issue_access_token(user)}"

    def _create_and_accept_merchant(self, name="售后资格商家"):
        invitation = create_staff_invitation(
            inviter=self.inviter,
            rule_id=self.rule.pk,
        )
        profile = accept_staff_invitation(
            invitee=self.invitee,
            token=invitation["token"],
            organization_name=name,
            region="重庆市",
        )
        return invitation, Organization.objects.get(pk=profile["organization_id"])

    def test_invitation_flag_is_ignored_and_new_merchant_starts_unqualified(self):
        response = self.client.post(
            self.create_invitation_url,
            data={
                "rule_id": self.rule.pk,
                "target_organization_provides_after_sales": True,
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=self._authorization(self.inviter),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)
        token = response.json()["data"]["token"]

        detail = get_invitation_detail(token=token, viewer=self.invitee)
        self.assertNotIn("target_organization_provides_after_sales", detail)
        invitation = InvitationRecord.objects.get(token=token)
        self.assertFalse(invitation.target_organization_provides_after_sales)

        profile = accept_staff_invitation(
            invitee=self.invitee,
            token=token,
            organization_name="售后资格商家",
            region="重庆市",
        )
        organization = Organization.objects.get(pk=profile["organization_id"])
        self.assertEqual(organization.organization_type.code, "MERCHANT")
        self.assertFalse(organization.provides_after_sales)

    def test_historical_invitation_flag_does_not_set_new_organization_qualification(self):
        invitation = create_staff_invitation(
            inviter=self.inviter,
            rule_id=self.rule.pk,
        )
        record = InvitationRecord.objects.get(token=invitation["token"])
        record.target_organization_provides_after_sales = True
        record.save(update_fields=("target_organization_provides_after_sales",))
        profile = accept_staff_invitation(
            invitee=self.invitee,
            token=invitation["token"],
            organization_name="历史标记兼容商家",
            region="重庆市",
        )
        organization = Organization.objects.get(pk=profile["organization_id"])
        self.assertFalse(organization.provides_after_sales)

    def test_manager_can_set_qualification_after_acceptance(self):
        _invitation, organization = self._create_and_accept_merchant()
        self.assertFalse(organization.provides_after_sales)
        relation = OrganizationRelation.objects.get(
            source_organization=self.factory,
            target_organization=organization,
            relation_type=OrganizationRelationType.DISTRIBUTION,
            is_active=True,
        )
        brand = Brand.objects.create(
            name="售后资格测试品牌",
            code="AFTER-SALES-QUAL-BRAND",
        )
        category = ProductCategory.objects.create(
            name="售后资格测试品类",
            code="AFTER-SALES-QUAL-CATEGORY",
        )
        product_model = ProductModel.objects.create(
            name="售后资格测试型号",
            code="AFTER-SALES-QUAL-MODEL",
            category=category,
        )
        category_scope = MerchantProductCategoryScope.objects.create(
            relation=relation,
            brand=brand,
            category=category,
            assigned_by=self.inviter,
        )
        MerchantProductModelScope.objects.create(
            category_scope=category_scope,
            product_model=product_model,
        )

        listing = self.client.get(
            self.subordinates_url,
            HTTP_AUTHORIZATION=self._authorization(self.inviter),
        )
        self.assertEqual(listing.json()["code"], 0)
        listed_merchant = next(
            item for item in listing.json()["data"]["items"]
            if item["organization"]["id"] == organization.pk
        )
        self.assertFalse(listed_merchant["organization"]["provides_after_sales"])

        response = self.client.post(
            self.qualification_url,
            data={
                "organization_id": organization.pk,
                "provides_after_sales": True,
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=self._authorization(self.inviter),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)
        self.assertTrue(response.json()["data"]["provides_after_sales"])
        organization.refresh_from_db()
        self.assertTrue(organization.provides_after_sales)
        listing = self.client.get(
            self.subordinates_url,
            HTTP_AUTHORIZATION=self._authorization(self.inviter),
        )
        listed_merchant = next(
            item for item in listing.json()["data"]["items"]
            if item["organization"]["id"] == organization.pk
        )
        self.assertTrue(listed_merchant["organization"]["provides_after_sales"])

    def test_unrelated_factory_chain_merchant_cannot_be_updated(self):
        other_factory = Organization.objects.create(
            code="OTHER-FACTORY-QUAL-TEST",
            name="其他厂家",
            organization_type=self.factory_type,
        )
        other_merchant = Organization.objects.create(
            code="OTHER-MERCHANT-QUAL-TEST",
            name="其他厂家商家",
            organization_type=self.merchant_type,
            factory_root=other_factory,
        )

        response = self.client.post(
            self.qualification_url,
            data={
                "organization_id": other_merchant.pk,
                "provides_after_sales": True,
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=self._authorization(self.inviter),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1)
        other_merchant.refresh_from_db()
        self.assertFalse(other_merchant.provides_after_sales)

    def test_non_boolean_qualification_is_rejected(self):
        _invitation, organization = self._create_and_accept_merchant()

        response = self.client.post(
            self.qualification_url,
            data={
                "organization_id": organization.pk,
                "provides_after_sales": "true",
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=self._authorization(self.inviter),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1)
        self.assertIn("布尔值", response.json()["msg"])
