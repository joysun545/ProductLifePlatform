"""Merchant downstream movements carry documents, not commercial debt."""

from base64 import b64decode
from decimal import Decimal
from tempfile import TemporaryDirectory

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone

from account.models import (
    Organization, OrganizationRelation, OrganizationRelationType,
    OrganizationRole, OrganizationType, User, UserOrganization,
)
from lifecycle.models import (
    FactoryReturnDocument, GoodsTransfer, GoodsTransferAccountCase,
    GoodsTransferAccountPayment, GoodsTransferCreditAllocation,
)
from lifecycle.private_storage import goods_transfer_private_storage
from lifecycle.services import goods_transfer_account_service as accounts
from lifecycle.services.factory_return_service import initialize_factory_return_process
from workflow.models import WorkItem


POLICY_MESSAGE = "商家向下发货不再形成应收账款，相关货款往来操作已停用。"


class MerchantAccountPolicyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        owner_type = OrganizationType.objects.get(code="OWNER")
        merchant_type = OrganizationType.objects.get(code="MERCHANT")
        cls.factory = Organization.objects.create(
            name="单据政策厂家", code="DOC-POLICY-FACTORY", organization_type=owner_type,
        )
        cls.upper = Organization.objects.create(
            name="单据政策直属商家", code="DOC-POLICY-UPPER",
            organization_type=merchant_type, factory_root=cls.factory,
        )
        cls.lower = Organization.objects.create(
            name="单据政策下级甲", code="DOC-POLICY-LOWER",
            organization_type=merchant_type, factory_root=cls.factory,
        )
        cls.other_lower = Organization.objects.create(
            name="单据政策下级乙", code="DOC-POLICY-OTHER",
            organization_type=merchant_type, factory_root=cls.factory,
        )
        for source, target in ((cls.factory, cls.upper), (cls.upper, cls.lower),
                               (cls.upper, cls.other_lower)):
            OrganizationRelation.objects.create(
                source_organization=source, target_organization=target,
                relation_type=OrganizationRelationType.DISTRIBUTION,
            )
        cls.upper_user = cls.make_user("doc-policy-upper", cls.upper, "merchant_owner")
        cls.lower_user = cls.make_user("doc-policy-lower", cls.lower, "merchant_owner")
        cls.other_user = cls.make_user("doc-policy-other", cls.other_lower, "merchant_owner")
        cls.assistant = cls.make_user("doc-policy-assistant", cls.factory, "factory_sales_assistant")
        cls.logistics = cls.make_user("doc-policy-logistics", cls.factory, "factory_logistics")
        cls.sales = cls.make_user("doc-policy-sales", cls.factory, "factory_sales")

    @classmethod
    def make_user(cls, name, organization, role_code):
        user = User.objects.create_user(username=name, wx_openid=name + "-openid")
        role = OrganizationRole.objects.get(
            organization_type=organization.organization_type, code=role_code,
        )
        UserOrganization.objects.create(user=user, organization=organization, role=role)
        return user

    def setUp(self):
        self.directory = TemporaryDirectory(prefix="merchant-document-policy-")
        self.original_storage = goods_transfer_private_storage._location
        goods_transfer_private_storage._location = self.directory.name
        self.clear_storage_cache()
        self.addCleanup(self.restore_storage)

    @staticmethod
    def clear_storage_cache():
        for key in ("base_location", "location"):
            goods_transfer_private_storage.__dict__.pop(key, None)

    def restore_storage(self):
        goods_transfer_private_storage._location = self.original_storage
        self.clear_storage_cache()
        self.directory.cleanup()

    def transfer(self, suffix, kind, *, source=None, target=None, instruction=None,
                 status=GoodsTransfer.Status.COMPLETED):
        if kind == GoodsTransfer.FlowType.RETURN:
            source, target = source or self.lower, target or self.upper
            actor, receiver = self.lower_user, self.upper_user
        else:
            source, target = source or self.lower, target or self.other_lower
            actor, receiver = self.upper_user, self.other_user
        return GoodsTransfer.objects.create(
            flow_no="DOC-POLICY-" + suffix, client_request_id="doc-policy-" + suffix,
            flow_type=kind, status=status,
            instruction_organization=instruction or self.upper,
            from_organization=source, to_organization=target,
            initiated_by=actor, target_account=receiver,
            submitted_at=timezone.now(), completed_at=timezone.now(),
        )

    def merchant_case(self, suffix, kind=GoodsTransferAccountCase.CaseType.TRANSFER_IN_RECEIVABLE,
                      status=GoodsTransferAccountCase.Status.CONFIRMED):
        is_return = kind == GoodsTransferAccountCase.CaseType.RETURN_CREDIT
        transfer = self.transfer(suffix, GoodsTransfer.FlowType.RETURN if is_return else GoodsTransfer.FlowType.TRANSFER)
        lower = self.lower if is_return else self.other_lower
        return GoodsTransferAccountCase.objects.create(
            transfer=transfer, case_type=kind, status=status,
            upper_organization=self.upper, lower_organization=lower,
            creditor_organization=lower if is_return else self.upper,
            debtor_organization=self.upper if is_return else lower,
            proposed_amount=Decimal("100.00"), confirmed_amount=Decimal("100.00"),
            submitted_by=self.upper_user, submitted_at=timezone.now(),
        )

    def assert_policy_rejects_without_writes(self, callback):
        case_before = list(GoodsTransferAccountCase.objects.order_by("pk").values())
        payment_before = list(GoodsTransferAccountPayment.objects.order_by("pk").values())
        credit_before = list(GoodsTransferCreditAllocation.objects.order_by("pk").values())
        tasks_before = WorkItem.objects.count()
        with self.assertRaisesMessage(accounts.GoodsTransferAccountError, POLICY_MESSAGE):
            callback()
        self.assertEqual(list(GoodsTransferAccountCase.objects.order_by("pk").values()), case_before)
        self.assertEqual(list(GoodsTransferAccountPayment.objects.order_by("pk").values()), payment_before)
        self.assertEqual(list(GoodsTransferCreditAllocation.objects.order_by("pk").values()), credit_before)
        self.assertEqual(WorkItem.objects.count(), tasks_before)

    def test_return_to_merchant_creates_no_credit_case_or_money_tasks(self):
        transfer = self.transfer("return", GoodsTransfer.FlowType.RETURN)
        rows, created = accounts.initialize_goods_transfer_accounts(transfer=transfer, actor=self.lower_user)
        self.assertEqual(rows, [])
        self.assertFalse(created)
        self.assertFalse(GoodsTransferAccountCase.objects.exists())
        self.assertFalse(WorkItem.objects.exists())
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, GoodsTransfer.Status.COMPLETED)

    def test_merchant_instructed_transfer_creates_neither_receivable_nor_credit(self):
        transfer = self.transfer("transfer", GoodsTransfer.FlowType.TRANSFER)
        rows, created = accounts.initialize_goods_transfer_accounts(transfer=transfer, actor=self.upper_user)
        self.assertEqual(rows, [])
        self.assertFalse(created)
        self.assertFalse(GoodsTransferAccountCase.objects.exists())
        self.assertFalse(WorkItem.objects.exists())

    def test_existing_merchant_cases_are_hidden_without_rewriting_them(self):
        case = self.merchant_case("old-view")
        before = GoodsTransferAccountCase.objects.values().get(pk=case.pk)
        result = accounts.get_goods_transfer_accounts(user=self.upper_user, transfer_id=case.transfer_id)
        self.assertEqual(result["cases"], [])
        self.assertFalse(result["merchant_accounts_enabled"])
        self.assertEqual(result["disabled_reason"], POLICY_MESSAGE)
        self.assertEqual(GoodsTransferAccountCase.objects.values().get(pk=case.pk), before)

    def test_existing_merchant_receivable_cannot_submit_amount(self):
        case = self.merchant_case("old-submit", status=GoodsTransferAccountCase.Status.DRAFT)
        self.assert_policy_rejects_without_writes(lambda: accounts.submit_goods_transfer_account_case(
            user=self.upper_user, transfer_id=case.transfer_id, case_type=case.case_type, amount="100.00",
        ))

    def test_existing_merchant_receivable_cannot_confirm_amount(self):
        case = self.merchant_case("old-confirm", status=GoodsTransferAccountCase.Status.PENDING_CONFIRM)
        self.assert_policy_rejects_without_writes(lambda: accounts.confirm_goods_transfer_account_case(
            user=self.other_user, transfer_id=case.transfer_id, case_id=case.pk, action="CONFIRM",
        ))

    def test_existing_merchant_credit_cannot_allocate_against_any_invoice(self):
        case = self.merchant_case("old-credit", kind=GoodsTransferAccountCase.CaseType.RETURN_CREDIT)
        self.assert_policy_rejects_without_writes(lambda: accounts.allocate_goods_transfer_credit(
            user=self.upper_user, transfer_id=case.transfer_id, case_id=case.pk,
            target_kind="SHIPMENT", target_id=99999, amount="10.00",
        ))

    def test_existing_merchant_receivable_cannot_upload_payment(self):
        case = self.merchant_case("old-pay")
        self.assert_policy_rejects_without_writes(lambda: accounts.upload_goods_transfer_account_payment(
            user=self.other_user, transfer_id=case.transfer_id, case_id=case.pk,
            client_request_id="no-merchant-money", image=None,
        ))

    def test_existing_merchant_receivable_cannot_confirm_payment(self):
        case = self.merchant_case("old-receive")
        self.assert_policy_rejects_without_writes(lambda: accounts.confirm_goods_transfer_account_payment(
            user=self.upper_user, transfer_id=case.transfer_id, case_id=case.pk,
            payment_id=99999, amount="10.00",
        ))

    def test_return_to_factory_retains_owner_credit_case_and_private_goods_list(self):
        transfer = self.transfer(
            "factory-return", GoodsTransfer.FlowType.RETURN, source=self.upper,
            target=self.factory, instruction=self.upper,
            status=GoodsTransfer.Status.PENDING_FACTORY_PICKUP,
        )
        transfer.initiated_by = self.upper_user
        transfer.target_account = self.logistics
        transfer.save(update_fields=("initiated_by", "target_account", "updated_at"))
        image = SimpleUploadedFile("returned-goods.png", b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        ), content_type="image/png")
        process = initialize_factory_return_process(
            transfer=transfer, items=[], actor=self.upper_user,
            goods_list_document=image, goods_list_client_request_id="factory-return-goods",
        )
        case = process.account_case
        self.assertIsNotNone(case)
        self.assertEqual(case.case_type, GoodsTransferAccountCase.CaseType.RETURN_CREDIT)
        self.assertEqual(case.upper_organization_id, self.factory.pk)
        self.assertEqual(case.lower_organization_id, self.upper.pk)
        self.assertEqual(case.status, GoodsTransferAccountCase.Status.DRAFT)
        document = process.documents.get(document_kind=FactoryReturnDocument.DocumentKind.GOODS_LIST)
        self.assertTrue(document.document_file.storage.exists(document.document_file.name))
        self.assertTrue(WorkItem.objects.filter(biz_ref=f"lifecycle.GoodsTransfer:{transfer.pk}").exists())
        self.assertFalse(WorkItem.objects.filter(type__startswith="GOODS_TRANSFER_ACCOUNT").exists())
