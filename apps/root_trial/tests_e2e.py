"""A complete trial request, Web review and explicit applicant Web login."""
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from account.models import User
from account.services.auth_service import build_user_payload
from web_portal.access import GRANT
from workflow.models import UserTask, WorkItem

from .models import RootTrialApplication
from .tests_api import RootTrialApiFixture


@override_settings(ROOT_URLCONF="root_trial.tests_api")
class RootTrialEndToEndTests(RootTrialApiFixture, TestCase):
    def test_guest_to_web_admin_review_to_root_owner_browser_end_to_end(self):
        # Two administrators receive the same review; either may complete it.
        second_admin = User.objects.create_superuser(username="trial_api_second_admin", password="test-only",
                                                     wx_openid="wx_trial_api_second_admin")
        guest_challenge, guest_token = self.begin()
        self.assertEqual(self.approve(guest_token).json()["data"]["status"], "trial_required")
        pk = self.application()
        application = RootTrialApplication.objects.get(pk=pk)
        workitem = WorkItem.objects.get(uniq_key="root-trial-review:%s" % pk)
        recipients = UserTask.objects.filter(workitem=workitem)
        self.assertEqual(set(recipients.values_list("to_openid", flat=True)),
                         {self.admin.wx_openid, second_admin.wx_openid})
        self.assertEqual(recipients.filter(state="NEW", need_action=True).count(), 2)

        # This separate browser gets its superadministrator grant through the
        # actual QR endpoints, without force_login or a fabricated session.
        admin_browser = Client(enforce_csrf_checks=True)
        _, admin_token = self.begin(admin_browser)
        self.assertEqual(self.approve(admin_token, self.admin).status_code, 200)
        self.assertEqual(self.browser_post("status", admin_browser).json()["data"]["status"], "ready")
        self.assertEqual(admin_browser.session[GRANT]["principal"]["role"], "superuser")
        review_url = reverse("admin:root_trial_roottrialapplication_review", args=(pk,))
        review_page = admin_browser.get(review_url)
        self.assertContains(review_page, application.organization_name)
        self.assertContains(review_page, 'name="csrfmiddlewaretoken"')
        license_url = reverse("admin:root_trial_roottrialapplication_license", args=(application.license_upload_id,))
        evidence = admin_browser.get(license_url)
        self.assertEqual(evidence.status_code, 200)
        self.assertEqual(evidence["Content-Type"], "image/png")
        self.assertTrue(b"".join(evidence.streaming_content).startswith(b"\x89PNG\r\n\x1a\n"))

        approved = admin_browser.post(review_url, {"decision": "APPROVE", "reason": "组织名称与营业执照已核验"},
                                      HTTP_X_CSRFTOKEN=admin_browser.cookies["csrftoken"].value)
        self.assertEqual(approved.status_code, 302, approved.content)
        self.assertEqual(approved.url, review_url)
        application.refresh_from_db()
        workitem.refresh_from_db()
        self.assertEqual(application.status, "APPROVED")
        self.assertEqual(workitem.status, "DONE")
        self.assertEqual(recipients.filter(state="DONE", done_at__isnull=False).count(), 2)
        self.assertEqual(recipients.get(to_openid=self.admin.wx_openid).done_reason, "SELF_ACTION")
        self.assertEqual(recipients.get(to_openid=second_admin.wx_openid).done_reason, "OTHER_ACTION")

        # The ordinary profile builder sees the new real identity immediately.
        self.guest.refresh_from_db()
        profile = build_user_payload(self.guest)
        self.assertEqual(profile["role"], "factory_admin")
        self.assertEqual(profile["organization_id"], application.organization_id)
        self.assertEqual(profile["factory_root_id"], application.organization_id)
        self.assertEqual(profile["factory_root_name"], application.organization_name)
        self.assertEqual(application.organization.factory_root_id, application.organization_id)
        guest_challenge.refresh_from_db()
        self.assertIsNone(guest_challenge.approved_at)
        self.assertNotIn(GRANT, self.browser.session)
        self.assertEqual(self.browser_post("status").json()["data"]["status"], "pending")
        # Only the applicant's explicit phone confirmation authorizes this PC.
        self.assertEqual(self.approve(guest_token).status_code, 200)
        self.assertEqual(self.browser_post("status").json()["data"]["status"], "ready")
        self.assertEqual(self.browser.session[GRANT]["principal"]["organization_id"], application.organization_id)
        self.assertEqual(self.browser.session[GRANT]["principal"]["role"], "factory_admin")
