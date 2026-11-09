from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone

from account.models import OrganizationRole
from lifecycle import tests as fixture_module
from lifecycle.models import (
    AfterSalesApplication,
    AfterSalesMedia,
    CustomerDeviceBinding,
    DriverVehicleBinding,
)
from workflow.models import UserTask, WorkItem


class AfterSalesSubmitRecoveryTests(TestCase):
    fixture = fixture_module.ScanOwnershipAndInstallationTests
    install_endpoint = fixture.install_endpoint
    setUpTestData = classmethod(fixture.setUpTestData.__func__)
    _user_identity = classmethod(fixture._user_identity.__func__)
    _create_distribution_relation = classmethod(fixture._create_distribution_relation.__func__)
    _install_and_token = fixture._install_and_token
    _accept_payload = fixture._accept_payload
    _mark_required_documents_read = fixture._mark_required_documents_read
    auth = fixture.auth

    def setUp(self):
        token = self._install_and_token()
        accepted = self.client.post(
            f"/pldp/api/lifecycle/scan/customer-invitations/{token}/accept/",
            self._accept_payload(),
            **self.auth(self.customer_user),
        )
        self.assertEqual(accepted.status_code, 201, accepted.json())
        self._mark_required_documents_read(self.customer_user)
        device_url = f"/pldp/api/lifecycle/customer/devices/{self.device.pk}/"
        activated = self.client.post(
            f"{device_url}activate/",
            {"activation_method": "VEHICLE_PLATE", "vehicle_no": "渝A12345"},
            **self.auth(self.customer_user),
        )
        self.assertEqual(activated.status_code, 200, activated.json())
        self.draft_url = f"{device_url}after-sales/applications/"
        factory_role = OrganizationRole.objects.get(
            organization_type=self.factory.organization_type,
            code="factory_aftersales",
        )
        self.factory_aftersales = self._user_identity(
            "submit_recovery_factory", self.factory, factory_role,
        )
        storage_root = TemporaryDirectory()
        self.addCleanup(storage_root.cleanup)
        storage = patch.object(
            AfterSalesMedia._meta.get_field("file"),
            "storage",
            FileSystemStorage(location=storage_root.name),
        )
        storage.start()
        self.addCleanup(storage.stop)

    def _post_draft(self, user, *, fault_description="设备启动后立即停机"):
        return self.client.post(
            self.draft_url,
            {
                "fault_description": fault_description,
                "location_name": "测试服务区",
                "location_address": "重庆市渝北区",
                "location_latitude": "29.6000000",
                "location_longitude": "106.5000000",
                "location_confirmed": True,
            },
            content_type="application/json",
            **self.auth(user),
        )

    def _create_draft(self, user, **kwargs):
        response = self._post_draft(user, **kwargs)
        self.assertEqual(response.status_code, 201, response.json())
        return AfterSalesApplication.objects.get(pk=response.json()["data"]["id"])

    @staticmethod
    def _case_url(case):
        return f"/pldp/api/lifecycle/after-sales/applications/{case.pk}/"

    def _upload_required_photos(self, case, user):
        for purpose in ("APPLICATION_EVIDENCE", "PLATE_PRESENCE_EVIDENCE"):
            with self.subTest(purpose=purpose):
                response = self.client.post(
                    f"{self._case_url(case)}media/",
                    {
                        "media_type": "IMAGE",
                        "purpose": purpose,
                        "file": SimpleUploadedFile(
                            "photo.jpg", b"\xff\xd8\xff\xe0test-image", content_type="image/jpeg",
                        ),
                    },
                    **self.auth(user),
                )
                self.assertEqual(response.status_code, 201, response.json())

    def _submit(self, case, user):
        return self.client.post(
            f"{self._case_url(case)}submit/", {}, content_type="application/json", **self.auth(user),
        )

    def _detail(self, case, user):
        return self.client.get(self._case_url(case), **self.auth(user))

    def _successful_submit(self, case, user):
        response = self._submit(case, user)
        self.assertEqual(response.status_code, 200, response.json())
        payload = response.json()["data"]
        self.assertEqual(payload["id"], case.pk)
        self.assertEqual(payload["case_no"], case.case_no)
        self.assertTrue(payload["submitted_at"])
        return payload

    def _driver(self):
        binding = CustomerDeviceBinding.objects.get(device=self.device, status="ACTIVE")
        driver = self._user_identity(
            "submit_recovery_driver", binding.customer_organization, self.driver_role,
        )
        authorization = DriverVehicleBinding.objects.create(vehicle=binding.vehicle, driver=driver)
        return driver, authorization

    def test_retry_returns_same_submission_without_rewriting_content_or_tasks(self):
        applicant = self.customer_user
        case = self._create_draft(applicant)
        self.assertEqual(self._submit(case, applicant).status_code, 409)
        self._upload_required_photos(case, applicant)
        first = self._successful_submit(case, applicant)

        # If the first success never reached the page, the exact draft ID is
        # still known. A fresh draft save must not rewrite that submitted case.
        changed = self._post_draft(applicant, fault_description="变更后的故障描述")
        self.assertEqual(changed.status_code, 409, changed.json())
        retried = self._successful_submit(case, applicant)
        self.assertEqual(retried["status"], "SUBMITTED")
        self.assertEqual(retried["fault_description"], first["fault_description"])
        self.assertEqual(retried["submitted_at"], first["submitted_at"])
        self.assertEqual(AfterSalesApplication.objects.filter(device=self.device).count(), 1)
        self.assertEqual(case.events.filter(event_type="SUBMITTED").count(), 1)
        self.assertEqual(case.media.count(), 2)
        items = WorkItem.objects.filter(biz_ref=f"AfterSalesApplication:{case.pk}")
        self.assertEqual(items.count(), 2)
        self.assertCountEqual(
            UserTask.objects.filter(workitem__in=items).values_list("to_openid", flat=True),
            [applicant.wx_openid, self.factory_aftersales.wx_openid],
        )

    def test_exact_id_recovers_closed_case_while_new_draft_exists(self):
        applicant = self.customer_user
        case = self._create_draft(applicant)
        self._upload_required_photos(case, applicant)
        first = self._successful_submit(case, applicant)
        AfterSalesApplication.objects.filter(pk=case.pk).update(
            status=AfterSalesApplication.Status.CLOSED,
            execution_stage=AfterSalesApplication.ExecutionStage.CLOSED,
            closed_at=timezone.now(),
        )
        new_draft = self._create_draft(applicant, fault_description="下一次设备报修")

        retried = self._successful_submit(case, applicant)
        self.assertEqual(retried["status"], "CLOSED")
        self.assertEqual(retried["submitted_at"], first["submitted_at"])
        self.assertEqual(case.events.filter(event_type="SUBMITTED").count(), 1)
        new_draft.refresh_from_db()
        self.assertEqual(new_draft.status, "DRAFT")
        self.assertIsNone(new_draft.submitted_at)

    def test_other_visible_viewer_cannot_replay_applicant_submit(self):
        case = self._create_draft(self.customer_user)
        self._upload_required_photos(case, self.customer_user)
        self._successful_submit(case, self.customer_user)

        driver, _ = self._driver()
        for viewer in (driver, self.factory_aftersales):
            with self.subTest(viewer=viewer.pk):
                self.assertEqual(self._detail(case, viewer).status_code, 200)
                response = self._submit(case, viewer)
                self.assertEqual(response.status_code, 409, response.json())
                self.assertEqual(response.json()["data"], {})
        self.assertEqual(case.events.filter(event_type="SUBMITTED").count(), 1)

    def test_applicant_with_revoked_vehicle_scope_cannot_replay_submit(self):
        driver, authorization = self._driver()
        case = self._create_draft(driver)
        self._upload_required_photos(case, driver)
        self._successful_submit(case, driver)
        self.assertEqual(self._detail(case, driver).status_code, 200)
        authorization.is_active = False
        authorization.save(update_fields=["is_active"])

        self.assertEqual(self._detail(case, driver).status_code, 409)
        response = self._submit(case, driver)
        self.assertEqual(response.status_code, 409, response.json())
        self.assertEqual(response.json()["data"], {})
        self.assertEqual(case.events.filter(event_type="SUBMITTED").count(), 1)

    def test_cancelled_draft_without_submission_fact_cannot_be_recovered(self):
        applicant = self.customer_user
        case = self._create_draft(applicant)
        AfterSalesApplication.objects.filter(pk=case.pk).update(
            status=AfterSalesApplication.Status.CANCELLED,
        )
        response = self._submit(case, applicant)
        self.assertEqual(response.status_code, 409, response.json())
        self.assertEqual(response.json()["data"], {})
        self.assertFalse(case.events.filter(event_type="SUBMITTED").exists())
