import tempfile
import uuid
from io import BytesIO
from unittest.mock import patch

from PIL import Image
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings

from account.models import Organization, OrganizationRole, OrganizationType, UserOrganization
from workflow.models import UserTask, WorkItem

from .models import RootTrialApplication
from .private_storage import private_storage
from .services import (
    RootTrialError, application_payload, create_license_upload, eligibility,
    get_application_for_user, get_license_for_user, open_license_file,
    review_application, submit_application,
)


class RootTrialServiceTests(TestCase):
    def setUp(self):
        self.private_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.private_dir.cleanup)
        settings_patch = override_settings(ROOT_TRIAL_PRIVATE_ROOT=self.private_dir.name)
        settings_patch.enable()
        self.addCleanup(settings_patch.disable)
        User = get_user_model()
        self.guest = User.objects.create_user(username="rt_guest", wx_openid="rt-guest-openid")
        self.other = User.objects.create_user(username="rt_other", wx_openid="rt-other-openid")
        self.admin = User.objects.create_user(
            username="rt_admin", is_superuser=True, is_staff=True, wx_openid="rt-admin-openid",
        )
        self.admin2 = User.objects.create_user(
            username="rt_admin2", is_superuser=True, is_staff=True, wx_openid="rt-admin2-openid",
        )
        self.owner_type, _ = OrganizationType.objects.get_or_create(
            code="OWNER", defaults={"name": "厂家"},
        )
        self.owner_type.is_active = True
        self.owner_type.save(update_fields=("is_active",))
        self.role, _ = OrganizationRole.objects.get_or_create(
            organization_type=self.owner_type, code="factory_admin",
            defaults={"name": "厂家负责人", "is_owner_role": True},
        )
        self.role.is_active = True
        self.role.is_owner_role = True
        self.role.save(update_fields=("is_active", "is_owner_role"))

    def image(self, color="white"):
        stream = BytesIO()
        Image.new("RGB", (12, 8), color).save(stream, format="PNG")
        return SimpleUploadedFile("license.png", stream.getvalue(), content_type="image/png")

    def media(self, user=None):
        return create_license_upload(user or self.guest, self.image())

    def application(self, **kwargs):
        media = kwargs.pop("media", None) or self.media()
        application, _ = submit_application(
            kwargs.pop("user", self.guest), kwargs.pop("name", "测试制造企业"),
            media.pk, kwargs.pop("request_id", uuid.uuid4()),
        )
        return application

    def identity(self, user=None, active=True):
        org = Organization.objects.create(
            name="现有组织", code=f"RT_{uuid.uuid4().hex}", organization_type=self.owner_type,
        )
        return UserOrganization.objects.create(
            user=user or self.guest, organization=org, role=self.role, is_active=active,
        )

    def test_submission_creates_platform_review_without_any_business_identity(self):
        org_count = Organization.objects.count()
        application = self.application()
        self.assertTrue(eligibility(self.guest)["eligible"])
        self.assertFalse(eligibility(self.guest)["can_submit"])
        self.assertFalse(UserOrganization.objects.filter(user=self.guest).exists())
        self.assertEqual(Organization.objects.count(), org_count)
        task = WorkItem.objects.get(uniq_key=f"root-trial-review:{application.pk}")
        self.assertEqual(task.payload["scope"], "PLATFORM")
        self.assertIsNone(task.payload["root_organization_id"])
        self.assertEqual(task.status, "OPEN")
        self.assertEqual(task.action_mode, "ANYONE_DO")
        self.assertEqual(set(task.user_tasks.values_list("to_openid", flat=True)),
                         {self.admin.wx_openid, self.admin2.wx_openid})

    def test_inactive_retained_identity_cannot_start_new_root(self):
        self.identity(active=False)
        self.assertEqual(eligibility(self.guest)["code"], "IDENTITY_EXISTS")
        with self.assertRaises(RootTrialError) as raised:
            self.media()
        self.assertEqual(raised.exception.http_status, 403)

    def test_original_retry_returns_same_application_without_resetting_tasks(self):
        media = self.media()
        request_id = uuid.uuid4()
        first, created = submit_application(self.guest, "测试企业", media.pk, request_id)
        self.assertTrue(created)
        task = UserTask.objects.filter(workitem__type="FACTORY_TRIAL_REVIEW").first()
        task.state = "READ"
        task.save(update_fields=("state",))
        again, created = submit_application(self.guest, "测试企业", media.pk, request_id)
        task.refresh_from_db()
        self.assertFalse(created)
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(task.state, "READ")
        self.assertEqual(WorkItem.objects.count(), 1)

    def test_changed_contents_with_same_request_id_are_rejected(self):
        media = self.media()
        application = self.application(media=media)
        with self.assertRaises(RootTrialError) as raised:
            submit_application(self.guest, "另一企业", media.pk, application.request_id)
        self.assertEqual(raised.exception.code, "REQUEST_CONTENT_CHANGED")

    def test_another_request_id_does_not_duplicate_pending_application(self):
        media = self.media()
        first = self.application(media=media)
        again, created = submit_application(self.guest, first.organization_name, media.pk, uuid.uuid4())
        self.assertFalse(created)
        self.assertEqual(again.pk, first.pk)
        with self.assertRaises(RootTrialError) as raised:
            submit_application(self.guest, "变更申请内容", media.pk, uuid.uuid4())
        self.assertEqual(raised.exception.code, "APPLICATION_PENDING")

    def test_approval_creates_self_rooted_factory_admin_without_platform_privilege(self):
        application = self.application()
        approved, changed = review_application(self.admin, application.pk, "APPROVE")
        self.assertTrue(changed)
        identity = UserOrganization.objects.get(user=self.guest)
        self.assertEqual(identity.role_id, self.role.pk)
        self.assertEqual(identity.inviter_id, self.admin.pk)
        self.assertEqual(identity.organization.factory_root_id, identity.organization_id)
        self.assertEqual(approved.identity_id, identity.pk)
        self.assertEqual(approved.organization_id, identity.organization_id)
        self.guest.refresh_from_db()
        self.assertFalse(self.guest.is_staff)
        self.assertFalse(self.guest.is_superuser)
        self.assertEqual(self.guest.wx_openid, "rt-guest-openid")

    def test_approval_retry_is_idempotent_and_conflicting_rejection_fails(self):
        application = self.application()
        first, _ = review_application(self.admin, application.pk, "APPROVE")
        task_count = WorkItem.objects.count()
        again, changed = review_application(self.admin2, application.pk, "APPROVE")
        self.assertFalse(changed)
        self.assertEqual(first.organization_id, again.organization_id)
        self.assertEqual(WorkItem.objects.count(), task_count)
        with self.assertRaises(RootTrialError) as raised:
            review_application(self.admin2, application.pk, "REJECT", "不同决定")
        self.assertEqual(raised.exception.code, "ALREADY_REVIEWED")

    def test_rejected_application_preserves_history_and_can_be_resubmitted(self):
        old = self.application()
        rejected, changed = review_application(self.admin, old.pk, "REJECT", "请补充清晰图片")
        self.assertTrue(changed)
        self.assertEqual(rejected.review_reason, "请补充清晰图片")
        self.assertTrue(eligibility(self.guest)["can_submit"])
        new = self.application(media=old.license_upload, name="修正后制造企业")
        old.refresh_from_db()
        self.assertNotEqual(new.pk, old.pk)
        self.assertEqual(old.status, "REJECTED")
        self.assertEqual(new.status, "PENDING")

    def test_reject_requires_reason_and_decision_must_be_string(self):
        application = self.application()
        for decision, reason in (("REJECT", "  "), ([], ""), ({"x": 1}, "")):
            with self.subTest(decision=decision):
                with self.assertRaises(RootTrialError):
                    review_application(self.admin, application.pk, decision, reason)
        application.refresh_from_db()
        self.assertEqual(application.status, "PENDING")

    def test_only_current_active_superadmin_can_review(self):
        application = self.application()
        with self.assertRaises(RootTrialError) as raised:
            review_application(self.other, application.pk, "APPROVE")
        self.assertEqual(raised.exception.http_status, 403)
        self.admin.is_superuser = False
        self.admin.save(update_fields=("is_superuser",))
        with self.assertRaises(RootTrialError):
            review_application(self.admin, application.pk, "APPROVE")

    def test_review_by_web_admin_without_wechat_account_is_supported(self):
        application = self.application()
        self.admin.wx_openid = None
        self.admin.save(update_fields=("wx_openid",))
        approved, _ = review_application(self.admin, application.pk, "APPROVE")
        self.assertEqual(approved.reviewed_by_id, self.admin.pk)
        self.assertTrue(application_payload(approved, self.admin)["organization_id"])
        self.assertEqual(get_application_for_user(self.admin, application.pk).pk, application.pk)

    def test_all_review_recipients_finish_when_one_admin_decides(self):
        application = self.application()
        review_application(self.admin, application.pk, "APPROVE")
        task = WorkItem.objects.get(uniq_key=f"root-trial-review:{application.pk}")
        self.assertEqual(task.status, "DONE")
        self.assertFalse(task.user_tasks.exclude(state="DONE").exists())
        self.assertEqual(task.user_tasks.get(to_openid=self.admin.wx_openid).done_reason, "SELF_ACTION")
        self.assertEqual(task.user_tasks.get(to_openid=self.admin2.wx_openid).done_reason, "OTHER_ACTION")
        self.assertEqual(task.payload["root_organization_id"],
                         UserOrganization.objects.get(user=self.guest).organization_id)
        result = UserTask.objects.get(workitem__type="FACTORY_TRIAL_RESULT")
        self.assertFalse(result.need_action)
        self.assertEqual(result.to_openid, self.guest.wx_openid)

    def test_missing_physical_license_prevents_approval_but_not_rejection(self):
        application = self.application()
        private_storage().delete(application.license_upload.storage_key)
        before = Organization.objects.count()
        with self.assertRaises(RootTrialError) as raised:
            review_application(self.admin, application.pk, "APPROVE")
        self.assertEqual(raised.exception.code, "LICENSE_MISSING")
        self.assertEqual(Organization.objects.count(), before)
        rejected, _ = review_application(self.admin, application.pk, "REJECT", "图片已缺失，请重传")
        self.assertEqual(rejected.status, "REJECTED")

    def test_changed_physical_license_is_detected(self):
        application = self.application()
        with open(private_storage().path(application.license_upload.storage_key), "wb") as handle:
            handle.write(b"changed")
        with self.assertRaises(RootTrialError) as raised:
            review_application(self.admin, application.pk, "APPROVE")
        self.assertEqual(raised.exception.code, "LICENSE_MISSING")

    def test_disabled_applicant_can_be_rejected_without_hanging_review_task(self):
        application = self.application()
        self.guest.is_active = False
        self.guest.save(update_fields=("is_active",))
        with self.assertRaises(RootTrialError):
            review_application(self.admin, application.pk, "APPROVE")
        rejected, _ = review_application(self.admin, application.pk, "REJECT", "账号已停用")
        self.assertEqual(rejected.status, "REJECTED")
        self.assertEqual(WorkItem.objects.get(uniq_key=f"root-trial-review:{application.pk}").status, "DONE")

    def test_applicant_who_gains_another_identity_can_still_be_rejected(self):
        application = self.application()
        self.identity()
        with self.assertRaises(RootTrialError) as raised:
            review_application(self.admin, application.pk, "APPROVE")
        self.assertEqual(raised.exception.code, "IDENTITY_EXISTS")
        rejected, _ = review_application(self.admin, application.pk, "REJECT", "已通过其他邀请建立身份")
        self.assertEqual(rejected.status, "REJECTED")

    def test_failed_identity_creation_rolls_back_new_root_and_decision(self):
        application = self.application()
        before = Organization.objects.count()
        with patch("root_trial.services.UserOrganization.save", side_effect=IntegrityError("raced")):
            with self.assertRaises(RootTrialError):
                review_application(self.admin, application.pk, "APPROVE")
        application.refresh_from_db()
        self.assertEqual(application.status, "PENDING")
        self.assertIsNone(application.organization_id)
        self.assertEqual(Organization.objects.count(), before)
        self.assertFalse(UserOrganization.objects.filter(user=self.guest).exists())

    def test_upload_is_real_image_and_has_no_public_url(self):
        fake = SimpleUploadedFile("fake.png", b"not-image", content_type="image/png")
        with self.assertRaises(RootTrialError):
            create_license_upload(self.guest, fake)
        media = self.media()
        with self.assertRaises(ValueError):
            private_storage().url(media.storage_key)
        with open_license_file(media) as handle:
            self.assertTrue(handle.read().startswith(b"\x89PNG"))

    def test_license_access_is_owner_or_admin_of_submitted_application(self):
        media = self.media()
        self.assertEqual(get_license_for_user(self.guest, media.pk).pk, media.pk)
        for viewer in (self.other, self.admin):
            with self.assertRaises(RootTrialError):
                get_license_for_user(viewer, media.pk)
        self.application(media=media)
        self.assertEqual(get_license_for_user(self.admin, media.pk).pk, media.pk)
        with self.assertRaises(RootTrialError):
            get_application_for_user(self.other, 1)

    def test_someone_elses_upload_cannot_be_used_for_application(self):
        media = self.media(user=self.other)
        with self.assertRaises(RootTrialError) as raised:
            submit_application(self.guest, "测试企业", media.pk, uuid.uuid4())
        self.assertEqual(raised.exception.code, "LICENSE_REQUIRED")

    def test_database_constraint_rejects_second_pending_even_outside_service(self):
        application = self.application()
        with self.assertRaises(IntegrityError), transaction.atomic():
            RootTrialApplication.objects.create(
                applicant=self.guest, request_id=uuid.uuid4(),
                organization_name="重复待审企业", license_upload=application.license_upload,
            )

    def test_submission_retry_after_approval_restores_its_original_result(self):
        application = self.application()
        review_application(self.admin, application.pk, "APPROVE")
        again, created = submit_application(
            self.guest, application.organization_name,
            application.license_upload_id, application.request_id,
        )
        self.assertFalse(created)
        self.assertEqual(again.status, "APPROVED")

    def test_no_wechat_admin_recipients_does_not_approve_or_drop_application(self):
        get_user_model().objects.filter(is_superuser=True).update(wx_openid=None)
        application = self.application()
        self.assertEqual(application.status, "PENDING")
        self.assertFalse(UserTask.objects.filter(workitem__type="FACTORY_TRIAL_REVIEW").exists())
        self.assertTrue(WorkItem.objects.filter(uniq_key=f"root-trial-review:{application.pk}").exists())

    def test_invalid_ids_are_business_errors(self):
        for value in ("bad", {}, [], True, -1):
            with self.subTest(value=value):
                with self.assertRaises(RootTrialError):
                    get_application_for_user(self.guest, value)
                with self.assertRaises(RootTrialError):
                    get_license_for_user(self.guest, value)
