"""Transport evidence contracts using actual private upload API receipts.

The existing batch fixture supplies a legal, launched multilevel merchant recall.
Every proof here is uploaded by its actor through the enabled API; malformed
requests never inject a verified internal snapshot or bypass the upload service.
"""
from uuid import uuid4

from django.core import signing
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from account.models import Organization, OrganizationType
from account.services.auth_service import issue_access_token
from lifecycle.models import DeviceState, LifecycleEvent, OrganizationDevice
from quality_trace.models import (
    QualityRecallBatchOperation, QualityRecallDevice, QualityRecallDeviceEvent,
    QualityRecallIsolationReceipt, QualityRecallTransportLeg, QualityTraceEvent,
)
from workflow.models import UserTask, WorkItem
from quality_trace.services.recall_evidence_service import UPLOAD_RECEIPT_SALT

from . import test_recall_batch as batch_fixtures


class RecallEvidenceValidationTests(batch_fixtures.RecallEvidenceUploadMixin, TestCase):
    make_user = staticmethod(batch_fixtures.RecallBatchTests.make_user)
    run_batch = batch_fixtures.RecallBatchTests.run_batch
    transport_to_factory = batch_fixtures.RecallBatchTests.transport_to_factory

    @classmethod
    def setUpTestData(cls):
        batch_fixtures.RecallBatchTests.setUpTestData.__func__(cls)
        other_root = Organization.objects.create(
            name="凭证隔离厂家", code="RECEIPT-OTHER-ROOT",
            organization_type=OrganizationType.objects.get(code="OWNER"),
        )
        cls.foreign_uploader = cls.make_user("foreign-proof", other_root, "factory_logistics")

    def _post(self, user, path, data):
        response = self.client.post(
            "/pldp/api/quality-trace/" + path, data,
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + issue_access_token(user),
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def _snapshot(self):
        models = (
            QualityRecallBatchOperation, QualityRecallTransportLeg, QualityRecallDevice,
            QualityRecallDeviceEvent, QualityRecallIsolationReceipt, QualityTraceEvent,
            WorkItem, UserTask, LifecycleEvent, DeviceState, OrganizationDevice,
        )
        return {model._meta.label: list(model.objects.order_by("pk").values()) for model in models}

    def _ready(self, action):
        if action == "DISPATCH":
            body = self._post(self.owners[2], f"recall-devices/{self.rows[0].pk}/next-transport/", {})
            self.assertEqual(body["code"], 0, body)
            actor = self.owners[2]
            leg = QualityRecallTransportLeg.objects.get(pk=body["data"]["leg_id"])
        elif action == "RECEIVE":
            self.run_batch(self.owners[2], "DISPATCH", self.rows[:2])
            actor = self.owners[1]
            leg = QualityRecallTransportLeg.objects.get(recall_device=self.rows[0])
        else:
            self.transport_to_factory()
            self.run_batch(self.logistics, "RECEIVE")
            actor = self.assistant
            leg = QualityRecallTransportLeg.objects.get(recall_device=self.rows[0], is_factory_destination=True)
        return actor, leg

    def _invalid_proofs(self, actor):
        own = self.upload_evidence(actor)
        signed = signing.loads(own["upload_receipt"], salt=UPLOAD_RECEIPT_SALT)
        legacy = {key: value for key, value in signed.items() if key != "user_id"}
        malformed_receipts = (
            legacy, [], {}, {**signed, "evidence": None},
            {**signed, "evidence": {"storage_key": own["storage_key"]}},
            {**signed, "evidence": {**signed["evidence"], "file_size": True}},
        )
        identity = actor.organization_identity
        peer = self.make_user("proof-peer-" + uuid4().hex, identity.organization, identity.role.code)
        peer_proof = self.upload_evidence(peer)
        foreign = self.upload_evidence(self.foreign_uploader)
        deleted = self.upload_evidence(actor)
        (self.evidence_root / deleted["storage_key"]).unlink()
        empty = self.upload_evidence(actor)
        (self.evidence_root / empty["storage_key"]).write_bytes(b"")
        changed = self.upload_evidence(actor)
        (self.evidence_root / changed["storage_key"]).write_bytes(b"different-nonempty-size")
        directory = self.upload_evidence(actor)
        path = self.evidence_root / directory["storage_key"]
        path.unlink()
        path.mkdir()
        return own, (
            None, [], {"storage_key": "recall_evidence/missing.jpg", "verified": True},
            {"storage_key": own["storage_key"]},
            {**own, "upload_receipt": "forged"},
            {**own, "storage_key": "recall_evidence/changed.jpg"},
            peer_proof, foreign, deleted, empty, changed, directory,
            *({**own, "upload_receipt": signing.dumps(value, salt=UPLOAD_RECEIPT_SALT)}
              for value in malformed_receipts),
        )

    def _single_body(self, action, proof):
        return {
            "evidence": proof, "logistics_provider": "召回专线", "tracking_no": "PROOF-1",
            "receipt_note": "核对SN后验收", "isolation_location": "凭证隔离库-A1",
            "disposition": QualityRecallIsolationReceipt.Disposition.PENDING_INSPECTION,
        }

    def _check_single(self, action):
        actor, leg = self._ready(action)
        own, invalid = self._invalid_proofs(actor)
        before = self._snapshot()
        path = f"recall-transport-legs/{leg.pk}/{action.lower()}/"
        for proof in invalid:
            with self.subTest(action=action, proof=proof):
                body = self._post(actor, path, self._single_body(action, proof))
                self.assertEqual(body["code"], 1, body)
                self.assertEqual(before, self._snapshot())
        body = self._post(actor, path, self._single_body(action, {**own, "original_name": "client-edited.jpg"}))
        self.assertEqual(body["code"], 0, body)
        leg.refresh_from_db()
        if action == "ISOLATE":
            snapshot = QualityRecallIsolationReceipt.objects.get(transport_leg=leg).evidence_snapshot
        else:
            snapshot = getattr(leg, "dispatch_evidence" if action == "DISPATCH" else "receipt_evidence")
        self.assertEqual(snapshot["storage_key"], own["storage_key"])
        self.assertEqual(snapshot["original_name"], "dispatch.png")
        self.assertNotIn("upload_receipt", snapshot)
        self.assertNotIn("verified", snapshot)

    def _check_batch(self, action):
        actor, leg = self._ready(action)
        own, invalid = self._invalid_proofs(actor)
        before = self._snapshot()
        path = f"recalls/{self.campaign.pk}/batch-operations/"
        request = {
            **self._single_body(action, None), "action": action,
            "recall_device_ids": [leg.recall_device_id], "client_request_id": "proof-batch-retry",
        }
        for proof in invalid:
            with self.subTest(action=action, proof=proof):
                body = self._post(actor, path, {**request, "evidence": proof})
                self.assertEqual(body["code"], 1, body)
                self.assertEqual(before, self._snapshot())
        request["evidence"] = own
        body = self._post(actor, path, request)
        self.assertEqual(body["code"], 0, body)
        operation = QualityRecallBatchOperation.objects.get(pk=body["data"]["operation_id"])
        self.assertNotIn("upload_receipt", operation.request_payload["evidence"])
        after = self._snapshot()
        (self.evidence_root / own["storage_key"]).unlink()
        replay = self._post(actor, path, request)
        self.assertEqual(replay["code"], 0, replay)
        self.assertTrue(replay["data"]["idempotent_replay"])
        self.assertEqual(after, self._snapshot())
        changed = self._post(actor, path, {**request, "receipt_note": "改变提交内容"})
        self.assertEqual(changed["code"], 1, changed)
        self.assertEqual(after, self._snapshot())

    def test_single_dispatch_requires_actor_uploaded_existing_proof(self):
        self._check_single("DISPATCH")

    def test_single_receive_requires_actor_uploaded_existing_proof(self):
        self._check_single("RECEIVE")

    def test_single_isolate_requires_actor_uploaded_existing_proof(self):
        self._check_single("ISOLATE")

    def test_batch_dispatch_validates_before_changes_and_replays_without_file(self):
        self._check_batch("DISPATCH")

    def test_batch_receive_validates_before_changes_and_replays_without_file(self):
        self._check_batch("RECEIVE")

    def test_batch_isolate_validates_before_changes_and_replays_without_file(self):
        self._check_batch("ISOLATE")

    def test_empty_upload_does_not_issue_a_receipt(self):
        response = self.client.post(
            "/pldp/api/quality-trace/recall-evidence/upload/",
            {"file": SimpleUploadedFile("empty.png", b"", content_type="image/png")},
            HTTP_AUTHORIZATION="Bearer " + issue_access_token(self.owners[2]),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1)
        self.assertNotIn("upload_receipt", response.json().get("data", {}))
