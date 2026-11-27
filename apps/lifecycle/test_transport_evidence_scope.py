"""C01 acceptance tests for the explicitly agreed upload contract.

The factory-transport tests form every aftersales/custody state through the
current APIs, starting with the source's full installation/activation fixture.
The OLD tests start with the source's documented persisted preapproval fixture
and approve its real original-machine return through the current API. They never
create replacement stock or mark a swap as completed. Deliberately altered media
metadata appears only in negative isolation/DB-constraint tests, never as proof
of a reachable successful inventory workflow. Permission, tasks and state
transitions are not mocked. The inherited storage patch only isolates files.
"""
import hashlib
from pathlib import Path
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.files.base import ContentFile
from django.db import IntegrityError, connection, connections, transaction
from django.test import Client, TestCase, TransactionTestCase
from django.utils import timezone

from account.models import OrganizationRole
from account.services.auth_service import issue_access_token
from lifecycle.models import AfterSalesEvent, AfterSalesMedia, ReplacementFlow
from lifecycle.services.aftersales_service import AfterSalesService
from workflow.models import UserTask
import lifecycle.test_aftersales_submit_recovery as submission_fixture
import lifecycle.test_replacement_flow as old_fixture


class UploadAssertions:
    @staticmethod
    def auth(user):
        return {'HTTP_AUTHORIZATION': 'Bearer ' + issue_access_token(user)}

    @staticmethod
    def case_url(case):
        return f'/pldp/api/lifecycle/after-sales/applications/{case.pk}/'

    def post_action(self, case, suffix, user, data):
        return self.client.post(self.case_url(case) + suffix + '/', data,
            content_type='application/json', **self.auth(user))

    def upload(self, case, user, *, purpose='REPAIR_RETURN_SHIPMENT_DOCUMENT',
               media_type='IMAGE', content=b'\xff\xd8\xff\xe0c01-image',
               name='proof.jpg', upload_id=None, **metadata):
        data = {'purpose': purpose, 'media_type': media_type,
            'file': SimpleUploadedFile(name, content,
                content_type='image/jpeg' if media_type == 'IMAGE' else 'application/pdf')}
        data.update(metadata)
        if upload_id is not None:
            data['client_upload_id'] = str(upload_id)
        return self.client.post(self.case_url(case) + 'media/', data, **self.auth(user))

    def upload_ok(self, *args, **kwargs):
        response = self.upload(*args, **kwargs)
        self.assertIn(response.status_code, (200, 201), response.json())
        self.assertEqual(response.json()['code'], 0, response.json())
        return response.json()['data']['id']

    def assert_rejected(self, response):
        self.assertEqual(response.status_code, 409, response.json())
        self.assertNotEqual(response.json()['code'], 0, response.json())

    @staticmethod
    def stored_files():
        storage = AfterSalesMedia._meta.get_field('file').storage
        root = Path(storage.location)
        return {str(path.relative_to(root)) for path in root.rglob('*') if path.is_file()}

    @staticmethod
    def task_states(case):
        return list(UserTask.objects.filter(workitem__biz_ref=f'AfterSalesApplication:{case.pk}')
            .order_by('id').values('id', 'state', 'need_action', 'workitem__status'))


