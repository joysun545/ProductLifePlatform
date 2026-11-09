"""Merchant shipments retain physical handoff and private documents, not debt."""
from datetime import date, timedelta
from tempfile import TemporaryDirectory
from unittest.mock import patch
import binascii
import struct
import zlib

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from account.models import (
    InvitationRecord, InvitationScope, Organization, OrganizationRelation,
    OrganizationRelationType, OrganizationRole, OrganizationType,
    RoleInvitationRule, User, UserOrganization,
)
from account.services.auth_service import issue_access_token
from lifecycle.models import (
    DeviceState, GoodsTransfer, GoodsTransferItem, GoodsTransferPayment,
    GoodsTransferPaymentAllocation, GoodsTransferReceivableAccount,
    GoodsTransferSettlement, OrganizationDevice,
)
from lifecycle.private_storage import goods_transfer_private_storage
from lifecycle.services.lifecycle_service import LifecycleService
from manufacturing.models import ProductionBatch
from product.models import BOM, ProductCategory, ProductModel
from workflow.models import WorkItem


def png_bytes():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", binascii.crc32(kind + data) & 0xffffffff)
    header = struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0)
    pixels = (b"\x00" + b"\x20\x80\xc0\xff" * 2) * 2
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b"")


class MerchantShipmentDocumentsOnlyTests(TestCase):
    endpoint = "/pldp/api/lifecycle/goods-transfers/"

    @classmethod
    def user(cls, name, organization, role_code):
        user = User.objects.create_user(username=name, wx_openid="openid-" + name, wx_nickname=name)
        UserOrganization.objects.create(user=user, organization=organization,
            role=OrganizationRole.objects.get(organization_type=organization.organization_type, code=role_code))
        return user

    @classmethod
    def relation(cls, inviter, invitee):
        source = inviter.organization_identity
        target = invitee.organization_identity
        rule, _ = RoleInvitationRule.objects.get_or_create(
            inviter_role=source.role, target_role=target.role,
            scope=InvitationScope.CROSS_ORGANIZATION, relation_type=OrganizationRelationType.DISTRIBUTION)
        invitation = InvitationRecord.objects.create(
            inviter=inviter, source_organization=source.organization, inviter_role=source.role,
            rule=rule, target_role=target.role, target_organization=target.organization,
            invitee=invitee, scope=InvitationScope.CROSS_ORGANIZATION,
            relation_type=OrganizationRelationType.DISTRIBUTION,
            status=InvitationRecord.Status.ACCEPTED, accepted_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=1))
        OrganizationRelation.objects.create(source_organization=source.organization,
            target_organization=target.organization, relation_type=OrganizationRelationType.DISTRIBUTION,
            created_by_invitation=invitation)

    @classmethod
    def setUpTestData(cls):
        cls.factory = Organization.objects.create(name="单据模式厂家", code="DOCS-ROOT",
            organization_type=OrganizationType.objects.get(code="OWNER"))
        merchant_type = OrganizationType.objects.get(code="MERCHANT")
        cls.parent = Organization.objects.create(name="单据模式直属商家", code="DOCS-PARENT",
            organization_type=merchant_type, factory_root=cls.factory)
        cls.child = Organization.objects.create(name="单据模式下级", code="DOCS-CHILD",
            organization_type=merchant_type, factory_root=cls.factory)
        cls.other = Organization.objects.create(name="无关商家", code="DOCS-OTHER",
            organization_type=merchant_type, factory_root=cls.factory)
        cls.factory_sales = cls.user("docs-factory-sales", cls.factory, "factory_sales")
        cls.sender_owner = cls.user("docs-sender-owner", cls.parent, "merchant_owner")
        cls.sender_sales = cls.user("docs-sender-sales", cls.parent, "merchant_sales")
        cls.sender_stock = cls.user("docs-sender-stock", cls.parent, "merchant_stock")
        cls.receiver_owner = cls.user("docs-receiver-owner", cls.child, "merchant_owner")
        cls.receiver_stock = cls.user("docs-receiver-stock", cls.child, "merchant_stock")
        cls.outsider = cls.user("docs-outsider", cls.other, "merchant_owner")
        cls.electrician = cls.user("docs-electrician", cls.parent, "merchant_electrician")
        cls.relation(cls.factory_sales, cls.sender_owner)
        cls.relation(cls.sender_sales, cls.receiver_owner)
        category = ProductCategory.objects.create(name="单据模式品类", code="DOCS-CAT")
        model = ProductModel.objects.create(name="单据模式设备", code="DOCS-MODEL", category=category)
        bom = BOM.objects.create(product_model=model, version_no=1, version="docs", created_by=cls.factory_sales)
        batch = ProductionBatch.objects.create(batch_no="DOCS-BATCH", product_model=model, bom=bom,
            manufacturer=cls.factory, production_date=date(2026, 11, 9), quantity=3)
        cls.devices = []
        for index in range(3):
            device = LifecycleService.create_device(product_model=model, batch=batch,
                organization=cls.factory, operator=cls.factory_sales, sn=f"DOCS-SN-{index}")
            OrganizationDevice.objects.filter(device=device).update(organization=cls.parent, status="IN_STOCK")
            DeviceState.objects.filter(device=device).update(current_organization=cls.parent, status="IN_STOCK",
                install_status="NOT_INSTALLED", activation_status="NOT_ACTIVATED")
            cls.devices.append(device)

    def setUp(self):
        media = TemporaryDirectory(prefix="merchant-documents-only-")
        self.addCleanup(media.cleanup)
        for attribute in ("location", "base_location"):
            change = patch.object(goods_transfer_private_storage, attribute, media.name)
            change.start()
            self.addCleanup(change.stop)

    def auth(self, user):
        return {"HTTP_AUTHORIZATION": "Bearer " + issue_access_token(user)}

    def image(self, name="goods.png"):
        return SimpleUploadedFile(name, png_bytes(), content_type="image/png")

    def post(self, path, user, data=None, *, ok=True):
        response = self.client.post(path, data or {}, **self.auth(user))
        body = response.json()
        if ok:
            self.assertIn(response.status_code, (200, 201), body)
            self.assertEqual(body["code"], 0, body)
        else:
            self.assertNotEqual(body.get("code"), 0, body)
        return body

    def start(self, index=0):
        flow = self.post(self.endpoint, self.sender_stock, {
            "flow_type": "SHIPMENT", "client_request_id": f"docs-flow-{index}",
            "target_account_id": self.receiver_owner.pk})["data"]
        self.post(f"{self.endpoint}{flow['id']}/scan/", self.sender_stock,
                  {"qr_text": self.devices[index].qrcode.code})
        submitted = self.post(f"{self.endpoint}{flow['id']}/submit/", self.sender_stock)["data"]
        self.assertEqual(submitted["status"], "PENDING_RECEIPT")
        return GoodsTransfer.objects.get(pk=flow["id"])

    def url(self, flow, suffix=""):
        return f"{self.endpoint}{flow.pk}/settlement/{suffix}"

    def detail(self, flow, user=None):
        response = self.client.get(self.url(flow), **self.auth(user or self.sender_owner))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["code"], 0, response.json())
        return response.json()["data"]

    def statement(self, flow, *, extra=None, user=None):
        return self.post(self.url(flow, "statement/"), user or self.sender_owner,
            {"statement_image": self.image(), **(extra or {})})["data"]

    def logistics(self, flow):
        return self.post(self.url(flow, "logistics-documents/"), self.sender_stock, {
            "client_request_id": f"docs-logistics-{flow.pk}", "document_file": self.image("waybill.png")})["data"]

    def assert_no_money(self):
        self.assertFalse(GoodsTransferReceivableAccount.objects.exists())
        self.assertFalse(GoodsTransferPayment.objects.exists())
        self.assertFalse(GoodsTransferPaymentAllocation.objects.exists())
        self.assertFalse(WorkItem.objects.filter(type__in=("GOODS_TRANSFER_PAYMENT_UPLOAD", "GOODS_TRANSFER_PAYMENT_CONFIRM")).exists())

    def test_list_without_amount_keeps_required_documents_and_creates_no_debt(self):
        flow = self.start()
        data = self.statement(flow)
        self.assertEqual(data["financial_mode"], "DOCUMENTS_ONLY")
        self.assertFalse(data["financial_enabled"])
        self.assertEqual(data["state"], "WAITING_DOCUMENTS")
        self.assertTrue(data["statement"]["submitted"])
        self.assertFalse(data["commercial_documents_complete"])
        settlement = GoodsTransferSettlement.objects.get(transfer=flow)
        self.assertIsNone(settlement.receivable_amount)
        self.assertEqual(settlement.statement_image.read(), png_bytes())
        for name in ("receivable_amount", "confirmed_received_amount", "confirmed_credit_amount", "outstanding_amount"):
            self.assertIsNone(data[name])
        self.assertEqual(data["payments"], [])
        self.assertFalse(data["rolling_receivables"]["enabled"])
        task = WorkItem.objects.get(type="GOODS_TRANSFER_DOCUMENTS_UPLOAD", biz_ref=f"lifecycle.GoodsTransfer:{flow.pk}")
        self.assertEqual(task.status, "OPEN")
        self.assert_no_money()

    def test_legacy_amount_input_cannot_create_a_receivable(self):
        for index, amount in enumerate(("500.00", "-500.00", "not-money")):
            flow = self.start(index)
            self.statement(flow, extra={"receivable_amount": amount})
            self.assertIsNone(GoodsTransferSettlement.objects.get(transfer=flow).receivable_amount)
        self.assert_no_money()

    def test_missing_or_nonimage_statement_is_rejected_and_no_finance_is_created(self):
        flow = self.start()
        self.post(self.url(flow, "statement/"), self.sender_owner, {}, ok=False)
        self.post(self.url(flow, "statement/"), self.sender_owner,
            {"statement_image": SimpleUploadedFile("bad.png", b"not an image", content_type="image/png")}, ok=False)
        self.assertFalse(GoodsTransferSettlement.objects.get(transfer=flow).statement_submitted_at)
        self.assert_no_money()

    def test_documents_first_do_not_move_goods_and_receipt_remains_required(self):
        flow = self.start()
        self.statement(flow)
        data = self.logistics(flow)
        self.assertEqual(data["state"], "DOCUMENTS_COMPLETE")
        self.assertTrue(data["commercial_documents_complete"])
        flow.refresh_from_db()
        self.assertEqual(flow.status, "PENDING_RECEIPT")
        self.assertEqual(OrganizationDevice.objects.get(device=self.devices[0]).organization_id, self.parent.pk)
        self.assertEqual(flow.items.count(), 1)
        task = WorkItem.objects.get(type="GOODS_TRANSFER_DOCUMENTS_UPLOAD", biz_ref=f"lifecycle.GoodsTransfer:{flow.pk}")
        self.assertEqual(task.status, "DONE")
        self.post(f"{self.endpoint}{flow.pk}/receipt/", self.receiver_stock)
        flow.refresh_from_db()
        self.assertEqual(flow.status, "COMPLETED")
        self.assertEqual(OrganizationDevice.objects.get(device=self.devices[0]).organization_id, self.child.pk)
        self.assert_no_money()

    def test_receipt_first_keeps_count_and_later_documents_complete_normally(self):
        flow = self.start()
        self.post(f"{self.endpoint}{flow.pk}/receipt/", self.receiver_stock)
        before = self.detail(flow)
        self.assertEqual(before["physical_status"], "COMPLETED")
        self.assertEqual(before["goods_transfer"]["device_count"], 1)
        self.assertEqual(before["state"], "WAITING_DOCUMENTS")
        self.logistics(flow)
        data = self.statement(flow)
        self.assertEqual(data["state"], "DOCUMENTS_COMPLETE")
        self.assertEqual(data["goods_transfer"]["device_count"], 1)
        self.assert_no_money()

    def test_manual_payment_and_confirmation_endpoints_are_disabled_before_any_write(self):
        flow = self.start()
        self.statement(flow)
        before_tasks = WorkItem.objects.count()
        self.post(self.url(flow, "payments/"), self.receiver_owner,
            {"client_request_id": "cannot-create-payment", "voucher_image": self.image()}, ok=False)
        self.post(self.url(flow, "payments/99999/confirm/"), self.sender_owner,
            {"confirmed_amount": "500"}, ok=False)
        self.assertEqual(WorkItem.objects.count(), before_tasks)
        self.assert_no_money()

    def test_preview_is_private_and_sender_receiver_can_read_the_same_file(self):
        flow = self.start()
        self.statement(flow)
        for user in (self.sender_owner, self.receiver_owner):
            response = self.client.get(self.url(flow, "statement-file/"), **self.auth(user))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b"".join(response.streaming_content), png_bytes())
            self.assertEqual(response["Cache-Control"], "private, no-store")
            response.close()
        response = self.client.get(self.url(flow, "statement-file/"), **self.auth(self.outsider))
        self.assertNotEqual(response.json()["code"], 0)

    def test_logistics_retry_is_idempotent_and_private_preview_is_preserved(self):
        flow = self.start()
        first = self.logistics(flow)
        second = self.logistics(flow)
        self.assertEqual(len(first["logistics_documents"]), 1)
        self.assertEqual(len(second["logistics_documents"]), 1)
        document_id = first["logistics_documents"][0]["id"]
        for user in (self.sender_owner, self.receiver_owner):
            response = self.client.get(self.url(flow, f"logistics-documents/{document_id}/file/"), **self.auth(user))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b"".join(response.streaming_content), png_bytes())
            self.assertEqual(response["Cache-Control"], "private, no-store")
            response.close()
        response = self.client.get(self.url(flow, f"logistics-documents/{document_id}/file/"), **self.auth(self.outsider))
        self.assertNotEqual(response.json()["code"], 0)
        self.assert_no_money()

    def test_nonparticipant_and_non_document_role_cannot_read_or_submit(self):
        flow = self.start()
        for user in (self.outsider, self.electrician, self.receiver_owner):
            self.post(self.url(flow, "statement/"), user, {"statement_image": self.image()}, ok=False)
        for user in (self.outsider, self.electrician):
            response = self.client.get(self.url(flow), **self.auth(user))
            self.assertNotEqual(response.json()["code"], 0)
        self.assertFalse(GoodsTransferSettlement.objects.get(transfer=flow).statement_submitted_at)
        self.assert_no_money()

    def test_detail_get_does_not_create_or_repair_containers_or_tasks(self):
        flow = self.start()
        GoodsTransferSettlement.objects.filter(transfer=flow).delete()
        header = self.auth(self.sender_owner)
        before_tasks = WorkItem.objects.count()
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url(flow), **header)
        self.assertEqual(response.json()["code"], 0, response.json())
        self.assertFalse(GoodsTransferSettlement.objects.filter(transfer=flow).exists())
        self.assertEqual(WorkItem.objects.count(), before_tasks)
        writes = [q["sql"] for q in queries if q["sql"].lstrip().split(" ", 1)[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"}]
        self.assertEqual(writes, [])
        self.assert_no_money()
