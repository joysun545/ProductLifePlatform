"""Real concurrent requests against the shared test database, without mock locks."""
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

from PIL import Image
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections
from django.test import TransactionTestCase, override_settings

from account.models import Organization, OrganizationRole, OrganizationType, UserOrganization
from workflow.models import UserTask, WorkItem

from .models import RootTrialApplication
from .services import RootTrialError, create_license_upload, review_application, submit_application


class RootTrialConcurrencyTests(TransactionTestCase):
    """SQLite may reject a simultaneous writer; the original retry must converge."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings_patch = override_settings(ROOT_TRIAL_PRIVATE_ROOT=directory.name)
        settings_patch.enable()
        self.addCleanup(settings_patch.disable)
        User = get_user_model()
        self.guest = User.objects.create_user(username="rt_parallel_guest", wx_openid="rt-parallel-guest")
        self.admins = [User.objects.create_user(
            username=f"rt_parallel_admin_{index}", wx_openid=f"rt-parallel-admin-{index}",
            is_superuser=True, is_staff=True,
        ) for index in (1, 2)]
        owner_type, _ = OrganizationType.objects.get_or_create(code="OWNER", defaults={"name": "厂家"})
        role, _ = OrganizationRole.objects.get_or_create(
            organization_type=owner_type, code="factory_admin",
            defaults={"name": "厂家负责人", "is_owner_role": True},
        )
        role.is_active = True
        role.is_owner_role = True
        role.save(update_fields=("is_active", "is_owner_role"))
        stream = BytesIO()
        Image.new("RGB", (12, 8), "white").save(stream, format="PNG")
        self.media = create_license_upload(
            self.guest, SimpleUploadedFile("license.png", stream.getvalue(), content_type="image/png"),
        )

    def run_together(self, calls):
        barrier = threading.Barrier(len(calls))

        def invoke(call):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                try:
                    application, changed = call()
                    return application.pk, changed, None
                except RootTrialError as exc:
                    # Do not absorb unexpected domain or database exceptions.
                    if exc.code != "REQUEST_BUSY" or exc.http_status != 409:
                        raise
                    return None, False, exc.code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=len(calls)) as executor:
            futures = [executor.submit(invoke, call) for call in calls]
            return [future.result(timeout=20) for future in futures]

    def test_concurrent_identical_submissions_converge_without_resetting_tasks(self):
        request_id = uuid.uuid4()

        def submit():
            return submit_application(self.guest, "并发提交制造企业", self.media.pk, request_id)

        results = self.run_together([submit, submit])
        # A busy response is an explicit retry, not a silent test exception.
        for result in results:
            if result[2] == "REQUEST_BUSY":
                submit()
        application, created = submit()
        self.assertFalse(created)
        self.assertEqual(RootTrialApplication.objects.filter(applicant=self.guest).count(), 1)
        self.assertEqual(application.status, "PENDING")
        self.assertEqual(WorkItem.objects.filter(type="FACTORY_TRIAL_REVIEW").count(), 1)
        workitem = WorkItem.objects.get(uniq_key=f"root-trial-review:{application.pk}")
        task = workitem.user_tasks.order_by("pk").first()
        task.state = "READ"
        task.save(update_fields=("state",))
        application_again, created = submit()
        task.refresh_from_db()
        self.assertEqual(application_again.pk, application.pk)
        self.assertFalse(created)
        self.assertEqual(task.state, "READ")
        self.assertEqual(workitem.user_tasks.count(), 2)

    def test_two_admins_concurrently_approving_create_one_root_and_finish_all_tasks(self):
        application, _ = submit_application(
            self.guest, "并发审核制造企业", self.media.pk, uuid.uuid4(),
        )
        root_count = Organization.objects.count()
        calls = [lambda reviewer=reviewer: review_application(reviewer, application.pk, "APPROVE")
                 for reviewer in self.admins]
        results = self.run_together(calls)
        for call, result in zip(calls, results):
            if result[2] == "REQUEST_BUSY":
                call()
        application.refresh_from_db()
        self.assertEqual(application.status, "APPROVED")
        self.assertEqual(Organization.objects.count(), root_count + 1)
        self.assertEqual(UserOrganization.objects.filter(user=self.guest).count(), 1)
        identity = UserOrganization.objects.get(user=self.guest)
        self.assertEqual(identity.organization_id, application.organization_id)
        self.assertEqual(identity.organization.factory_root_id, identity.organization_id)
        self.assertEqual(WorkItem.objects.filter(type="FACTORY_TRIAL_RESULT").count(), 1)
        workitem = WorkItem.objects.get(uniq_key=f"root-trial-review:{application.pk}")
        self.assertEqual(workitem.status, "DONE")
        self.assertEqual(workitem.user_tasks.count(), 2)
        self.assertFalse(UserTask.objects.filter(workitem=workitem).exclude(state="DONE").exists())
        self.assertEqual(workitem.user_tasks.filter(done_reason="SELF_ACTION").count(), 1)
        self.assertEqual(workitem.user_tasks.filter(done_reason="OTHER_ACTION").count(), 1)
