from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from django.core.files.uploadedfile import SimpleUploadedFile
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
from lifecycle.models import (
    DeviceMaterialFaultRecord,
    DeviceMaterialWarrantyEvaluation,
    DeviceState,
    FactoryReturnDocument,
    FactoryReturnInventoryItem,
    FactoryReturnMaterialReturnRecord,
    FactoryReturnProcess,
    FactoryReturnRepairMaterialRequirement,
    FactoryReturnWarrantyDispatch,
    GoodsTransfer,
    GoodsTransferItem,
    GoodsTransferAccountCase,
    GoodsTransferCreditAllocation,
    LifecycleEvent,
    OrganizationDevice,
    QRCode,
)
from lifecycle.services.lifecycle_service import LifecycleService
from lifecycle.services.factory_return_service import (
    _original_supplier_task_recipients,
)
from manufacturing.models import ProductionBatch
from product.models import BOM, BOMItem, ProductCategory, ProductModel
from supply_chain.models import (
    Material,
    MaterialSpec,
    MaterialSpecSupplier,
    SupplierResponsibilityScope,
    SupplierSalesResponsibility,
)
from workflow.models import WorkItem


class GoodsTransferApiTests(TestCase):
    endpoint = "/pldp/api/lifecycle/goods-transfers/"

    @classmethod
    def setUpTestData(cls):
        owner_type = OrganizationType.objects.get(code="OWNER")
        merchant_type = OrganizationType.objects.get(code="MERCHANT")
        supplier_type = OrganizationType.objects.get(code="SUPPLIER")
        cls.factory_sales_role = OrganizationRole.objects.get(code="factory_sales")
        cls.factory_logistics_role = OrganizationRole.objects.get(code="factory_logistics")
        cls.factory_sales_assistant_role = OrganizationRole.objects.get(
            code="factory_sales_assistant"
        )
        cls.factory_matching_role = OrganizationRole.objects.get(code="factory_matching")
        cls.factory_production_role = OrganizationRole.objects.get(code="factory_production")
        cls.factory_stock_role = OrganizationRole.objects.get(code="factory_stock")
        cls.merchant_owner_role = OrganizationRole.objects.get(code="merchant_owner")
        cls.merchant_sales_role = OrganizationRole.objects.get(code="merchant_sales")
        cls.merchant_stock_role = OrganizationRole.objects.get(code="merchant_stock")
        cls.supplier_owner_role = OrganizationRole.objects.get(code="supplier_owner")
        cls.supplier_sales_role = OrganizationRole.objects.get(code="supplier_sales")

        cls.factory = Organization.objects.create(
            name="流转测试厂家",
            code="FLOW-FACTORY",
            organization_type=owner_type,
        )
        cls.parent = Organization.objects.create(
            name="上级商家A",
            code="FLOW-MERCHANT-A",
            organization_type=merchant_type,
        )
        cls.child_b = Organization.objects.create(
            name="下级商家B",
            code="FLOW-MERCHANT-B",
            organization_type=merchant_type,
        )
        cls.child_c = Organization.objects.create(
            name="下级商家C",
            code="FLOW-MERCHANT-C",
            organization_type=merchant_type,
        )
        cls.supplier = Organization.objects.create(
            name="流转测试配套供应商",
            code="FLOW-SUPPLIER",
            organization_type=supplier_type,
        )
        cls.other_supplier = Organization.objects.create(
            name="同规格备选供应商",
            code="FLOW-OTHER-SUPPLIER",
            organization_type=supplier_type,
        )

        cls.factory_sales = cls._user(
            "flow_factory_sales", cls.factory, cls.factory_sales_role
        )
        cls.factory_logistics = cls._user(
            "flow_factory_logistics", cls.factory, cls.factory_logistics_role
        )
        cls.factory_sales_assistant = cls._user(
            "flow_factory_sales_assistant",
            cls.factory,
            cls.factory_sales_assistant_role,
        )
        cls.factory_matching = cls._user(
            "flow_factory_matching", cls.factory, cls.factory_matching_role
        )
        cls.factory_production = cls._user(
            "flow_factory_production", cls.factory, cls.factory_production_role
        )
        cls.factory_stock = cls._user(
            "flow_factory_stock", cls.factory, cls.factory_stock_role
        )
        cls.parent_owner = cls._user(
            "flow_parent_owner", cls.parent, cls.merchant_owner_role
        )
        cls.parent_sales = cls._user(
            "flow_parent_sales", cls.parent, cls.merchant_sales_role
        )
        cls.parent_stock = cls._user(
            "flow_parent_stock", cls.parent, cls.merchant_stock_role
        )
        cls.child_b_owner = cls._user(
            "flow_child_b_owner", cls.child_b, cls.merchant_owner_role
        )
        cls.child_b_sales = cls._user(
            "flow_child_b_sales", cls.child_b, cls.merchant_sales_role
        )
        cls.child_b_stock = cls._user(
            "flow_child_b_stock", cls.child_b, cls.merchant_stock_role
        )
        cls.child_c_owner = cls._user(
            "flow_child_c_owner", cls.child_c, cls.merchant_owner_role
        )
        cls.child_c_stock = cls._user(
            "flow_child_c_stock", cls.child_c, cls.merchant_stock_role
        )
        cls.supplier_owner = cls._user(
            "flow_supplier_owner", cls.supplier, cls.supplier_owner_role
        )
        cls.supplier_sales = cls._user(
            "flow_supplier_sales", cls.supplier, cls.supplier_sales_role
        )

        cls._relation(
            cls.factory_sales,
            cls.factory_sales_role,
            cls.parent_owner,
            cls.parent,
        )
        cls._relation(
            cls.parent_sales,
            cls.merchant_sales_role,
            cls.child_b_owner,
            cls.child_b,
        )
        cls._relation(
            cls.parent_owner,
            cls.merchant_owner_role,
            cls.child_c_owner,
            cls.child_c,
        )

        category = ProductCategory.objects.create(
            code="FLOW-CATEGORY",
            name="流转测试产品",
        )
        cls.product_model = ProductModel.objects.create(
            code="FLOW-MODEL",
            name="流转测试设备",
            category=category,
        )
        bom = BOM.objects.create(
            product_model=cls.product_model,
            version_no=1,
            version="v1",
            created_by=cls.factory_sales,
        )
        cls.fault_material = Material.objects.create(
            code="FLOW-CONTROL-MODULE",
            name="流转测试控制模块",
            is_key_part=True,
        )
        cls.fault_material.product_categories.add(category)
        cls.fault_material_spec = MaterialSpec.objects.create(
            material=cls.fault_material,
            code="FLOW-CONTROL-MODULE-24V",
            name="24V控制模块",
        )
        MaterialSpecSupplier.objects.create(
            material_spec=cls.fault_material_spec,
            supplier=cls.supplier,
            warranty_months=12,
        )
        MaterialSpecSupplier.objects.create(
            material_spec=cls.fault_material_spec,
            supplier=cls.other_supplier,
            warranty_months=12,
        )
        cls.bom_item = BOMItem.objects.create(
            bom=bom,
            material=cls.fault_material,
            material_spec=cls.fault_material_spec,
            supplier=cls.supplier,
            quantity=1,
        )
        SupplierSalesResponsibility.objects.create(
            supplier=cls.supplier,
            sales_identity=cls.supplier_sales.organization_identity,
            scope_type=SupplierResponsibilityScope.MATERIAL,
            material=cls.fault_material,
            assigned_by=cls.supplier_owner,
        )
        batch = ProductionBatch.objects.create(
            batch_no="FLOW-BATCH-001",
            product_model=cls.product_model,
            bom=bom,
            manufacturer=cls.factory,
            production_date=date(2026, 8, 17),
            quantity=3,
        )
        cls.devices = []
        for index in range(1, 4):
            device = LifecycleService.create_device(
                product_model=cls.product_model,
                batch=batch,
                organization=cls.factory,
                operator=cls.factory_sales,
                sn=f"FLOW-SN-{index:03d}",
            )
            cls.devices.append(device)

    @classmethod
    def _user(cls, username, organization, role):
        user = User.objects.create_user(
            username=username,
            wx_openid=f"openid-{username}",
            wx_nickname=username,
        )
        UserOrganization.objects.create(
            user=user,
            organization=organization,
            role=role,
        )
        return user

    @classmethod
    def _relation(cls, inviter, inviter_role, invitee, target_org):
        rule, _ = RoleInvitationRule.objects.get_or_create(
            inviter_role=inviter_role,
            target_role=invitee.organization_identity.role,
            scope=InvitationScope.CROSS_ORGANIZATION,
            relation_type=OrganizationRelationType.DISTRIBUTION,
        )
        invitation = InvitationRecord.objects.create(
            inviter=inviter,
            source_organization=inviter.organization_identity.organization,
            inviter_role=inviter_role,
            rule=rule,
            target_role=invitee.organization_identity.role,
            target_organization=target_org,
            invitee=invitee,
            scope=InvitationScope.CROSS_ORGANIZATION,
            relation_type=OrganizationRelationType.DISTRIBUTION,
            status=InvitationRecord.Status.ACCEPTED,
            expires_at=timezone.now() + timedelta(days=1),
            accepted_at=timezone.now(),
        )
        return OrganizationRelation.objects.create(
            source_organization=inviter.organization_identity.organization,
            target_organization=target_org,
            relation_type=OrganizationRelationType.DISTRIBUTION,
            created_by_invitation=invitation,
        )

    def auth(self, user):
        return {"HTTP_AUTHORIZATION": f"Bearer {issue_access_token(user)}"}

    def set_owner(self, device, organization):
        OrganizationDevice.objects.filter(device=device).update(
            organization=organization,
            status="IN_STOCK",
        )
        DeviceState.objects.filter(device=device).update(
            current_organization=organization,
            status="IN_STOCK",
            install_status="NOT_INSTALLED",
            activation_status="NOT_ACTIVATED",
        )

    def create_flow(self, user, payload):
        response = self.client.post(self.endpoint, payload, **self.auth(user))
        self.assertEqual(response.json()["code"], 0, response.json())
        return response.json()["data"]

    def scan_and_submit(self, user, transfer_id, device):
        scan = self.client.post(
            f"{self.endpoint}{transfer_id}/scan/",
            {"qr_text": device.qrcode.code},
            **self.auth(user),
        )
        self.assertEqual(scan.json()["code"], 0, scan.json())
        submit = self.submit_transfer(user, transfer_id)
        self.assertEqual(submit.json()["code"], 0, submit.json())
        return submit.json()["data"]

    def submit_transfer(self, user, transfer_id, *, return_remark="测试退货说明"):
        transfer = GoodsTransfer.objects.select_related(
            "to_organization__organization_type"
        ).get(pk=transfer_id)
        payload = {}
        if (
            transfer.flow_type == GoodsTransfer.FlowType.RETURN
            and transfer.to_organization.organization_type.code == "OWNER"
        ):
            payload = {
                "goods_list_client_request_id": f"test-goods-list-{transfer_id}",
                "return_remark": return_remark,
                "goods_list_file": SimpleUploadedFile(
                    f"factory-return-{transfer_id}.pdf",
                    b"%PDF-1.4\nfactory-return-goods-list",
                    content_type="application/pdf",
                ),
            }
        return self.client.post(
            f"{self.endpoint}{transfer_id}/submit/",
            payload,
            **self.auth(user),
        )

    def test_releasing_scanning_draft_frees_devices_and_keeps_draft(self):
        device = self.devices[0]
        self.set_owner(device, self.parent)
        flow = self.create_flow(
            self.parent_owner,
            {
                "flow_type": "SHIPMENT",
                "client_request_id": "shipment-release-scan-draft-1",
                "target_account_id": self.child_b_owner.pk,
            },
        )

        scanned = self.client.post(
            f"{self.endpoint}{flow['id']}/scan/",
            {"qr_text": device.qrcode.code},
            **self.auth(self.parent_owner),
        )
        self.assertEqual(scanned.json()["code"], 0, scanned.json())
        self.assertTrue(
            GoodsTransferItem.objects.filter(
                transfer_id=flow["id"], device=device
            ).exists()
        )

        ownership_before = OrganizationDevice.objects.get(device=device)
        state_before = DeviceState.objects.get(device=device)
        lifecycle_event_count = LifecycleEvent.objects.filter(
            device=device
        ).count()

        released = self.client.post(
            f"{self.endpoint}{flow['id']}/release-scan/",
            {},
            **self.auth(self.parent_owner),
        )
        self.assertEqual(released.json()["code"], 0, released.json())
        self.assertEqual(released.json()["data"]["released_count"], 1)
        self.assertEqual(released.json()["data"]["item_count"], 0)

        transfer = GoodsTransfer.objects.get(pk=flow["id"])
        self.assertEqual(transfer.status, GoodsTransfer.Status.SCANNING)
        self.assertFalse(transfer.items.exists())
        ownership_after = OrganizationDevice.objects.get(device=device)
        state_after = DeviceState.objects.get(device=device)
        self.assertEqual(ownership_after.organization_id, ownership_before.organization_id)
        self.assertEqual(ownership_after.status, ownership_before.status)
        self.assertEqual(state_after.current_organization_id, state_before.current_organization_id)
        self.assertEqual(state_after.status, state_before.status)
        self.assertEqual(
            LifecycleEvent.objects.filter(device=device).count(),
            lifecycle_event_count,
        )

        next_flow = self.create_flow(
            self.parent_owner,
            {
                "flow_type": "SHIPMENT",
                "client_request_id": "shipment-release-scan-draft-2",
                "target_account_id": self.child_b_owner.pk,
            },
        )
        rescanned = self.client.post(
            f"{self.endpoint}{next_flow['id']}/scan/",
            {"qr_text": device.qrcode.code},
            **self.auth(self.parent_owner),
        )
        self.assertEqual(rescanned.json()["code"], 0, rescanned.json())

    def test_only_draft_initiator_can_release_scanned_devices(self):
        device = self.devices[0]
        self.set_owner(device, self.parent)
        flow = self.create_flow(
            self.parent_owner,
            {
                "flow_type": "SHIPMENT",
                "client_request_id": "shipment-release-scan-owner-only",
                "target_account_id": self.child_b_owner.pk,
            },
        )
        self.assertTrue(flow["actions"]["can_delete"])
        scanned = self.client.post(
            f"{self.endpoint}{flow['id']}/scan/",
            {"qr_text": device.qrcode.code},
            **self.auth(self.parent_owner),
        )
        self.assertEqual(scanned.json()["code"], 0, scanned.json())

        forbidden = self.client.post(
            f"{self.endpoint}{flow['id']}/release-scan/",
            {},
            **self.auth(self.parent_sales),
        )
        self.assertEqual(forbidden.status_code, 409)
        self.assertEqual(forbidden.json()["code"], 1)
        self.assertTrue(
            GoodsTransferItem.objects.filter(
                transfer_id=flow["id"], device=device
            ).exists()
        )

    def test_submitted_transfer_items_cannot_be_released(self):
        device = self.devices[0]
        self.set_owner(device, self.parent)
        flow = self.create_flow(
            self.parent_owner,
            {
                "flow_type": "SHIPMENT",
                "client_request_id": "shipment-release-scan-after-submit",
                "target_account_id": self.child_b_owner.pk,
            },
        )
        scanned = self.client.post(
            f"{self.endpoint}{flow['id']}/scan/",
            {"qr_text": device.qrcode.code},
            **self.auth(self.parent_owner),
        )
        self.assertEqual(scanned.json()["code"], 0, scanned.json())
        submitted = self.submit_transfer(self.parent_owner, flow["id"])
        self.assertEqual(submitted.json()["code"], 0, submitted.json())

        release = self.client.post(
            f"{self.endpoint}{flow['id']}/release-scan/",
            {},
            **self.auth(self.parent_owner),
        )
        self.assertEqual(release.status_code, 409)
        transfer = GoodsTransfer.objects.get(pk=flow["id"])
        self.assertEqual(transfer.status, GoodsTransfer.Status.PENDING_RECEIPT)
        self.assertEqual(transfer.items.count(), 1)

    def test_rescan_conflict_returns_link_to_callers_existing_draft(self):
        device = self.devices[0]
        self.set_owner(device, self.parent)
        first_flow = self.create_flow(
            self.parent_owner,
            {
                "flow_type": "SHIPMENT",
                "client_request_id": "shipment-rescan-existing-draft-1",
                "target_account_id": self.child_b_owner.pk,
            },
        )
        scanned = self.client.post(
            f"{self.endpoint}{first_flow['id']}/scan/",
            {"qr_text": device.qrcode.code},
            **self.auth(self.parent_owner),
        )
        self.assertEqual(scanned.json()["code"], 0, scanned.json())

        second_flow = self.create_flow(
            self.parent_owner,
            {
                "flow_type": "SHIPMENT",
                "client_request_id": "shipment-rescan-existing-draft-2",
                "target_account_id": self.child_c_owner.pk,
            },
        )
        conflict = self.client.post(
            f"{self.endpoint}{second_flow['id']}/scan/",
            {"qr_text": device.qrcode.code},
            **self.auth(self.parent_owner),
        )

        self.assertEqual(conflict.status_code, 409)
        payload = conflict.json()
        self.assertEqual(
            payload["data"]["reason_code"],
            "DEVICE_IN_UNFINISHED_GOODS_TRANSFER",
        )
        self.assertEqual(
            payload["data"]["conflict_transfer"],
            {
                "id": first_flow["id"],
                "flow_no": first_flow["flow_no"],
                "can_continue": True,
            },
        )
        self.assertEqual(
            GoodsTransferItem.objects.filter(device=device).count(),
            1,
        )

    def test_delete_unsubmitted_draft_removes_only_draft_and_allows_rescan(self):
        device = self.devices[0]
        self.set_owner(device, self.parent)
        flow = self.create_flow(
            self.parent_owner,
            {
                "flow_type": "SHIPMENT",
                "client_request_id": "shipment-delete-scan-draft-1",
                "target_account_id": self.child_b_owner.pk,
            },
        )
        self.assertTrue(flow["actions"]["can_delete"])
        scanned = self.client.post(
            f"{self.endpoint}{flow['id']}/scan/",
            {"qr_text": device.qrcode.code},
            **self.auth(self.parent_owner),
        )
        self.assertEqual(scanned.json()["code"], 0, scanned.json())

        ownership_before = OrganizationDevice.objects.get(device=device)
        state_before = DeviceState.objects.get(device=device)
        events_before = LifecycleEvent.objects.filter(device=device).count()
        deleted = self.client.delete(
            f"{self.endpoint}{flow['id']}/",
            **self.auth(self.parent_owner),
        )

        self.assertEqual(deleted.status_code, 200, deleted.json())
        self.assertEqual(deleted.json()["data"]["deleted_item_count"], 1)
        self.assertFalse(GoodsTransfer.objects.filter(pk=flow["id"]).exists())
        self.assertFalse(
            GoodsTransferItem.objects.filter(transfer_id=flow["id"]).exists()
        )
        ownership_after = OrganizationDevice.objects.get(device=device)
        state_after = DeviceState.objects.get(device=device)
        self.assertEqual(
            ownership_after.organization_id,
            ownership_before.organization_id,
        )
        self.assertEqual(ownership_after.status, ownership_before.status)
        self.assertEqual(
            state_after.current_organization_id,
            state_before.current_organization_id,
        )
        self.assertEqual(state_after.status, state_before.status)
        self.assertEqual(LifecycleEvent.objects.filter(device=device).count(), events_before)

        next_flow = self.create_flow(
            self.parent_owner,
            {
                "flow_type": "SHIPMENT",
                "client_request_id": "shipment-delete-scan-draft-2",
                "target_account_id": self.child_b_owner.pk,
            },
        )
        rescanned = self.client.post(
            f"{self.endpoint}{next_flow['id']}/scan/",
            {"qr_text": device.qrcode.code},
            **self.auth(self.parent_owner),
        )
        self.assertEqual(rescanned.json()["code"], 0, rescanned.json())

    def test_only_draft_initiator_can_delete_unsubmitted_transfer(self):
        device = self.devices[0]
        self.set_owner(device, self.parent)
        flow = self.create_flow(
            self.parent_owner,
            {
                "flow_type": "SHIPMENT",
                "client_request_id": "shipment-delete-scan-owner-only",
                "target_account_id": self.child_b_owner.pk,
            },
        )
        denied = self.client.delete(
            f"{self.endpoint}{flow['id']}/",
            **self.auth(self.parent_sales),
        )

        self.assertEqual(denied.status_code, 409)
        self.assertTrue(GoodsTransfer.objects.filter(pk=flow["id"]).exists())

    def test_submitted_transfer_cannot_be_deleted_as_scan_draft(self):
        device = self.devices[0]
        self.set_owner(device, self.parent)
        flow = self.create_flow(
            self.parent_owner,
            {
                "flow_type": "SHIPMENT",
                "client_request_id": "shipment-delete-after-submit",
                "target_account_id": self.child_b_owner.pk,
            },
        )
        scanned = self.client.post(
            f"{self.endpoint}{flow['id']}/scan/",
            {"qr_text": device.qrcode.code},
            **self.auth(self.parent_owner),
        )
        self.assertEqual(scanned.json()["code"], 0, scanned.json())
        submitted = self.submit_transfer(self.parent_owner, flow["id"])
        self.assertEqual(submitted.json()["code"], 0, submitted.json())

        denied = self.client.delete(
            f"{self.endpoint}{flow['id']}/",
            **self.auth(self.parent_owner),
        )

        self.assertEqual(denied.status_code, 409)
        self.assertEqual(
            GoodsTransfer.objects.get(pk=flow["id"]).status,
            GoodsTransfer.Status.PENDING_RECEIPT,
        )
        self.assertEqual(
            GoodsTransferItem.objects.filter(transfer_id=flow["id"]).count(),
            1,
        )
        detail = self.client.get(
            f"{self.endpoint}{flow['id']}/",
            **self.auth(self.parent_owner),
        )
        self.assertEqual(detail.json()["code"], 0, detail.json())
        self.assertFalse(detail.json()["data"]["actions"]["can_delete"])

    def test_shipment_requires_receiver_confirmation_before_ownership_moves(self):
        device = self.devices[0]
        self.set_owner(device, self.parent)
        flow = self.create_flow(
            self.parent_stock,
            {
                "flow_type": "SHIPMENT",
                "client_request_id": "shipment-flow-1",
                "target_account_id": self.child_b_owner.pk,
            },
        )
        submitted = self.scan_and_submit(self.parent_stock, flow["id"], device)
        self.assertEqual(submitted["status"], "PENDING_RECEIPT")
        self.assertEqual(
            OrganizationDevice.objects.get(device=device).organization,
            self.parent,
        )
        self.assertEqual(
            OrganizationDevice.objects.get(device=device).status,
            "TRANSFERRED",
        )
        receipt_workitem = WorkItem.objects.get(
            type="GOODS_TRANSFER_RECEIPT_CONFIRM",
            biz_ref=f"lifecycle.GoodsTransfer:{flow['id']}",
        )
        self.assertSetEqual(
            set(
                receipt_workitem.user_tasks.values_list(
                    "to_openid",
                    flat=True,
                )
            ),
            {
                self.child_b_owner.wx_openid,
                self.child_b_stock.wx_openid,
            },
        )
        self.assertTrue(
            all(
                task.need_action
                for task in receipt_workitem.user_tasks.all()
            )
        )

        sales_receipt = self.client.post(
            f"{self.endpoint}{flow['id']}/receipt/",
            {},
            **self.auth(self.child_b_sales),
        )
        self.assertEqual(sales_receipt.json()["code"], 1, sales_receipt.json())

        receipt = self.client.post(
            f"{self.endpoint}{flow['id']}/receipt/",
            {},
            **self.auth(self.child_b_stock),
        )
        self.assertEqual(receipt.json()["code"], 0, receipt.json())
        self.assertEqual(receipt.json()["data"]["status"], "COMPLETED")
        self.assertEqual(
            OrganizationDevice.objects.get(device=device).organization,
            self.child_b,
        )
        receipt_workitem.refresh_from_db()
        self.assertEqual(
            receipt_workitem.status,
            "DONE",
        )

    def test_sales_can_only_start_the_transfer_flow(self):
        capability = self.client.get(
            f"{self.endpoint}options/?flow_type=CAPABILITIES",
            **self.auth(self.parent_sales),
        )
        self.assertEqual(capability.json()["code"], 0, capability.json())
        self.assertEqual(
            capability.json()["data"]["available_flow_types"], ["TRANSFER"]
        )
        for flow_type in ("SHIPMENT", "RETURN"):
            response = self.client.get(
                f"{self.endpoint}options/?flow_type={flow_type}",
                **self.auth(self.parent_sales),
            )
            self.assertEqual(response.json()["code"], 1, response.json())

    def test_return_to_upstream_merchant_waits_for_receipt_then_moves_owner(self):
        device = self.devices[0]
        self.set_owner(device, self.child_b)
        flow = self.create_flow(
            self.child_b_owner,
            {
                "flow_type": "RETURN",
                "client_request_id": "return-merchant-flow-1",
                "target_account_id": self.parent_sales.pk,
            },
        )
        submitted = self.scan_and_submit(self.child_b_owner, flow["id"], device)
        self.assertEqual(submitted["status"], "PENDING_RECEIPT")
        ownership = OrganizationDevice.objects.get(device=device)
        self.assertEqual(ownership.organization, self.child_b)
        self.assertEqual(ownership.status, "RETURNED")

        sales_receipt = self.client.post(
            f"{self.endpoint}{flow['id']}/receipt/",
            {},
            **self.auth(self.parent_sales),
        )
        self.assertEqual(sales_receipt.json()["code"], 1, sales_receipt.json())
        ownership.refresh_from_db()
        self.assertEqual(ownership.organization, self.child_b)

        receipt = self.client.post(
            f"{self.endpoint}{flow['id']}/receipt/",
            {},
            **self.auth(self.parent_stock),
        )
        self.assertEqual(receipt.json()["code"], 0, receipt.json())
        ownership.refresh_from_db()
        self.assertEqual(ownership.organization, self.parent)
        self.assertEqual(ownership.status, "IN_STOCK")
        self.assertEqual(
            DeviceState.objects.get(device=device).status,
            "IN_STOCK",
        )

    def test_transfer_moves_b_to_c_and_only_publishes_notice_tasks(self):
        device = self.devices[1]
        self.set_owner(device, self.child_b)
        flow = self.create_flow(
            self.parent_sales,
            {
                "flow_type": "TRANSFER",
                "client_request_id": "transfer-flow-1",
                "source_account_id": self.child_b_owner.pk,
                "target_account_id": self.child_c_owner.pk,
            },
        )
        submitted = self.scan_and_submit(self.parent_sales, flow["id"], device)
        self.assertEqual(submitted["status"], "COMPLETED")
        self.assertEqual(
            OrganizationDevice.objects.get(device=device).organization,
            self.child_c,
        )
        notices = WorkItem.objects.filter(
            type="GOODS_TRANSFER_NOTICE",
            biz_ref=f"lifecycle.GoodsTransfer:{flow['id']}",
        )
        self.assertEqual(notices.count(), 2)
        self.assertFalse(
            WorkItem.objects.filter(
                type="GOODS_TRANSFER_RECEIPT_CONFIRM",
                biz_ref=f"lifecycle.GoodsTransfer:{flow['id']}",
            ).exists()
        )
        self.assertTrue(
            all(
                not task.need_action
                for notice in notices
                for task in notice.user_tasks.all()
            )
        )

    def _legacy_return_to_factory_terminates_current_lifecycle_after_receipt(self):
        device = self.devices[2]
        self.set_owner(device, self.parent)
        flow = self.create_flow(
            self.parent_owner,
            {
                "flow_type": "RETURN",
                "client_request_id": "return-factory-flow-1",
                "target_account_id": self.factory_sales.pk,
            },
        )
        submitted = self.scan_and_submit(self.parent_owner, flow["id"], device)
        self.assertEqual(submitted["status"], "PENDING_RECEIPT")
        self.assertFalse(submitted["factory_lifecycle_terminated"])

        receipt = self.client.post(
            f"{self.endpoint}{flow['id']}/receipt/",
            {},
            **self.auth(self.factory_stock),
        )
        self.assertEqual(receipt.json()["code"], 0, receipt.json())
        self.assertTrue(receipt.json()["data"]["factory_lifecycle_terminated"])
        self.assertEqual(
            DeviceState.objects.get(device=device).status,
            "RETURNED_TO_FACTORY",
        )
        ownership = OrganizationDevice.objects.get(device=device)
        self.assertEqual(ownership.organization, self.factory)
        self.assertEqual(ownership.status, "RETURNED_TO_FACTORY")
        device.qrcode.refresh_from_db()
        self.assertEqual(device.qrcode.status, "DISABLED")
        self.assertIsNotNone(device.qrcode.disabled_at)
        disposition = FactoryReturnDisposition.objects.get(
            transfer_item__transfer_id=flow["id"],
            transfer_item__device=device,
        )
        self.assertEqual(disposition.decision, "PENDING")
        self.assertEqual(disposition.source_bom, device.batch.bom)
        self.assertTrue(
            LifecycleEvent.objects.filter(
                device=device,
                event_type="LIFECYCLE_CLOSE",
            ).exists()
        )
        disposition_task = WorkItem.objects.get(
            type="FACTORY_RETURN_DISPOSITION",
            biz_ref=f"lifecycle.GoodsTransfer:{flow['id']}",
        )
        self.assertEqual(disposition_task.status, "OPEN")
        self.assertSetEqual(
            set(disposition_task.user_tasks.values_list("to_openid", flat=True)),
            {self.factory_stock.wx_openid},
        )

    def _legacy_factory_stock_decides_each_return_item_and_routes_reissue_to_sales(self):
        first, second = self.devices[:2]
        self.set_owner(first, self.parent)
        self.set_owner(second, self.parent)
        initial_device_count = type(first).objects.count()
        initial_qrcode_count = QRCode.objects.count()
        flow = self.create_flow(
            self.parent_owner,
            {
                "flow_type": "RETURN",
                "client_request_id": "factory-disposition-flow-1",
                "target_account_id": self.factory_sales.pk,
            },
        )
        for device in (first, second):
            scanned = self.client.post(
                f"{self.endpoint}{flow['id']}/scan/",
                {"qr_text": device.qrcode.code},
                **self.auth(self.parent_owner),
            )
            self.assertEqual(scanned.json()["code"], 0, scanned.json())
        submitted = self.submit_transfer(self.parent_owner, flow["id"])
        self.assertEqual(submitted.json()["code"], 0, submitted.json())

        receipt = self.client.post(
            f"{self.endpoint}{flow['id']}/receipt/",
            {},
            **self.auth(self.factory_stock),
        )
        self.assertEqual(receipt.json()["code"], 0, receipt.json())

        dispositions_url = (
            f"{self.endpoint}{flow['id']}/factory-return-dispositions/"
        )
        merchant_view = self.client.get(
            dispositions_url,
            **self.auth(self.parent_owner),
        )
        self.assertEqual(merchant_view.json()["code"], 1)
        sales_view = self.client.get(
            dispositions_url,
            **self.auth(self.factory_sales),
        )
        self.assertEqual(sales_view.json()["code"], 0, sales_view.json())
        self.assertFalse(sales_view.json()["data"]["viewer"]["can_decide"])

        items = sales_view.json()["data"]["items"]
        forbidden = self.client.post(
            f"{dispositions_url}{items[0]['goods_transfer_item_id']}/decision/",
            {"decision": "TERMINATE"},
            **self.auth(self.factory_sales),
        )
        self.assertEqual(forbidden.json()["code"], 1)

        first_decision = self.client.post(
            f"{dispositions_url}{items[0]['goods_transfer_item_id']}/decision/",
            {"decision": "TERMINATE", "remark": "本台结束"},
            **self.auth(self.factory_stock),
        )
        self.assertEqual(first_decision.json()["code"], 0, first_decision.json())
        self.assertEqual(first_decision.json()["data"]["pending_count"], 1)
        disposition_task = WorkItem.objects.get(
            type="FACTORY_RETURN_DISPOSITION",
            biz_ref=f"lifecycle.GoodsTransfer:{flow['id']}",
        )
        self.assertEqual(disposition_task.status, "OPEN")

        second_decision = self.client.post(
            f"{dispositions_url}{items[1]['goods_transfer_item_id']}/decision/",
            {
                "decision": "REISSUE_CHANGED_BOM",
                "remark": "更换部分配套物料",
            },
            **self.auth(self.factory_stock),
        )
        self.assertEqual(second_decision.json()["code"], 0, second_decision.json())
        self.assertTrue(second_decision.json()["data"]["all_decided"])
        disposition_task.refresh_from_db()
        self.assertEqual(disposition_task.status, "DONE")
        reissue = WorkItem.objects.get(type="FACTORY_RETURN_REISSUE_ORDER")
        self.assertEqual(reissue.status, "OPEN")
        self.assertSetEqual(
            set(reissue.user_tasks.values_list("to_openid", flat=True)),
            {self.factory_sales.wx_openid},
        )
        self.assertEqual(reissue.payload["bom_strategy"], "REISSUE_CHANGED_BOM")
        self.assertEqual(type(first).objects.count(), initial_device_count)
        self.assertEqual(QRCode.objects.count(), initial_qrcode_count)
        self.assertEqual(
            FactoryReturnDisposition.objects.filter(
                decision="PENDING",
            ).count(),
            0,
        )
        self.assertEqual(
            LifecycleEvent.objects.filter(
                event_type="RETURN_DISPOSITION",
            ).count(),
            2,
        )

    def test_factory_return_submit_waits_for_logistics_pickup(self):
        device = self.devices[0]
        self.set_owner(device, self.parent)
        flow = self.create_flow(
            self.parent_stock,
            {
                "flow_type": "RETURN",
                "client_request_id": "factory-return-pickup-1",
                "target_account_id": self.factory_sales.pk,
            },
        )
        submitted = self.scan_and_submit(self.parent_stock, flow["id"], device)
        self.assertEqual(submitted["status"], "PENDING_FACTORY_PICKUP")
        self.assertFalse(submitted["factory_lifecycle_terminated"])
        ownership = OrganizationDevice.objects.get(device=device)
        self.assertEqual(ownership.organization, self.parent)
        self.assertEqual(ownership.status, "RETURNED")
        process = FactoryReturnProcess.objects.get(transfer_id=flow["id"])
        self.assertEqual(process.phase, "WAITING_PICKUP")
        inventory_item = FactoryReturnInventoryItem.objects.get(process=process)
        self.assertEqual(inventory_item.stock_status, "WAITING_POSTING")
        self.assertEqual(inventory_item.source_bom, device.batch.bom)

        sales_notice = WorkItem.objects.get(
            type="FACTORY_RETURN_SALES_NOTICE_SUBMITTED",
            biz_ref=f"lifecycle.GoodsTransfer:{flow['id']}",
        )
        self.assertSetEqual(
            set(sales_notice.user_tasks.values_list("to_openid", flat=True)),
            {self.factory_sales.wx_openid},
        )
        pickup_task = WorkItem.objects.get(
            type="FACTORY_RETURN_PICKUP",
            biz_ref=f"lifecycle.GoodsTransfer:{flow['id']}",
        )
        self.assertSetEqual(
            set(pickup_task.user_tasks.values_list("to_openid", flat=True)),
            {self.factory_logistics.wx_openid},
        )
        logistics_view = self.client.get(
            f"{self.endpoint}{flow['id']}/factory-return/",
            **self.auth(self.factory_logistics),
        ).json()["data"]
        self.assertEqual(len(logistics_view["workflow_tasks"]), 1)
        self.assertTrue(logistics_view["workflow_tasks"][0]["published"])
        self.assertEqual(
            logistics_view["workflow_tasks"][0]["read_state"],
            "UNREAD",
        )
        self.assertEqual(
            logistics_view["workflow_tasks"][0]["action_state"],
            "PENDING",
        )

        # The matching role can already see its complete planned branch, but
        # neither branch is readable/operable before classification publishes
        # a real task to that role.
        matching_view = self.client.get(
            f"{self.endpoint}{flow['id']}/factory-return/",
            **self.auth(self.factory_matching),
        ).json()["data"]
        self.assertEqual(len(matching_view["workflow_tasks"]), 2)
        self.assertTrue(
            all(
                not row["published"]
                and row["read_state"] == "NOT_PUBLISHED"
                and row["action_state"] == "NOT_PUBLISHED"
                for row in matching_view["workflow_tasks"]
            )
        )
        forbidden_receipt = self.client.post(
            f"{self.endpoint}{flow['id']}/receipt/",
            {},
            **self.auth(self.factory_stock),
        )
        self.assertEqual(forbidden_receipt.json()["code"], 1)

    def test_factory_return_final_submit_requires_goods_list_atomically(self):
        device = self.devices[0]
        self.set_owner(device, self.parent)
        flow = self.create_flow(
            self.parent_owner,
            {
                "flow_type": "RETURN",
                "client_request_id": "factory-return-required-goods-list",
                "target_account_id": self.factory_sales.pk,
            },
        )
        scan = self.client.post(
            f"{self.endpoint}{flow['id']}/scan/",
            {"qr_text": device.qrcode.code},
            **self.auth(self.parent_owner),
        )
        self.assertEqual(scan.json()["code"], 0, scan.json())
        rejected = self.client.post(
            f"{self.endpoint}{flow['id']}/submit/",
            {"return_remark": "缺少货品清单"},
            **self.auth(self.parent_owner),
        )
        self.assertEqual(rejected.json()["code"], 1)
        transfer = GoodsTransfer.objects.get(pk=flow["id"])
        self.assertEqual(transfer.status, "SCANNING")
        ownership = OrganizationDevice.objects.get(device=device)
        self.assertEqual(ownership.status, "IN_STOCK")
        self.assertFalse(
            FactoryReturnProcess.objects.filter(transfer=transfer).exists()
        )

        submitted = self.submit_transfer(
            self.parent_owner,
            flow["id"],
            return_remark="包装完整，退货冲抵金额由厂家销售助理填写",
        )
        self.assertEqual(submitted.json()["code"], 0, submitted.json())
        transfer.refresh_from_db()
        self.assertEqual(transfer.remark, "包装完整，退货冲抵金额由厂家销售助理填写")
        process = FactoryReturnProcess.objects.get(transfer=transfer)
        self.assertEqual(process.documents.filter(document_kind="GOODS_LIST").count(), 1)
        self.assertEqual(process.account_case.status, "DRAFT")

    def test_factory_return_documents_belong_to_initiator_and_are_non_blocking(self):
        device = self.devices[0]
        self.set_owner(device, self.parent)
        flow = self.create_flow(
            self.parent_stock,
            {
                "flow_type": "RETURN",
                "client_request_id": "factory-return-material-document-1",
                "target_account_id": self.factory_sales.pk,
            },
        )
        submitted = self.scan_and_submit(self.parent_stock, flow["id"], device)
        self.assertTrue(
            submitted["actions"][
                "can_upload_factory_return_logistics_document"
            ]
        )
        self.assertTrue(
            submitted["actions"][
                "can_upload_factory_return_goods_list_document"
            ]
        )
        self.assertEqual(submitted["factory_return_logistics_documents"], [])
        self.assertEqual(
            len(submitted["factory_return_goods_list_documents"]),
            1,
        )

        document_task = WorkItem.objects.get(
            type="FACTORY_RETURN_DOCUMENT_UPLOAD",
            biz_ref=f"lifecycle.GoodsTransfer:{flow['id']}",
        )
        self.assertEqual(document_task.action_mode, "ANYONE_DO")
        self.assertSetEqual(
            set(document_task.user_tasks.values_list("to_openid", flat=True)),
            {self.parent_stock.wx_openid},
        )
        assistant_task = WorkItem.objects.get(
            type="FACTORY_RETURN_POSTING",
            biz_ref=f"lifecycle.GoodsTransfer:{flow['id']}",
        )
        self.assertTrue(assistant_task.payload["blocked_by_pickup"])

        # 同组织其他可扫码岗位能够查看，但不协同完成本流转单的上传操作。
        peer_view = self.client.get(
            f"{self.endpoint}{flow['id']}/",
            **self.auth(self.parent_owner),
        )
        self.assertEqual(peer_view.json()["code"], 0, peer_view.json())
        self.assertFalse(
            peer_view.json()["data"]["actions"][
                "can_upload_factory_return_logistics_document"
            ]
        )

        # 上传单据不是物流取回的前置条件；现有取回接口仍可直接执行。
        pickup = self.client.post(
            f"{self.endpoint}{flow['id']}/factory-return/pickup/",
            {},
            **self.auth(self.factory_logistics),
        )
        self.assertEqual(pickup.json()["code"], 0, pickup.json())
        document_task.refresh_from_db()
        self.assertEqual(document_task.status, "OPEN")

        logistics_url = (
            f"{self.endpoint}{flow['id']}/factory-return/logistics-documents/"
        )
        forbidden_peer = self.client.post(
            logistics_url,
            {
                "client_request_id": "factory-return-logistics-peer",
                "document_file": self.image_file("peer-logistics.png"),
            },
            **self.auth(self.parent_sales),
        )
        self.assertEqual(forbidden_peer.json()["code"], 1)

        uploaded = self.client.post(
            logistics_url,
            {
                "client_request_id": "factory-return-logistics-upload-1",
                "remark": "退货物流单据第一份",
                "document_file": self.image_file("return-logistics.png"),
            },
            **self.auth(self.parent_stock),
        )
        self.assertEqual(uploaded.json()["code"], 0, uploaded.json())
        documents = uploaded.json()["data"]["logistics_documents"]
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["file_type"], "IMAGE")
        self.assertEqual(documents[0]["remark"], "退货物流单据第一份")
        self.assertEqual(
            FactoryReturnDocument.objects.filter(
                document_kind="LOGISTICS",
            ).count(),
            1,
        )

        # 货品清单已随最终提交完成；补传物流单据后原任务直接完成。
        document_task.refresh_from_db()
        self.assertEqual(document_task.status, "DONE")
        self.assertFalse(
            WorkItem.objects.filter(
                type="FACTORY_RETURN_LOGISTICS_DOCUMENT_NOTICE",
                biz_ref=f"lifecycle.GoodsTransfer:{flow['id']}",
            ).exists()
        )

        logistics_view = self.client.get(
            f"{self.endpoint}{flow['id']}/factory-return/",
            **self.auth(self.factory_logistics),
        )
        self.assertEqual(logistics_view.json()["code"], 0, logistics_view.json())
        self.assertEqual(
            len(logistics_view.json()["data"]["logistics_documents"]),
            1,
        )
        self.assertEqual(logistics_view.json()["data"]["goods_list_documents"], [])

        goods_list_url = (
            f"{self.endpoint}{flow['id']}/factory-return/goods-list-documents/"
        )
        goods_list_upload = self.client.post(
            goods_list_url,
            {
                "client_request_id": "factory-return-goods-list-upload-1",
                "remark": "本批退货货品清单",
                "document_file": SimpleUploadedFile(
                    "return-goods-list.pdf",
                    b"%PDF-1.4\nfactory-return-goods-list",
                    content_type="application/pdf",
                ),
            },
            **self.auth(self.parent_stock),
        )
        self.assertEqual(goods_list_upload.json()["code"], 0, goods_list_upload.json())
        self.assertEqual(
            len(goods_list_upload.json()["data"]["goods_list_documents"]),
            2,
        )
        goods_list_document = FactoryReturnDocument.objects.filter(
            document_kind="GOODS_LIST",
        ).order_by("-sequence_no").first()

        document_task.refresh_from_db()
        self.assertEqual(document_task.status, "DONE")
        actor_task = document_task.user_tasks.get(
            to_openid=self.parent_stock.wx_openid
        )
        self.assertEqual(actor_task.done_reason, "SELF_ACTION")

        self.assertFalse(
            WorkItem.objects.filter(
                type="FACTORY_RETURN_GOODS_LIST_NOTICE",
                biz_ref=f"lifecycle.GoodsTransfer:{flow['id']}",
            ).exists()
        )

        assistant_view = self.client.get(
            f"{self.endpoint}{flow['id']}/factory-return/",
            **self.auth(self.factory_sales_assistant),
        )
        self.assertEqual(assistant_view.json()["code"], 0, assistant_view.json())
        self.assertEqual(assistant_view.json()["data"]["logistics_documents"], [])
        self.assertEqual(
            len(assistant_view.json()["data"]["goods_list_documents"]),
            2,
        )
        self.assertFalse(
            assistant_view.json()["data"]["rules"][
                "return_documents_block_pickup"
            ]
        )

        goods_list_file = self.client.get(
            f"{goods_list_url}{goods_list_document.pk}/file/",
            **self.auth(self.factory_sales_assistant),
        )
        self.assertEqual(goods_list_file.status_code, 200)
        forbidden_upload = self.client.post(
            goods_list_url,
            {
                "client_request_id": "factory-return-goods-list-forbidden",
                "document_file": self.image_file("forbidden.png"),
            },
            **self.auth(self.factory_logistics),
        )
        self.assertEqual(forbidden_upload.json()["code"], 1)

        sales_view = self.client.get(
            f"{self.endpoint}{flow['id']}/factory-return/",
            **self.auth(self.factory_sales),
        )
        self.assertEqual(sales_view.json()["code"], 0, sales_view.json())
        self.assertEqual(len(sales_view.json()["data"]["logistics_documents"]), 1)
        self.assertEqual(len(sales_view.json()["data"]["goods_list_documents"]), 2)
        self.assertEqual(
            WorkItem.objects.filter(
                biz_ref=f"lifecycle.GoodsTransfer:{flow['id']}",
                type__in=(
                    "FACTORY_RETURN_SALES_NOTICE_SUBMITTED",
                    "FACTORY_RETURN_PICKUP",
                    "FACTORY_RETURN_POSTING",
                ),
            ).count(),
            3,
        )

    def test_factory_return_pickup_then_posts_inventory_before_finance_confirmation(self):
        for device in self.devices:
            self.set_owner(device, self.parent)
        flow = self.create_flow(
            self.parent_owner,
            {
                "flow_type": "RETURN",
                "client_request_id": "factory-return-posting-1",
                "target_account_id": self.factory_sales.pk,
            },
        )
        for device in self.devices:
            scanned = self.client.post(
                f"{self.endpoint}{flow['id']}/scan/",
                {"qr_text": device.qrcode.code},
                **self.auth(self.parent_owner),
            )
            self.assertEqual(scanned.json()["code"], 0, scanned.json())
        submitted = self.submit_transfer(self.parent_owner, flow["id"])
        self.assertEqual(submitted.json()["code"], 0, submitted.json())

        assistant_task = WorkItem.objects.get(
            type="FACTORY_RETURN_POSTING",
            biz_ref=f"lifecycle.GoodsTransfer:{flow['id']}",
        )
        self.assertEqual(assistant_task.status, "OPEN")
        self.assertTrue(assistant_task.payload["blocked_by_pickup"])
        forbidden_amount = self.client.post(
            f"{self.endpoint}{flow['id']}/factory-return/credit-amount/",
            {"amount": "800.00"},
            **self.auth(self.factory_sales_assistant),
        )
        self.assertEqual(forbidden_amount.json()["code"], 1)

        forbidden_pickup = self.client.post(
            f"{self.endpoint}{flow['id']}/factory-return/pickup/",
            {},
            **self.auth(self.factory_sales),
        )
        self.assertEqual(forbidden_pickup.json()["code"], 1)
        pickup = self.client.post(
            f"{self.endpoint}{flow['id']}/factory-return/pickup/",
            {},
            **self.auth(self.factory_logistics),
        )
        self.assertEqual(pickup.json()["code"], 0, pickup.json())
        self.assertEqual(
            pickup.json()["data"]["transfer"]["status"],
            "PENDING_FACTORY_POSTING",
        )
        assistant_task.refresh_from_db()
        self.assertEqual(assistant_task.status, "OPEN")
        self.assertFalse(assistant_task.payload["blocked_by_pickup"])
        for device in self.devices:
            ownership = OrganizationDevice.objects.get(device=device)
            self.assertEqual(ownership.organization, self.factory)
            self.assertEqual(ownership.status, "RETURNED_TO_FACTORY")
            self.assertEqual(
                DeviceState.objects.get(device=device).status,
                "RETURNED_TO_FACTORY",
            )
            device.qrcode.refresh_from_db()
            self.assertEqual(device.qrcode.status, "DISABLED")
        self.assertFalse(
            LifecycleEvent.objects.filter(event_type="LIFECYCLE_CLOSE").exists()
        )
        account_case = GoodsTransferAccountCase.objects.get(
            transfer_id=flow["id"],
            case_type="RETURN_CREDIT",
        )
        self.assertEqual(account_case.status, "DRAFT")

        process_url = f"{self.endpoint}{flow['id']}/factory-return/"
        assistant_view = self.client.get(
            process_url,
            **self.auth(self.factory_sales_assistant),
        )
        self.assertEqual(assistant_view.json()["code"], 0, assistant_view.json())
        rows = assistant_view.json()["data"]["items"]
        posting_payload = {
            "posting_remark": "先分类入库，冲抵金额后续确认",
            "items": [
                {
                    "item_id": rows[0]["goods_transfer_item_id"],
                    "stock_status": "PENDING_DISASSEMBLY",
                    "remark": "待拆机",
                },
                {
                    "item_id": rows[1]["goods_transfer_item_id"],
                    "stock_status": "PENDING_REISSUE",
                    "remark": "配套暂不变",
                },
                {
                    "item_id": rows[2]["goods_transfer_item_id"],
                    "stock_status": "PENDING_REPAIR",
                    "remark": "待检测修复",
                },
            ],
        }
        posting = self.client.post(
            f"{process_url}posting/",
            posting_payload,
            content_type="application/json",
            **self.auth(self.factory_sales_assistant),
        )
        self.assertEqual(posting.json()["code"], 0, posting.json())
        self.assertEqual(posting.json()["data"]["process"]["phase"], "POSTED")
        self.assertEqual(
            posting.json()["data"]["transfer"]["status"],
            "COMPLETED",
        )
        account_case.refresh_from_db()
        self.assertEqual(account_case.status, "DRAFT")
        assistant_task.refresh_from_db()
        self.assertEqual(assistant_task.status, "OPEN")
        self.assertTrue(assistant_task.payload["inventory_posted"])
        self.assertFalse(assistant_task.payload["credit_amount_recorded"])
        self.assertSetEqual(
            set(
                FactoryReturnInventoryItem.objects.filter(
                    process__transfer_id=flow["id"]
                ).values_list("stock_status", flat=True)
            ),
            {"PENDING_DISASSEMBLY", "PENDING_REISSUE", "PENDING_REPAIR"},
        )
        stock_notice = WorkItem.objects.get(type="FACTORY_RETURN_STOCK_NOTICE")
        self.assertSetEqual(
            set(stock_notice.user_tasks.values_list("to_openid", flat=True)),
            {self.factory_stock.wx_openid},
        )
        for work_type in (
            "FACTORY_RETURN_DISASSEMBLY_PRODUCTION",
            "FACTORY_RETURN_REPAIR_PRODUCTION",
        ):
            task = WorkItem.objects.get(type=work_type)
            self.assertSetEqual(
                set(task.user_tasks.values_list("to_openid", flat=True)),
                {self.factory_production.wx_openid},
            )
        for work_type in (
            "FACTORY_RETURN_DISASSEMBLY_MATCHING",
            "FACTORY_RETURN_REPAIR_MATCHING",
        ):
            task = WorkItem.objects.get(type=work_type)
            self.assertSetEqual(
                set(task.user_tasks.values_list("to_openid", flat=True)),
                {self.factory_matching.wx_openid},
            )

        amount = self.client.post(
            f"{self.endpoint}{flow['id']}/factory-return/credit-amount/",
            {"amount": "800.00"},
            **self.auth(self.factory_sales_assistant),
        )
        self.assertEqual(amount.json()["code"], 0, amount.json())
        account_case.refresh_from_db()
        self.assertEqual(account_case.status, "CONFIRMED")
        self.assertEqual(str(account_case.confirmed_amount), "800.00")
        assistant_task.refresh_from_db()
        self.assertEqual(assistant_task.status, "DONE")
        self.assertTrue(assistant_task.payload["inventory_posted"])
        self.assertTrue(assistant_task.payload["credit_amount_recorded"])

    def test_factory_return_credit_amount_can_be_recorded_before_inventory_posting(self):
        device = self.devices[0]
        self.set_owner(device, self.parent)
        flow = self.create_flow(
            self.parent_owner,
            {
                "flow_type": "RETURN",
                "client_request_id": "factory-return-credit-first",
                "target_account_id": self.factory_sales.pk,
            },
        )
        self.scan_and_submit(self.parent_owner, flow["id"], device)
        pickup = self.client.post(
            f"{self.endpoint}{flow['id']}/factory-return/pickup/",
            {},
            **self.auth(self.factory_logistics),
        )
        self.assertEqual(pickup.json()["code"], 0, pickup.json())

        amount = self.client.post(
            f"{self.endpoint}{flow['id']}/factory-return/credit-amount/",
            {"amount": "260.50"},
            **self.auth(self.factory_sales_assistant),
        )
        self.assertEqual(amount.json()["code"], 0, amount.json())
        assistant_task = WorkItem.objects.get(
            type="FACTORY_RETURN_POSTING",
            biz_ref=f"lifecycle.GoodsTransfer:{flow['id']}",
        )
        self.assertEqual(assistant_task.status, "OPEN")
        self.assertTrue(assistant_task.payload["credit_amount_recorded"])
        self.assertFalse(assistant_task.payload["inventory_posted"])

        row = amount.json()["data"]["items"][0]
        posting = self.client.post(
            f"{self.endpoint}{flow['id']}/factory-return/posting/",
            {
                "posting_remark": "金额已先登记",
                "items": [
                    {
                        "item_id": row["goods_transfer_item_id"],
                        "stock_status": "PENDING_REISSUE",
                        "remark": "直接进入待流转库存",
                    }
                ],
            },
            content_type="application/json",
            **self.auth(self.factory_sales_assistant),
        )
        self.assertEqual(posting.json()["code"], 0, posting.json())
        assistant_task.refresh_from_db()
        self.assertEqual(assistant_task.status, "DONE")
        self.assertTrue(assistant_task.payload["credit_amount_recorded"])
        self.assertTrue(assistant_task.payload["inventory_posted"])

    def test_return_supplier_recipients_keep_original_order_responsibility_snapshot(self):
        WorkItem.objects.create(
            domain="SALES",
            type="ORDER_PLAN_SUPPLIER_PREPARE",
            biz_id="snapshot-owner-only",
            biz_ref="sales.OrderPlan:9001",
            uniq_key="test:order-plan:9001:supplier",
            payload={
                "supplier_id": self.supplier.pk,
                "recipient_scopes": {
                    self.supplier_owner.wx_openid: {
                        "role": "supplier_owner",
                        "identity_id": self.supplier_owner.organization_identity.pk,
                        "material_ids": [self.fault_material.pk],
                    },
                    # 当前责任范围后来增加了销售经理，但不能回写首次快照。
                    self.supplier_sales.wx_openid: {
                        "role": "supplier_sales",
                        "identity_id": self.supplier_sales.organization_identity.pk,
                        "material_ids": [self.fault_material.pk],
                    },
                },
                "initial_recipient_scopes": {
                    self.supplier_owner.wx_openid: {
                        "role": "supplier_owner",
                        "identity_id": self.supplier_owner.organization_identity.pk,
                        "material_ids": [self.fault_material.pk],
                    },
                },
            },
        )
        with patch(
            "lifecycle.services.factory_return_service._source_order_item",
            return_value=SimpleNamespace(plan_id=9001),
        ):
            recipients = _original_supplier_task_recipients(
                inventory_item=object(),
                supplier=self.supplier,
                material_ids={self.fault_material.pk},
            )
        self.assertEqual(recipients["openids"], [self.supplier_owner.wx_openid])
        self.assertEqual(recipients["sales_openids"], [])
        self.assertEqual(
            recipients["unassigned_sales_material_ids"],
            [self.fault_material.pk],
        )
        self.assertEqual(
            recipients["responsibility_source"],
            "ORDER_CONFIRMATION_INITIAL_SNAPSHOT",
        )

        WorkItem.objects.create(
            domain="SALES",
            type="ORDER_PLAN_SUPPLIER_PREPARE",
            biz_id="snapshot-with-sales",
            biz_ref="sales.OrderPlan:9002",
            uniq_key="test:order-plan:9002:supplier",
            payload={
                "supplier_id": self.supplier.pk,
                "recipient_scopes": {
                    self.supplier_owner.wx_openid: {
                        "role": "supplier_owner",
                        "identity_id": self.supplier_owner.organization_identity.pk,
                        "material_ids": [self.fault_material.pk],
                    },
                    self.supplier_sales.wx_openid: {
                        "role": "supplier_sales",
                        "identity_id": self.supplier_sales.organization_identity.pk,
                        "material_ids": [self.fault_material.pk],
                    },
                },
                "initial_recipient_scopes": {
                    self.supplier_owner.wx_openid: {
                        "role": "supplier_owner",
                        "identity_id": self.supplier_owner.organization_identity.pk,
                        "material_ids": [self.fault_material.pk],
                    },
                    self.supplier_sales.wx_openid: {
                        "role": "supplier_sales",
                        "identity_id": self.supplier_sales.organization_identity.pk,
                        "material_ids": [self.fault_material.pk],
                    },
                },
            },
        )
        with patch(
            "lifecycle.services.factory_return_service._source_order_item",
            return_value=SimpleNamespace(plan_id=9002),
        ):
            recipients = _original_supplier_task_recipients(
                inventory_item=object(),
                supplier=self.supplier,
                material_ids={self.fault_material.pk},
            )
        self.assertSetEqual(
            set(recipients["openids"]),
            {self.supplier_owner.wx_openid, self.supplier_sales.wx_openid},
        )
        self.assertEqual(recipients["unassigned_sales_material_ids"], [])

    def test_factory_return_internal_disposition_moves_repaired_items_to_reissue(self):
        for device in self.devices:
            self.set_owner(device, self.parent)
        flow = self.create_flow(
            self.parent_owner,
            {
                "flow_type": "RETURN",
                "client_request_id": "factory-return-internal-disposition",
                "target_account_id": self.factory_sales.pk,
            },
        )
        for device in self.devices:
            scan = self.client.post(
                f"{self.endpoint}{flow['id']}/scan/",
                {"qr_text": device.qrcode.code},
                **self.auth(self.parent_owner),
            )
            self.assertEqual(scan.json()["code"], 0, scan.json())
        submit = self.submit_transfer(self.parent_owner, flow["id"])
        self.assertEqual(submit.json()["code"], 0, submit.json())
        pickup = self.client.post(
            f"{self.endpoint}{flow['id']}/factory-return/pickup/",
            {},
            **self.auth(self.factory_logistics),
        )
        self.assertEqual(pickup.json()["code"], 0, pickup.json())
        rows = pickup.json()["data"]["items"]
        posting = self.client.post(
            f"{self.endpoint}{flow['id']}/factory-return/posting/",
            {
                "posting_remark": "两台修复、一台拆机",
                "items": [
                    {
                        "item_id": rows[0]["goods_transfer_item_id"],
                        "stock_status": "PENDING_REPAIR",
                    },
                    {
                        "item_id": rows[1]["goods_transfer_item_id"],
                        "stock_status": "PENDING_REPAIR",
                    },
                    {
                        "item_id": rows[2]["goods_transfer_item_id"],
                        "stock_status": "PENDING_DISASSEMBLY",
                    },
                ],
            },
            content_type="application/json",
            **self.auth(self.factory_sales_assistant),
        )
        self.assertEqual(posting.json()["code"], 0, posting.json())
        items = posting.json()["data"]["items"]
        first_repair, second_repair, disassembly = items

        repair_matching = WorkItem.objects.get(
            type="FACTORY_RETURN_REPAIR_MATCHING",
            biz_ref=f"lifecycle.GoodsTransfer:{flow['id']}",
        )
        self.assertFalse(
            repair_matching.user_tasks.get(
                to_openid=self.factory_matching.wx_openid
            ).need_action
        )

        diagnosis_without_parts = self.client.post(
            f"{self.endpoint}{flow['id']}/factory-return/items/"
            f"{first_repair['id']}/production/",
            {
                "action": "REPAIR_DIAGNOSIS",
                "needs_replacement_material": False,
                "production_remark": "无需换料，保养后可修复",
            },
            content_type="application/json",
            **self.auth(self.factory_production),
        )
        self.assertEqual(
            diagnosis_without_parts.json()["code"],
            0,
            diagnosis_without_parts.json(),
        )
        repair_matching.refresh_from_db()
        self.assertFalse(
            repair_matching.user_tasks.get(
                to_openid=self.factory_matching.wx_openid
            ).need_action
        )

        diagnosis_with_parts = self.client.post(
            f"{self.endpoint}{flow['id']}/factory-return/items/"
            f"{second_repair['id']}/production/",
            {
                "action": "REPAIR_DIAGNOSIS",
                "needs_replacement_material": True,
                "production_remark": "控制模块故障",
                "repair_materials": [
                    {
                        "bom_item_id": self.bom_item.pk,
                        "note": "原控制模块损坏，需要同规格替换",
                    }
                ],
            },
            content_type="application/json",
            **self.auth(self.factory_production),
        )
        self.assertEqual(
            diagnosis_with_parts.json()["code"], 0, diagnosis_with_parts.json()
        )
        diagnosed_item = next(
            row
            for row in diagnosis_with_parts.json()["data"]["items"]
            if row["id"] == second_repair["id"]
        )
        self.assertEqual(diagnosed_item["stock_status"], "REPAIRING")
        self.assertEqual(diagnosed_item["stock_status_name"], "修复处理中")
        repair_matching.refresh_from_db()
        self.assertTrue(
            repair_matching.user_tasks.get(
                to_openid=self.factory_matching.wx_openid
            ).need_action
        )
        requirement = FactoryReturnRepairMaterialRequirement.objects.get(
            inventory_item_id=second_repair["id"],
        )
        self.assertEqual(requirement.material_id, self.fault_material.pk)
        self.assertEqual(requirement.supplier_id, self.supplier.pk)
        self.assertEqual(requirement.trace_source, "PRODUCTION_BATCH")

        # 原订单没有匹配责任销售经理时，只通知供应商负责人，不能阻断
        # 配套经理完成处置，也不能临时随意匹配其他销售经理。
        SupplierSalesResponsibility.objects.filter(
            supplier=self.supplier,
            material=self.fault_material,
        ).delete()
        matching_plan = self.client.post(
            f"{self.endpoint}{flow['id']}/factory-return/items/"
            f"{second_repair['id']}/matching/",
            {
                "action": "REPAIR_MATERIAL_PLAN",
                "material_plan": "沿用原BOM，补领同规格控制模块",
                "matching_remark": "物料准备完成",
            },
            content_type="application/json",
            **self.auth(self.factory_matching),
        )
        self.assertEqual(matching_plan.json()["code"], 0, matching_plan.json())
        requirement.refresh_from_db()
        self.assertIsNotNone(requirement.notified_at)
        repair_supplier_task = WorkItem.objects.get(
            type="FACTORY_RETURN_SUPPLIER_REPAIR_MATERIAL",
            payload__supplier_id=self.supplier.pk,
        )
        self.assertEqual(
            set(repair_supplier_task.user_tasks.values_list("to_openid", flat=True)),
            {self.supplier_owner.wx_openid},
        )
        self.assertEqual(
            repair_supplier_task.payload["unassigned_sales_material_ids"],
            [self.fault_material.pk],
        )
        supplier_view = self.client.get(
            "/pldp/api/lifecycle/supplier-warranty-lists/",
            **self.auth(self.supplier_owner),
        )
        self.assertEqual(supplier_view.json()["code"], 0, supplier_view.json())
        repair_rows = supplier_view.json()["data"]["repair_material_requests"]
        self.assertEqual(len(repair_rows), 1)
        self.assertEqual(repair_rows[0]["material_id"], self.fault_material.pk)
        self.assertEqual(
            repair_rows[0]["trace_source"],
            "PRODUCTION_BATCH",
        )
        repair_matching.refresh_from_db()
        self.assertEqual(repair_matching.status, "DONE")

        # 后续拆机三包分支继续覆盖“已配置责任销售经理则同步通知”。
        SupplierSalesResponsibility.objects.create(
            supplier=self.supplier,
            sales_identity=self.supplier_sales.organization_identity,
            scope_type=SupplierResponsibilityScope.MATERIAL,
            material=self.fault_material,
            assigned_by=self.supplier_owner,
        )

        for item in (first_repair, second_repair):
            completed = self.client.post(
                f"{self.endpoint}{flow['id']}/factory-return/items/"
                f"{item['id']}/production/",
                {
                    "action": "REPAIR_COMPLETE",
                    "production_remark": "修复检测完成",
                },
                content_type="application/json",
                **self.auth(self.factory_production),
            )
            self.assertEqual(completed.json()["code"], 0, completed.json())
        repair_production = WorkItem.objects.get(
            type="FACTORY_RETURN_REPAIR_PRODUCTION",
            biz_ref=f"lifecycle.GoodsTransfer:{flow['id']}",
        )
        self.assertEqual(repair_production.status, "DONE")
        requirement.refresh_from_db()
        self.assertIsNotNone(requirement.returned_material_record_id)
        repair_old_part = requirement.returned_material_record
        self.assertEqual(
            repair_old_part.return_kind,
            FactoryReturnMaterialReturnRecord.ReturnKind.REPAIR_REPLACED,
        )
        self.assertEqual(repair_old_part.returned_quantity, requirement.requested_quantity)
        self.assertFalse(repair_old_part.is_fault_material)
        self.assertEqual(repair_old_part.warranty_result, "IN_WARRANTY")

        # 实际退回与故障件次是两层事实：下面增加一件非故障
        # 但实际拆下退回的关键件，验证它只进实物/结算口径。
        returned_material = Material.objects.create(
            code="FLOW-RETURNED-NON-FAULT",
            name="流转测试非故障关键件",
            product_category=self.product_model.category,
            is_key_part=True,
        )
        returned_spec = MaterialSpec.objects.create(
            material=returned_material,
            code="FLOW-RETURNED-NON-FAULT-SPEC",
            name="非故障关键件规格",
        )
        MaterialSpecSupplier.objects.create(
            material_spec=returned_spec,
            supplier=self.supplier,
            warranty_months=18,
        )
        returned_bom_item = BOMItem.objects.create(
            bom=self.bom_item.bom,
            material=returned_material,
            material_spec=returned_spec,
            supplier=self.supplier,
            quantity=1,
        )
        SupplierSalesResponsibility.objects.create(
            supplier=self.supplier,
            sales_identity=self.supplier_sales.organization_identity,
            scope_type=SupplierResponsibilityScope.MATERIAL,
            material=returned_material,
            assigned_by=self.supplier_owner,
        )

        dismantled = self.client.post(
            f"{self.endpoint}{flow['id']}/factory-return/items/"
            f"{disassembly['id']}/production/",
            {
                "action": "DISASSEMBLY_COMPLETE",
                "returned_materials": [
                    {
                        "bom_item_id": self.bom_item.pk,
                        "note": "控制模块烧蚀，生产现场确认直接关联",
                        "is_fault_material": True,
                    },
                    {
                        "bom_item_id": returned_bom_item.pk,
                        "note": "实际拆下退回，但不是本次拆机原因",
                        "is_fault_material": False,
                    },
                ],
                "production_remark": "整机结束生命周期",
            },
            content_type="application/json",
            **self.auth(self.factory_production),
        )
        self.assertEqual(dismantled.json()["code"], 0, dismantled.json())
        disassembly_production = WorkItem.objects.get(
            type="FACTORY_RETURN_DISASSEMBLY_PRODUCTION",
            biz_ref=f"lifecycle.GoodsTransfer:{flow['id']}",
        )
        self.assertEqual(disassembly_production.status, "DONE")
        disassembly_matching = WorkItem.objects.get(
            type="FACTORY_RETURN_DISASSEMBLY_MATCHING",
            biz_ref=f"lifecycle.GoodsTransfer:{flow['id']}",
        )
        self.assertTrue(
            disassembly_matching.user_tasks.get(
                to_openid=self.factory_matching.wx_openid
            ).need_action
        )
        fault = DeviceMaterialFaultRecord.objects.get(
            inventory_item_id=disassembly["id"]
        )
        self.assertEqual(fault.source_bom_item_id, self.bom_item.pk)
        self.assertEqual(fault.material_id, self.fault_material.pk)
        self.assertEqual(fault.supplier_id, self.supplier.pk)
        self.assertEqual(fault.trace_source, "BOM_INFERRED")
        self.assertEqual(fault.schema_version, 1)
        self.assertTrue(fault.is_warranty_candidate)
        self.assertEqual(fault.material_name_snapshot, "流转测试控制模块")
        self.assertEqual(fault.supplier_name_snapshot, "流转测试配套供应商")
        fault.original_note = "试图覆盖生产现场原始事实"
        with self.assertRaisesMessage(ValueError, "故障物料事实创建后不可覆盖"):
            fault.save()
        fault.refresh_from_db()
        self.assertEqual(
            fault.original_note,
            "控制模块烧蚀，生产现场确认直接关联",
        )
        actual_returns = FactoryReturnMaterialReturnRecord.objects.filter(
            inventory_item_id=disassembly["id"],
            return_kind=(
                FactoryReturnMaterialReturnRecord.ReturnKind.DISASSEMBLY
            ),
        ).order_by("id")
        self.assertEqual(actual_returns.count(), 2)
        self.assertEqual(
            actual_returns.filter(is_fault_material=True).count(),
            1,
        )
        self.assertEqual(
            DeviceMaterialFaultRecord.objects.filter(
                inventory_item_id=disassembly["id"]
            ).count(),
            1,
        )
        self.assertSetEqual(
            set(actual_returns.values_list("warranty_result", flat=True)),
            {"IN_WARRANTY"},
        )

        disposition = self.client.post(
            f"{self.endpoint}{flow['id']}/factory-return/items/"
            f"{disassembly['id']}/matching/",
            {
                "action": "DISASSEMBLY_MATERIAL_DISPOSITION",
                "matching_remark": "待后续关联供应商协作",
            },
            content_type="application/json",
            **self.auth(self.factory_matching),
        )
        self.assertEqual(disposition.json()["code"], 0, disposition.json())
        disassembly_matching.refresh_from_db()
        self.assertEqual(disassembly_matching.status, "DONE")
        dispatch = FactoryReturnWarrantyDispatch.objects.get(
            inventory_item_id=disassembly["id"],
            supplier=self.supplier,
        )
        supplier_workitem = WorkItem.objects.get(
            type="FACTORY_RETURN_SUPPLIER_WARRANTY",
            biz_ref=f"lifecycle.FactoryReturnWarrantyDispatch:{dispatch.pk}",
        )
        self.assertEqual(supplier_workitem.action_mode, "EACH_ACK")
        self.assertEqual(supplier_workitem.status, "DONE")
        self.assertEqual(
            supplier_workitem.link,
            f"/pages/supply_chain/warranty_list/warranty_list?dispatch_id={dispatch.pk}",
        )
        self.assertSetEqual(
            set(supplier_workitem.user_tasks.values_list("to_openid", flat=True)),
            {
                self.supplier_owner.wx_openid,
                self.supplier_sales.wx_openid,
            },
        )
        self.assertFalse(
            supplier_workitem.user_tasks.get(
                to_openid=self.supplier_owner.wx_openid
            ).need_action
        )
        self.assertFalse(
            supplier_workitem.user_tasks.get(
                to_openid=self.supplier_sales.wx_openid
            ).need_action
        )
        evaluation = DeviceMaterialWarrantyEvaluation.objects.get(
            fault_record=fault
        )
        self.assertEqual(evaluation.result, "IN_WARRANTY")
        self.assertEqual(evaluation.warranty_months, 12)
        self.assertEqual(evaluation.warranty_start_date, date(2026, 8, 17))
        self.assertEqual(evaluation.rule_source, "EXACT_SUPPLIER_SPEC")
        supplier_detail = self.client.get(
            f"/pldp/api/lifecycle/factory-return-warranty-dispatches/{dispatch.pk}/",
            **self.auth(self.supplier_sales),
        )
        self.assertEqual(supplier_detail.json()["code"], 0, supplier_detail.json())
        self.assertEqual(
            supplier_detail.json()["data"]["materials"][0]["id"],
            fault.pk,
        )
        self.assertFalse(
            supplier_detail.json()["data"]["actions"]["can_receive"]
        )
        supplier_lists = self.client.get(
            "/pldp/api/lifecycle/supplier-warranty-lists/",
            **self.auth(self.supplier_sales),
        )
        self.assertEqual(supplier_lists.json()["code"], 0, supplier_lists.json())
        supplier_summary = supplier_lists.json()["data"]["summary"]
        self.assertEqual(supplier_summary["return_case_count"], 1)
        self.assertEqual(supplier_summary["returned_material_count"], 2)
        self.assertEqual(supplier_summary["returned_material_quantity"], "2.00")
        self.assertEqual(supplier_summary["fault_material_count"], 1)
        self.assertEqual(supplier_summary["fault_occurrence_count"], 1)
        self.assertEqual(
            len(supplier_lists.json()["data"]["return_materials"]),
            2,
        )
        # 修复需求推送时未给该销售经理分配责任，后来新增责任
        # 也不得让其继承这条历史清单。
        self.assertEqual(
            supplier_lists.json()["data"]["repair_material_requests"],
            [],
        )
        self.assertEqual(
            supplier_lists.json()["data"]["fault_materials"][0]["id"],
            fault.pk,
        )
        owner_lists = self.client.get(
            "/pldp/api/lifecycle/supplier-warranty-lists/",
            **self.auth(self.supplier_owner),
        )
        self.assertEqual(owner_lists.json()["code"], 0, owner_lists.json())
        self.assertEqual(
            owner_lists.json()["data"]["viewer"]["scope_name"],
            "本供应商全部退回、故障与修复数据",
        )
        repair_list_row = owner_lists.json()["data"][
            "repair_material_requests"
        ][0]
        self.assertTrue(repair_list_row["actual_return_recorded"])
        self.assertEqual(repair_list_row["actual_return_quantity"], "1.00")
        self.assertEqual(
            repair_list_row["new_material_quantity_rule"],
            "新换入物料由后续订单计划重新计数，本清单只统计换下退回旧件。",
        )
        sales_task = supplier_workitem.user_tasks.get(
            to_openid=self.supplier_sales.wx_openid
        )
        owner_task = supplier_workitem.user_tasks.get(
            to_openid=self.supplier_owner.wx_openid
        )
        opened = self.client.post(
            "/pldp/api/workflow/task/open/",
            {"usertask_id": sales_task.pk},
            content_type="application/json",
            **self.auth(self.supplier_sales),
        )
        self.assertEqual(opened.json()["code"], 0, opened.json())
        sales_task.refresh_from_db()
        owner_task.refresh_from_db()
        dispatch.refresh_from_db()
        self.assertEqual(sales_task.state, "DONE")
        self.assertEqual(sales_task.done_reason, "OPENED")
        self.assertEqual(owner_task.state, "NEW")
        self.assertEqual(dispatch.status, "NOTIFIED")

        self.assertEqual(
            FactoryReturnInventoryItem.objects.filter(
                process__transfer_id=flow["id"],
                stock_status="PENDING_REISSUE",
            ).count(),
            2,
        )
        self.assertEqual(
            FactoryReturnInventoryItem.objects.filter(
                process__transfer_id=flow["id"],
                stock_status="LIFECYCLE_ENDED",
            ).count(),
            1,
        )
        self.assertEqual(
            DeviceState.objects.get(device=self.devices[2]).status,
            "SCRAPPED",
        )
        self.assertTrue(
            LifecycleEvent.objects.filter(
                device=self.devices[2],
                event_type="LIFECYCLE_CLOSE",
            ).exists()
        )

        inventory = self.client.get(
            "/pldp/api/lifecycle/factory-returns/inventory/",
            **self.auth(self.factory_stock),
        )
        self.assertEqual(inventory.json()["code"], 0, inventory.json())
        self.assertEqual(inventory.json()["data"]["counts"]["PENDING_REISSUE"], 2)
        self.assertEqual(inventory.json()["data"]["counts"]["LIFECYCLE_ENDED"], 1)

    def test_merchant_stock_cannot_transfer_between_subordinates(self):
        response = self.client.get(
            f"{self.endpoint}options/?flow_type=TRANSFER",
            **self.auth(self.parent_stock),
        )
        self.assertEqual(response.json()["code"], 1)

    def _submit_shipment_statement(self, flow_id, amount="1000.00"):
        response = self.client.post(
            f"{self.endpoint}{flow_id}/settlement/statement/",
            {
                "receivable_amount": amount,
                "statement_image": self.image_file("statement.png"),
            },
            **self.auth(self.parent_owner),
        )
        self.assertEqual(response.json()["code"], 0, response.json())
        return response.json()["data"]

    def test_return_credit_is_manually_confirmed_and_offsets_vertical_receivable(self):
        device = self.devices[0]
        self.set_owner(device, self.parent)
        shipment = self.create_flow(
            self.parent_owner,
            {
                "flow_type": "SHIPMENT",
                "client_request_id": "credit-source-shipment",
                "target_account_id": self.child_b_owner.pk,
            },
        )
        self.scan_and_submit(self.parent_owner, shipment["id"], device)
        self._submit_shipment_statement(shipment["id"], "1000.00")
        receipt = self.client.post(
            f"{self.endpoint}{shipment['id']}/receipt/",
            {},
            **self.auth(self.child_b_owner),
        )
        self.assertEqual(receipt.json()["code"], 0, receipt.json())

        return_flow = self.create_flow(
            self.child_b_owner,
            {
                "flow_type": "RETURN",
                "client_request_id": "return-credit-flow",
                "target_account_id": self.parent_sales.pk,
            },
        )
        self.scan_and_submit(self.child_b_owner, return_flow["id"], device)
        returned = self.client.post(
            f"{self.endpoint}{return_flow['id']}/receipt/",
            {},
            **self.auth(self.parent_stock),
        )
        self.assertEqual(returned.json()["code"], 0, returned.json())

        accounts_url = f"{self.endpoint}{return_flow['id']}/accounts/"
        child_view = self.client.get(
            accounts_url,
            **self.auth(self.child_b_owner),
        ).json()
        self.assertEqual(child_view["code"], 0, child_view)
        self.assertEqual(len(child_view["data"]["cases"]), 1)
        self.assertEqual(
            child_view["data"]["cases"][0]["case_type"],
            "RETURN_CREDIT",
        )

        submitted = self.client.post(
            f"{accounts_url}RETURN_CREDIT/submit/",
            {"amount": "300.00", "remark": "关联退货批次"},
            **self.auth(self.child_b_owner),
        )
        self.assertEqual(submitted.json()["code"], 0, submitted.json())
        case = submitted.json()["data"]["cases"][0]
        self.assertEqual(case["status"], "PENDING_CONFIRM")

        confirmed = self.client.post(
            f"{accounts_url}{case['id']}/confirm/",
            {"action": "CONFIRM"},
            content_type="application/json",
            **self.auth(self.parent_owner),
        )
        self.assertEqual(confirmed.json()["code"], 0, confirmed.json())
        parent_case = confirmed.json()["data"]["cases"][0]
        self.assertEqual(parent_case["confirmed_amount"], "300.00")
        self.assertEqual(parent_case["available_credit_amount"], "300.00")
        candidate = next(
            row
            for row in parent_case["allocation_candidates"]
            if row["flow_no"] == shipment["flow_no"]
        )

        allocated = self.client.post(
            f"{accounts_url}{case['id']}/allocations/",
            {
                "target_kind": candidate["target_kind"],
                "target_id": candidate["target_id"],
                "amount": "300.00",
            },
            content_type="application/json",
            **self.auth(self.parent_sales),
        )
        self.assertEqual(allocated.json()["code"], 0, allocated.json())
        allocated_case = allocated.json()["data"]["cases"][0]
        self.assertEqual(allocated_case["available_credit_amount"], "0.00")
        self.assertEqual(GoodsTransferCreditAllocation.objects.count(), 1)

        settlement_detail = self.client.get(
            f"{self.endpoint}{shipment['id']}/settlement/",
            **self.auth(self.parent_owner),
        ).json()["data"]
        self.assertEqual(settlement_detail["confirmed_credit_amount"], "300.00")
        self.assertEqual(settlement_detail["outstanding_amount"], "700.00")
        self.assertFalse(WorkItem.objects.filter(type__icontains="REFUND").exists())

    def test_transfer_creates_two_vertical_cases_and_never_sibling_account(self):
        device = self.devices[1]
        self.set_owner(device, self.child_b)
        flow = self.create_flow(
            self.parent_sales,
            {
                "flow_type": "TRANSFER",
                "client_request_id": "vertical-transfer-account-flow",
                "source_account_id": self.child_b_owner.pk,
                "target_account_id": self.child_c_owner.pk,
            },
        )
        completed = self.scan_and_submit(self.parent_sales, flow["id"], device)
        self.assertEqual(completed["status"], "COMPLETED")
        cases = GoodsTransferAccountCase.objects.filter(transfer_id=flow["id"])
        self.assertEqual(cases.count(), 2)
        self.assertSetEqual(
            set(cases.values_list("case_type", flat=True)),
            {"TRANSFER_OUT_CREDIT", "TRANSFER_IN_RECEIVABLE"},
        )
        self.assertFalse(
            cases.filter(
                upper_organization=self.child_b,
                lower_organization=self.child_c,
            ).exists()
        )
        for case in cases:
            self.assertEqual(case.upper_organization, self.parent)

        accounts_url = f"{self.endpoint}{flow['id']}/accounts/"
        parent_data = self.client.get(
            accounts_url,
            **self.auth(self.parent_owner),
        ).json()["data"]
        self.assertEqual(len(parent_data["cases"]), 2)
        b_data = self.client.get(
            accounts_url,
            **self.auth(self.child_b_owner),
        ).json()["data"]
        c_data = self.client.get(
            accounts_url,
            **self.auth(self.child_c_owner),
        ).json()["data"]
        self.assertEqual(
            [row["case_type"] for row in b_data["cases"]],
            ["TRANSFER_OUT_CREDIT"],
        )
        self.assertEqual(
            [row["case_type"] for row in c_data["cases"]],
            ["TRANSFER_IN_RECEIVABLE"],
        )

        out_submit = self.client.post(
            f"{accounts_url}TRANSFER_OUT_CREDIT/submit/",
            {"amount": "200.00"},
            **self.auth(self.parent_owner),
        ).json()
        self.assertEqual(out_submit["code"], 0, out_submit)
        out_case = next(
            row
            for row in out_submit["data"]["cases"]
            if row["case_type"] == "TRANSFER_OUT_CREDIT"
        )
        in_submit = self.client.post(
            f"{accounts_url}TRANSFER_IN_RECEIVABLE/submit/",
            {"amount": "350.00"},
            **self.auth(self.parent_sales),
        ).json()
        self.assertEqual(in_submit["code"], 0, in_submit)
        in_case = next(
            row
            for row in in_submit["data"]["cases"]
            if row["case_type"] == "TRANSFER_IN_RECEIVABLE"
        )

        b_confirm = self.client.post(
            f"{accounts_url}{out_case['id']}/confirm/",
            {"action": "CONFIRM"},
            content_type="application/json",
            **self.auth(self.child_b_owner),
        )
        self.assertEqual(b_confirm.json()["code"], 0, b_confirm.json())
        c_confirm = self.client.post(
            f"{accounts_url}{in_case['id']}/confirm/",
            {"action": "CONFIRM"},
            content_type="application/json",
            **self.auth(self.child_c_owner),
        )
        self.assertEqual(c_confirm.json()["code"], 0, c_confirm.json())

        c_case = c_confirm.json()["data"]["cases"][0]
        self.assertTrue(c_case["actions"]["can_upload_payment"])
        voucher = self.client.post(
            f"{accounts_url}{in_case['id']}/payments/",
            {
                "client_request_id": "transfer-account-payment-1",
                "voucher_image": self.image_file("transfer-payment.png"),
            },
            **self.auth(self.child_c_stock),
        )
        self.assertEqual(voucher.json()["code"], 0, voucher.json())
        payment = voucher.json()["data"]["cases"][0]["payments"][0]
        payment_confirm = self.client.post(
            f"{accounts_url}{in_case['id']}/payments/{payment['id']}/confirm/",
            {"confirmed_amount": "100.00"},
            content_type="application/json",
            **self.auth(self.parent_stock),
        )
        self.assertEqual(
            payment_confirm.json()["code"],
            0,
            payment_confirm.json(),
        )
        paid_case = next(
            row
            for row in payment_confirm.json()["data"]["cases"]
            if row["case_type"] == "TRANSFER_IN_RECEIVABLE"
        )
        self.assertEqual(paid_case["outstanding_amount"], "250.00")

        forbidden = self.client.post(
            f"{accounts_url}{in_case['id']}/confirm/",
            {"action": "CONFIRM"},
            content_type="application/json",
            **self.auth(self.child_b_owner),
        )
        self.assertEqual(forbidden.json()["code"], 1)
        self.assertFalse(
            GoodsTransferAccountCase.objects.filter(
                creditor_organization=self.child_b,
                debtor_organization=self.child_c,
            ).exists()
        )

    def image_file(self, name="proof.png"):
        return SimpleUploadedFile(
            name,
            b"\x89PNG\r\n\x1a\n" + b"goods-transfer-test-content",
            content_type="image/png",
        )

    def test_shipment_documents_payment_and_receivable_are_independent_of_receipt(self):
        device = self.devices[0]
        self.set_owner(device, self.parent)
        flow = self.create_flow(
            self.parent_stock,
            {
                "flow_type": "SHIPMENT",
                "client_request_id": "shipment-settlement-flow-1",
                "target_account_id": self.child_b_owner.pk,
            },
        )
        submitted = self.scan_and_submit(self.parent_stock, flow["id"], device)
        self.assertEqual(submitted["status"], "PENDING_RECEIPT")

        documents_task = WorkItem.objects.get(
            type="GOODS_TRANSFER_DOCUMENTS_UPLOAD",
            biz_ref=f"lifecycle.GoodsTransfer:{flow['id']}",
        )
        self.assertSetEqual(
            set(documents_task.user_tasks.values_list("to_openid", flat=True)),
            {
                self.parent_owner.wx_openid,
                self.parent_sales.wx_openid,
                self.parent_stock.wx_openid,
            },
        )

        statement = self.client.post(
            f"{self.endpoint}{flow['id']}/settlement/statement/",
            {
                "receivable_amount": "1000.00",
                "statement_image": self.image_file("goods-list.png"),
            },
            **self.auth(self.parent_owner),
        )
        self.assertEqual(statement.json()["code"], 0, statement.json())
        self.assertEqual(statement.json()["data"]["receivable_amount"], "1000.00")
        documents_task.refresh_from_db()
        self.assertEqual(documents_task.status, "OPEN")

        payment_task = WorkItem.objects.get(
            type="GOODS_TRANSFER_PAYMENT_UPLOAD",
            uniq_key=f"lifecycle:goods-transfer:{flow['id']}:payment-upload:1",
        )
        self.assertSetEqual(
            set(payment_task.user_tasks.values_list("to_openid", flat=True)),
            {
                self.child_b_owner.wx_openid,
                self.child_b_stock.wx_openid,
            },
        )

        logistics = self.client.post(
            f"{self.endpoint}{flow['id']}/settlement/logistics-documents/",
            {
                "client_request_id": "settlement-logistics-1",
                "remark": "运单FLOW-001",
                "document_file": self.image_file("logistics.png"),
            },
            **self.auth(self.parent_stock),
        )
        self.assertEqual(logistics.json()["code"], 0, logistics.json())
        documents_task.refresh_from_db()
        self.assertEqual(documents_task.status, "DONE")

        # 实物收货可以先于付款完成，物权转移不结清应收货款。
        receipt = self.client.post(
            f"{self.endpoint}{flow['id']}/receipt/",
            {},
            **self.auth(self.child_b_stock),
        )
        self.assertEqual(receipt.json()["code"], 0, receipt.json())
        self.assertEqual(
            OrganizationDevice.objects.get(device=device).organization,
            self.child_b,
        )
        detail_before_payment = self.client.get(
            f"{self.endpoint}{flow['id']}/settlement/",
            **self.auth(self.child_b_owner),
        ).json()["data"]
        self.assertEqual(detail_before_payment["physical_status"], "COMPLETED")
        self.assertEqual(detail_before_payment["state"], "WAITING_PAYMENT")
        self.assertEqual(detail_before_payment["outstanding_amount"], "1000.00")

        first_voucher = self.client.post(
            f"{self.endpoint}{flow['id']}/settlement/payments/",
            {
                "client_request_id": "settlement-payment-1",
                "voucher_image": self.image_file("payment-1.png"),
            },
            **self.auth(self.child_b_owner),
        )
        self.assertEqual(first_voucher.json()["code"], 0, first_voucher.json())
        first_payment = first_voucher.json()["data"]["payments"][0]
        confirm_first = self.client.post(
            f"{self.endpoint}{flow['id']}/settlement/payments/{first_payment['id']}/confirm/",
            {"confirmed_amount": "400.00"},
            content_type="application/json",
            **self.auth(self.parent_sales),
        )
        self.assertEqual(confirm_first.json()["code"], 0, confirm_first.json())
        self.assertEqual(confirm_first.json()["data"]["confirmed_received_amount"], "400.00")
        self.assertEqual(confirm_first.json()["data"]["outstanding_amount"], "600.00")
        self.assertEqual(confirm_first.json()["data"]["state"], "PARTIALLY_RECEIVED")
        self.assertTrue(
            WorkItem.objects.filter(
                type="GOODS_TRANSFER_PAYMENT_UPLOAD",
                uniq_key=f"lifecycle:goods-transfer:{flow['id']}:payment-upload:2",
                status="OPEN",
            ).exists()
        )

        second_voucher = self.client.post(
            f"{self.endpoint}{flow['id']}/settlement/payments/",
            {
                "client_request_id": "settlement-payment-2",
                "voucher_image": self.image_file("payment-2.png"),
            },
            **self.auth(self.child_b_stock),
        )
        self.assertEqual(second_voucher.json()["code"], 0, second_voucher.json())
        second_payment = second_voucher.json()["data"]["payments"][1]
        confirm_second = self.client.post(
            f"{self.endpoint}{flow['id']}/settlement/payments/{second_payment['id']}/confirm/",
            {"confirmed_amount": "600.00"},
            content_type="application/json",
            **self.auth(self.parent_owner),
        )
        self.assertEqual(confirm_second.json()["code"], 0, confirm_second.json())
        self.assertEqual(confirm_second.json()["data"]["confirmed_received_amount"], "1000.00")
        self.assertEqual(confirm_second.json()["data"]["outstanding_amount"], "0.00")
        self.assertEqual(confirm_second.json()["data"]["state"], "FULLY_RECEIVED")

    def test_goods_transfer_settlement_rejects_non_participant_and_non_shipment(self):
        device = self.devices[1]
        self.set_owner(device, self.parent)
        flow = self.create_flow(
            self.parent_owner,
            {
                "flow_type": "SHIPMENT",
                "client_request_id": "shipment-settlement-access-1",
                "target_account_id": self.child_b_owner.pk,
            },
        )
        self.scan_and_submit(self.parent_owner, flow["id"], device)
        outsider = self.client.get(
            f"{self.endpoint}{flow['id']}/settlement/",
            **self.auth(self.child_c_owner),
        )
        self.assertEqual(outsider.json()["code"], 1)

        self.set_owner(self.devices[2], self.child_b)
        return_flow = self.create_flow(
            self.child_b_owner,
            {
                "flow_type": "RETURN",
                "client_request_id": "return-no-settlement-1",
                "target_account_id": self.parent_sales.pk,
            },
        )
        self.scan_and_submit(self.child_b_owner, return_flow["id"], self.devices[2])
        response = self.client.get(
            f"{self.endpoint}{return_flow['id']}/settlement/",
            **self.auth(self.parent_owner),
        )
        self.assertEqual(response.json()["code"], 1)
