from datetime import date, timedelta
import tempfile
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
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
from common.domain.device_ownership import (
    devices_under_account,
    devices_under_organization,
    resolve_device_ownership,
)
from lifecycle.models import (
    AfterSalesApplication,
    CustomerDeviceBinding,
    DeviceState,
    DriverVehicleBinding,
    VehicleDeviceAssignment,
    InstallationInvitation,
    InstallationRecord,
    LifecycleEvent,
    OrganizationDevice,
    ProductDocumentRead,
)
from lifecycle.services.lifecycle_service import LifecycleService
from lifecycle.services.aftersales_service import AfterSalesService
from manufacturing.models import ProductionBatch
from product.models import (
    BOM,
    BOMItem,
    ProductCategory,
    ProductDocument,
    ProductModel,
)
from supply_chain.models import Material, MaterialSpec
from workflow.models import UserTask, WorkItem


class ScanOwnershipAndInstallationTests(TestCase):
    resolve_endpoint = "/pldp/api/lifecycle/scan/resolve/"
    install_endpoint = "/pldp/api/lifecycle/scan/install/"
    refresh_endpoint = "/pldp/api/lifecycle/scan/customer-invitation/refresh/"

    @classmethod
    def setUpTestData(cls):
        owner_type, _ = OrganizationType.objects.update_or_create(
            code="OWNER", defaults={"name": "厂家", "is_active": True}
        )
        merchant_type, _ = OrganizationType.objects.update_or_create(
            code="MERCHANT", defaults={"name": "商家", "is_active": True}
        )
        customer_type, _ = OrganizationType.objects.update_or_create(
            code="CUSTOMER", defaults={"name": "用户", "is_active": True}
        )
        supplier_type, _ = OrganizationType.objects.update_or_create(
            code="SUPPLIER", defaults={"name": "供应商", "is_active": True}
        )

        cls.factory_admin_role, _ = OrganizationRole.objects.update_or_create(
            organization_type=owner_type,
            code="factory_admin",
            defaults={
                "name": "厂家负责人账号",
                "is_owner_role": True,
                "is_active": True,
            },
        )
        cls.merchant_owner_role, _ = OrganizationRole.objects.update_or_create(
            organization_type=merchant_type,
            code="merchant_owner",
            defaults={
                "name": "商家负责人账号",
                "is_owner_role": True,
                "is_active": True,
            },
        )
        cls.merchant_sales_role, _ = OrganizationRole.objects.update_or_create(
            organization_type=merchant_type,
            code="merchant_sales",
            defaults={"name": "商家销售经理", "is_active": True},
        )
        cls.merchant_manager_role, _ = OrganizationRole.objects.update_or_create(
            organization_type=merchant_type,
            code="merchant_manager",
            defaults={"name": "商家管理人员", "is_active": True},
        )
        (
            cls.merchant_electrician_role,
            _,
        ) = OrganizationRole.objects.update_or_create(
            organization_type=merchant_type,
            code="merchant_electrician",
            defaults={"name": "商家电工", "is_active": True},
        )
        cls.customer_owner_role, _ = OrganizationRole.objects.update_or_create(
            organization_type=customer_type,
            code="customer_owner",
            defaults={
                "name": "用户负责人账号",
                "is_owner_role": True,
                "is_active": True,
            },
        )
        cls.driver_role, _ = OrganizationRole.objects.update_or_create(
            organization_type=customer_type,
            code="driver",
            defaults={"name": "司机", "is_active": True},
        )
        cls.supplier_owner_role, _ = OrganizationRole.objects.update_or_create(
            organization_type=supplier_type,
            code="supplier_owner",
            defaults={"name": "供应商负责人账号", "is_owner_role": True, "is_active": True},
        )
        cls.supplier_sales_role, _ = OrganizationRole.objects.update_or_create(
            organization_type=supplier_type,
            code="supplier_sales",
            defaults={"name": "供应商销售经理", "is_active": True},
        )

        cls.factory = Organization.objects.create(
            name="测试厂家",
            code="FACTORY-TEST",
            organization_type=owner_type,
            region="全国",
        )
        cls.regional_merchant = Organization.objects.create(
            name="重庆区域商家",
            code="MERCHANT-REGIONAL",
            organization_type=merchant_type,
            region="重庆市",
        )
        cls.direct_merchant = Organization.objects.create(
            name="重庆直接库存商家",
            code="MERCHANT-DIRECT",
            organization_type=merchant_type,
            region="重庆市渝北区",
        )
        cls.outside_merchant = Organization.objects.create(
            name="无关商家",
            code="MERCHANT-OUTSIDE",
            organization_type=merchant_type,
            region="四川省",
        )
        cls.supplier = Organization.objects.create(
            name="测试供应商",
            code="SUPPLIER-TEST",
            organization_type=supplier_type,
        )

        cls.factory_user = cls._user_identity(
            "factory", cls.factory, cls.factory_admin_role
        )
        cls.regional_owner_user = cls._user_identity(
            "regional_owner", cls.regional_merchant, cls.merchant_owner_role
        )
        cls.regional_sales_user = cls._user_identity(
            "regional_sales", cls.regional_merchant, cls.merchant_sales_role
        )
        cls.direct_owner_user = cls._user_identity(
            "direct_owner", cls.direct_merchant, cls.merchant_owner_role
        )
        cls.direct_manager_user = cls._user_identity(
            "direct_manager", cls.direct_merchant, cls.merchant_manager_role
        )
        cls.direct_sales_user = cls._user_identity(
            "direct_sales", cls.direct_merchant, cls.merchant_sales_role
        )
        cls.direct_electrician_user = cls._user_identity(
            "direct_electrician",
            cls.direct_merchant,
            cls.merchant_electrician_role,
        )
        cls.outside_owner_user = cls._user_identity(
            "outside_owner", cls.outside_merchant, cls.merchant_owner_role
        )
        cls.tourist_user = User.objects.create_user(
            username="wx_tourist",
            wx_openid="openid-tourist",
        )
        cls.customer_user = User.objects.create_user(
            username="wx_customer_new",
            wx_openid="openid-customer-new",
            wx_nickname="新用户负责人",
        )
        cls.supplier_owner_user = cls._user_identity(
            "supplier_owner", cls.supplier, cls.supplier_owner_role
        )
        cls.supplier_sales_user = cls._user_identity(
            "supplier_sales", cls.supplier, cls.supplier_sales_role
        )

        cls._create_distribution_relation(
            inviter_user=cls.factory_user,
            inviter_role=cls.factory_admin_role,
            target_user=cls.regional_owner_user,
            target_org=cls.regional_merchant,
        )
        cls._create_distribution_relation(
            inviter_user=cls.regional_sales_user,
            inviter_role=cls.merchant_sales_role,
            target_user=cls.direct_owner_user,
            target_org=cls.direct_merchant,
        )

        category = ProductCategory.objects.create(code="GEN", name="发电机")
        product_model = ProductModel.objects.create(
            code="GEN-5000",
            name="5000W驻车发电机",
            category=category,
        )
        for document_type, title in ProductDocument.DocumentType.choices:
            ProductDocument.objects.create(
                product_model=product_model,
                document_type=document_type,
                title=title,
                content=f"{title}测试正文",
            )
        bom = BOM.objects.create(
            product_model=product_model,
            version_no=1,
            version="V1",
            created_by=cls.factory_user,
        )
        cls.material = Material.objects.create(
            code="GEN-CTRL",
            name="控制器",
            product_category=category,
        )
        spec = MaterialSpec.objects.create(
            material=cls.material,
            code="GEN-CTRL-01",
            name="标准版",
        )
        cls.bom_item = BOMItem.objects.create(
            bom=bom,
            material=cls.material,
            material_spec=spec,
            supplier=cls.supplier,
        )
        batch = ProductionBatch.objects.create(
            batch_no="BATCH-20260808",
            product_model=product_model,
            bom=bom,
            manufacturer=cls.factory,
            production_date=date(2026, 8, 8),
            quantity=1,
        )
        cls.device = LifecycleService.create_device(
            product_model=product_model,
            batch=batch,
            organization=cls.factory,
            operator=cls.factory_user,
            sn="SN-TEST-001",
        )
        OrganizationDevice.objects.filter(device=cls.device).update(
            organization=cls.direct_merchant,
            status="IN_STOCK",
        )
        DeviceState.objects.filter(device=cls.device).update(
            current_organization=cls.direct_merchant,
            status="IN_STOCK",
            install_status="NOT_INSTALLED",
            activation_status="NOT_ACTIVATED",
        )

    @classmethod
    def _user_identity(cls, suffix, organization, role):
        user = User.objects.create_user(
            username=f"wx_{suffix}",
            wx_openid=f"openid-{suffix}",
            wx_nickname=suffix,
        )
        UserOrganization.objects.create(
            user=user,
            organization=organization,
            role=role,
        )
        return user

    @classmethod
    def _create_distribution_relation(
        cls,
        *,
        inviter_user,
        inviter_role,
        target_user,
        target_org,
    ):
        target_role = target_user.organization_identity.role
        rule, _ = RoleInvitationRule.objects.get_or_create(
            inviter_role=inviter_role,
            target_role=target_role,
            scope=InvitationScope.CROSS_ORGANIZATION,
            relation_type=OrganizationRelationType.DISTRIBUTION,
        )
        invitation = InvitationRecord.objects.create(
            inviter=inviter_user,
            source_organization=inviter_user.organization_identity.organization,
            inviter_role=inviter_role,
            rule=rule,
            target_role=target_role,
            target_organization=target_org,
            invitee=target_user,
            scope=InvitationScope.CROSS_ORGANIZATION,
            relation_type=OrganizationRelationType.DISTRIBUTION,
            status=InvitationRecord.Status.ACCEPTED,
            expires_at=timezone.now() + timedelta(days=1),
            accepted_at=timezone.now(),
        )
        return OrganizationRelation.objects.create(
            source_organization=inviter_user.organization_identity.organization,
            target_organization=target_org,
            relation_type=OrganizationRelationType.DISTRIBUTION,
            created_by_invitation=invitation,
        )

    def auth(self, user):
        return {"HTTP_AUTHORIZATION": f"Bearer {issue_access_token(user)}"}

    def resolve(self, user):
        return self.client.post(
            self.resolve_endpoint,
            {"qr_text": self.device.qrcode.code},
            **self.auth(user),
        )

    def test_device_creation_builds_complete_initial_chain(self):
        self.assertEqual(self.device.qrcode.status, "ACTIVE")
        self.assertTrue(DeviceState.objects.filter(device=self.device).exists())
        self.assertTrue(
            LifecycleEvent.objects.filter(
                device=self.device,
                event_type="CREATE",
            ).exists()
        )

    def test_tourist_only_receives_public_fields(self):
        response = self.resolve(self.tourist_user)
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["view_mode"], "public")
        self.assertEqual(data["capabilities"], ["view_public_detail"])
        self.assertNotIn("region", data)
        self.assertNotIn("ownership", data)
        self.assertNotIn("traceability", data)

    def test_unrelated_system_user_receives_region_but_no_chain_data(self):
        response = self.resolve(self.outside_owner_user)
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["view_mode"], "registered")
        self.assertEqual(data["viewer_relation"], "unrelated")
        self.assertEqual(data["region"]["name"], "重庆市")
        self.assertEqual(
            data["region"]["owner"]["id"],
            self.regional_merchant.id,
        )
        self.assertNotIn("ownership", data)
        self.assertNotIn("install_device", data["capabilities"])

    def test_direct_owner_can_install_but_manager_cannot(self):
        owner_data = self.resolve(self.direct_owner_user).json()["data"]
        self.assertEqual(owner_data["viewer_relation"], "direct_owner")
        self.assertIn("install_device", owner_data["capabilities"])

        manager_data = self.resolve(self.direct_manager_user).json()["data"]
        self.assertEqual(manager_data["viewer_relation"], "direct_owner")
        self.assertNotIn("install_device", manager_data["capabilities"])

        sales_data = self.resolve(self.direct_sales_user).json()["data"]
        electrician_data = self.resolve(
            self.direct_electrician_user
        ).json()["data"]
        self.assertIn("install_device", sales_data["capabilities"])
        self.assertIn("install_device", electrician_data["capabilities"])

    def test_account_and_organization_upstream_owners_are_distinguished(self):
        sales_data = self.resolve(self.regional_sales_user).json()["data"]
        self.assertEqual(
            sales_data["viewer_relation"],
            "upstream_account_owner",
        )

        owner_data = self.resolve(self.regional_owner_user).json()["data"]
        self.assertEqual(
            owner_data["viewer_relation"],
            "upstream_organization_owner",
        )
        self.assertTrue(owner_data["ownership"]["flags"]["is_regional_owner"])
        self.assertEqual(
            owner_data["viewer_relation_names"],
            ["本组织下级链路设备", "本区域设备"],
        )

    def test_shared_queries_cover_organization_and_account_subtrees(self):
        regional_identity = self.regional_sales_user.organization_identity
        self.assertIn(
            self.device,
            devices_under_account(regional_identity),
        )
        self.assertIn(
            self.device,
            devices_under_organization(self.regional_merchant),
        )

    def test_unrelated_installer_role_cannot_install(self):
        response = self.client.post(
            self.install_endpoint,
            {"device_id": self.device.id},
            **self.auth(self.outside_owner_user),
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(InstallationRecord.objects.filter(device=self.device).exists())

    def test_install_creates_event_record_and_one_time_customer_invitation(self):
        response = self.client.post(
            self.install_endpoint,
            {"device_id": self.device.id, "location": "重庆市渝北区"},
            **self.auth(self.direct_owner_user),
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()["data"]
        self.assertEqual(
            data["scan_result"]["device"]["state"]["install_status"],
            "INSTALLED",
        )
        self.assertNotIn(
            "install_device",
            data["scan_result"]["capabilities"],
        )
        self.assertIn(
            "create_customer_invitation",
            data["scan_result"]["capabilities"],
        )
        self.assertEqual(data["invitation"]["target_role"], "customer_owner")
        self.assertTrue(InstallationRecord.objects.filter(device=self.device).exists())
        self.assertTrue(
            InstallationInvitation.objects.filter(
                device=self.device,
                status=InstallationInvitation.Status.PENDING,
            ).exists()
        )
        self.assertTrue(
            LifecycleEvent.objects.filter(
                device=self.device,
                event_type="INSTALL",
            ).exists()
        )

    def test_refresh_customer_invitation_cancels_previous_token(self):
        install_response = self.client.post(
            self.install_endpoint,
            {"device_id": self.device.id},
            **self.auth(self.direct_owner_user),
        )
        old_token = install_response.json()["data"]["invitation"]["token"]

        refresh_response = self.client.post(
            self.refresh_endpoint,
            {"device_id": self.device.id},
            **self.auth(self.direct_owner_user),
        )
        self.assertEqual(refresh_response.status_code, 200)
        new_token = refresh_response.json()["data"]["invitation"]["token"]
        self.assertNotEqual(old_token, new_token)
        self.assertEqual(
            InstallationInvitation.objects.get(token=old_token).status,
            InstallationInvitation.Status.CANCELLED,
        )

    @patch(
        "apps.lifecycle.api.views.generate_unlimited_miniprogram_code",
        return_value=(b"wechat-miniprogram-code", "image/png"),
    )
    def test_pending_customer_invitation_renders_no_cache_qrcode(
        self,
        generate_code,
    ):
        install_response = self.client.post(
            self.install_endpoint,
            {"device_id": self.device.id},
            **self.auth(self.direct_owner_user),
        )
        token = install_response.json()["data"]["invitation"]["token"]

        response = self.client.get(
            "/pldp/api/lifecycle/scan/customer-invitations/"
            f"{token}/qrcode/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertEqual(response["Cache-Control"], "no-store, max-age=0")
        invitation = InstallationInvitation.objects.get(token=token)
        generate_code.assert_called_once_with(
            scene=invitation.token.hex,
            page="pages/customer/invite/accept/accept",
        )

    def test_request_requires_signed_login_token(self):
        response = self.client.post(
            self.resolve_endpoint,
            {"qr_text": self.device.qrcode.code},
        )
        self.assertEqual(response.status_code, 401)

    def test_resolver_returns_same_direct_and_regional_owners_for_all_consumers(self):
        context = resolve_device_ownership(
            device=self.device,
            viewer_identity=self.regional_sales_user.organization_identity,
        )
        self.assertEqual(context.direct_owner, self.direct_merchant)
        self.assertEqual(context.regional_owner, self.regional_merchant)
        self.assertEqual(context.organization_path[0], self.factory)
        self.assertEqual(context.organization_path[-1], self.direct_merchant)

    def _install_and_token(self):
        response = self.client.post(
            self.install_endpoint,
            {"device_id": self.device.id, "location": "重庆市渝北区"},
            **self.auth(self.direct_owner_user),
        )
        self.assertEqual(response.status_code, 201)
        return response.json()["data"]["invitation"]["token"]

    def _accept_payload(self):
        return {
            "customer_type": "FLEET",
            "customer_name": "重庆扫码测试车队",
            "contact_name": "张负责人",
            "phone": "13900000001",
            "region": "重庆市,重庆市,渝北区",
            "address": "测试地址",
            "vehicle_no": "渝A12345",
        }

    def test_customer_invitation_detail_and_accept_build_complete_chain(self):
        token = self._install_and_token()
        detail_url = (
            f"/pldp/api/lifecycle/scan/customer-invitations/{token}/"
        )
        detail = self.client.get(detail_url, **self.auth(self.customer_user))
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(
            detail.json()["data"]["target_role"]["code"],
            "customer_owner",
        )

        response = self.client.post(
            f"{detail_url}accept/",
            self._accept_payload(),
            **self.auth(self.customer_user),
        )
        self.assertEqual(response.status_code, 201)
        identity = UserOrganization.objects.get(user=self.customer_user)
        self.assertEqual(identity.role.code, "customer_owner")
        self.assertEqual(identity.organization.organization_type.code, "CUSTOMER")

        invitation = InstallationInvitation.objects.get(token=token)
        self.assertEqual(invitation.status, InstallationInvitation.Status.ACCEPTED)
        self.assertEqual(invitation.invitee, self.customer_user)
        self.assertEqual(invitation.target_organization, identity.organization)
        self.assertEqual(invitation.customer_data["vehicle_no"], "渝A12345")

        self.assertTrue(
            OrganizationRelation.objects.filter(
                source_organization=self.direct_merchant,
                target_organization=identity.organization,
                relation_type=OrganizationRelationType.DISTRIBUTION,
                is_active=True,
            ).exists()
        )
        self.assertEqual(
            OrganizationDevice.objects.get(device=self.device).organization,
            identity.organization,
        )
        self.assertEqual(
            DeviceState.objects.get(device=self.device).current_organization,
            identity.organization,
        )

        installer_result = self.resolve(self.direct_owner_user)
        self.assertEqual(installer_result.status_code, 200)
        installer_data = installer_result.json()["data"]
        self.assertEqual(
            installer_data["viewer_relation"],
            "upstream_account_owner",
        )
        self.assertEqual(
            installer_data["customer_invitation"]["status"],
            InstallationInvitation.Status.ACCEPTED,
        )
        self.assertIn(
            self.device,
            devices_under_account(
                self.direct_owner_user.organization_identity
            ),
        )

    def test_existing_customer_adds_second_device_from_another_merchant(self):
        first_token = self._install_and_token()
        first_payload = self._accept_payload()
        first_payload.update(
            customer_type="PERSONAL",
            customer_name="",
            vehicle_no="渝A10001",
        )
        first_accept = self.client.post(
            "/pldp/api/lifecycle/scan/customer-invitations/"
            f"{first_token}/accept/",
            first_payload,
            **self.auth(self.customer_user),
        )
        self.assertEqual(first_accept.status_code, 201)
        original_identity = UserOrganization.objects.get(user=self.customer_user)
        original_organization = original_identity.organization
        first_binding = CustomerDeviceBinding.objects.get(device=self.device)
        self.assertEqual(
            first_binding.acceptance_mode,
            CustomerDeviceBinding.AcceptanceMode.NEW_CUSTOMER,
        )

        second_merchant = Organization.objects.create(
            name="重庆第二设备商家",
            code="MERCHANT-SECOND",
            organization_type=self.direct_merchant.organization_type,
            region="重庆市江北区",
        )
        second_owner = self._user_identity(
            "second_owner",
            second_merchant,
            self.merchant_owner_role,
        )
        self._create_distribution_relation(
            inviter_user=self.factory_user,
            inviter_role=self.factory_admin_role,
            target_user=second_owner,
            target_org=second_merchant,
        )
        second_device = LifecycleService.create_device(
            product_model=self.device.product_model,
            batch=self.device.batch,
            organization=self.factory,
            operator=self.factory_user,
            sn="SN-TEST-SECOND-001",
        )
        OrganizationDevice.objects.filter(device=second_device).update(
            organization=second_merchant,
            status="IN_STOCK",
        )
        DeviceState.objects.filter(device=second_device).update(
            current_organization=second_merchant,
            status="IN_STOCK",
            install_status="NOT_INSTALLED",
            activation_status="NOT_ACTIVATED",
        )
        install = self.client.post(
            self.install_endpoint,
            {"device_id": second_device.id, "location": "重庆市江北区"},
            **self.auth(second_owner),
        )
        self.assertEqual(install.status_code, 201)
        second_token = install.json()["data"]["invitation"]["token"]
        second_detail_url = (
            "/pldp/api/lifecycle/scan/customer-invitations/"
            f"{second_token}/"
        )
        second_detail = self.client.get(
            second_detail_url,
            **self.auth(self.customer_user),
        )
        self.assertEqual(second_detail.status_code, 200)
        self.assertEqual(
            second_detail.json()["data"]["acceptance_mode"],
            "ADD_DEVICE",
        )
        self.assertEqual(
            second_detail.json()["data"]["customer_profile"]["customer_type"],
            "PERSONAL",
        )

        second_payload = self._accept_payload()
        second_payload.update(
            customer_type="FLEET",
            customer_name="不应新建的车队",
            vehicle_no="渝A20002",
        )
        second_accept = self.client.post(
            f"{second_detail_url}accept/",
            second_payload,
            **self.auth(self.customer_user),
        )
        self.assertEqual(second_accept.status_code, 201)

        identities = UserOrganization.objects.filter(user=self.customer_user)
        self.assertEqual(identities.count(), 1)
        self.assertEqual(identities.get().id, original_identity.id)
        self.assertEqual(identities.get().organization_id, original_organization.id)
        second_binding = CustomerDeviceBinding.objects.get(device=second_device)
        self.assertEqual(
            second_binding.acceptance_mode,
            CustomerDeviceBinding.AcceptanceMode.EXISTING_CUSTOMER,
        )
        self.assertEqual(second_binding.customer_organization, original_organization)
        self.assertEqual(second_binding.source_organization, second_merchant)
        self.assertEqual(second_binding.customer_type, "PERSONAL")
        self.assertEqual(second_binding.vehicle_no, "渝A20002")
        self.assertTrue(
            OrganizationRelation.objects.filter(
                source_organization=second_merchant,
                target_organization=original_organization,
                relation_type=OrganizationRelationType.DISTRIBUTION,
                is_active=True,
            ).exists()
        )

        device_list = self.client.get(
            "/pldp/api/lifecycle/customer/devices/",
            **self.auth(self.customer_user),
        )
        self.assertEqual(device_list.status_code, 200)
        self.assertEqual(device_list.json()["data"]["total"], 2)
        second_result = self.client.get(
            f"/pldp/api/lifecycle/customer/devices/{second_device.id}/",
            **self.auth(self.customer_user),
        )
        self.assertEqual(second_result.status_code, 200)
        result_data = second_result.json()["data"]
        self.assertEqual(
            result_data["asset"]["source_organization"],
            second_merchant.name,
        )
        self.assertEqual(
            result_data["customer"]["profile"]["vehicle_no"],
            "渝A20002",
        )

    def test_existing_business_identity_cannot_accept_customer_invitation(self):
        token = self._install_and_token()
        response = self.client.post(
            "/pldp/api/lifecycle/scan/customer-invitations/"
            f"{token}/accept/",
            self._accept_payload(),
            **self.auth(self.outside_owner_user),
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            InstallationInvitation.objects.get(token=token).status,
            InstallationInvitation.Status.PENDING,
        )

    def test_customer_owner_can_activate_accepted_device_once(self):
        token = self._install_and_token()
        accept_response = self.client.post(
            "/pldp/api/lifecycle/scan/customer-invitations/"
            f"{token}/accept/",
            self._accept_payload(),
            **self.auth(self.customer_user),
        )
        self.assertEqual(accept_response.status_code, 201)

        result_url = f"/pldp/api/lifecycle/customer/devices/{self.device.id}/"
        result = self.client.get(result_url, **self.auth(self.customer_user))
        self.assertEqual(result.status_code, 200)
        self.assertIn(
            "activate_device",
            result.json()["data"]["scan_result"]["capabilities"],
        )

        activate = self.client.post(
            f"{result_url}activate/",
            {"vehicle_no": "渝A12345", "location": "重庆市渝北区"},
            **self.auth(self.customer_user),
        )
        self.assertEqual(activate.status_code, 200)
        state = DeviceState.objects.get(device=self.device)
        self.assertEqual(state.activation_status, "ACTIVATED")
        self.assertTrue(
            LifecycleEvent.objects.filter(
                device=self.device,
                event_type="ACTIVATE",
            ).exists()
        )

        duplicate = self.client.post(
            f"{result_url}activate/",
            {"vehicle_no": "渝A12345"},
            **self.auth(self.customer_user),
        )
        self.assertEqual(duplicate.status_code, 409)

    def test_driver_can_activate_only_devices_on_shared_vehicle(self):
        token = self._install_and_token()
        accept_response = self.client.post(
            "/pldp/api/lifecycle/scan/customer-invitations/"
            f"{token}/accept/",
            self._accept_payload(),
            **self.auth(self.customer_user),
        )
        self.assertEqual(accept_response.status_code, 201)

        customer_identity = UserOrganization.objects.get(user=self.customer_user)
        driver = self._user_identity(
            "shared_vehicle_driver",
            customer_identity.organization,
            self.driver_role,
        )
        unrelated_driver = self._user_identity(
            "unrelated_vehicle_driver",
            customer_identity.organization,
            self.driver_role,
        )
        binding = CustomerDeviceBinding.objects.select_related("vehicle").get(
            device=self.device,
            status=CustomerDeviceBinding.Status.ACTIVE,
        )
        self.assertIsNotNone(binding.vehicle_id)

        result_url = f"/pldp/api/lifecycle/customer/devices/{self.device.id}/"
        denied_result = self.client.get(
            result_url,
            **self.auth(unrelated_driver),
        )
        self.assertEqual(denied_result.status_code, 403)
        denied_activate = self.client.post(
            f"{result_url}activate/",
            {"vehicle_no": "渝A12345"},
            **self.auth(unrelated_driver),
        )
        self.assertEqual(denied_activate.status_code, 403)

        DriverVehicleBinding.objects.create(
            vehicle=binding.vehicle,
            driver=driver,
            is_active=True,
        )
        result = self.client.get(result_url, **self.auth(driver))
        self.assertEqual(result.status_code, 200)
        result_data = result.json()["data"]
        self.assertIn(
            "activate_device",
            result_data["scan_result"]["capabilities"],
        )
        self.assertTrue(result_data["actions"]["can_activate"])

        activate = self.client.post(
            f"{result_url}activate/",
            {"vehicle_no": "渝A12345", "location": "重庆市渝北区"},
            **self.auth(driver),
        )
        self.assertEqual(activate.status_code, 200)
        self.assertEqual(
            DeviceState.objects.get(device=self.device).activation_status,
            "ACTIVATED",
        )

    def test_owner_device_list_shows_all_shared_drivers_and_assignment_history(self):
        token = self._install_and_token()
        accept_response = self.client.post(
            "/pldp/api/lifecycle/scan/customer-invitations/"
            f"{token}/accept/",
            self._accept_payload(),
            **self.auth(self.customer_user),
        )
        self.assertEqual(accept_response.status_code, 201)
        binding = CustomerDeviceBinding.objects.select_related("vehicle").get(
            device=self.device,
        )
        self.assertTrue(VehicleDeviceAssignment.objects.filter(
            device=self.device,
            vehicle=binding.vehicle,
            is_current=True,
        ).exists())

        first_driver = self._user_identity(
            "first_shared_driver", binding.customer_organization, self.driver_role,
        )
        first_driver.wx_nickname = "司机甲"
        first_driver.save(update_fields=("wx_nickname",))
        second_driver = self._user_identity(
            "second_shared_driver", binding.customer_organization, self.driver_role,
        )
        second_driver.wx_nickname = "司机乙"
        second_driver.save(update_fields=("wx_nickname",))
        DriverVehicleBinding.objects.create(
            vehicle=binding.vehicle, driver=first_driver, is_active=True,
        )
        DriverVehicleBinding.objects.create(
            vehicle=binding.vehicle, driver=second_driver, is_active=True,
        )

        response = self.client.get(
            "/pldp/api/lifecycle/customer/devices/",
            **self.auth(self.customer_user),
        )
        self.assertEqual(response.status_code, 200)
        vehicle = response.json()["data"]["vehicles"][0]
        self.assertEqual(vehicle["shared_driver_count"], 2)
        self.assertEqual(vehicle["shared_driver_names"], ["司机甲", "司机乙"])
        self.assertEqual(
            vehicle["sharing_text"], "该车辆绑定设备已与司机甲、司机乙共享",
        )

    def test_personal_customer_must_read_current_three_documents_before_activation(self):
        token = self._install_and_token()
        payload = self._accept_payload()
        payload.update(customer_type="PERSONAL", customer_name="")
        accept_response = self.client.post(
            "/pldp/api/lifecycle/scan/customer-invitations/"
            f"{token}/accept/",
            payload,
            **self.auth(self.customer_user),
        )
        self.assertEqual(accept_response.status_code, 201)

        base_url = f"/pldp/api/lifecycle/customer/devices/{self.device.id}/"
        documents_response = self.client.get(
            f"{base_url}documents/",
            **self.auth(self.customer_user),
        )
        self.assertEqual(documents_response.status_code, 200)
        gate = documents_response.json()["data"]
        self.assertTrue(gate["gate_required"])
        self.assertFalse(gate["activation_allowed"])
        self.assertEqual(len(gate["documents"]), 3)

        blocked = self.client.post(
            f"{base_url}activate/",
            {"vehicle_no": "渝A12345"},
            **self.auth(self.customer_user),
        )
        self.assertEqual(blocked.status_code, 409)

        for document in gate["documents"]:
            read_response = self.client.post(
                f"{base_url}documents/{document['id']}/read/",
                {},
                **self.auth(self.customer_user),
            )
            self.assertEqual(read_response.status_code, 200)

        self.assertEqual(
            ProductDocumentRead.objects.filter(
                user=self.customer_user,
                device=self.device,
            ).count(),
            3,
        )
        activate = self.client.post(
            f"{base_url}activate/",
            {"vehicle_no": "渝A12345"},
            **self.auth(self.customer_user),
        )
        self.assertEqual(activate.status_code, 200)

    def test_document_original_file_requires_owned_device_and_authentication(self):
        token = self._install_and_token()
        payload = self._accept_payload()
        payload.update(customer_type="PERSONAL", customer_name="")
        self.client.post(
            "/pldp/api/lifecycle/scan/customer-invitations/"
            f"{token}/accept/",
            payload,
            **self.auth(self.customer_user),
        )
        document = ProductDocument.objects.filter(
            product_model=self.device.product_model,
            document_type=ProductDocument.DocumentType.USER_MANUAL,
        ).first()
        file_content = b"%PDF-1.4\noriginal manual\n%%EOF"

        with tempfile.TemporaryDirectory() as media_root, override_settings(
            MEDIA_ROOT=media_root
        ):
            document.document_file.save(
                "manual.pdf",
                ContentFile(file_content),
                save=False,
            )
            document.original_filename = "使用说明书.pdf"
            document.file_type = ProductDocument.FileType.PDF
            document.mime_type = "application/pdf"
            document.file_size = len(file_content)
            document.save()

            endpoint = (
                f"/pldp/api/lifecycle/customer/devices/{self.device.id}/documents/"
                f"{document.id}/file/"
            )
            unauthenticated = self.client.get(endpoint)
            self.assertEqual(unauthenticated.status_code, 401)

            response = self.client.get(
                endpoint,
                **self.auth(self.customer_user),
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b"".join(response.streaming_content), file_content)

    def test_telemetry_endpoint_is_optional_and_bom_driven(self):
        token = self._install_and_token()
        self.client.post(
            "/pldp/api/lifecycle/scan/customer-invitations/"
            f"{token}/accept/",
            self._accept_payload(),
            **self.auth(self.customer_user),
        )
        endpoint = (
            f"/pldp/api/lifecycle/customer/devices/{self.device.id}/telemetry/"
        )

        unsupported = self.client.get(endpoint, **self.auth(self.customer_user))
        self.assertEqual(unsupported.status_code, 200)
        self.assertFalse(unsupported.json()["data"]["capable"])

        spec = MaterialSpec.objects.get(code="GEN-CTRL-01")
        spec.parameters = {"capabilities": ["telemetry"]}
        spec.save(update_fields=("parameters",))
        supported = self.client.get(endpoint, **self.auth(self.customer_user))
        self.assertEqual(supported.status_code, 200)
        self.assertTrue(supported.json()["data"]["capable"])
        self.assertEqual(supported.json()["data"]["status"], "NOT_CONFIGURED")

    def test_after_sales_application_submit_snapshots_and_publishes_tasks(self):
        token = self._install_and_token()
        accepted = self.client.post(
            f"/pldp/api/lifecycle/scan/customer-invitations/{token}/accept/",
            self._accept_payload(),
            **self.auth(self.customer_user),
        )
        self.assertEqual(accepted.status_code, 201)

        factory_sales_role, _ = OrganizationRole.objects.update_or_create(
            organization_type=self.factory.organization_type,
            code="factory_sales",
            defaults={"name": "厂家销售经理", "is_active": True},
        )
        factory_sales = User.objects.create_user(
            username="aftersales_factory_sales",
            wx_openid="openid-aftersales-factory-sales",
        )
        UserOrganization.objects.create(
            user=factory_sales,
            organization=self.factory,
            role=factory_sales_role,
        )

        context = self.client.get(
            f"/pldp/api/lifecycle/customer/devices/{self.device.id}/after-sales/context/",
            **self.auth(self.customer_user),
        )
        self.assertEqual(context.status_code, 200)
        context_data = context.json()["data"]
        self.assertEqual(context_data["device"]["id"], self.device.id)
        self.assertIn("summary", context_data["warranty"])
        self.assertIn("key_materials", context_data["bom"])

        create = self.client.post(
            f"/pldp/api/lifecycle/customer/devices/{self.device.id}/after-sales/applications/",
            {
                "device_id": self.device.id,
                "fault_description": "发电机无法启动，已检查基本接线。",
                "location_name": "重庆市渝北区",
                "location_address": "测试服务区",
                "location_latitude": "29.600000",
                "location_longitude": "106.500000",
                "location_confirmed": True,
            },
            content_type="application/json",
            **self.auth(self.customer_user),
        )
        self.assertEqual(create.status_code, 201)
        application_id = create.json()["data"]["id"]

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            upload = self.client.post(
                f"/pldp/api/lifecycle/after-sales/applications/{application_id}/media/",
                {
                    "media_type": "IMAGE",
                    "file": SimpleUploadedFile(
                        "fault.jpg", b"\xff\xd8\xff\xe0test-image", content_type="image/jpeg"
                    ),
                },
                **self.auth(self.customer_user),
            )
            self.assertEqual(upload.status_code, 201)
            video_upload = self.client.post(
                f"/pldp/api/lifecycle/after-sales/applications/{application_id}/media/",
                {
                    "media_type": "VIDEO",
                    "duration_seconds": "3",
                    "file": SimpleUploadedFile(
                        "fault.mp4", b"\x00\x00\x00\x18ftypmp42test-video", content_type="video/mp4"
                    ),
                },
                **self.auth(self.customer_user),
            )
            self.assertEqual(video_upload.status_code, 201, video_upload.json())
            submitted = self.client.post(
                f"/pldp/api/lifecycle/after-sales/applications/{application_id}/submit/",
                {},
                content_type="application/json",
                **self.auth(self.customer_user),
            )
            self.assertEqual(submitted.status_code, 200)
            detail = self.client.get(
                f"/pldp/api/lifecycle/after-sales/applications/{application_id}/",
                **self.auth(factory_sales),
            )
            self.assertEqual(detail.status_code, 200)
            factory_detail = detail.json()["data"]
            self.assertEqual(len(factory_detail["media"]), 2)
            self.assertEqual(factory_detail["viewer_stage"]["mode"], "ACTION_REQUIRED")
            self.assertEqual(
                factory_detail["viewer_stage"]["badge"],
                "待您认定并分派",
            )
            self.assertEqual(factory_detail["applicant_instruction"], {})

            applicant_detail = self.client.get(
                f"/pldp/api/lifecycle/after-sales/applications/{application_id}/",
                **self.auth(self.customer_user),
            )
            self.assertEqual(applicant_detail.status_code, 200)
            self.assertEqual(
                applicant_detail.json()["data"]["viewer_stage"]["title"],
                "等待厂家销售经理判断并指派",
            )
            self.assertEqual(
                applicant_detail.json()["data"]["viewer_stage"]["badge"],
                "等待厂家销售经理判断并指派",
            )

        application = AfterSalesApplication.objects.get(pk=application_id)
        self.assertEqual(application.status, AfterSalesApplication.Status.SUBMITTED)
        self.assertEqual(application.device_snapshot["sn"], self.device.sn)
        self.assertTrue(application.location_confirmed)
        self.assertEqual(
            set(application.events.values_list("event_type", flat=True)),
            {"DRAFT_CREATED", "MEDIA_UPLOADED", "SUBMITTED"},
        )
        self.assertTrue(
            WorkItem.objects.filter(type="AFTERSALES_FACTORY_WORKORDER", biz_id=str(application_id)).exists()
        )
        self.assertTrue(
            UserTask.objects.filter(
                workitem__type="AFTERSALES_FACTORY_WORKORDER",
                to_openid=factory_sales.wx_openid,
                need_action=True,
            ).exists()
        )
        applicant_task = UserTask.objects.get(
            workitem__uniq_key=(
                f"aftersales:{application_id}:applicant-workorder"
            ),
            to_openid=self.customer_user.wx_openid,
        )
        self.assertEqual(
            applicant_task.workitem.type,
            "AFTERSALES_APPLICANT_WORKORDER",
        )
        self.assertFalse(applicant_task.need_action)

        # The same stage is rendered differently for the actor who owns the
        # current action and for the counterparty who is waiting.
        application.dispatch_route = AfterSalesApplication.DispatchRoute.MERCHANT_ELECTRICIAN
        application.execution_stage = (
            AfterSalesApplication.ExecutionStage.AWAITING_APPLICANT_SHIPMENT
        )
        application.status = AfterSalesApplication.Status.ROUTING
        application.assigned_user = self.direct_electrician_user
        application.assigned_organization = self.direct_merchant
        application.assignee_snapshot = {
            "name": "测试商家电工",
            "organization_name": self.direct_merchant.name,
            "organization_address": "重庆测试维修地址",
            "phone": "13900000002",
        }
        application.repair_plan = "寄送后检查控制器与线路。"
        application.save()

        applicant_shipping_detail = self.client.get(
            f"/pldp/api/lifecycle/after-sales/applications/{application_id}/",
            **self.auth(self.customer_user),
        ).json()["data"]
        self.assertEqual(
            applicant_shipping_detail["viewer_stage"]["mode"],
            "ACTION_REQUIRED",
        )
        self.assertEqual(
            applicant_shipping_detail["viewer_stage"]["badge"],
            "待您寄送设备",
        )
        self.assertTrue(applicant_shipping_detail["actions"]["can_applicant_shipment"])
        self.assertEqual(
            applicant_shipping_detail["applicant_instruction"]["type"],
            "SHIP_TO_REPAIRER",
        )

        assignee_waiting_detail = self.client.get(
            f"/pldp/api/lifecycle/after-sales/applications/{application_id}/",
            **self.auth(self.direct_electrician_user),
        ).json()["data"]
        self.assertEqual(
            assignee_waiting_detail["viewer_stage"]["title"],
            "等待售后发起人寄送设备",
        )
        self.assertEqual(
            assignee_waiting_detail["viewer_stage"]["badge"],
            "等待售后发起人寄送设备",
        )
        self.assertEqual(assignee_waiting_detail["applicant_instruction"], {})

        application.status = AfterSalesApplication.Status.SUBMITTED
        application.execution_stage = AfterSalesApplication.ExecutionStage.NONE
        application.dispatch_route = ""
        application.assigned_user = None
        application.assigned_organization = None
        application.assignee_snapshot = {}
        application.repair_plan = ""
        application.save()

        # Simulate the user-facing task produced by the previous release.  A
        # later push must adopt this original row instead of adding another
        # card for the same after-sales application.
        applicant_task.workitem.uniq_key = (
            f"aftersales:{application_id}:notice-submitted:v0"
        )
        applicant_task.workitem.type = "AFTERSALES_APPLICATION_SUBMITTED"
        applicant_task.workitem.save(
            update_fields=("uniq_key", "type", "updated_at")
        )

        duplicated = self.client.post(
            f"/pldp/api/lifecycle/customer/devices/{self.device.id}/after-sales/applications/",
            {"device_id": self.device.id, "fault_description": "重复申请", "location_confirmed": True},
            content_type="application/json",
            **self.auth(self.customer_user),
        )
        self.assertEqual(duplicated.status_code, 409)

        remote = self.client.post(
            f"/pldp/api/lifecycle/after-sales/applications/{application_id}/triage/",
            {
                "preliminary_fault_type": "INSTALLATION_OR_WIRING",
                "diagnosis_note": "根据故障视频判断为接线端子松动。",
                "repair_plan": "远程指导复紧接线端子后重新启动测试。",
                "dispatch_route": "REMOTE",
                "final_fault_type": "INSTALLATION_OR_WIRING",
                "final_fault_materials": [],
                "final_solution_type": "REMOTE_GUIDANCE",
                "final_solution_note": "已远程指导完成复紧和启动测试，设备恢复正常。",
            },
            content_type="application/json",
            **self.auth(factory_sales),
        )
        self.assertEqual(remote.status_code, 200, remote.json())
        applicant_task.refresh_from_db()
        self.assertEqual(
            WorkItem.objects.get(pk=applicant_task.workitem_id).uniq_key,
            f"aftersales:{application_id}:applicant-workorder",
        )
        self.assertEqual(
            applicant_task.display_title_snapshot,
            "厂家远程指导已完成并结案",
        )
        self.assertEqual(applicant_task.state, "NEW")
        self.assertFalse(applicant_task.need_action)
        self.assertEqual(
            UserTask.objects.filter(
                workitem__biz_ref=f"AfterSalesApplication:{application_id}",
                to_openid=self.customer_user.wx_openid,
            ).count(),
            1,
        )
        application.refresh_from_db()
        self.assertEqual(application.status, AfterSalesApplication.Status.CLOSED)
        self.assertTrue(
            application.events.filter(
                event_type="REMOTE_RESOLVED_AND_CLOSED"
            ).exists()
        )
        closed_detail = self.client.get(
            f"/pldp/api/lifecycle/after-sales/applications/{application_id}/",
            **self.auth(self.customer_user),
        )
        self.assertEqual(closed_detail.status_code, 200)
        self.assertEqual(
            closed_detail.json()["data"]["viewer_stage"]["mode"],
            "COMPLETED",
        )
        self.assertEqual(
            closed_detail.json()["data"]["viewer_stage"]["badge"],
            "售后服务已完成",
        )

    def test_supplier_quality_notice_is_one_task_and_sanitized(self):
        material = {
            "bom_item_id": self.bom_item.id,
            "material_id": self.material.id,
            "material_name": self.material.name,
            "specification_id": self.bom_item.material_spec_id,
            "specification_name": self.bom_item.material_spec.name,
            "supplier_id": self.supplier.id,
            "supplier_name": self.supplier.name,
            "is_key_part": True,
            "quantity": 1,
        }
        application = AfterSalesApplication.objects.create(
            case_no="AS-SUPPLIER-QUALITY-001",
            device=self.device,
            customer_organization=self.direct_merchant,
            factory_organization=self.factory,
            applicant=self.direct_owner_user,
            status=AfterSalesApplication.Status.CLOSED,
            execution_stage=AfterSalesApplication.ExecutionStage.CLOSED,
            fault_description="控制器故障测试",
            location_name="不得泄露的位置",
            location_address="不得泄露的详细地址",
            device_snapshot={
                "sn": self.device.sn,
                "product_model": self.device.product_model.name,
                "production_batch": self.device.batch.batch_no,
            },
            bom_snapshot={"version": "V1", "items": [material]},
            repair_summary="更换控制器后运行正常",
            repair_material_replaced=True,
            repair_materials=[material],
            final_fault_type=AfterSalesApplication.FaultType.COMPONENT_SUSPECTED,
            final_fault_materials=[material],
            final_solution_type=AfterSalesApplication.SolutionType.COMPONENT_REPLACEMENT,
            final_solution_note="确认控制器故障并完成更换",
            closed_by=self.factory_user,
            closed_at=timezone.now(),
        )

        AfterSalesService._publish_supplier_quality_notices(
            application, actor_openid=self.factory_user.wx_openid
        )
        AfterSalesService._publish_supplier_quality_notices(
            application, actor_openid=self.factory_user.wx_openid
        )
        workitems = WorkItem.objects.filter(
            uniq_key=f"aftersales:{application.id}:supplier-quality:{self.supplier.id}"
        )
        self.assertEqual(workitems.count(), 1)
        workitem = workitems.get()
        self.assertEqual(
            set(workitem.user_tasks.values_list("to_openid", flat=True)),
            {self.supplier_owner_user.wx_openid, self.supplier_sales_user.wx_openid},
        )
        self.assertTrue(
            workitem.user_tasks.filter(need_action=False, state="NEW").exists()
        )

        url = (
            f"/pldp/api/lifecycle/after-sales/applications/{application.id}/"
            f"supplier-quality-notices/{self.supplier.id}/"
        )
        response = self.client.get(url, **self.auth(self.supplier_sales_user))
        self.assertEqual(response.status_code, 200, response.json())
        data = response.json()["data"]
        self.assertEqual([item["material_id"] for item in data["materials"]], [self.material.id])
        self.assertNotIn("applicant", data)
        self.assertNotIn("location", data)
        self.assertNotIn("media", data)
        self.assertNotIn("phone", str(data).lower())

        list_response = self.client.get(
            "/pldp/api/lifecycle/after-sales/supplier-quality-notices/",
            **self.auth(self.supplier_sales_user),
        )
        self.assertEqual(list_response.status_code, 200, list_response.json())
        list_data = list_response.json()["data"]
        self.assertEqual(list_data["count"], 1)
        self.assertEqual(list_data["items"][0]["application_id"], application.id)
        self.assertEqual(
            list_data["items"][0]["material_names"], [self.material.name]
        )
        self.assertNotIn("location", str(list_data).lower())
        self.assertNotIn("media", str(list_data).lower())

        denied = self.client.get(url, **self.auth(self.direct_owner_user))
        self.assertEqual(denied.status_code, 409)
        denied_list = self.client.get(
            "/pldp/api/lifecycle/after-sales/supplier-quality-notices/",
            **self.auth(self.direct_owner_user),
        )
        self.assertEqual(denied_list.status_code, 409)

        non_material_case = AfterSalesApplication.objects.create(
            case_no="AS-NO-SUPPLIER-QUALITY-001",
            device=self.device,
            customer_organization=self.direct_merchant,
            factory_organization=self.factory,
            applicant=self.direct_owner_user,
            status=AfterSalesApplication.Status.CLOSED,
            execution_stage=AfterSalesApplication.ExecutionStage.CLOSED,
            final_fault_type=AfterSalesApplication.FaultType.INSTALLATION_OR_WIRING,
            final_solution_type=AfterSalesApplication.SolutionType.ON_SITE_ADJUSTMENT,
            repair_material_replaced=False,
            closed_at=timezone.now(),
        )
        AfterSalesService._publish_supplier_quality_notices(non_material_case)
        self.assertFalse(
            WorkItem.objects.filter(
                uniq_key=f"aftersales:{non_material_case.id}:supplier-quality:{self.supplier.id}"
            ).exists()
        )