class FactoryTransportMediaAcceptance(UploadAssertions, TestCase):
    baseline = submission_fixture.AfterSalesSubmitRecoveryTests
    fixture = baseline.fixture
    install_endpoint = fixture.install_endpoint
    setUpTestData = classmethod(fixture.setUpTestData.__func__)
    _user_identity = classmethod(fixture._user_identity.__func__)
    _create_distribution_relation = classmethod(fixture._create_distribution_relation.__func__)
    _install_and_token = fixture._install_and_token
    _accept_payload = fixture._accept_payload
    _mark_required_documents_read = fixture._mark_required_documents_read
    _post_draft = baseline._post_draft
    _create_draft = baseline._create_draft
    _case_url = staticmethod(baseline._case_url)
    _upload_required_photos = baseline._upload_required_photos
    _submit = baseline._submit
    _successful_submit = baseline._successful_submit

    def setUp(self):
        self.baseline.setUp(self)

    def submitted_installer_case(self):
        case = self._create_draft(self.customer_user)
        self._upload_required_photos(case, self.customer_user)
        self._successful_submit(case, self.customer_user)
        response = self.post_action(case, 'triage', self.factory_aftersales, {
            'preliminary_fault_type': 'INSTALLATION_OR_WIRING',
            'preliminary_fault_materials': [], 'diagnosis_note': '检查原装车线路',
            'repair_plan': '现场检查线路', 'dispatch_route': 'ORIGINAL_INSTALLER',
            'assigned_user_id': self.direct_owner_user.pk,
        })
        self.assertEqual(response.status_code, 200, response.json())
        case.refresh_from_db()
        self.assertEqual(case.execution_stage, 'CONTACT_INSTALLER')
        return case

    def transport_case(self):
        case = self.submitted_installer_case()
        self.upload_ok(case, self.direct_owner_user, purpose='REPAIR_EVIDENCE')
        requested = self.post_action(case, 'escalation', self.direct_owner_user, {
            'action': 'REQUEST', 'requested': 'FACTORY_REPAIR', 'holder': 'EXECUTOR',
            'reason': '现场无法修复，需要逐级返厂', 'factory_return_route': 'CHAIN',
        })
        self.assertEqual(requested.status_code, 200, requested.json())
        decided = self.post_action(case, 'escalation', self.factory_aftersales, {
            'action': 'DECIDE', 'decision': 'FACTORY_REPAIR',
            'receiving_address': '厂家维修部，13900000000',
        })
        self.assertEqual(decided.status_code, 200, decided.json())
        case.refresh_from_db()
        transport = case.escalation['factory_transport']
        self.assertEqual(len(transport['inbound']), 2)
        self.assertTrue(transport['transport_id'])
        return case

    def ft_metadata(self, case, *, index=0, direction='inbound', **extra):
        return {'evidence_action': 'FACTORY_TRANSPORT_SEND',
            'transport_id': case.escalation['factory_transport']['transport_id'],
            'direction': direction, 'index': index, 'evidence_kind': 'LOGISTICS', **extra}

    def ft_upload(self, case, user, *, index=0, direction='inbound', upload_id=None, **extra):
        return self.upload(case, user, upload_id=upload_id or uuid.uuid4(),
            **self.ft_metadata(case, index=index, direction=direction, **extra))

    def advance_first_inbound(self, case, document_ids):
        sent = self.post_action(case, 'escalation', self.direct_owner_user, {
            'action': 'FACTORY_TRANSPORT_SEND', 'direction': 'inbound', 'index': 0,
            'document_ids': document_ids, 'logistics_company': '顺丰', 'tracking_no': 'C01-IN-0',
        })
        self.assertEqual(sent.status_code, 200, sent.json())
        received = self.post_action(case, 'escalation', self.regional_owner_user, {
            'action': 'FACTORY_TRANSPORT_RECEIVE', 'direction': 'inbound', 'index': 0,
            'confirmed': True,
        })
        self.assertEqual(received.status_code, 200, received.json())
        case.refresh_from_db()

    def test_ordinary_application_and_plate_limits_are_unchanged(self):
        case = self._create_draft(self.customer_user)
        for _ in range(9):
            self.upload_ok(case, self.customer_user, purpose='APPLICATION_EVIDENCE')
        self.assert_rejected(self.upload(case, self.customer_user, purpose='APPLICATION_EVIDENCE'))
        for _ in range(3):
            self.upload_ok(case, self.customer_user, purpose='PLATE_PRESENCE_EVIDENCE')
        self.assert_rejected(self.upload(case, self.customer_user, purpose='PLATE_PRESENCE_EVIDENCE'))
        self.assertEqual(case.media.filter(purpose='APPLICATION_EVIDENCE').count(), 9)
        self.assertEqual(case.media.filter(purpose='PLATE_PRESENCE_EVIDENCE').count(), 3)

    def test_ordinary_repair_evidence_limit_is_unchanged(self):
        case = self.submitted_installer_case()
        for _ in range(9):
            self.upload_ok(case, self.direct_owner_user, purpose='REPAIR_EVIDENCE')
        self.assert_rejected(self.upload(case, self.direct_owner_user, purpose='REPAIR_EVIDENCE'))
        self.assertEqual(case.media.filter(purpose='REPAIR_EVIDENCE').count(), 9)

    def test_factory_transport_has_nine_slots_per_leg_not_per_case(self):
        case = self.transport_case()
        first_ids = []
        for _ in range(9):
            response = self.ft_upload(case, self.direct_owner_user)
            self.assertEqual(response.status_code, 201, response.json())
            first_ids.append(response.json()['data']['id'])
        self.assert_rejected(self.ft_upload(case, self.direct_owner_user))
        self.advance_first_inbound(case, first_ids)
        next_ids = []
        for _ in range(9):
            response = self.ft_upload(case, self.regional_owner_user, index=1)
            self.assertEqual(response.status_code, 201, response.json())
            next_ids.append(response.json()['data']['id'])
        self.assert_rejected(self.ft_upload(case, self.regional_owner_user, index=1))
        self.assertEqual(case.media.filter(purpose='REPAIR_RETURN_SHIPMENT_DOCUMENT').count(), 18)
        self.assertEqual(len(set(first_ids + next_ids)), 18)
        for index in (0, 1):
            scope = f"ft:{case.escalation['factory_transport']['transport_id']}:inbound:{index}:send"
            self.assertEqual(case.media.filter(evidence_scope_key=scope).count(), 9)
            self.assertEqual(set(case.media.filter(evidence_scope_key=scope).values_list('scope_slot', flat=True)), set(range(1, 10)))

    def test_factory_transport_rejects_missing_future_and_foreign_scope(self):
        case = self.transport_case()
        invalid = [
            {},
            self.ft_metadata(case, index=1),
            self.ft_metadata(case, transport_id=str(uuid.uuid4())),
            self.ft_metadata(case, index=99),
            self.ft_metadata(case, direction='outbound'),
        ]
        with patch.object(AfterSalesService, '_validate_uploaded_file',
                side_effect=AssertionError('unauthorized files must not be parsed')):
            for metadata in invalid:
                with self.subTest(metadata=metadata):
                    self.assert_rejected(self.upload(case, self.direct_owner_user,
                        upload_id=uuid.uuid4(), **metadata))
            self.assert_rejected(self.ft_upload(case, self.customer_user))
            self.assert_rejected(self.upload(case, self.customer_user, purpose='REPAIR_EVIDENCE'))
        self.assertEqual(case.media.filter(purpose='REPAIR_RETURN_SHIPMENT_DOCUMENT').count(), 0)

    def test_same_file_retry_returns_original_id_and_changed_content_is_rejected(self):
        case = self.transport_case()
        request_id = uuid.uuid4()
        first = self.ft_upload(case, self.direct_owner_user, upload_id=request_id)
        self.assertEqual(first.status_code, 201, first.json())
        repeated = self.ft_upload(case, self.direct_owner_user, upload_id=request_id)
        self.assertIn(repeated.status_code, (200, 201), repeated.json())
        self.assertEqual(repeated.json()['data']['id'], first.json()['data']['id'])
        conflict = self.upload(case, self.direct_owner_user, upload_id=request_id,
            content=b'\xff\xd8\xff\xe0different-content', **self.ft_metadata(case))
        self.assert_rejected(conflict)
        records = case.media.filter(client_upload_id=request_id)
        self.assertEqual(records.count(), 1)
        self.assertEqual(records.get().content_sha256, hashlib.sha256(b'\xff\xd8\xff\xe0c01-image').hexdigest())
        self.assertEqual(case.events.filter(event_type='MEDIA_UPLOADED',
            payload__media_id=first.json()['data']['id']).count(), 1)

    def test_sqlite_constraints_physically_limit_one_scope_to_nine_slots(self):
        self.assertEqual(connection.vendor, 'sqlite')
        case = self.transport_case()
        ids = []
        for _ in range(9):
            response = self.ft_upload(case, self.direct_owner_user)
            self.assertEqual(response.status_code, 201, response.json())
            ids.append(response.json()['data']['id'])
        original = AfterSalesMedia.objects.get(pk=ids[0])
        metadata = {'application': case, 'purpose': original.purpose, 'media_type': 'IMAGE',
            'file': 'constraint-only/no-business-file.jpg', 'uploaded_by': self.direct_owner_user,
            'evidence_scope_key': original.evidence_scope_key, 'evidence_kind': 'LOGISTICS',
            'content_sha256': hashlib.sha256(b'constraint').hexdigest()}
        # These direct inserts intentionally test the database, not file creation
        # or a reachable aftersales workflow. None may become a usable proof.
        for slot in (1, 9, 10, 0, None):
            with self.subTest(slot=slot):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    AfterSalesMedia.objects.create(**metadata, scope_slot=slot,
                        client_upload_id=uuid.uuid4())
        self.assertEqual(case.media.filter(evidence_scope_key=original.evidence_scope_key).count(), 9)

    def test_partial_storage_failure_rolls_back_record_slot_event_and_file(self):
        case = self.transport_case()
        storage = AfterSalesMedia._meta.get_field('file').storage
        files_before = self.stored_files()
        media_before = case.media.count()
        events_before = case.events.count()
        request_id = uuid.uuid4()
        partial_paths = []

        def partially_write_then_fail(name, content, max_length=None):
            target = Path(storage.path(name))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'injected partial private file')
            partial_paths.append(target)
            raise OSError('injected storage failure after partial write')

        with patch.object(storage, 'save', side_effect=partially_write_then_fail):
            with self.assertRaisesRegex(OSError, 'injected storage failure'):
                self.ft_upload(case, self.direct_owner_user, upload_id=request_id)
        self.assertTrue(partial_paths)
        self.assertTrue(all(not path.exists() for path in partial_paths))
        self.assertEqual(self.stored_files(), files_before)
        self.assertEqual(case.media.count(), media_before)
        self.assertEqual(case.events.count(), events_before)
        self.assertFalse(case.media.filter(client_upload_id=request_id).exists())
        retried = self.ft_upload(case, self.direct_owner_user, upload_id=request_id)
        self.assertEqual(retried.status_code, 201, retried.json())
        self.assertEqual(case.media.get(pk=retried.json()['data']['id']).scope_slot, 1)

    def test_event_failure_after_storage_write_deletes_file_and_releases_slot(self):
        case = self.transport_case()
        storage = AfterSalesMedia._meta.get_field('file').storage
        files_before = self.stored_files()
        media_before = case.media.count()
        events_before = case.events.count()
        request_id = uuid.uuid4()
        real_save = storage.save
        with patch.object(storage, 'save', wraps=real_save) as save_spy:
            # Fault injection occurs after a real private file write, rather
            # than replacing normal permissions or business transitions.
            with patch.object(AfterSalesService, '_event',
                    side_effect=RuntimeError('injected post-storage event failure')):
                with self.assertRaisesRegex(RuntimeError, 'injected post-storage event failure'):
                    self.ft_upload(case, self.direct_owner_user, upload_id=request_id)
        self.assertEqual(save_spy.call_count, 1)
        self.assertEqual(self.stored_files(), files_before)
        self.assertEqual(case.media.count(), media_before)
        self.assertEqual(case.events.count(), events_before)
        self.assertFalse(case.media.filter(client_upload_id=request_id).exists())
        retried = self.ft_upload(case, self.direct_owner_user, upload_id=request_id)
        self.assertEqual(retried.status_code, 201, retried.json())
        self.assertEqual(case.media.get(pk=retried.json()['data']['id']).scope_slot, 1)

    def test_same_upload_id_cannot_move_to_later_scope_or_delete_original_file(self):
        case = self.transport_case()
        request_id = uuid.uuid4()
        first = self.ft_upload(case, self.direct_owner_user, upload_id=request_id)
        self.assertEqual(first.status_code, 201, first.json())
        original = AfterSalesMedia.objects.get(pk=first.json()['data']['id'])
        original_name = original.file.name
        original_digest = original.content_sha256
        self.advance_first_inbound(case, [original.pk])

        logistics = self._user_identity('c01-logistics', self.factory,
            OrganizationRole.objects.get(organization_type=self.factory.organization_type,
                code='factory_logistics'))
        second = self.ft_upload(case, self.regional_owner_user, index=1)
        self.assertEqual(second.status_code, 201, second.json())
        sent = self.post_action(case, 'escalation', self.regional_owner_user, {
            'action': 'FACTORY_TRANSPORT_SEND', 'direction': 'inbound', 'index': 1,
            'document_ids': [second.json()['data']['id']], 'logistics_company': '顺丰',
            'tracking_no': 'C01-IN-1',
        })
        self.assertEqual(sent.status_code, 200, sent.json())
        received = self.post_action(case, 'escalation', logistics, {
            'action': 'FACTORY_TRANSPORT_RECEIVE', 'direction': 'inbound', 'index': 1,
            'confirmed': True,
        })
        self.assertEqual(received.status_code, 200, received.json())
        self.upload_ok(case, self.factory_aftersales, purpose='REPAIR_EVIDENCE')
        repaired = self.post_action(case, 'repair-complete', self.factory_aftersales, {
            'repair_summary': '厂家检修并测试完成', 'repair_material_replaced': False,
        })
        self.assertEqual(repaired.status_code, 200, repaired.json())
        finalized = self.post_action(case, 'escalation', self.factory_aftersales, {
            'action': 'FACTORY_TRANSPORT_FINALIZE', 'final_fault_type': 'INSTALLATION_OR_WIRING',
            'final_fault_materials': [], 'final_solution_type': 'MAIL_IN_REPAIR',
            'final_solution_note': '维修完成并按原路返件',
        })
        self.assertEqual(finalized.status_code, 200, finalized.json())
        case.refresh_from_db()
        for index, sender, receiver in (
            (0, logistics, self.regional_owner_user),
            (1, self.regional_owner_user, self.direct_owner_user),
        ):
            document = self.ft_upload(case, sender, direction='outbound', index=index)
            self.assertEqual(document.status_code, 201, document.json())
            outbound = self.post_action(case, 'escalation', sender, {
                'action': 'FACTORY_TRANSPORT_SEND', 'direction': 'outbound', 'index': index,
                'document_ids': [document.json()['data']['id']],
                'logistics_company': '顺丰', 'tracking_no': f'C01-OUT-{index}',
            })
            self.assertEqual(outbound.status_code, 200, outbound.json())
            receipt = self.post_action(case, 'escalation', receiver, {
                'action': 'FACTORY_TRANSPORT_RECEIVE', 'direction': 'outbound', 'index': index,
                'confirmed': True,
            })
            self.assertEqual(receipt.status_code, 200, receipt.json())
            case.refresh_from_db()
        files_before = self.stored_files()
        events_before = case.events.count()
        conflict = self.ft_upload(case, self.direct_owner_user, direction='outbound', index=2,
            upload_id=request_id)
        self.assert_rejected(conflict)
        original.refresh_from_db()
        self.assertEqual(original.file.name, original_name)
        self.assertEqual(original.content_sha256, original_digest)
        self.assertTrue(original.file.storage.exists(original_name))
        with original.file.storage.open(original_name, 'rb') as stored:
            self.assertEqual(hashlib.sha256(stored.read()).hexdigest(), original_digest)
        self.assertEqual(self.stored_files(), files_before)
        self.assertEqual(case.events.count(), events_before)
        self.assertEqual(case.media.filter(client_upload_id=request_id).count(), 1)

    def test_factory_transport_missing_private_file_cannot_send_or_complete_tasks(self):
        case = self.transport_case()
        response = self.ft_upload(case, self.direct_owner_user)
        self.assertEqual(response.status_code, 201, response.json())
        media = AfterSalesMedia.objects.get(pk=response.json()['data']['id'])
        self.assertTrue(media.file.storage.exists(media.file.name))
        tasks_before = self.task_states(case)
        events_before = case.events.count()
        media.file.storage.delete(media.file.name)
        sent = self.post_action(case, 'escalation', self.direct_owner_user, {
            'action': 'FACTORY_TRANSPORT_SEND', 'direction': 'inbound', 'index': 0,
            'document_ids': [media.pk], 'logistics_company': '顺丰', 'tracking_no': 'C01-MISSING',
        })
        self.assert_rejected(sent)
        case.refresh_from_db()
        self.assertEqual(case.escalation['factory_transport']['inbound'][0]['status'], 'PENDING')
        self.assertEqual(self.task_states(case), tasks_before)
        self.assertEqual(case.events.count(), events_before)

    def test_old_column_insert_allows_null_metadata_but_cannot_be_scoped_send_proof(self):
        case = self.transport_case()
        # Reproduce an old process's INSERT column list rather than asking the
        # new ORM to supply Python defaults for columns the old process lacks.
        # The ordinary legacy upload has a real nonempty private file, the
        # correct sender/purpose and a current timestamp; only its new scope
        # metadata is absent when it is submitted to the new sending contract.
        payload = b'\xff\xd8\xff\xe0legacy-schema-compatible-image'
        storage = AfterSalesMedia._meta.get_field('file').storage
        filename = storage.save(f'legacy-schema/{uuid.uuid4().hex}.jpg', ContentFile(payload))
        old_columns = [
            'application_id', 'media_type', 'purpose', 'file', 'original_name',
            'caption', 'content_type', 'file_size', 'duration_seconds', 'sequence',
            'uploaded_by_id', 'created_at',
        ]
        values = [
            case.pk, 'IMAGE', 'REPAIR_RETURN_SHIPMENT_DOCUMENT', filename, 'legacy.jpg',
            '', 'image/jpeg', len(payload), None, case.media.count(),
            self.direct_owner_user.pk, connection.ops.adapt_datetimefield_value(timezone.now()),
        ]
        quote = connection.ops.quote_name
        statement = 'INSERT INTO {} ({}) VALUES ({})'.format(
            quote(AfterSalesMedia._meta.db_table), ', '.join(quote(column) for column in old_columns),
            ', '.join(['%s'] * len(old_columns)),
        )
        with connection.cursor() as cursor:
            cursor.execute(statement, values)
            legacy_id = cursor.lastrowid
        legacy = AfterSalesMedia.objects.get(pk=legacy_id)
        self.assertIsNone(legacy.evidence_scope_key)
        self.assertIsNone(legacy.scope_slot)
        self.assertIsNone(legacy.client_upload_id)
        self.assertIsNone(legacy.evidence_kind)
        self.assertIsNone(legacy.content_sha256)
        self.assertEqual(legacy.file.storage.size(legacy.file.name), legacy.file_size)
        tasks_before = self.task_states(case)
        events_before = case.events.count()
        sent = self.post_action(case, 'escalation', self.direct_owner_user, {
            'action': 'FACTORY_TRANSPORT_SEND', 'direction': 'inbound', 'index': 0,
            'document_ids': [legacy_id], 'logistics_company': '顺丰', 'tracking_no': 'C01-LEGACY',
        })
        self.assert_rejected(sent)
        case.refresh_from_db()
        self.assertEqual(case.escalation['factory_transport']['inbound'][0]['status'], 'PENDING')
        self.assertEqual(self.task_states(case), tasks_before)
        self.assertEqual(case.events.count(), events_before)
        self.assertTrue(legacy.file.storage.exists(legacy.file.name))


