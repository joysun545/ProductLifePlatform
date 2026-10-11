from datetime import timedelta

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
from channel.models import (
    MerchantProductCategoryScope,
    MerchantProductModelScope,
)
from product.models import Brand, ProductCategory, ProductModel


class MerchantCatalogScopeAPITests(TestCase):
    catalog_url = "/pldp/api/account/subordinates/merchant-catalog/"
    qualification_url = "/pldp/api/account/subordinates/after-sales-qualification/"
    subordinates_url = "/pldp/api/account/subordinates/"

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
            code="CATALOG-FACTORY-A",
            name="经营范围测试厂家A",
            organization_type=self.factory_type,
        )
        self.other_factory = Organization.objects.create(
            code="CATALOG-FACTORY-B",
            name="经营范围测试厂家B",
            organization_type=self.factory_type,
        )
        self.sales = self._user_with_identity(
            "catalog-sales-a", self.factory, self.factory_sales
        )
        self.root_owner = self._new_organization(
            "CATALOG-MERCHANT-ROOT", "直属商家", self.factory
        )
        self.root_owner_user = self._user_with_identity(
            "catalog-owner-root", self.root_owner, self.merchant_owner, self.sales
        )
        self.root_relation = self._create_distribution(
            self.sales,
            self.sales.organization_identity,
            self.root_owner_user,
            self.root_owner,
            self.merchant_owner,
        )

        self.child = self._new_organization(
            "CATALOG-MERCHANT-CHILD", "下级商家", self.factory
        )
        self.child_user = self._user_with_identity(
            "catalog-owner-child",
            self.child,
            self.merchant_owner,
            self.root_owner_user,
        )
        self.child_relation = self._create_distribution(
            self.root_owner_user,
            self.root_owner_user.organization_identity,
            self.child_user,
            self.child,
            self.merchant_owner,
        )

        self.foreign_merchant = self._new_organization(
            "CATALOG-MERCHANT-FOREIGN", "另一厂家商家", self.other_factory
        )
        self.foreign_user = self._user_with_identity(
            "catalog-owner-foreign", self.foreign_merchant, self.merchant_owner
        )
        self.foreign_relation = self._create_distribution(
            self.other_factory,
            None,
            self.foreign_user,
            self.foreign_merchant,
            self.merchant_owner,
            inviter_role=self.factory_sales,
        )

        self.brand_a = Brand.objects.create(name="范围测试品牌A", code="CAT-A")
        self.brand_b = Brand.objects.create(name="范围测试品牌B", code="CAT-B")
        self.category_a = ProductCategory.objects.create(
            name="驻车发电机测试品类", code="CAT-GEN"
        )
        self.category_b = ProductCategory.objects.create(
            name="驻车空调测试品类", code="CAT-AIR"
        )
        self.model_a1 = ProductModel.objects.create(
            code="CAT-A1", name="发电机型号A1", category=self.category_a
        )
        self.model_a2 = ProductModel.objects.create(
            code="CAT-A2", name="发电机型号A2", category=self.category_a
        )
        self.model_b1 = ProductModel.objects.create(
            code="CAT-B1", name="空调型号B1", category=self.category_b
        )

    def _new_organization(self, code, name, root):
        return Organization.objects.create(
            code=code,
            name=name,
            organization_type=self.merchant_type,
            factory_root=root,
        )

    def _user_with_identity(self, username, organization, role, inviter=None):
        user = User.objects.create_user(
            username=username,
            wx_openid=f"openid-{username}",
        )
        UserOrganization.objects.create(
            user=user,
            organization=organization,
            role=role,
            inviter=inviter,
        )
        return user

    def _create_distribution(
        self,
        inviter,
        inviter_identity,
        invitee,
        target_organization,
        target_role,
        inviter_role=None,
    ):
        source_organization = (
            inviter_identity.organization if inviter_identity else inviter
        )
        inviter_role = inviter_role or inviter_identity.role
        rule, _ = RoleInvitationRule.objects.get_or_create(
            inviter_role=inviter_role,
            target_role=target_role,
            scope=InvitationScope.CROSS_ORGANIZATION,
            relation_type=OrganizationRelationType.DISTRIBUTION,
            defaults={"is_active": True},
        )
        invitation = InvitationRecord.objects.create(
            inviter=inviter if isinstance(inviter, User) else self.sales,
            source_organization=source_organization,
            inviter_role=inviter_role,
            rule=rule,
            target_role=target_role,
            target_organization=target_organization,
            invitee=invitee,
            scope=InvitationScope.CROSS_ORGANIZATION,
            relation_type=OrganizationRelationType.DISTRIBUTION,
            status=InvitationRecord.Status.ACCEPTED,
            expires_at=timezone.now() + timedelta(days=1),
            accepted_at=timezone.now(),
        )
        return OrganizationRelation.objects.create(
            source_organization=source_organization,
            target_organization=target_organization,
            relation_type=OrganizationRelationType.DISTRIBUTION,
            created_by_invitation=invitation,
        )

    def _auth(self, user):
        return f"Bearer {issue_access_token(user)}"

    def _scope_payload(self, brand, category, models):
        return {
            "brand_id": brand.pk,
            "categories": [
                {
                    "category_id": category.pk,
                    "product_model_ids": [model.pk for model in models],
                }
            ],
        }

    def _save(self, user, organization, scopes):
        return self.client.post(
            self.catalog_url,
            data={"organization_id": organization.pk, "catalog": scopes},
            content_type="application/json",
            HTTP_AUTHORIZATION=self._auth(user),
        )

    def test_factory_sales_saves_multiple_brands_categories_and_model_subset(self):
        response = self._save(
            self.sales,
            self.root_owner,
            [
                self._scope_payload(
                    self.brand_a, self.category_a, [self.model_a1]
                ),
                self._scope_payload(
                    self.brand_b, self.category_b, [self.model_b1]
                ),
            ],
        )

        self.assertEqual(response.json()["code"], 0, response.json())
        self.assertEqual(
            MerchantProductCategoryScope.objects.filter(
                relation=self.root_relation
            ).count(),
            2,
        )
        self.assertEqual(
            MerchantProductModelScope.objects.filter(
                category_scope__relation=self.root_relation
            ).count(),
            2,
        )

        options = self.client.get(
            self.catalog_url,
            {"organization_id": self.root_owner.pk},
            HTTP_AUTHORIZATION=self._auth(self.sales),
        )
        self.assertEqual(options.json()["code"], 0)
        self.assertEqual(
            {row["name"] for row in options.json()["data"]["selected"]},
            {self.brand_a.name, self.brand_b.name},
        )

        subordinates = self.client.get(
            self.subordinates_url,
            HTTP_AUTHORIZATION=self._auth(self.sales),
        )
        self.assertEqual(subordinates.json()["code"], 0, subordinates.json())
        merchant = next(
            row
            for row in subordinates.json()["data"]["items"]
            if row["organization"]["id"] == self.root_owner.pk
        )
        self.assertEqual(
            merchant["catalog_brand_ids"],
            [self.brand_a.pk, self.brand_b.pk],
        )
        self.assertEqual(
            merchant["catalog_brand_names"],
            [self.brand_a.name, self.brand_b.name],
        )
        self.assertEqual(
            merchant["catalog_brand_label"],
            f"{self.brand_a.name}、{self.brand_b.name}",
        )

    def test_multiple_categories_of_one_brand_allow_independent_model_subsets(self):
        response = self._save(
            self.sales,
            self.root_owner,
            [
                {
                    "brand_id": self.brand_a.pk,
                    "categories": [
                        {
                            "category_id": self.category_a.pk,
                            "product_model_ids": [self.model_a1.pk],
                        },
                        {
                            "category_id": self.category_b.pk,
                            "product_model_ids": [self.model_b1.pk],
                        },
                    ],
                }
            ],
        )

        self.assertEqual(response.json()["code"], 0, response.json())
        brand_scope = next(
            row
            for row in response.json()["data"]["selected"]
            if row["id"] == self.brand_a.pk
        )
        models_by_category = {
            category["id"]: {model["id"] for model in category["models"]}
            for category in brand_scope["categories"]
        }
        self.assertEqual(
            models_by_category[self.category_a.pk],
            {self.model_a1.pk},
        )
        self.assertEqual(
            models_by_category[self.category_b.pk],
            {self.model_b1.pk},
        )

    def test_merchant_owner_can_manage_only_its_direct_child_scope(self):
        response = self.client.get(
            self.subordinates_url,
            HTTP_AUTHORIZATION=self._auth(self.root_owner_user),
        )
        self.assertEqual(response.json()["code"], 0, response.json())
        item = next(
            row
            for row in response.json()["data"]["items"]
            if row["organization"]["id"] == self.child.pk
        )
        self.assertEqual(item["management_mode"], "merchant_catalog")
        self.assertFalse(item["has_operating_categories"])

    def test_child_can_select_only_parent_brand_category_and_model_subset(self):
        self._save(
            self.sales,
            self.root_owner,
            [
                self._scope_payload(
                    self.brand_a,
                    self.category_a,
                    [self.model_a1, self.model_a2],
                )
            ],
        )

        allowed = self._save(
            self.root_owner_user,
            self.child,
            [
                self._scope_payload(
                    self.brand_a, self.category_a, [self.model_a2]
                )
            ],
        )
        self.assertEqual(allowed.json()["code"], 0, allowed.json())

        disallowed_model = self._save(
            self.root_owner_user,
            self.child,
            [
                self._scope_payload(
                    self.brand_a,
                    self.category_a,
                    [self.model_a1, self.model_b1],
                )
            ],
        )
        self.assertEqual(disallowed_model.json()["code"], 1)

        disallowed_category = self._save(
            self.root_owner_user,
            self.child,
            [
                self._scope_payload(
                    self.brand_b, self.category_b, [self.model_b1]
                )
            ],
        )
        self.assertEqual(disallowed_category.json()["code"], 1)

    def test_after_sales_category_coverage_does_not_narrow_to_selected_models(self):
        self._save(
            self.sales,
            self.root_owner,
            [
                self._scope_payload(
                    self.brand_a, self.category_a, [self.model_a1]
                )
            ],
        )
        self._save(
            self.root_owner_user,
            self.child,
            [
                self._scope_payload(
                    self.brand_a, self.category_a, [self.model_a1]
                )
            ],
        )

        response = self.client.post(
            self.qualification_url,
            data={
                "organization_id": self.child.pk,
                "provides_after_sales": True,
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=self._auth(self.root_owner_user),
        )
        self.assertEqual(response.json()["code"], 0, response.json())

        from account.services.merchant_catalog_service import (
            get_effective_merchant_catalog,
        )

        effective = get_effective_merchant_catalog(self.child)
        self.assertEqual(effective["category_ids"], {self.category_a.pk})
        # The category remains covered even though the child has one model grant.
        model_ids = effective["scopes"][(self.brand_a.pk, self.category_a.pk)][
            "model_ids"
        ]
        self.assertEqual(model_ids, {self.model_a1.pk})

    def test_qualification_requires_a_configured_operating_category(self):
        response = self.client.post(
            self.qualification_url,
            data={
                "organization_id": self.root_owner.pk,
                "provides_after_sales": True,
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=self._auth(self.sales),
        )
        self.assertEqual(response.json()["code"], 1)
        self.assertFalse(self.root_owner.provides_after_sales)

    def test_category_permission_is_intersected_after_parent_scope_changes(self):
        self._save(
            self.sales,
            self.root_owner,
            [
                self._scope_payload(
                    self.brand_a,
                    self.category_a,
                    [self.model_a1, self.model_a2],
                )
            ],
        )
        self._save(
            self.root_owner_user,
            self.child,
            [
                self._scope_payload(
                    self.brand_a, self.category_a, [self.model_a2]
                )
            ],
        )

        # The parent later narrows the product-model grant. Category-level
        # after-sales eligibility remains governed by the category path.
        self._save(
            self.sales,
            self.root_owner,
            [
                self._scope_payload(
                    self.brand_a, self.category_a, [self.model_a1]
                )
            ],
        )

        from account.services.merchant_catalog_service import (
            get_effective_merchant_catalog,
        )

        effective = get_effective_merchant_catalog(self.child)
        self.assertEqual(effective["category_ids"], {self.category_a.pk})
        self.assertEqual(
            effective["scopes"][(self.brand_a.pk, self.category_a.pk)]["model_ids"],
            set(),
        )

    def test_removing_last_ancestor_category_clears_child_after_sales_qualification(self):
        self._save(
            self.sales,
            self.root_owner,
            [self._scope_payload(self.brand_a, self.category_a, [self.model_a1])],
        )
        self._save(
            self.root_owner_user,
            self.child,
            [self._scope_payload(self.brand_a, self.category_a, [self.model_a1])],
        )
        qualified = self.client.post(
            self.qualification_url,
            data={
                "organization_id": self.child.pk,
                "provides_after_sales": True,
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=self._auth(self.root_owner_user),
        )
        self.assertEqual(qualified.json()["code"], 0, qualified.json())

        removed = self._save(self.sales, self.root_owner, [])
        self.assertEqual(removed.json()["code"], 0, removed.json())
        self.child.refresh_from_db()
        self.assertFalse(self.child.provides_after_sales)

    def test_merchant_parent_scope_removal_clears_child_after_sales_qualification(self):
        self._save(
            self.sales,
            self.root_owner,
            [self._scope_payload(self.brand_a, self.category_a, [self.model_a1])],
        )
        self._save(
            self.root_owner_user,
            self.child,
            [self._scope_payload(self.brand_a, self.category_a, [self.model_a1])],
        )
        qualified = self.client.post(
            self.qualification_url,
            data={
                "organization_id": self.child.pk,
                "provides_after_sales": True,
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=self._auth(self.root_owner_user),
        )
        self.assertEqual(qualified.json()["code"], 0, qualified.json())

        removed = self._save(self.root_owner_user, self.child, [])
        self.assertEqual(removed.json()["code"], 0, removed.json())
        self.child.refresh_from_db()
        self.assertFalse(self.child.provides_after_sales)

    def test_cross_factory_organization_cannot_be_edited(self):
        response = self._save(self.sales, self.foreign_merchant, [])
        self.assertEqual(response.json()["code"], 1)
