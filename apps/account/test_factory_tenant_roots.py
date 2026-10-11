from datetime import date

from django.core.exceptions import ValidationError
from django.test import TestCase

from account.models import (
    Organization,
    OrganizationRelation,
    OrganizationRole,
    OrganizationType,
    User,
    UserOrganization,
)
from account.services.auth_service import build_user_payload, get_active_identity
from account.services.invite_service import (
    InvitationError,
    accept_staff_invitation,
    create_platform_factory_invitation,
    create_staff_invitation,
    get_available_invitation_roles,
    get_invitation_detail,
)
from lifecycle.models import Device, LifecycleEvent, OrganizationDevice
from manufacturing.models import ProductionBatch
from common.domain.device_ownership.queries import descendant_organization_ids
from product.models import BOM, ProductCategory, ProductModel


class FactoryTenantRootTests(TestCase):
    def setUp(self):
        self.owner_type = OrganizationType.objects.get(code="OWNER")
        self.merchant_type = OrganizationType.objects.get(code="MERCHANT")
        self.admin = User.objects.create_superuser(
            username="platform-admin", password="unused", wx_openid="platform-admin-openid"
        )
        self.factory_admin_role = OrganizationRole.objects.get(
            organization_type=self.owner_type, code="factory_admin"
        )
        self.factory_sales_role = OrganizationRole.objects.get(
            organization_type=self.owner_type, code="factory_sales"
        )
        self.merchant_owner_role = OrganizationRole.objects.get(
            organization_type=self.merchant_type, code="merchant_owner"
        )

    def _user(self, username):
        return User.objects.create_user(
            username=username, password="unused", wx_openid=f"openid-{username}"
        )

    def _accept_factory(self, organization_name, username):
        result = create_platform_factory_invitation(
            inviter=self.admin,
            organization_name=organization_name,
            region="重庆",
        )
        invitee = self._user(username)
        detail = get_invitation_detail(token=result["token"], viewer=invitee)
        self.assertEqual(detail["target_role"]["code"], "factory_admin")
        self.assertEqual(detail["target_organization"]["name"], organization_name)
        payload = accept_staff_invitation(invitee=invitee, token=result["token"])
        identity = UserOrganization.objects.get(user=invitee)
        self.assertEqual(identity.organization.name, organization_name)
        self.assertEqual(identity.organization.factory_root_id, identity.organization_id)
        self.assertEqual(payload["factory_root_name"], organization_name)
        return identity

    def test_platform_admin_can_create_multiple_independent_factory_roots(self):
        first = self._accept_factory("甲厂家", "factory-owner-a")
        second = self._accept_factory("乙厂家", "factory-owner-b")

        self.assertNotEqual(first.organization_id, second.organization_id)
        self.assertNotEqual(
            first.organization.factory_root_id,
            second.organization.factory_root_id,
        )

    def test_non_admin_cannot_issue_factory_owner_invitation(self):
        with self.assertRaises(InvitationError):
            create_platform_factory_invitation(
                inviter=self._user("ordinary-user"),
                organization_name="未经授权厂家",
            )

    def test_ordinary_cross_organization_invite_inherits_factory_root(self):
        owner_identity = self._accept_factory("根厂家", "factory-owner")
        sales_user = self._user("factory-sales")
        UserOrganization.objects.create(
            user=sales_user,
            organization=owner_identity.organization,
            role=self.factory_sales_role,
            inviter=owner_identity.user,
        )
        rule = get_available_invitation_roles(sales_user)
        merchant_rule = next(row for row in rule if row["role"] == "merchant_owner")
        invite = create_staff_invitation(inviter=sales_user, rule_id=merchant_rule["rule_id"])
        merchant_user = self._user("merchant-owner")
        accept_staff_invitation(
            invitee=merchant_user,
            token=invite["token"],
            organization_name="根厂家直属商家",
            region="重庆",
        )
        merchant_identity = UserOrganization.objects.get(user=merchant_user)
        self.assertEqual(
            merchant_identity.organization.factory_root_id,
            owner_identity.organization_id,
        )
        self.assertEqual(
            build_user_payload(merchant_user)["factory_root_name"], "根厂家"
        )

    def test_relation_save_rejects_cross_factory_links(self):
        first = self._accept_factory("厂家甲", "owner-a")
        second = self._accept_factory("厂家乙", "owner-b")
        merchant = Organization.objects.create(
            name="甲商家",
            code="TENANT-MERCHANT-A",
            organization_type=self.merchant_type,
            factory_root=first.organization,
        )
        relation = OrganizationRelation(
            source_organization=second.organization,
            target_organization=merchant,
            relation_type="DISTRIBUTION",
        )
        with self.assertRaises(ValidationError):
            relation.save()
        merchant.factory_root = second.organization
        with self.assertRaises(ValidationError):
            merchant.save()

    def test_descendant_lookup_ignores_a_corrupt_cross_factory_relation(self):
        first = self._accept_factory("链路隔离厂家甲", "chain-owner-a")
        second = self._accept_factory("链路隔离厂家乙", "chain-owner-b")
        local_merchant = Organization.objects.create(
            name="甲厂家商家",
            code="TENANT-CHAIN-MERCHANT-A",
            organization_type=self.merchant_type,
            factory_root=first.organization,
        )
        foreign_merchant = Organization.objects.create(
            name="乙厂家商家",
            code="TENANT-CHAIN-MERCHANT-B",
            organization_type=self.merchant_type,
            factory_root=second.organization,
        )
        OrganizationRelation.objects.create(
            source_organization=first.organization,
            target_organization=local_merchant,
            relation_type="DISTRIBUTION",
        )
        # Simulate a malformed legacy row that bypassed model validation.
        OrganizationRelation.objects.bulk_create([
            OrganizationRelation(
                source_organization=first.organization,
                target_organization=foreign_merchant,
                relation_type="DISTRIBUTION",
            )
        ])

        self.assertEqual(
            descendant_organization_ids(first.organization),
            {first.organization_id, local_merchant.pk},
        )

    def test_identity_without_a_factory_root_fails_closed(self):
        organization = Organization.objects.create(
            name="待人工核对组织",
            code="TENANT-ROOTLESS-ORG",
            organization_type=self.merchant_type,
        )
        user = self._user("rootless-user")
        UserOrganization.objects.create(
            user=user,
            organization=organization,
            role=self.merchant_owner_role,
        )

        self.assertIsNone(get_active_identity(user))
        self.assertEqual(build_user_payload(user)["role"], "tourist")

    def test_device_inventory_and_events_cannot_cross_factory_roots(self):
        first = self._accept_factory("设备厂家甲", "device-owner-a")
        second = self._accept_factory("设备厂家乙", "device-owner-b")
        category = ProductCategory.objects.create(
            name="租户边界测试分类", code="TENANT-BOUNDARY-CATEGORY"
        )
        product = ProductModel.objects.create(
            code="TENANT-BOUNDARY-MODEL",
            name="租户边界测试设备",
            category=category,
        )
        bom = BOM.objects.create(
            product_model=product,
            version="V1",
            created_by=first.user,
        )
        batch = ProductionBatch.objects.create(
            batch_no="TENANT-BOUNDARY-BATCH",
            product_model=product,
            bom=bom,
            manufacturer=first.organization,
            production_date=date(2026, 9, 29),
            quantity=1,
        )
        device = Device.objects.create(
            sn="TENANT-BOUNDARY-DEVICE",
            product_model=product,
            batch=batch,
        )

        with self.assertRaises(ValidationError):
            OrganizationDevice.objects.create(
                device=device,
                organization=second.organization,
                status="IN_STOCK",
            )
        with self.assertRaises(ValidationError):
            LifecycleEvent.objects.create(
                device=device,
                event_type="SHIP",
                organization=second.organization,
            )

    def test_production_batch_requires_the_factory_root_as_manufacturer(self):
        factory = self._accept_factory("批次根厂家", "batch-owner")
        merchant = Organization.objects.create(
            name="厂家直属商家",
            code="TENANT-BATCH-MERCHANT",
            organization_type=self.merchant_type,
            factory_root=factory.organization,
        )
        category = ProductCategory.objects.create(
            name="厂家批次边界分类", code="TENANT-BATCH-CATEGORY"
        )
        product = ProductModel.objects.create(
            code="TENANT-BATCH-MODEL",
            name="厂家批次边界设备",
            category=category,
        )
        bom = BOM.objects.create(product_model=product, version="V1")

        with self.assertRaises(ValidationError):
            ProductionBatch.objects.create(
                batch_no="TENANT-BATCH-CHILD-MANUFACTURER",
                product_model=product,
                bom=bom,
                manufacturer=merchant,
                production_date=date(2026, 9, 29),
                quantity=1,
            )
