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
    SeniorManagerProfile,
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
    PhysicalDeviceCode,
    DriverVehicleBinding,
    VehicleDeviceAssignment,
    InstallationInvitation,
    InstallationRecord,
    LifecycleEvent,
    GoodsTransfer,
    GoodsTransferItem,
    OrganizationDevice,
    ProductDocumentRead,
)
from lifecycle.services.lifecycle_service import LifecycleService
from lifecycle.services.aftersales_service import AfterSalesService
from lifecycle.services.goods_transfer_service import (
    GoodsTransferError,
    create_goods_transfer,
    confirm_goods_transfer_receipt,
    get_goods_transfer_options,
    scan_goods_transfer_device,
    submit_goods_transfer,
)
from manufacturing.models import ProductionBatch
from product.models import (
    BOM,
    BOMItem,
    ProductCategory,
    ProductDocument,
    ProductModel,
)
from quality_trace.models import (
    BatchQualityAffectedDevice,
    BatchQualityCase,
    QualityGateDecision,
)
from quality_trace.services import place_temporary_hold, revoke_temporary_hold
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
        cls.merchant_stock_role, _ = OrganizationRole.objects.update_or_create(
            organization_type=merchant_type,
            code="merchant_stock",
            defaults={"name": "商家库管", "is_active": True},
        )
        cls.merchant_senior_manager_role, _ = OrganizationRole.objects.update_or_create(
            organization_type=merchant_type,
            code="merchant_senior_manager",
            defaults={"name": "商家高级经理", "is_active": True},
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
        outside_factory = Organization.objects.create(
            name="其他厂家", code="FACTORY-OUTSIDE", organization_type=owner_type,
        )
        cls.outside_merchant = Organization.objects.create(
            name="无关商家", code="MERCHANT-OUTSIDE", organization_type=merchant_type,
            region="四川省", factory_root=outside_factory,
        )
        cls.supplier = Organization.objects.create(
            name="测试供应商",
            code="SUPPLIER-TEST",
            organization_type=supplier_type,
            factory_root=cls.factory,
        )

        cls.factory_user = cls._user_identity(
            "factory", cls.factory, cls.factory_admin_role
        )
        cls.quality_chief = cls._user_identity(
            "quality-chief", cls.factory,
            OrganizationRole.objects.get(organization_type=owner_type, code="factory_chief_engineer"),
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
        )
        cls.material.product_categories.add(category)
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

    def _create_transfer_target(self, suffix):
        organization = Organization.objects.create(
            code=f"MERCHANT-{suffix}",
            name=f"调拨目标商家-{suffix}",
            organization_type=self.direct_merchant.organization_type,
        )
        owner = self._user_identity(
            f"transfer_target_{suffix}",
            organization,
            self.merchant_owner_role,
        )
        self._create_distribution_relation(
            inviter_user=self.regional_sales_user,
            inviter_role=self.merchant_sales_role,
            target_user=owner,
            target_org=organization,
        )
        return organization, owner

    def _create_device_quality_hold(self, suffix):
        quality_case = BatchQualityCase.objects.create(
            case_no=f"QT-R1356-{suffix}",
            factory_organization=self.factory,
            scope_type="DEVICE",
            scope_key=f"device:{self.device.pk}",
            scope_snapshot={"device_id": self.device.pk},
            reason="验证商品流转真实入口质量门禁",
            opened_by=self.factory_user,
        )
        BatchQualityAffectedDevice.objects.create(
            quality_case=quality_case,
            device=self.device,
            sn_snapshot=self.device.sn,
            production_batch_snapshot=self.device.batch.batch_no,
            product_model_snapshot={
                "id": self.device.product_model_id,
                "code": self.device.product_model.code,
                "name": self.device.product_model.name,
            },
            location_snapshot={},
            trace_evidence_snapshot={"is_fault": True},
        )
        action = place_temporary_hold(
            user=self.quality_chief,
            case_id=quality_case.pk,
            reason="验证商品流转拦截和事务外审计",
            duration_hours=1,
        )
        return quality_case, action

    def _create_direct_merchant_transfer(self, suffix):
        _target_org, target_owner = self._create_transfer_target(suffix)
        result, created = create_goods_transfer(
            user=self.regional_owner_user,
            flow_type=GoodsTransfer.FlowType.TRANSFER,
            client_request_id=f"r1356-transfer-{suffix}",
            source_account_id=self.direct_owner_user.pk,
            target_account_id=target_owner.pk,
            remark="R13.5.6质量门禁回归",
        )
        self.assertTrue(created)
        return GoodsTransfer.objects.get(pk=result["id"])

    def _create_direct_merchant_shipment(self, suffix):
        organization = Organization.objects.create(
            code=f"SHIPMENT-MERCHANT-{suffix}",
            name=f"发货目标商家-{suffix}",
            organization_type=self.direct_merchant.organization_type,
        )
        target_owner = self._user_identity(
            f"shipment_target_{suffix}",
            organization,
            self.merchant_owner_role,
        )
        self._create_distribution_relation(
            inviter_user=self.direct_sales_user,
            inviter_role=self.merchant_sales_role,
            target_user=target_owner,
            target_org=organization,
        )
        result, created = create_goods_transfer(
            user=self.direct_owner_user,
            flow_type=GoodsTransfer.FlowType.SHIPMENT,
            client_request_id=f"r1357-shipment-{suffix}",
            target_account_id=target_owner.pk,
            remark="R13.5.7发货质量门禁回归",
        )
        self.assertTrue(created)
        return GoodsTransfer.objects.get(pk=result["id"])

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
        self.assertNotIn("install_device", sales_data["capabilities"])
        self.assertIn("install_device", electrician_data["capabilities"])
        sales_install = self.client.post(
            self.install_endpoint,
            {"device_id": self.device.id, "location": "重庆市渝北区"},
            **self.auth(self.direct_sales_user),
        )
        self.assertEqual(sales_install.status_code, 403, sales_install.json())
        self.assertFalse(InstallationRecord.objects.filter(device=self.device).exists())

    def test_authorized_senior_manager_takes_over_owner_installation_capability(self):
        manager = self._user_identity(
            "merchant_install_senior_manager",
            self.direct_merchant,
            self.merchant_senior_manager_role,
        )
        manager_identity = UserOrganization.objects.get(user=manager)
        profile = SeniorManagerProfile.objects.create(identity=manager_identity)
        profile.managed_roles.add(self.merchant_electrician_role)

        manager_data = self.resolve(manager).json()["data"]
        owner_data = self.resolve(self.direct_owner_user).json()["data"]
        sales_data = self.resolve(self.direct_sales_user).json()["data"]
        electrician_data = self.resolve(self.direct_electrician_user).json()["data"]

        self.assertIn("install_device", manager_data["capabilities"])
        self.assertNotIn("install_device", owner_data["capabilities"])
        self.assertNotIn("install_device", sales_data["capabilities"])
        self.assertIn("install_device", electrician_data["capabilities"])

    def test_authorized_senior_manager_can_install_without_a_task(self):
        manager = self._user_identity(
            "merchant_install_senior_manager_action",
            self.direct_merchant,
            self.merchant_senior_manager_role,
        )
        profile = SeniorManagerProfile.objects.create(
            identity=manager.organization_identity
        )
        profile.managed_roles.add(self.merchant_electrician_role)

        response = self.client.post(
            self.install_endpoint,
            {"device_id": self.device.id, "location": "重庆市渝北区"},
            **self.auth(manager),
        )

        self.assertEqual(response.status_code, 201, response.json())
        self.assertEqual(
            InstallationRecord.objects.get(device=self.device).operator_id,
            manager.pk,
        )

    def test_stock_senior_manager_can_start_shipping_without_an_inbox_task(self):
        manager = self._user_identity(
            "merchant_shipping_senior_manager",
            self.direct_merchant,
            self.merchant_senior_manager_role,
        )
        manager_identity = UserOrganization.objects.get(user=manager)
        profile = SeniorManagerProfile.objects.create(identity=manager_identity)
        profile.managed_roles.add(self.merchant_stock_role)

        target_org = Organization.objects.create(
            code="MERCHANT-SENIOR-SHIP-TARGET",
            name="高级经理主动发货目标商家",
            organization_type=self.direct_merchant.organization_type,
        )
        target_owner = self._user_identity(
            "merchant_senior_ship_target_owner",
            target_org,
            self.merchant_owner_role,
        )
        self._create_distribution_relation(
            inviter_user=self.direct_owner_user,
            inviter_role=self.merchant_owner_role,
            target_user=target_owner,
            target_org=target_org,
        )

        with self.assertRaises(GoodsTransferError):
            get_goods_transfer_options(
                user=self.direct_owner_user,
                flow_type=GoodsTransfer.FlowType.SHIPMENT,
            )
        options = get_goods_transfer_options(
            user=manager,
            flow_type=GoodsTransfer.FlowType.SHIPMENT,
        )
        self.assertEqual(options["targets"][0]["user_id"], target_owner.pk)

        result, created = create_goods_transfer(
            user=manager,
            flow_type=GoodsTransfer.FlowType.SHIPMENT,
            client_request_id="merchant-senior-manager-proactive-shipment",
            target_account_id=target_owner.pk,
        )
        self.assertTrue(created)
        transfer = GoodsTransfer.objects.get(pk=result["id"])
        self.assertEqual(transfer.initiated_by_id, manager.pk)

    def test_stock_senior_manager_receives_collaboration_task_and_replaces_owner(self):
        self._exercise_stock_senior_manager_receipt(staffed=True)

    def test_vacant_stock_role_creates_actionable_receipt_for_senior_manager(self):
        self._exercise_stock_senior_manager_receipt(staffed=False)

    def _exercise_stock_senior_manager_receipt(self, *, staffed):
        if staffed:
            self._user_identity("independent_receipt_stock", self.direct_merchant, self.merchant_stock_role)
        manager = self._user_identity(
            "merchant_receipt_senior_manager",
            self.direct_merchant,
            self.merchant_senior_manager_role,
        )
        profile = SeniorManagerProfile.objects.create(
            identity=manager.organization_identity
        )
        profile.managed_roles.add(self.merchant_stock_role)

        OrganizationDevice.objects.filter(device=self.device).update(
            organization=self.regional_merchant,
            status="IN_STOCK",
        )
        DeviceState.objects.filter(device=self.device).update(
            current_organization=self.regional_merchant,
            status="IN_STOCK",
        )
        result, created = create_goods_transfer(
            user=self.regional_owner_user,
            flow_type=GoodsTransfer.FlowType.SHIPMENT,
            client_request_id="merchant-senior-manager-receipt-no-task",
            target_account_id=self.direct_owner_user.pk,
        )
        self.assertTrue(created)
        transfer = GoodsTransfer.objects.get(pk=result["id"])
        scan_goods_transfer_device(
            user=self.regional_owner_user,
            transfer_id=transfer.pk,
            qr_text=self.device.qrcode.code,
        )
        submit_goods_transfer(
            user=self.regional_owner_user,
            transfer_id=transfer.pk,
        )
        receipt_workitem = WorkItem.objects.get(
            type="GOODS_TRANSFER_RECEIPT_CONFIRM",
            uniq_key=f"lifecycle:goods-transfer:{transfer.pk}:receipt",
        )
        self.assertTrue(receipt_workitem.user_tasks.filter(
            to_openid=manager.wx_openid, need_action=True,
        ).exists())

        with self.assertRaises(GoodsTransferError):
            confirm_goods_transfer_receipt(
                user=self.direct_owner_user,
                transfer_id=transfer.pk,
            )

        _result, changed = confirm_goods_transfer_receipt(
            user=manager,
            transfer_id=transfer.pk,
        )
        self.assertTrue(changed)
        transfer.refresh_from_db()
        self.assertEqual(transfer.received_by_id, manager.pk)

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

    def _mark_required_documents_read(self, user):
        documents = ProductDocument.objects.filter(
            product_model=self.device.product_model,
            is_active=True,
        )
        self.assertEqual(documents.count(), 3)
        for document in documents:
            ProductDocumentRead.objects.get_or_create(
                user=user,
                device=self.device,
                document=document,
                document_version=document.version,
            )

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
        self._mark_required_documents_read(self.customer_user)

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

    def test_quality_hold_blocks_real_activation_and_preserves_audit(self):
        token = self._install_and_token()
        accept_response = self.client.post(
            "/pldp/api/lifecycle/scan/customer-invitations/"
            f"{token}/accept/",
            self._accept_payload(),
            **self.auth(self.customer_user),
        )
        self.assertEqual(accept_response.status_code, 201)
        self._mark_required_documents_read(self.customer_user)

        quality_case = BatchQualityCase.objects.create(
            case_no="QT-R135-ACTIVATION-GATE",
            factory_organization=self.factory,
            scope_type="DEVICE",
            scope_key=f"device:{self.device.pk}",
            scope_snapshot={"device_id": self.device.pk},
            reason="验证真实激活入口质量门禁",
            opened_by=self.factory_user,
        )
        BatchQualityAffectedDevice.objects.create(
            quality_case=quality_case,
            device=self.device,
            sn_snapshot=self.device.sn,
            production_batch_snapshot=self.device.batch.batch_no,
            product_model_snapshot={
                "id": self.device.product_model_id,
                "code": self.device.product_model.code,
                "name": self.device.product_model.name,
            },
            location_snapshot={},
            trace_evidence_snapshot={"is_fault": True},
        )
        action = place_temporary_hold(
            user=self.quality_chief,
            case_id=quality_case.pk,
            reason="验证激活入口阻断和事务外审计",
            duration_hours=1,
        )

        result_url = f"/pldp/api/lifecycle/customer/devices/{self.device.id}/"
        activate = self.client.post(
            f"{result_url}activate/",
            {"vehicle_no": "渝A12345", "location": "重庆市渝北区"},
            **self.auth(self.customer_user),
        )
        self.assertEqual(activate.status_code, 409, activate.json())
        self.assertIn(quality_case.case_no, activate.json()["msg"])
        self.assertIn("门禁审计", activate.json()["msg"])

        state = DeviceState.objects.get(device=self.device)
        self.assertEqual(state.activation_status, "NOT_ACTIVATED")
        self.assertFalse(
            LifecycleEvent.objects.filter(
                device=self.device,
                event_type="ACTIVATE",
            ).exists()
        )
        decision = QualityGateDecision.objects.get(
            device=self.device,
            operation="ACTIVATION",
            allowed=False,
        )
        self.assertEqual(decision.quality_case_id, quality_case.pk)
        self.assertEqual(decision.control_action_id, action.pk)
        self.assertEqual(
            decision.context_snapshot["source"],
            "CUSTOMER_DEVICE_ACTIVATION",
        )

    def test_quality_hold_blocks_goods_transfer_scan_and_preserves_audit(self):
        transfer = self._create_direct_merchant_transfer("scan")
        quality_case, action = self._create_device_quality_hold("SCAN")

        with self.assertRaisesMessage(GoodsTransferError, quality_case.case_no):
            scan_goods_transfer_device(
                user=self.regional_owner_user,
                transfer_id=transfer.pk,
                qr_text=self.device.qrcode.code,
            )

        self.assertFalse(GoodsTransferItem.objects.filter(transfer=transfer).exists())
        self.assertEqual(
            OrganizationDevice.objects.get(device=self.device).organization_id,
            self.direct_merchant.pk,
        )
        decision = QualityGateDecision.objects.get(
            device=self.device,
            operation="TRANSFER",
            allowed=False,
        )
        self.assertEqual(decision.quality_case_id, quality_case.pk)
        self.assertEqual(decision.control_action_id, action.pk)
        self.assertEqual(decision.context_snapshot["source"], "GOODS_TRANSFER_SCAN")

    def test_quality_hold_rechecks_goods_transfer_submit_and_rolls_back(self):
        transfer = self._create_direct_merchant_transfer("submit")
        scan_goods_transfer_device(
            user=self.regional_owner_user,
            transfer_id=transfer.pk,
            qr_text=self.device.qrcode.code,
        )
        quality_case, action = self._create_device_quality_hold("SUBMIT")

        with self.assertRaisesMessage(GoodsTransferError, quality_case.case_no):
            submit_goods_transfer(
                user=self.regional_owner_user,
                transfer_id=transfer.pk,
            )

        transfer.refresh_from_db()
        item = transfer.items.get(device=self.device)
        self.assertEqual(transfer.status, GoodsTransfer.Status.SCANNING)
        self.assertEqual(item.status, GoodsTransferItem.Status.SCANNED)
        self.assertIsNone(item.lifecycle_event_id)
        self.assertEqual(
            OrganizationDevice.objects.get(device=self.device).organization_id,
            self.direct_merchant.pk,
        )
        decision = QualityGateDecision.objects.get(
            device=self.device,
            operation="TRANSFER",
            allowed=False,
        )
        self.assertEqual(decision.control_action_id, action.pk)
        self.assertEqual(decision.context_snapshot["source"], "GOODS_TRANSFER_SUBMIT")

        revoke_temporary_hold(
            user=self.quality_chief,
            action_id=action.pk,
            reason="提交二次门禁回归完成",
        )
        result, changed = submit_goods_transfer(
            user=self.regional_owner_user,
            transfer_id=transfer.pk,
        )
        self.assertTrue(changed)
        self.assertEqual(result["status"], GoodsTransfer.Status.COMPLETED)
        self.assertEqual(
            OrganizationDevice.objects.get(device=self.device).organization_id,
            transfer.to_organization_id,
        )

    def test_quality_hold_allows_return_goods_transfer_to_recall_device(self):
        quality_case, action = self._create_device_quality_hold("RETURN")
        result, created = create_goods_transfer(
            user=self.direct_owner_user,
            flow_type=GoodsTransfer.FlowType.RETURN,
            client_request_id="r1356-return-bypass",
            target_account_id=self.regional_sales_user.pk,
            remark="质量召回设备向上级退回",
        )
        self.assertTrue(created)
        transfer = GoodsTransfer.objects.get(pk=result["id"])

        scan_result = scan_goods_transfer_device(
            user=self.direct_owner_user,
            transfer_id=transfer.pk,
            qr_text=self.device.qrcode.code,
        )
        self.assertEqual(scan_result["item_count"], 1)
        self.assertFalse(
            QualityGateDecision.objects.filter(
                device=self.device,
                operation="TRANSFER",
            ).exists()
        )
        action.refresh_from_db()
        quality_case.refresh_from_db()
        self.assertEqual(action.status, "ACTIVE")
        self.assertEqual(quality_case.status, "HELD")

    def test_quality_hold_blocks_goods_shipment_scan(self):
        transfer = self._create_direct_merchant_shipment("scan")
        quality_case, action = self._create_device_quality_hold("SHIPMENT-SCAN")

        with self.assertRaisesMessage(GoodsTransferError, quality_case.case_no):
            scan_goods_transfer_device(
                user=self.direct_owner_user,
                transfer_id=transfer.pk,
                qr_text=self.device.qrcode.code,
            )

        self.assertFalse(transfer.items.exists())
        decision = QualityGateDecision.objects.get(
            device=self.device,
            operation="SHIPMENT",
            allowed=False,
        )
        self.assertEqual(decision.control_action_id, action.pk)
        self.assertEqual(decision.context_snapshot["source"], "GOODS_TRANSFER_SCAN")
        self.assertEqual(
            OrganizationDevice.objects.get(device=self.device).organization_id,
            self.direct_merchant.pk,
        )

    def test_quality_hold_rechecks_goods_shipment_submit_then_resumes(self):
        transfer = self._create_direct_merchant_shipment("submit")
        scan_goods_transfer_device(
            user=self.direct_owner_user,
            transfer_id=transfer.pk,
            qr_text=self.device.qrcode.code,
        )
        quality_case, action = self._create_device_quality_hold("SHIPMENT-SUBMIT")

        with self.assertRaisesMessage(GoodsTransferError, quality_case.case_no):
            submit_goods_transfer(
                user=self.direct_owner_user,
                transfer_id=transfer.pk,
            )

        transfer.refresh_from_db()
        item = transfer.items.get(device=self.device)
        state = DeviceState.objects.get(device=self.device)
        self.assertEqual(transfer.status, GoodsTransfer.Status.SCANNING)
        self.assertEqual(item.status, GoodsTransferItem.Status.SCANNED)
        self.assertIsNone(item.lifecycle_event_id)
        self.assertEqual(state.status, "IN_STOCK")
        decision = QualityGateDecision.objects.get(
            device=self.device,
            operation="SHIPMENT",
            allowed=False,
        )
        self.assertEqual(decision.control_action_id, action.pk)
        self.assertEqual(decision.context_snapshot["source"], "GOODS_TRANSFER_SUBMIT")

        revoke_temporary_hold(
            user=self.quality_chief,
            action_id=action.pk,
            reason="发货提交二次门禁回归完成",
        )
        result, changed = submit_goods_transfer(
            user=self.direct_owner_user,
            transfer_id=transfer.pk,
        )
        self.assertTrue(changed)
        self.assertEqual(result["status"], GoodsTransfer.Status.PENDING_RECEIPT)
        transfer.refresh_from_db()
        item.refresh_from_db()
        state.refresh_from_db()
        self.assertEqual(item.status, GoodsTransferItem.Status.PENDING_RECEIPT)
        self.assertEqual(state.status, "SHIPPED")
        self.assertEqual(
            OrganizationDevice.objects.get(device=self.device).organization_id,
            self.direct_merchant.pk,
        )

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
        self._mark_required_documents_read(driver)

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
        self._exercise_after_sales_factory_workorder(staffed=True)

    def test_vacant_aftersales_role_routes_submission_to_granted_manager(self):
        self._exercise_after_sales_factory_workorder(staffed=False)

    def _exercise_after_sales_factory_workorder(self, *, staffed):
        token = self._install_and_token()
        accepted = self.client.post(
            f"/pldp/api/lifecycle/scan/customer-invitations/{token}/accept/",
            self._accept_payload(),
            **self.auth(self.customer_user),
        )
        self.assertEqual(accepted.status_code, 201)
        self._mark_required_documents_read(self.customer_user)
        activated = self.client.post(
            f"/pldp/api/lifecycle/customer/devices/{self.device.id}/activate/",
            {"activation_method": "VEHICLE_PLATE", "vehicle_no": "渝A12345"},
            **self.auth(self.customer_user),
        )
        self.assertEqual(activated.status_code, 200, activated.json())

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
        factory_aftersales_role, _ = OrganizationRole.objects.update_or_create(
            organization_type=self.factory.organization_type,
            code="factory_aftersales",
            defaults={"name": "厂家售后经理", "is_active": True},
        )
        factory_senior_manager_role, _ = OrganizationRole.objects.update_or_create(
            organization_type=self.factory.organization_type,
            code="factory_senior_manager",
            defaults={"name": "厂家高级经理", "is_active": True},
        )
        factory_senior_manager = User.objects.create_user(
            username="aftersales_factory_senior_manager",
            wx_openid="openid-aftersales-factory-senior-manager",
        )
        senior_identity = UserOrganization.objects.create(
            user=factory_senior_manager,
            organization=self.factory,
            role=factory_senior_manager_role,
        )
        senior_profile = SeniorManagerProfile.objects.create(
            identity=senior_identity
        )
        senior_profile.managed_roles.add(factory_aftersales_role)
        factory_aftersales = User.objects.create_user(
            username="aftersales_factory_aftersales",
            wx_openid="openid-aftersales-factory-aftersales",
        )
        UserOrganization.objects.create(
            user=factory_aftersales,
            organization=self.factory,
            role=factory_aftersales_role,
            inviter=factory_senior_manager,
            is_active=staffed,
        )

        factory_actor = factory_aftersales if staffed else factory_senior_manager

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
            plate_upload = self.client.post(
                f"/pldp/api/lifecycle/after-sales/applications/{application_id}/media/",
                {
                    "media_type": "IMAGE",
                    "purpose": "PLATE_PRESENCE_EVIDENCE",
                    "file": SimpleUploadedFile(
                        "plate.jpg", b"\xff\xd8\xff\xe0test-plate", content_type="image/jpeg"
                    ),
                },
                **self.auth(self.customer_user),
            )
            self.assertEqual(plate_upload.status_code, 201, plate_upload.json())
            submitted = self.client.post(
                f"/pldp/api/lifecycle/after-sales/applications/{application_id}/submit/",
                {},
                content_type="application/json",
                **self.auth(self.customer_user),
            )
            self.assertEqual(submitted.status_code, 200)
            detail = self.client.get(
                f"/pldp/api/lifecycle/after-sales/applications/{application_id}/",
                **self.auth(factory_actor),
            )
            self.assertEqual(detail.status_code, 200)
            factory_detail = detail.json()["data"]
            self.assertEqual(len(factory_detail["media"]), 3)
            self.assertEqual(factory_detail["viewer_stage"]["mode"], "ACTION_REQUIRED")
            self.assertEqual(
                factory_detail["viewer_stage"]["badge"],
                "待您认定并分派",
            )
            self.assertTrue(factory_detail["actions"]["can_triage"])
            self.assertEqual(factory_detail["applicant_instruction"], {})

            senior_detail = self.client.get(
                f"/pldp/api/lifecycle/after-sales/applications/{application_id}/",
                **self.auth(factory_senior_manager),
            )
            self.assertEqual(senior_detail.status_code, 200)
            self.assertTrue(senior_detail.json()["data"]["actions"]["can_triage"])

            sales_detail = self.client.get(
                f"/pldp/api/lifecycle/after-sales/applications/{application_id}/",
                **self.auth(factory_sales),
            )
            self.assertEqual(sales_detail.status_code, 200)
            self.assertFalse(sales_detail.json()["data"]["actions"]["can_triage"])

            other_factory = Organization.objects.create(
                code="AFTERSALES-OTHER-FACTORY",
                name="其他厂家根组织",
                organization_type=self.factory.organization_type,
            )
            other_factory_aftersales = self._user_identity(
                "aftersales_other_factory",
                other_factory,
                factory_aftersales_role,
            )
            other_factory_detail = self.client.get(
                f"/pldp/api/lifecycle/after-sales/applications/{application_id}/",
                **self.auth(other_factory_aftersales),
            )
            self.assertNotEqual(other_factory_detail.status_code, 200)

            applicant_detail = self.client.get(
                f"/pldp/api/lifecycle/after-sales/applications/{application_id}/",
                **self.auth(self.customer_user),
            )
            self.assertEqual(applicant_detail.status_code, 200)
            self.assertEqual(
                applicant_detail.json()["data"]["viewer_stage"]["title"],
                "等待厂家售后经理判断并指派",
            )
            self.assertEqual(
                applicant_detail.json()["data"]["viewer_stage"]["badge"],
                "等待厂家售后经理判断并指派",
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
        self.assertEqual(
            UserTask.objects.filter(
                workitem__type="AFTERSALES_FACTORY_WORKORDER",
                to_openid=factory_aftersales.wx_openid,
                need_action=True,
            ).exists(),
            staffed,
        )
        self.assertFalse(
            UserTask.objects.filter(
                workitem__type="AFTERSALES_FACTORY_WORKORDER",
                to_openid=factory_sales.wx_openid,
            ).exists()
        )
        legacy_sales_task = UserTask.objects.create(
            workitem=WorkItem.objects.get(
                uniq_key=f"aftersales:{application_id}:factory-workorder"
            ),
            to_openid=factory_sales.wx_openid,
            need_action=True,
        )
        legacy_sales_detail = self.client.get(
            f"/pldp/api/lifecycle/after-sales/applications/{application_id}/",
            **self.auth(factory_sales),
        )
        self.assertEqual(legacy_sales_detail.status_code, 200)
        self.assertTrue(legacy_sales_detail.json()["data"]["actions"]["can_triage"])
        senior_task = UserTask.objects.get(
            workitem__type="AFTERSALES_FACTORY_WORKORDER",
            to_openid=factory_senior_manager.wx_openid,
        )
        self.assertTrue(senior_task.need_action)
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

        triage_url = f"/pldp/api/lifecycle/after-sales/applications/{application_id}/triage/"
        triage_payload = {
            "preliminary_fault_type": "INSTALLATION_OR_WIRING",
            "diagnosis_note": "根据故障视频判断为接线端子松动。",
            "repair_plan": "检查接线端子后重新启动测试。",
        }
        review = self.client.get(
            f"/pldp/api/lifecycle/after-sales/applications/{application_id}/",
            **self.auth(factory_actor),
        ).json()["data"]
        self.assertEqual(
            [option["value"] for option in review["triage_options"]["routes"]],
            ["ORIGINAL_INSTALLER", "SERVICE_STATION", "FACTORY_AFTERSALES"],
        )
        for route in ("REMOTE", "THIRD_PARTY", "MERCHANT_ELECTRICIAN"):
            with self.subTest(route=route):
                response = self.client.post(
                    triage_url,
                    {**triage_payload, "dispatch_route": route},
                    content_type="application/json",
                    **self.auth(factory_actor),
                )
                self.assertEqual(response.status_code, 409, response.json())
                self.assertEqual(response.json()["msg"], "请选择有效的售后处理路径")
        application.refresh_from_db()
        self.assertEqual(application.status, AfterSalesApplication.Status.SUBMITTED)
        self.assertEqual(application.dispatch_route, "")
        senior_task.refresh_from_db()
        self.assertTrue(senior_task.need_action)
        self.assertNotEqual(senior_task.state, "DONE")
        legacy_sales_task.refresh_from_db()
        self.assertNotEqual(legacy_sales_task.state, "DONE")

        # Start at final attribution to exercise remote guidance as a closing result.
        application.dispatch_route = AfterSalesApplication.DispatchRoute.FACTORY_AFTERSALES
        application.status = AfterSalesApplication.Status.PENDING_FACTORY_CLOSE
        application.execution_stage = AfterSalesApplication.ExecutionStage.FINAL_DIAGNOSIS
        application.save(update_fields=("dispatch_route", "status", "execution_stage"))
        closed = self.client.post(
            f"/pldp/api/lifecycle/after-sales/applications/{application_id}/factory-close/",
            {
                "final_fault_type": "INSTALLATION_OR_WIRING",
                "final_fault_materials": [],
                "final_solution_type": "REMOTE_GUIDANCE",
                "final_solution_note": "已远程指导复紧接线端子，设备恢复正常。",
            },
            content_type="application/json",
            **self.auth(factory_actor),
        )
        self.assertEqual(closed.status_code, 200, closed.json())
        senior_task.refresh_from_db()
        self.assertEqual(senior_task.state, "DONE")
        self.assertEqual(senior_task.done_reason, "OTHER_ACTION" if staffed else "SELF_ACTION")
        legacy_sales_task.refresh_from_db()
        self.assertEqual(legacy_sales_task.state, "DONE")
        self.assertEqual(legacy_sales_task.done_reason, "OTHER_ACTION")
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
                event_type="FACTORY_CLOSED"
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

    def test_device_code_activation_and_aftersales_presence_scan(self):
        from lifecycle.models import ActivationRecord, Device, PhysicalDeviceCode

        token = self._install_and_token()
        accepted = self.client.post(
            f"/pldp/api/lifecycle/scan/customer-invitations/{token}/accept/",
            self._accept_payload(),
            **self.auth(self.customer_user),
        )
        self.assertEqual(accepted.status_code, 201, accepted.json())

        self._mark_required_documents_read(self.customer_user)
        device_url = f"/pldp/api/lifecycle/customer/devices/{self.device.id}/"
        verify_activation_code_url = (
            f"{device_url}activation-code/verify/"
        )
        unknown_code = self.client.post(
            verify_activation_code_url,
            {"device_code": "PLDP_DEVICE|DC-UNREGISTERED-0001"},
            content_type="application/json",
            **self.auth(self.customer_user),
        )
        self.assertEqual(unknown_code.status_code, 409, unknown_code.json())
        self.assertIn("未在厂家编码库登记", unknown_code.json()["msg"])

        bypass_verification = self.client.post(
            f"{device_url}activate/",
            {
                "activation_method": "DEVICE_CODE",
                "physical_device_code": "PLDP_DEVICE|DC-UNREGISTERED-0001",
            },
            content_type="application/json",
            **self.auth(self.customer_user),
        )
        self.assertEqual(
            bypass_verification.status_code,
            409,
            bypass_verification.json(),
        )
        self.assertIn("未在厂家编码库登记", bypass_verification.json()["msg"])

        lifecycle_code = self.client.post(
            verify_activation_code_url,
            {"device_code": self.device.qrcode.code},
            content_type="application/json",
            **self.auth(self.customer_user),
        )
        self.assertEqual(lifecycle_code.status_code, 409, lifecycle_code.json())
        self.assertIn("包装生命周期二维码", lifecycle_code.json()["msg"])

        other_factory = Organization.objects.create(
            name="另一个测试厂家",
            code="FACTORY-OTHER-DEVICE-CODE",
            organization_type=self.factory.organization_type,
            region="全国",
        )
        foreign_code = PhysicalDeviceCode.objects.create(
            factory_root=other_factory,
            code="BODY-QR-OTHER-0001",
        )
        wrong_factory_code = self.client.post(
            verify_activation_code_url,
            {"device_code": foreign_code.qr_payload},
            content_type="application/json",
            **self.auth(self.customer_user),
        )
        self.assertEqual(wrong_factory_code.status_code, 409, wrong_factory_code.json())
        self.assertIn("不属于此设备对应的厂家", wrong_factory_code.json()["msg"])

        registered_code = PhysicalDeviceCode.objects.create(
            factory_root=self.factory,
            code="BODY-QR-0001",
            generated_by=self.factory_user,
        )
        verified_before_binding = self.client.post(
            verify_activation_code_url,
            {"device_code": "PLDP_DEVICE|BODY-QR-0001"},
            content_type="application/json",
            **self.auth(self.customer_user),
        )
        self.assertEqual(
            verified_before_binding.status_code,
            200,
            verified_before_binding.json(),
        )
        self.assertTrue(verified_before_binding.json()["data"]["verified"])
        self.assertEqual(
            verified_before_binding.json()["data"]["device_code"],
            "BODY-QR-0001",
        )
        registered_code.refresh_from_db()
        self.assertEqual(registered_code.status, PhysicalDeviceCode.Status.GENERATED)
        self.assertIsNone(registered_code.device_id)
        activated = self.client.post(
            f"{device_url}activate/",
            {
                "activation_method": "DEVICE_CODE",
                "physical_device_code": "PLDP_DEVICE_CODE|BODY-QR-0001",
            },
            **self.auth(self.customer_user),
        )
        self.assertEqual(activated.status_code, 200, activated.json())
        self.device.refresh_from_db()
        activation = ActivationRecord.objects.get(device=self.device)
        self.assertEqual(self.device.physical_device_code, "BODY-QR-0001")
        self.assertEqual(activation.activation_method, "DEVICE_CODE")
        self.assertEqual(activation.vehicle_no, "")
        self.assertEqual(activation.physical_device_code_snapshot, "BODY-QR-0001")
        registered_code.refresh_from_db()
        self.assertEqual(registered_code.status, PhysicalDeviceCode.Status.BOUND)
        self.assertEqual(registered_code.device_id, self.device.id)

        # The on-device code must resolve to the same lifecycle record after
        # activation while preserving the packaging QR as canonical code.
        scan_response = self.client.post(
            "/pldp/api/lifecycle/scan/resolve/",
            {"qr_text": "PLDP_DEVICE|BODY-QR-0001"},
            content_type="application/json",
            **self.auth(self.customer_user),
        )
        self.assertEqual(scan_response.status_code, 200, scan_response.json())
        scan_data = scan_response.json()["data"]
        self.assertEqual(scan_data["device"]["id"], self.device.id)
        self.assertEqual(scan_data["qr_code"], self.device.qrcode.code)
        self.assertEqual(
            scan_data["device"]["physical_device_code"], "BODY-QR-0001"
        )

        context_url = (
            f"/pldp/api/lifecycle/customer/devices/{self.device.id}/after-sales/context/"
        )
        context = self.client.get(context_url, **self.auth(self.customer_user))
        self.assertEqual(context.status_code, 200, context.json())
        self.assertEqual(context.json()["data"]["activation"]["activation_method"], "DEVICE_CODE")

        verify_url = (
            f"/pldp/api/lifecycle/customer/devices/{self.device.id}/"
            "after-sales/verify-device-code/"
        )
        mismatch = self.client.post(
            verify_url,
            {"device_code": "WRONG-CODE"},
            content_type="application/json",
            **self.auth(self.customer_user),
        )
        self.assertEqual(mismatch.status_code, 409)

        draft_url = (
            f"/pldp/api/lifecycle/customer/devices/{self.device.id}/after-sales/applications/"
        )
        draft_data = {
            "fault_description": "设备无法启动",
            "location_name": "测试现场",
            "location_address": "重庆市渝北区",
            "location_latitude": "29.6000000",
            "location_longitude": "106.5000000",
            "location_confirmed": True,
        }
        blocked = self.client.post(
            draft_url,
            draft_data,
            content_type="application/json",
            **self.auth(self.customer_user),
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertFalse(AfterSalesApplication.objects.filter(device=self.device).exists())

        verified = self.client.post(
            verify_url,
            {"device_code": "BODY-QR-0001"},
            content_type="application/json",
            **self.auth(self.customer_user),
        )
        self.assertEqual(verified.status_code, 200, verified.json())
        draft_data["device_code_verification_token"] = verified.json()["data"][
            "verification_token"
        ]
        created = self.client.post(
            draft_url,
            draft_data,
            content_type="application/json",
            **self.auth(self.customer_user),
        )
        self.assertEqual(created.status_code, 201, created.json())
        application = AfterSalesApplication.objects.get(device=self.device)
        self.assertIsNotNone(application.device_presence_verified_at)
        self.assertEqual(application.device_presence_code_snapshot, "BODY-QR-0001")
        self.assertEqual(application.activation_snapshot["activation_method"], "DEVICE_CODE")

        # Reopening a saved draft requires a fresh scan; the proof is consumed
        # exactly once and refreshes the server-side presence timestamp.
        from lifecycle.models import AfterSalesDeviceCodeVerification

        second_verified = self.client.post(
            verify_url,
            {"device_code": "BODY-QR-0001"},
            content_type="application/json",
            **self.auth(self.customer_user),
        )
        self.assertEqual(second_verified.status_code, 200, second_verified.json())
        second_token = second_verified.json()["data"]["verification_token"]
        resumed = self.client.post(
            draft_url,
            {**draft_data, "device_code_verification_token": second_token},
            content_type="application/json",
            **self.auth(self.customer_user),
        )
        self.assertEqual(resumed.status_code, 200, resumed.json())
        proof = AfterSalesDeviceCodeVerification.objects.get(token=second_token)
        self.assertIsNotNone(proof.used_at)
        replayed = self.client.post(
            draft_url,
            {**draft_data, "device_code_verification_token": second_token},
            content_type="application/json",
            **self.auth(self.customer_user),
        )
        self.assertEqual(replayed.status_code, 409)
        self.assertIn("核验已失效", replayed.json()["msg"])

    def test_factory_can_generate_pre_registered_physical_device_code_batch(self):
        from lifecycle.models import PhysicalDeviceCode

        response = self.client.post(
            "/pldp/api/lifecycle/factory/device-codes/batches/",
            {"quantity": 3},
            content_type="application/json",
            **self.auth(self.factory_user),
        )
        self.assertEqual(response.status_code, 201, response.json())
        data = response.json()["data"]
        self.assertEqual(data["count"], 3)
        codes = [item["code"] for item in data["codes"]]
        self.assertEqual(len(set(codes)), 3)
        self.assertTrue(all(item["qr_payload"] == f"PLDP_DEVICE|{item['code']}" for item in data["codes"]))
        records = list(PhysicalDeviceCode.objects.filter(code__in=codes))
        self.assertEqual(len(records), 3)
        self.assertTrue(all(record.factory_root_id == self.factory.pk for record in records))
        self.assertTrue(all(record.status == PhysicalDeviceCode.Status.GENERATED for record in records))

        denied = self.client.post(
            "/pldp/api/lifecycle/factory/device-codes/batches/",
            {"quantity": 1},
            content_type="application/json",
            **self.auth(self.direct_owner_user),
        )
        self.assertEqual(denied.status_code, 403, denied.json())

    def test_plate_activation_requires_a_camera_photo_for_aftersales(self):
        token = self._install_and_token()
        accepted = self.client.post(
            f"/pldp/api/lifecycle/scan/customer-invitations/{token}/accept/",
            self._accept_payload(),
            **self.auth(self.customer_user),
        )
        self.assertEqual(accepted.status_code, 201, accepted.json())
        self._mark_required_documents_read(self.customer_user)

        device_url = f"/pldp/api/lifecycle/customer/devices/{self.device.id}/"
        activated = self.client.post(
            f"{device_url}activate/",
            {"activation_method": "VEHICLE_PLATE", "vehicle_no": "渝A12345"},
            **self.auth(self.customer_user),
        )
        self.assertEqual(activated.status_code, 200, activated.json())

        draft = self.client.post(
            f"{device_url}after-sales/applications/",
            {
                "fault_description": "设备无法启动",
                "location_name": "测试现场",
                "location_address": "重庆市渝北区",
                "location_latitude": "29.6000000",
                "location_longitude": "106.5000000",
                "location_confirmed": True,
            },
            content_type="application/json",
            **self.auth(self.customer_user),
        )
        self.assertEqual(draft.status_code, 201, draft.json())
        application_id = draft.json()["data"]["id"]

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            evidence = self.client.post(
                f"/pldp/api/lifecycle/after-sales/applications/{application_id}/media/",
                {
                    "media_type": "IMAGE",
                    "purpose": "APPLICATION_EVIDENCE",
                    "file": SimpleUploadedFile(
                        "fault.jpg", b"\xff\xd8\xff\xe0test-fault", content_type="image/jpeg"
                    ),
                },
                **self.auth(self.customer_user),
            )
            self.assertEqual(evidence.status_code, 201, evidence.json())
            blocked = self.client.post(
                f"/pldp/api/lifecycle/after-sales/applications/{application_id}/submit/",
                {},
                content_type="application/json",
                **self.auth(self.customer_user),
            )
            self.assertEqual(blocked.status_code, 409)
            self.assertIn("车牌号", blocked.json()["msg"])

            plate_photo = self.client.post(
                f"/pldp/api/lifecycle/after-sales/applications/{application_id}/media/",
                {
                    "media_type": "IMAGE",
                    "purpose": "PLATE_PRESENCE_EVIDENCE",
                    "file": SimpleUploadedFile(
                        "plate.jpg", b"\xff\xd8\xff\xe0test-plate", content_type="image/jpeg"
                    ),
                },
                **self.auth(self.customer_user),
            )
            self.assertEqual(plate_photo.status_code, 201, plate_photo.json())
            self.assertEqual(plate_photo.json()["data"]["purpose"], "PLATE_PRESENCE_EVIDENCE")
            # With the required plate photo present, validation advances to the
            # separate factory-recipient check instead of failing the proof gate.
            advanced = self.client.post(
                f"/pldp/api/lifecycle/after-sales/applications/{application_id}/submit/",
                {},
                content_type="application/json",
                **self.auth(self.customer_user),
            )
            self.assertEqual(advanced.status_code, 409)
            self.assertIn("厂家尚未配置", advanced.json()["msg"])

    def test_unactivated_customer_device_cannot_create_aftersales_draft(self):
        token = self._install_and_token()
        auth = self.auth(self.customer_user)
        accepted = self.client.post(
            f"/pldp/api/lifecycle/scan/customer-invitations/{token}/accept/",
            self._accept_payload(),
            **auth,
        )
        self.assertEqual(accepted.status_code, 201, accepted.json())

        device_url = f"/pldp/api/lifecycle/customer/devices/{self.device.id}/"
        device = self.client.get(device_url, **auth)
        self.assertEqual(device.status_code, 200, device.json())
        self.assertFalse(device.json()["data"]["actions"]["can_apply_aftersale"])
        draft = self.client.post(
            f"{device_url}after-sales/applications/",
            {"fault_description": "设备无法启动"},
            content_type="application/json",
            **auth,
        )
        self.assertEqual(draft.status_code, 409, draft.json())
        self.assertIn("尚未激活", draft.json()["msg"])
        self.assertFalse(AfterSalesApplication.objects.filter(device=self.device).exists())

    def test_revoked_driver_cannot_upload_or_submit_aftersales_draft(self):
        token = self._install_and_token()
        accepted = self.client.post(
            f"/pldp/api/lifecycle/scan/customer-invitations/{token}/accept/",
            self._accept_payload(),
            **self.auth(self.customer_user),
        )
        self.assertEqual(accepted.status_code, 201, accepted.json())
        binding = CustomerDeviceBinding.objects.get(
            device=self.device, status=CustomerDeviceBinding.Status.ACTIVE,
        )
        driver = self._user_identity(
            "aftersales_draft_driver", binding.customer_organization, self.driver_role,
        )
        driver_binding = DriverVehicleBinding.objects.create(
            vehicle=binding.vehicle, driver=driver,
        )
        factory_role, _ = OrganizationRole.objects.update_or_create(
            organization_type=self.factory.organization_type,
            code="factory_aftersales",
            defaults={"name": "厂家售后经理", "is_active": True},
        )
        factory_aftersales = self._user_identity(
            "aftersales_draft_factory", self.factory, factory_role,
        )
        self._mark_required_documents_read(driver)
        auth = self.auth(driver)
        device_url = f"/pldp/api/lifecycle/customer/devices/{self.device.id}/"
        activated = self.client.post(
            f"{device_url}activate/",
            {"activation_method": "VEHICLE_PLATE", "vehicle_no": "渝A12345"},
            **auth,
        )
        self.assertEqual(activated.status_code, 200, activated.json())
        draft = self.client.post(
            f"{device_url}after-sales/applications/",
            {
                "fault_description": "设备无法启动",
                "location_name": "测试现场",
                "location_address": "重庆市渝北区",
                "location_latitude": "29.6000000",
                "location_longitude": "106.5000000",
                "location_confirmed": True,
            },
            content_type="application/json",
            **auth,
        )
        self.assertEqual(draft.status_code, 201, draft.json())
        application = AfterSalesApplication.objects.get(pk=draft.json()["data"]["id"])
        url = f"/pldp/api/lifecycle/after-sales/applications/{application.pk}/"

        def upload(purpose):
            return self.client.post(
                f"{url}media/",
                {
                    "media_type": "IMAGE",
                    "purpose": purpose,
                    "file": SimpleUploadedFile(
                        "evidence.jpg", b"\xff\xd8\xff\xe0test-image", content_type="image/jpeg"
                    ),
                },
                **auth,
            )

        with tempfile.TemporaryDirectory() as media_root, override_settings(
            AFTERSALES_PRIVATE_ROOT=media_root,
        ):
            purposes = ("APPLICATION_EVIDENCE", "PLATE_PRESENCE_EVIDENCE")
            for purpose in purposes:
                uploaded = upload(purpose)
                self.assertEqual(uploaded.status_code, 201, uploaded.json())
            event_count = application.events.count()
            driver_binding.is_active = False
            driver_binding.save(update_fields=("is_active",))

            for purpose in purposes:
                blocked = upload(purpose)
                self.assertEqual(blocked.status_code, 409, blocked.json())
                self.assertIn("获授权车辆", blocked.json()["msg"])
            blocked = self.client.post(
                f"{url}submit/", {}, content_type="application/json", **auth,
            )
            self.assertEqual(blocked.status_code, 409, blocked.json())
            self.assertIn("获授权车辆", blocked.json()["msg"])
            application.refresh_from_db()
            self.assertEqual(application.status, AfterSalesApplication.Status.DRAFT)
            self.assertEqual(application.media.count(), 2)
            self.assertEqual(application.events.count(), event_count)
            self.assertFalse(WorkItem.objects.filter(
                domain="AFTERSALE", biz_id=str(application.pk),
            ).exists())

            driver_binding.is_active = True
            driver_binding.save(update_fields=("is_active",))
            submitted = self.client.post(
                f"{url}submit/", {}, content_type="application/json", **auth,
            )
            self.assertEqual(submitted.status_code, 200, submitted.json())
            factory_detail = self.client.get(url, **self.auth(factory_aftersales))
            self.assertEqual(factory_detail.status_code, 200, factory_detail.json())
