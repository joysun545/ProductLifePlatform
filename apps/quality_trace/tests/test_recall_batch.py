from base64 import b64decode
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone

from account.models import (
    Organization, OrganizationRelation, OrganizationRelationType,
    OrganizationRole, OrganizationType, User, UserOrganization,
)
from account.services.auth_service import issue_access_token
from lifecycle.models import (
    ActivationRecord, CustomerDeviceBinding, Device, DeviceState,
    FactoryReturnInventoryItem, FactoryReturnProcess, GoodsTransfer,
    LifecycleEvent, OrganizationDevice,
)
from manufacturing.models import ProductionBatch
from product.models import BOM, ProductCategory, ProductModel
from quality_trace.models import (
    BatchQualityAffectedDevice, BatchQualityCase, QualityRecallBatchOperation,
    QualityRecallCampaign, QualityRecallCustomerHandover, QualityRecallDevice,
    QualityRecallInventoryBatchConfirmation, QualityRecallIsolationReceipt,
    QualityRecallTransportLeg, QualityTraceEvent,
)
from quality_trace.services.recall_batch_service import (
    execute_recall_batch, get_recall_batch_groups,
)
from quality_trace.services.recall_service import (
    QualityRecallError, create_recall_campaign, launch_recall_campaign,
)
from quality_trace.services import recall_transport_service
from quality_trace.services.recall_transport_service import (
    confirm_recall_inventory_batch, create_next_recall_transport_leg,
)
from workflow.models import WorkItem


class RecallEvidenceUploadMixin:
    def setUp(self):
        super().setUp()
        self.evidence_root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(self.settings(QUALITY_RECALL_PRIVATE_ROOT=self.evidence_root))

    def upload_evidence(self, user):
        image = b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1Pe"
            "AAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC"
        )
        response = self.client.post(
            "/pldp/api/quality-trace/recall-evidence/upload/",
            {"file": SimpleUploadedFile("dispatch.png", image, content_type="image/png")},
            HTTP_AUTHORIZATION=f"Bearer {issue_access_token(user)}",
        )
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        evidence = body["data"]
        self.assertTrue((self.evidence_root / evidence["storage_key"]).is_file())
        return evidence


