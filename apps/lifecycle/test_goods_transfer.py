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
    GoodsTransferAccountCase,
    GoodsTransferCreditAllocation,
    OrganizationDevice,
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
        submit = self.client.post(
            f"{self.endpoint}{transfer_id}/submit/",
            {},
            **self.auth(user),
        )
        self.assertEqual(submit.json()["code"], 0, submit.json())
        return submit.json()["data"]

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

    def test_return_to_factory_terminates_current_lifecycle_after_receipt(self):
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
