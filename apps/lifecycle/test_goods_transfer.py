from datetime import date, timedelta

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
    DeviceState,
    FactoryReturnDocument,
    FactoryReturnInventoryItem,
    FactoryReturnProcess,
    GoodsTransfer,
    GoodsTransferAccountCase,
    GoodsTransferCreditAllocation,
    LifecycleEvent,
    OrganizationDevice,
    QRCode,
)
from lifecycle.services.lifecycle_service import LifecycleService
from manufacturing.models import ProductionBatch
from product.models import BOM, ProductCategory, ProductModel
from workflow.models import WorkItem


class GoodsTransferApiTests(TestCase):
    endpoint = "/pldp/api/lifecycle/goods-transfers/"

    @classmethod
    def setUpTestData(cls):
        owner_type = OrganizationType.objects.get(code="OWNER")
        merchant_type = OrganizationType.objects.get(code="MERCHANT")
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
        cls.child_b_stock = cls._user(
            "flow_child_b_stock", cls.child_b, cls.merchant_stock_role
        )
        cls.child_c_owner = cls._user(
            "flow_child_c_owner", cls.child_c, cls.merchant_owner_role
        )
        cls.child_c_stock = cls._user(
            "flow_child_c_stock", cls.child_c, cls.merchant_stock_role
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
                "material_requirement": "更换控制模块一件",
            },
            content_type="application/json",
            **self.auth(self.factory_production),
        )
        self.assertEqual(
            diagnosis_with_parts.json()["code"], 0, diagnosis_with_parts.json()
        )
        repair_matching.refresh_from_db()
        self.assertTrue(
            repair_matching.user_tasks.get(
                to_openid=self.factory_matching.wx_openid
            ).need_action
        )

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
        repair_matching.refresh_from_db()
        self.assertEqual(repair_matching.status, "DONE")

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

        dismantled = self.client.post(
            f"{self.endpoint}{flow['id']}/factory-return/items/"
            f"{disassembly['id']}/production/",
            {
                "action": "DISASSEMBLY_COMPLETE",
                "disassembly_result": "拆出发电机总成和控制模块",
                "has_warranty_material": True,
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

        disposition = self.client.post(
            f"{self.endpoint}{flow['id']}/factory-return/items/"
            f"{disassembly['id']}/matching/",
            {
                "action": "DISASSEMBLY_MATERIAL_DISPOSITION",
                "warranty_disposition": "控制模块进入三包责任追溯",
                "matching_remark": "待后续关联供应商协作",
            },
            content_type="application/json",
            **self.auth(self.factory_matching),
        )
        self.assertEqual(disposition.json()["code"], 0, disposition.json())
        disassembly_matching.refresh_from_db()
        self.assertEqual(disassembly_matching.status, "DONE")

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