class RecallBatchTests(RecallEvidenceUploadMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        factory_type = OrganizationType.objects.get(code="OWNER")
        merchant_type = OrganizationType.objects.get(code="MERCHANT")
        cls.factory = Organization.objects.create(name="批量测试厂家", code="RB-F", organization_type=factory_type)
        cls.merchants = [Organization.objects.create(
            name=f"批量测试商家{i}", code=f"RB-M{i}", organization_type=merchant_type,
        ) for i in range(3)]
        for parent, child in zip([cls.factory, *cls.merchants], cls.merchants):
            OrganizationRelation.objects.create(
                source_organization=parent, target_organization=child,
                relation_type=OrganizationRelationType.DISTRIBUTION,
            )
        cls.chief = cls.make_user("chief", cls.factory, "factory_chief_engineer")
        cls.logistics = cls.make_user("logistics", cls.factory, "factory_logistics")
        cls.assistant = cls.make_user("assistant", cls.factory, "factory_sales_assistant")
        cls.owners = [cls.make_user(f"owner{i}", org, "merchant_owner") for i, org in enumerate(cls.merchants)]
        cls.middle_stock = cls.make_user("stock", cls.merchants[1], "merchant_stock")
        outsider_org = Organization.objects.create(name="无关商家", code="RB-OTHER", organization_type=merchant_type)
        cls.outsider = cls.make_user("outsider", outsider_org, "merchant_owner")
        category = ProductCategory.objects.create(name="批量测试品类", code="RB-C")
        model = ProductModel.objects.create(name="批量测试型号", code="RB-M", category=category)
        bom = BOM.objects.create(product_model=model, version="V1", created_by=cls.chief)
        batch = ProductionBatch.objects.create(
            batch_no="RB-BATCH", product_model=model, bom=bom,
            manufacturer=cls.factory, production_date=timezone.localdate(), quantity=3,
        )
        cls.devices = [Device.objects.create(sn=f"RB-SN-{i}", product_model=model, batch=batch) for i in range(3)]
        for device, org in zip(cls.devices, [cls.merchants[2], cls.merchants[2], cls.merchants[1]]):
            DeviceState.objects.create(device=device, current_organization=org, status="IN_STOCK")
            OrganizationDevice.objects.create(device=device, organization=org, status="IN_STOCK")
        case = BatchQualityCase.objects.create(
            case_no="RB-CASE", factory_organization=cls.factory, scope_type="TEST",
            scope_key="RB", reason="商家库存批量召回", opened_by=cls.chief,
        )
        for device in cls.devices:
            BatchQualityAffectedDevice.objects.create(
                quality_case=case, device=device, sn_snapshot=device.sn,
                product_model_snapshot={"id": model.pk, "name": model.name},
            )
        cls.campaign = create_recall_campaign(
            user=cls.chief, case_id=case.pk, title="批量召回测试", reason="商家库存测试",
            deadline=timezone.localdate() + timedelta(days=7), risk_level="", remedy_type="",
        )
        launch_recall_campaign(user=cls.chief, campaign_id=cls.campaign.pk)
        cls.campaign.refresh_from_db()
        cls.rows = list(cls.campaign.devices.order_by("device_id"))

    @staticmethod
    def make_user(name, org, role):
        user = User.objects.create_user(username=f"rb-{name}", wx_openid=f"rb-openid-{name}")
        UserOrganization.objects.create(user=user, organization=org, role=OrganizationRole.objects.get(
            organization_type=org.organization_type, code=role,
        ))
        return user

    def run_batch(self, user, action, rows=None, **overrides):
        payload = dict(
            user=user, campaign_id=self.campaign.pk, action=action,
            recall_device_ids=[row.pk for row in (self.rows if rows is None else rows)],
            client_request_id=uuid4().hex, evidence=None,
            logistics_provider="测试物流" if action == "DISPATCH" else "",
            tracking_no="RB-SHIPMENT" if action == "DISPATCH" else "",
            isolation_location="召回隔离-A01" if action == "ISOLATE" else "",
            receipt_note="已逐台核对实物",
        )
        payload.update(overrides)
        if "evidence" not in overrides:
            payload["evidence"] = self.upload_evidence(user)
        return execute_recall_batch(**payload)

    def groups(self, user):
        return get_recall_batch_groups(user=user, campaign=self.campaign)["groups"]

    def transport_to_factory(self):
        self.run_batch(self.owners[2], "DISPATCH", self.rows[:2])
        self.run_batch(self.owners[1], "RECEIVE", self.rows[:2])
        self.run_batch(self.owners[1], "DISPATCH")
        self.run_batch(self.owners[0], "RECEIVE")
        self.run_batch(self.owners[0], "DISPATCH")

    def test_multilevel_return_combines_inventory_and_received_devices(self):
        self.assertEqual(self.groups(self.owners[2])[0]["device_count"], 2)
        self.assertEqual(self.groups(self.owners[1])[0]["device_count"], 1)
        self.assertEqual(self.groups(self.owners[0]), [])
        self.transport_to_factory()
        self.assertEqual(self.groups(self.logistics)[0]["device_count"], 3)
        self.assertEqual(self.groups(self.assistant), [])
        self.run_batch(self.logistics, "RECEIVE")
        self.assertEqual(self.groups(self.assistant)[0]["device_count"], 3)
        with self.assertRaises(QualityRecallError):
            confirm_recall_inventory_batch(user=self.assistant, campaign_id=self.campaign.pk)
        result = self.run_batch(self.assistant, "ISOLATE")
        self.assertEqual(result["processed_count"], 3)
        self.assertEqual(QualityRecallTransportLeg.objects.count(), 8)
        self.assertEqual(QualityRecallIsolationReceipt.objects.count(), 3)
        self.assertFalse(QualityRecallIsolationReceipt.objects.filter(ordinary_inventory_created=True).exists())
        self.assertFalse(GoodsTransfer.objects.exists())
        self.assertFalse(FactoryReturnProcess.objects.exists())
        self.assertFalse(FactoryReturnInventoryItem.objects.exists())
        self.assertEqual(OrganizationDevice.objects.filter(status="IN_STOCK").count(), 0)
        for row in self.rows:
            legs = list(row.transport_legs.order_by("sequence_no"))
            self.assertEqual([leg.sequence_no for leg in legs], list(range(1, len(legs) + 1)))
            self.assertTrue(all(leg.status == "RECEIVED" for leg in legs))
            self.assertTrue(all(leg.dispatch_evidence and leg.receipt_evidence for leg in legs))
        self.assertTrue(WorkItem.objects.filter(
            uniq_key=f"quality-recall:{self.campaign.pk}:batch-posting-confirm", status="OPEN",
        ).exists())
        confirm_recall_inventory_batch(user=self.assistant, campaign_id=self.campaign.pk)
        self.assertTrue(WorkItem.objects.filter(
            uniq_key=f"quality-recall:{self.campaign.pk}:post-recall-disposition", status="OPEN",
            user_tasks__to_openid=self.chief.wx_openid, user_tasks__need_action=True,
        ).exists())
        self.assertFalse(WorkItem.objects.filter(
            type__in=["QUALITY_RECALL_BATCH_RECEIPT", "QUALITY_RECALL_BATCH_RETURN", "QUALITY_RECALL_BATCH_INVENTORY_POSTING"], status="OPEN",
        ).exists())

    def test_inactive_batch_cannot_create_a_posting_confirmation(self):
        self.transport_to_factory()
        self.run_batch(self.logistics, "RECEIVE")
        self.run_batch(self.assistant, "ISOLATE")
        self.assertEqual(self.campaign.isolation_receipts.count(), self.campaign.device_count)
        work_count = WorkItem.objects.count()
        trace_count = QualityTraceEvent.objects.count()
        auth = {"HTTP_AUTHORIZATION": f"Bearer {issue_access_token(self.assistant)}"}

        def confirm(campaign_id):
            response = self.client.post(
                f"/pldp/api/quality-trace/recalls/{campaign_id}/inventory-confirm/", {}, **auth,
            )
            self.assertEqual(response.status_code, 200)
            return response.json()

        missing = confirm(self.campaign.pk + 1)
        self.assertEqual(missing["code"], 1)
        self.assertIn("召回批次不存在", missing["msg"])
        for status in ("SUPERSEDED", "CANCELLED", "DRAFT", "COMPLETED"):
            with self.subTest(status=status):
                QualityRecallCampaign.objects.filter(pk=self.campaign.pk).update(status=status)
                result = confirm(self.campaign.pk)
                self.assertEqual(result["code"], 1)
                self.assertIn("当前不能确认入库记账", result["msg"])
                self.assertFalse(QualityRecallInventoryBatchConfirmation.objects.exists())
                self.assertEqual(WorkItem.objects.count(), work_count)
                self.assertEqual(QualityTraceEvent.objects.count(), trace_count)

        QualityRecallCampaign.objects.filter(pk=self.campaign.pk).update(status="ACTIVE")
        success = confirm(self.campaign.pk)
        self.assertEqual(success["code"], 0)
        QualityRecallCampaign.objects.filter(pk=self.campaign.pk).update(status="COMPLETED")
        self.assertEqual(confirm(self.campaign.pk)["data"], success["data"])
        self.assertEqual(QualityRecallInventoryBatchConfirmation.objects.count(), 1)
        self.assertEqual(WorkItem.objects.filter(
            uniq_key=f"quality-recall:{self.campaign.pk}:post-recall-disposition",
        ).count(), 1)

    def test_disappearing_inventory_returns_business_error_without_partial_dispatch(self):
        row = self.rows[1]
        upstream = recall_transport_service._upstream_target

        def change_inventory(current, factory_id):
            relation = upstream(current, factory_id)
            if OrganizationDevice.objects.filter(device_id=self.rows[0].device_id, status="RETURNED").exists():
                OrganizationDevice.objects.filter(device_id=row.device_id).delete()
            return relation

        models = (QualityRecallBatchOperation, QualityRecallTransportLeg, QualityTraceEvent, WorkItem, LifecycleEvent)
        before = [model.objects.count() for model in models]
        with patch.object(recall_transport_service, "_upstream_target", side_effect=change_inventory):
            with self.assertRaisesMessage(QualityRecallError, "设备库存状态已变化"):
                self.run_batch(self.owners[2], "DISPATCH", self.rows[:2])
        self.assertEqual(before, [model.objects.count() for model in models])
        self.assertEqual(OrganizationDevice.objects.filter(
            device_id__in=[item.device_id for item in self.rows[:2]], status="IN_STOCK",
        ).count(), 2)

    def test_repeat_submission_returns_original_result_without_new_tasks_or_records(self):
        evidence = self.upload_evidence(self.owners[2])
        request = {"client_request_id": "retry-1", "evidence": evidence}
        first = self.run_batch(self.owners[2], "DISPATCH", self.rows[:2], **request)
        counts = [model.objects.count() for model in (QualityRecallBatchOperation, QualityRecallTransportLeg, QualityTraceEvent, WorkItem, LifecycleEvent)]
        (self.evidence_root / evidence["storage_key"]).unlink()
        second = self.run_batch(self.owners[2], "DISPATCH", self.rows[:2], **request)
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(first["operation_id"], second["operation_id"])
        self.assertEqual(counts, [model.objects.count() for model in (QualityRecallBatchOperation, QualityRecallTransportLeg, QualityTraceEvent, WorkItem, LifecycleEvent)])
        with self.assertRaises(QualityRecallError):
            self.run_batch(self.owners[2], "DISPATCH", self.rows[:2], tracking_no="CHANGED", **request)
        with self.assertRaises(QualityRecallError):
            self.run_batch(self.owners[2], "DISPATCH", self.rows[:2])

    def test_wrong_owner_wrong_role_and_unreceived_downstream_cannot_dispatch(self):
        evidence = self.upload_evidence(self.owners[2])
        for user in (self.owners[0], self.owners[1], self.outsider, self.chief):
            with self.assertRaises(QualityRecallError):
                self.run_batch(user, "DISPATCH", self.rows[:2], evidence=evidence)
        self.run_batch(self.owners[2], "DISPATCH", self.rows[:2])
        with self.assertRaises(QualityRecallError):
            self.run_batch(self.owners[1], "DISPATCH", self.rows[:2])
        with self.assertRaises(QualityRecallError):
            self.run_batch(self.logistics, "RECEIVE", self.rows[:2])

    def test_invalid_device_rolls_back_entire_selection(self):
        DeviceState.objects.filter(device=self.devices[1]).update(install_status="INSTALLED")
        with self.assertRaises(QualityRecallError):
            self.run_batch(self.owners[2], "DISPATCH", self.rows[:2])
        self.assertFalse(QualityRecallBatchOperation.objects.exists())
        self.assertFalse(QualityRecallTransportLeg.objects.exists())
        self.assertFalse(LifecycleEvent.objects.exists())
        self.assertEqual(OrganizationDevice.objects.filter(status="IN_STOCK").count(), 3)

    def test_missing_recipient_rolls_back_inventory_events_and_all_legs(self):
        UserOrganization.objects.filter(organization=self.merchants[1]).update(is_active=False)
        before = (WorkItem.objects.count(), QualityTraceEvent.objects.count())
        with self.assertRaisesMessage(QualityRecallError, "没有可接收"):
            self.run_batch(self.owners[2], "DISPATCH", self.rows[:2])
        self.assertFalse(QualityRecallTransportLeg.objects.exists())
        self.assertFalse(QualityRecallBatchOperation.objects.exists())
        self.assertFalse(LifecycleEvent.objects.exists())
        self.assertEqual(OrganizationDevice.objects.filter(status="IN_STOCK").count(), 3)
        self.assertEqual(before, (WorkItem.objects.count(), QualityTraceEvent.objects.count()))

    def test_partial_receipt_keeps_remaining_task_then_closes_all_recipient_tasks(self):
        self.run_batch(self.owners[2], "DISPATCH", self.rows[:2])
        task = WorkItem.objects.get(type="QUALITY_RECALL_BATCH_RECEIPT")
        self.assertEqual(task.user_tasks.count(), 2)
        self.assertNotIn("recall_device_id", task.payload)
        self.run_batch(self.owners[1], "RECEIVE", self.rows[:1])
        task.refresh_from_db()
        self.assertEqual(task.status, "OPEN")
        self.assertEqual(task.payload["pending_count"], 1)
        self.assertEqual(task.payload["pending_device_ids"], [self.rows[1].pk])
        groups = self.groups(self.owners[1])
        self.assertEqual({g["action"]: g["device_count"] for g in groups}, {"RECEIVE": 1, "DISPATCH": 2})
        self.run_batch(self.middle_stock, "RECEIVE", self.rows[1:2])
        task.refresh_from_db()
        self.assertEqual(task.status, "DONE")
        self.assertFalse(task.user_tasks.exclude(state="DONE").exists())
        self.assertEqual(self.groups(self.owners[1])[0]["device_count"], 3)

    def test_different_arriving_shipments_cannot_be_received_as_one_consignment(self):
        self.run_batch(self.owners[2], "DISPATCH", self.rows[:1], tracking_no="TRACK-A")
        self.run_batch(self.owners[2], "DISPATCH", self.rows[1:2], tracking_no="TRACK-B")
        with self.assertRaisesMessage(QualityRecallError, "不同到货批次"):
            self.run_batch(self.owners[1], "RECEIVE", self.rows[:2])
        self.assertEqual(QualityRecallTransportLeg.objects.filter(status="IN_TRANSIT").count(), 2)

    def test_changed_distribution_chain_is_rejected_including_prepared_legs(self):
        leg = create_next_recall_transport_leg(user=self.owners[2], recall_device_id=self.rows[0].pk)
        OrganizationRelation.objects.filter(target_organization=self.merchants[2]).update(is_active=False)
        OrganizationRelation.objects.create(
            source_organization=self.factory, target_organization=self.merchants[2],
            relation_type=OrganizationRelationType.DISTRIBUTION,
        )
        with self.assertRaisesMessage(QualityRecallError, "上级关系已变化"):
            self.run_batch(self.owners[2], "DISPATCH", self.rows[:1])
        leg.refresh_from_db()
        self.assertEqual(leg.status, "PREPARING")

    def test_user_handover_and_inventory_can_share_return_without_losing_activation(self):
        row = self.rows[0]
        customer_type, _ = OrganizationType.objects.get_or_create(code="CUSTOMER", defaults={"name": "用户"})
        customer = Organization.objects.create(
            name="用户组织", code="RB-CUSTOMER", organization_type=customer_type,
            factory_root=self.factory,
        )
        activation = ActivationRecord.objects.create(device=row.device, organization=customer, operator=self.owners[2])
        binding = CustomerDeviceBinding.objects.create(device=row.device, customer_organization=customer, source_organization=self.merchants[2])
        OrganizationDevice.objects.filter(device=row.device).update(organization=customer, status="SOLD")
        DeviceState.objects.filter(device=row.device).update(
            current_organization=customer, status="ACTIVATED", install_status="INSTALLED", activation_status="ACTIVATED",
        )
        QualityRecallCustomerHandover.objects.create(
            campaign=self.campaign, recall_device=row, customer_organization=customer,
            merchant_organization=self.merchants[2], status="MERCHANT_RECEIVED",
        )
        before = ActivationRecord.objects.filter(pk=activation.pk).values().get()
        self.transport_to_factory()
        self.run_batch(self.logistics, "RECEIVE")
        self.run_batch(self.assistant, "ISOLATE")
        self.assertEqual(before, ActivationRecord.objects.filter(pk=activation.pk).values().get())
        binding.refresh_from_db()
        self.assertEqual(binding.status, "ACTIVE")
        self.assertEqual(binding.customer_organization_id, customer.pk)
        state = DeviceState.objects.get(device=row.device)
        self.assertEqual((state.current_organization_id, state.status, state.activation_status), (customer.pk, "ACTIVATED", "ACTIVATED"))
        self.assertFalse(GoodsTransfer.objects.exists())

    def test_campaign_detail_and_batch_api_use_current_identity_and_visible_scope(self):
        auth = {"HTTP_AUTHORIZATION": f"Bearer {issue_access_token(self.owners[2])}"}
        url = f"/pldp/api/quality-trace/recalls/{self.campaign.pk}/"
        response = self.client.get(url, **auth)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["batch_flow"]["groups"][0]["device_count"], 2)
        self.assertEqual({row["campaign_id"] for row in data["devices"]}, {self.campaign.pk})
        body = {"action": "DISPATCH", "recall_device_ids": [row.pk for row in self.rows[:2]],
                "client_request_id": "api-batch", "logistics_provider": "测试物流", "tracking_no": "API-1",
                "evidence": self.upload_evidence(self.owners[2])}
        response = self.client.post(url + "batch-operations/", body, content_type="application/json", **auth)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"]["processed_count"], 2)
        response = self.client.post(url + "batch-operations/", body, content_type="application/json", **auth)
        self.assertTrue(response.json()["data"]["idempotent_replay"])
        upstream = self.client.get(url, HTTP_AUTHORIZATION=f"Bearer {issue_access_token(self.middle_stock)}")
        self.assertEqual(upstream.status_code, 200, upstream.content)
        receive = [g for g in upstream.json()["data"]["batch_flow"]["groups"] if g["action"] == "RECEIVE"]
        self.assertEqual(receive[0]["device_count"], 2)
        for leg in QualityRecallTransportLeg.objects.filter(status="IN_TRANSIT"):
            self.assertEqual(leg.dispatch_evidence["storage_key"], body["evidence"]["storage_key"])
            self.assertNotIn("upload_receipt", leg.dispatch_evidence)

    def test_dispatch_rejects_unverified_wrong_scope_or_removed_evidence_without_changes(self):
        user = self.owners[2]
        deleted = self.upload_evidence(user)
        (self.evidence_root / deleted["storage_key"]).unlink()
        changed = self.upload_evidence(user)
        (self.evidence_root / changed["storage_key"]).write_bytes(b"")
        own = self.upload_evidence(user)
        other = self.upload_evidence(self.owners[1])
        peer = self.make_user("peer", self.merchants[2], "merchant_stock")
        peer_evidence = self.upload_evidence(peer)
        fabricated = {"storage_key": "recall_evidence/never-uploaded.png"}
        models = (QualityRecallBatchOperation, QualityRecallTransportLeg, QualityTraceEvent, WorkItem, LifecycleEvent)
        before = [model.objects.count() for model in models]
        statuses = list(
            self.campaign.devices.order_by("pk").values_list("status", flat=True)
        )
        url = f"/pldp/api/quality-trace/recalls/{self.campaign.pk}/batch-operations/"
        auth = {"HTTP_AUTHORIZATION": f"Bearer {issue_access_token(user)}"}
        for evidence, reason in (
            (fabricated, "未通过上传校验"),
            ({"storage_key": own["storage_key"]}, "未通过上传校验"),
            ({**own, "storage_key": fabricated["storage_key"]}, "不符"),
            ({**own, "upload_receipt": "fabricated"}, "未通过上传校验"),
            (other, "不符"),
            (peer_evidence, "不符"),
            (deleted, "已失效"),
            (changed, "已失效"),
        ):
            with self.subTest(reason=reason, evidence=evidence["storage_key"]):
                response = self.client.post(url, {
                    "action": "DISPATCH", "recall_device_ids": [row.pk for row in self.rows[:2]],
                    "client_request_id": "repair-retry-1", "logistics_provider": "测试物流",
                    "tracking_no": "API-2", "evidence": evidence,
                }, content_type="application/json", **auth)
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(response.json()["code"], 1, response.json())
                self.assertIn(reason, response.json()["msg"])
                self.assertEqual(before, [model.objects.count() for model in models])
                self.assertEqual(statuses, list(
                    self.campaign.devices.order_by("pk").values_list("status", flat=True)
                ))
                self.assertEqual(OrganizationDevice.objects.filter(status="IN_STOCK").count(), 3)
                self.assertFalse(WorkItem.objects.filter(type="QUALITY_RECALL_BATCH_RECEIPT").exists())

        response = self.client.post(url, {
            "action": "DISPATCH", "recall_device_ids": [row.pk for row in self.rows[:2]],
            "client_request_id": "repair-retry-1", "logistics_provider": "测试物流",
            "tracking_no": "API-2", "evidence": {**own, "original_name": "tampered.png"},
        }, content_type="application/json", **auth)
        self.assertEqual(response.json()["code"], 0, response.json())
        self.assertEqual(QualityRecallTransportLeg.objects.filter(status="IN_TRANSIT").count(), 2)
        self.assertEqual(WorkItem.objects.filter(type="QUALITY_RECALL_BATCH_RECEIPT", status="OPEN").count(), 1)
        operation = QualityRecallBatchOperation.objects.get(client_request_id="repair-retry-1")
        self.assertEqual(operation.request_payload["evidence"]["original_name"], "dispatch.png")
        self.assertNotIn("upload_receipt", operation.request_payload["evidence"])
        for leg in QualityRecallTransportLeg.objects.filter(status="IN_TRANSIT"):
            self.assertEqual(leg.dispatch_evidence["original_name"], "dispatch.png")

    def test_superseded_campaign_and_invalid_lists_are_rejected(self):
        for ids in ([], [self.rows[0].pk, self.rows[0].pk], [True], ["1"], [999999], [1] * 1001):
            with self.assertRaises(QualityRecallError):
                self.run_batch(self.owners[2], "DISPATCH", recall_device_ids=ids)
        QualityRecallCampaign.objects.filter(pk=self.campaign.pk).update(status="SUPERSEDED")
        with self.assertRaises(QualityRecallError):
            self.run_batch(self.owners[2], "DISPATCH", self.rows[:2])
        self.assertFalse(QualityRecallTransportLeg.objects.exists())