class OldReturnMediaAcceptance(UploadAssertions, TestCase):
    user = classmethod(old_fixture.ReplacementFlowTests.user.__func__)
    setUpTestData = classmethod(old_fixture.ReplacementFlowTests.setUpTestData.__func__)

    def setUp(self):
        # Deliberately do not call the fixture's setUp: it mocks workflow and
        # quality checks. This suite retains real permissions/tasks/services.
        approved = self.post_action(self.case, 'escalation', self.af, {
            'action': 'DECIDE', 'decision': 'REPLACEMENT', 'replacement_flow_version': 3,
            'execution_mode': 'FACTORY', 'receiving_address': '厂家维修部',
        })
        self.assertEqual(approved.status_code, 200, approved.json())
        self.case.refresh_from_db()
        self.flow = ReplacementFlow.objects.get(application=self.case)
        self.leg = self.flow.legs.get(direction='OLD')
        self.assertIsNone(self.flow.swapped_at)
        self.assertNotIn('factory_transport', self.case.escalation)

    def old_upload(self, user=None, *, kind='LOGISTICS', upload_id=None, **extra):
        return self.upload(self.case, user or self.sales, upload_id=upload_id or uuid.uuid4(),
            evidence_action='SEND_REPLACEMENT_LEG', leg_id=self.leg.pk,
            evidence_kind=kind, **extra)

    def old_upload_ok(self, **kwargs):
        response = self.old_upload(**kwargs)
        self.assertEqual(response.status_code, 201, response.json())
        return response.json()['data']['id']

    def old_send(self, device_ids, document_ids, *, user=None):
        return self.post_action(self.case, 'escalation', user or self.sales, {
            'action': 'SEND_REPLACEMENT_LEG', 'leg_id': self.leg.pk,
            'device_image_ids': device_ids, 'logistics_document_ids': document_ids,
            'confirmed': True,
        })

    def test_old_return_device_and_logistics_share_nine_slots(self):
        device_ids = [self.old_upload_ok(kind='DEVICE') for _ in range(4)]
        document_ids = [self.old_upload_ok(kind='LOGISTICS') for _ in range(5)]
        self.assert_rejected(self.old_upload(kind='DEVICE'))
        self.assert_rejected(self.old_upload(kind='LOGISTICS'))
        scope = f'old:{self.leg.pk}:send'
        self.assertEqual(self.case.media.filter(evidence_scope_key=scope).count(), 9)
        self.assertEqual(set(self.case.media.filter(evidence_scope_key=scope).values_list('scope_slot', flat=True)), set(range(1, 10)))
        sent = self.old_send(device_ids, document_ids)
        self.assertEqual(sent.status_code, 200, sent.json())
        self.leg.refresh_from_db()
        self.assertEqual(self.leg.status, 'SENT')
        self.assertEqual(set(self.leg.logistics['device_image_ids']), set(device_ids))
        self.assertEqual(set(self.leg.logistics['logistics_document_ids']), set(document_ids))

    def test_old_return_wrong_kind_is_rejected_even_when_both_files_are_images(self):
        device_id = self.old_upload_ok(kind='DEVICE')
        document_id = self.old_upload_ok(kind='LOGISTICS')
        swapped = self.old_send([document_id], [device_id])
        self.assert_rejected(swapped)
        duplicate = self.old_send([device_id], [device_id])
        self.assert_rejected(duplicate)
        self.leg.refresh_from_db()
        self.assertEqual(self.leg.status, 'PENDING')
        accepted = self.old_send([device_id], [document_id])
        self.assertEqual(accepted.status_code, 200, accepted.json())

    def test_old_return_rejects_other_uploaders_proofs(self):
        device_id = self.old_upload_ok(kind='DEVICE')
        document_id = self.old_upload_ok(kind='LOGISTICS')
        # The same-organization owner is a genuine handoff actor, but has not
        # uploaded these files. Rejection must retain uploader ownership.
        response = self.old_send([device_id], [document_id], user=self.owner)
        self.assert_rejected(response)
        self.leg.refresh_from_db()
        self.assertEqual(self.leg.status, 'PENDING')

    def test_old_return_rejects_other_scope_and_legacy_unscoped_files(self):
        device_id = self.old_upload_ok(kind='DEVICE')
        document_id = self.old_upload_ok(kind='LOGISTICS')
        # Negative boundary test only: make one otherwise valid proof carry a
        # different leg's key, then a historical unscoped key. Never use these
        # deliberately malformed metadata rows for a successful transition.
        original_scope = f'old:{self.leg.pk}:send'
        foreign_scope = f'old:{self.leg.pk + 100000}:send'
        AfterSalesMedia.objects.filter(pk=document_id).update(evidence_scope_key=foreign_scope)
        self.assert_rejected(self.old_send([device_id], [document_id]))
        AfterSalesMedia.objects.filter(pk=document_id).update(evidence_scope_key=original_scope)
        legacy = AfterSalesMedia.objects.create(application=self.case,
            purpose='REPAIR_RETURN_SHIPMENT_DOCUMENT', media_type='IMAGE',
            file='negative-boundary-only/historical-unscoped.jpg', uploaded_by=self.sales)
        self.assert_rejected(self.old_send([device_id], [legacy.pk]))
        self.leg.refresh_from_db()
        self.assertEqual(self.leg.status, 'PENDING')

    def test_old_return_rejects_device_documents_and_unscoped_upload_bypass(self):
        invalid_type = self.old_upload(kind='DEVICE', media_type='DOCUMENT',
            name='device.pdf', content=b'%PDF-1.4 device image required')
        self.assert_rejected(invalid_type)
        unscoped = self.upload(self.case, self.sales, upload_id=uuid.uuid4())
        self.assert_rejected(unscoped)
        self.assert_rejected(self.old_upload(kind='OTHER'))

    def test_old_return_missing_private_file_cannot_send_or_complete_tasks(self):
        device_id = self.old_upload_ok(kind='DEVICE')
        document_id = self.old_upload_ok(kind='LOGISTICS')
        media = AfterSalesMedia.objects.get(pk=document_id)
        self.assertTrue(media.file.storage.exists(media.file.name))
        tasks_before = self.task_states(self.case)
        events_before = self.case.events.count()
        media.file.storage.delete(media.file.name)
        sent = self.old_send([device_id], [document_id])
        self.assert_rejected(sent)
        self.leg.refresh_from_db()
        self.assertEqual(self.leg.status, 'PENDING')
        self.assertEqual(self.task_states(self.case), tasks_before)
        self.assertEqual(self.case.events.count(), events_before)


class FactoryTransportConcurrentMediaAcceptance(UploadAssertions, TransactionTestCase):
    """Two real SQLite connections contend for the last scoped slot."""
    baseline = submission_fixture.AfterSalesSubmitRecoveryTests
    fixture = baseline.fixture
    install_endpoint = fixture.install_endpoint
    _user_identity = classmethod(fixture._user_identity.__func__)
    _create_distribution_relation = classmethod(fixture._create_distribution_relation.__func__)
    _install_and_token = fixture._install_and_token
    _accept_payload = fixture._accept_payload
    _mark_required_documents_read = fixture._mark_required_documents_read
    _post_draft = baseline._post_draft
    _create_draft = baseline._create_draft
    _case_url = staticmethod(baseline._case_url)
    _upload_required_photos = baseline._upload_required_photos
    _submit = baseline._submit
    _successful_submit = baseline._successful_submit
    submitted_installer_case = FactoryTransportMediaAcceptance.submitted_installer_case
    transport_case = FactoryTransportMediaAcceptance.transport_case
    ft_metadata = FactoryTransportMediaAcceptance.ft_metadata
    ft_upload = FactoryTransportMediaAcceptance.ft_upload

    def setUp(self):
        from account.test_reference import restore_account_test_reference

        restore_account_test_reference()
        self.fixture.setUpTestData.__func__(type(self))
        self.baseline.setUp(self)

    def test_two_connections_contend_for_last_slot_without_extra_file_or_event(self):
        self.assertEqual(connection.vendor, 'sqlite')
        case = self.transport_case()
        for _ in range(8):
            response = self.ft_upload(case, self.direct_owner_user)
            self.assertEqual(response.status_code, 201, response.json())
        metadata = self.ft_metadata(case)
        authorization = self.auth(self.direct_owner_user)
        url = self.case_url(case) + 'media/'
        files_before = self.stored_files()
        scope = f"ft:{case.escalation['factory_transport']['transport_id']}:inbound:0:send"
        barrier = threading.Barrier(2)
        request_ids = [uuid.uuid4(), uuid.uuid4()]

        def worker(request_id):
            database = connections['default']
            database.ensure_connection()
            connection_id = id(database.connection)
            try:
                barrier.wait(timeout=10)
                response = Client().post(url, {
                    'purpose': 'REPAIR_RETURN_SHIPMENT_DOCUMENT', 'media_type': 'IMAGE',
                    'file': SimpleUploadedFile('last-slot.jpg',
                        b'\xff\xd8\xff\xe0concurrent-' + request_id.bytes, content_type='image/jpeg'),
                    'client_upload_id': str(request_id), **metadata,
                }, **authorization)
                return response.status_code, response.json(), connection_id
            finally:
                database.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker, request_id) for request_id in request_ids]
            outcomes = [future.result(timeout=30) for future in futures]
        self.assertEqual(len({item[2] for item in outcomes}), 2)
        for status_code, body, _ in outcomes:
            self.assertIn(status_code, (201, 409), body)
        rows = list(case.media.filter(evidence_scope_key=scope))
        self.assertLessEqual(len(rows), 9)
        success_count = sum(status_code == 201 for status_code, _, _ in outcomes)
        self.assertEqual(len(rows), 8 + success_count)
        self.assertLessEqual(success_count, 1)
        added_rows = [row for row in rows if row.client_upload_id in request_ids]
        self.assertEqual(len(added_rows), success_count)
        self.assertEqual(self.stored_files() - files_before, {row.file.name for row in added_rows})
        self.assertEqual(AfterSalesEvent.objects.filter(application=case, event_type='MEDIA_UPLOADED',
            payload__evidence_scope_key=scope).count(), len(rows))
        for row in added_rows:
            self.assertTrue(row.file.storage.exists(row.file.name))
        # Shared-cache SQLite may reject both write attempts under contention.
        # A clean retry must still claim the available final slot exactly once.
        if success_count == 0:
            retry = self.ft_upload(case, self.direct_owner_user, upload_id=request_ids[0])
            self.assertEqual(retry.status_code, 201, retry.json())
            self.assertEqual(case.media.filter(evidence_scope_key=scope).count(), 9)

