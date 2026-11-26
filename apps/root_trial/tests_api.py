"""Security regressions for tourist onboarding without browser access bypasses."""
import json
import shutil
import tempfile
import uuid
from datetime import timedelta
from io import BytesIO

from PIL import Image
from django.contrib import admin
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import include, path
from django.utils import timezone

from account.models import Organization, OrganizationRole, OrganizationType, User, UserOrganization
from account.services.auth_service import issue_access_token
from web_portal.access import GRANT
from web_portal.models import WebLoginChallenge
from web_portal.views import CHALLENGE, digest

from .models import RootTrialApplication

urlpatterns = [
    path("", include("root_trial.urls")),
    path("pldp/web/portal/", include("web_portal.urls")),
    path("pldp/admin/", admin.site.urls),
]


class RootTrialApiFixture:
    @classmethod
    def setUpTestData(cls):
        cls.owner_type = OrganizationType.objects.get(code="OWNER")
        cls.factory = Organization.objects.create(name="API已有厂家", code="API_EXISTING_ROOT", organization_type=cls.owner_type)
        cls.guest = User.objects.create_user(username="trial_api_guest", wx_openid="wx_trial_api_guest")
        cls.other = User.objects.create_user(username="trial_api_other", wx_openid="wx_trial_api_other")
        cls.admin = User.objects.create_superuser(username="trial_api_admin", password="test-only", wx_openid="wx_trial_api_admin")
        cls.owner = User.objects.create_user(username="trial_api_owner", wx_openid="wx_trial_api_owner")
        UserOrganization.objects.create(user=cls.owner, organization=cls.factory,
                                        role=OrganizationRole.objects.get(code="factory_admin", organization_type=cls.owner_type))
        cls.sales = User.objects.create_user(username="trial_api_sales", wx_openid="wx_trial_api_sales")
        UserOrganization.objects.create(user=cls.sales, organization=cls.factory,
                                        role=OrganizationRole.objects.get(code="factory_sales", organization_type=cls.owner_type))
        cls.suspended = User.objects.create_user(username="trial_api_suspended", wx_openid="wx_trial_api_suspended")
        UserOrganization.objects.create(user=cls.suspended, organization=cls.factory, is_active=False,
                                        role=OrganizationRole.objects.get(code="factory_stock", organization_type=cls.owner_type))

    def setUp(self):
        self.private_dir = tempfile.mkdtemp(prefix="root-trial-api-")
        self.settings_override = override_settings(ROOT_TRIAL_PRIVATE_ROOT=self.private_dir, ROOT_TRIAL_PRIVATE_DIR=self.private_dir)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.addCleanup(shutil.rmtree, self.private_dir, True)
        self.browser = Client(enforce_csrf_checks=True)
        self.mobile = Client(enforce_csrf_checks=True)

    def api(self, method, suffix, user=None, data=None, raw=None):
        headers = {"HTTP_AUTHORIZATION": "Bearer " + issue_access_token(user or self.guest)}
        url = "/pldp/api/root-trial/" + suffix
        if method == "get":
            return self.mobile.get(url, **headers)
        return self.mobile.post(url, data=raw if raw is not None else json.dumps(data or {}),
                                content_type="application/json", **headers)

    def browser_post(self, suffix, client=None):
        client = client or self.browser
        client.get("/pldp/web/portal/api/session/")
        return client.post("/pldp/web/portal/api/" + suffix + "/", data="{}", content_type="application/json",
                           HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value)

    def begin(self, client=None):
        client = client or self.browser
        response = self.browser_post("start", client)
        self.assertEqual(response.status_code, 200, response.content)
        challenge = WebLoginChallenge.objects.get(pk=client.session[CHALLENGE])
        token = "%064x" % challenge.pk
        challenge.token_digest = digest(token)
        challenge.save(update_fields=["token_digest"])
        return challenge, token

    def approve(self, token, user=None):
        return self.mobile.post("/pldp/web/portal/api/approve/", data=json.dumps({"token": token}),
                                content_type="application/json",
                                HTTP_AUTHORIZATION="Bearer " + issue_access_token(user or self.guest))

    def upload(self, user=None):
        stream = BytesIO()
        Image.new("RGB", (120, 80), (100, 140, 90)).save(stream, format="PNG")
        result = self.mobile.post("/pldp/api/root-trial/license/", {"file": SimpleUploadedFile("license.png", stream.getvalue(), "image/png")},
                                  HTTP_AUTHORIZATION="Bearer " + issue_access_token(user or self.guest))
        self.assertEqual(result.status_code, 200, result.content)
        return result.json()["data"]["license_upload_id"]

    def application(self, user=None):
        user = user or self.guest
        result = self.api("post", "applications/", user, {"organization_name": "API新根厂家", "license_upload_id": self.upload(user),
                                                          "request_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "root-trial-api-" + str(user.pk)))})
        self.assertEqual(result.status_code, 200, result.content)
        return result.json()["data"]["application"]["id"]

    def review(self, application_id, decision="APPROVE", user=None):
        return self.api("post", "applications/%s/review/" % application_id, user or self.admin,
                        {"decision": decision, "reason": "API审核说明"})

@override_settings(ROOT_URLCONF=__name__)
class RootTrialApiTests(RootTrialApiFixture, TestCase):
    def test_bearer_is_required_even_for_authenticated_browser(self):
        self.mobile.force_login(self.admin)
        token = issue_access_token(self.admin)
        for url in ("/pldp/api/root-trial/context/", "/pldp/api/root-trial/applications/", "/pldp/api/root-trial/licenses/00000000-0000-4000-8000-000000000000/"):
            self.assertEqual(self.mobile.get(url).status_code, 401)
            self.assertEqual(self.mobile.get(url + "?access_token=" + token).status_code, 401)
        self.assertEqual(self.mobile.post("/pldp/web/portal/api/approve/").status_code, 401)

    def test_guest_qr_handoff_never_approves_or_logs_browser_in(self):
        row, token = self.begin()
        result = self.approve(token)
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(result.json()["data"]["status"], "trial_required")
        self.assertIn("root_application?webToken=" + token, result.json()["data"]["application_path"])
        row.refresh_from_db()
        self.assertIsNone(row.approved_at)
        self.assertIsNone(row.user_id)
        self.assertIsNone(row.consumed_at)
        self.assertEqual(self.browser_post("status").json()["data"]["status"], "pending")
        self.assertNotIn(GRANT, self.browser.session)
        self.assertNotIn("_auth_user_id", self.browser.session)

    def test_guest_handoff_validates_token_shape_and_lifetime(self):
        row, token = self.begin()
        for invalid in (None, True, 3, [], {}, "wrong", "A" * 64, "0" * 65):
            self.assertEqual(self.approve(invalid).status_code, 400)
        self.assertEqual(self.approve("f" * 64).status_code, 410)
        row.expires_at = timezone.now() - timedelta(seconds=1)
        row.save(update_fields=["expires_at"])
        self.assertEqual(self.approve(token).status_code, 410)

    def test_guest_cannot_handoff_an_already_approved_or_consumed_challenge(self):
        row, token = self.begin()
        self.assertEqual(self.approve(token, self.owner).status_code, 200)
        self.assertEqual(self.approve(token).status_code, 410)
        self.assertEqual(self.browser_post("status").json()["data"]["status"], "ready")
        self.assertEqual(self.approve(token).status_code, 410)

    def test_existing_non_web_and_suspended_roles_cannot_apply_as_tourists(self):
        _, token = self.begin()
        for user in (self.sales, self.suspended):
            self.assertFalse(self.api("get", "context/", user).json()["data"]["eligibility"]["eligible"])
            self.assertEqual(self.approve(token, user).status_code, 403)
            self.assertEqual(self.api("post", "applications/", user,
                                      {"organization_name": "不得新建", "license_upload_id": str(uuid.uuid4()), "request_id": str(uuid.uuid4())}).status_code, 403)
        self.assertFalse(RootTrialApplication.objects.exists())

    def test_existing_owner_login_retains_browser_and_binding_checks(self):
        row, token = self.begin()
        self.assertEqual(self.approve(token, self.owner).status_code, 200)
        self.assertNotIn(GRANT, self.browser.session)
        stranger = Client(enforce_csrf_checks=True)
        self.assertEqual(self.browser_post("status", stranger).status_code, 410)
        User.objects.filter(pk=self.owner.pk).update(wx_openid="changed_wechat")
        self.assertEqual(self.browser_post("status").status_code, 403)
        self.assertNotIn(GRANT, self.browser.session)

    def test_application_approval_requires_explicit_phone_confirmation(self):
        row, token = self.begin()
        self.approve(token)
        pk = self.application()
        self.assertEqual(self.review(pk).status_code, 200)
        row.refresh_from_db()
        self.assertIsNone(row.approved_at)
        self.assertNotIn(GRANT, self.browser.session)
        self.assertEqual(self.browser_post("status").json()["data"]["status"], "pending")
        context = self.api("get", "context/").json()["data"]
        self.assertFalse(context["eligibility"]["eligible"])
        self.assertEqual(context["application"]["status"], "APPROVED")
        self.assertEqual(self.approve(token).status_code, 200)
        self.assertEqual(self.browser_post("status").json()["data"]["status"], "ready")
        self.assertIn(GRANT, self.browser.session)

    def test_expired_challenge_does_not_expire_approved_business_identity(self):
        row, token = self.begin()
        pk = self.application()
        row.expires_at = timezone.now() - timedelta(seconds=1)
        row.save(update_fields=["expires_at"])
        self.assertEqual(self.review(pk).status_code, 200)
        self.assertEqual(self.approve(token).status_code, 410)
        self.assertEqual(self.browser_post("status").status_code, 410)
        self.assertNotIn(GRANT, self.browser.session)
        self.assertTrue(UserOrganization.objects.filter(user=self.guest, role__code="factory_admin").exists())
        _, fresh_token = self.begin()
        self.assertEqual(self.approve(fresh_token).status_code, 200)
        self.assertEqual(self.browser_post("status").json()["data"]["status"], "ready")

    def test_cancelled_challenge_cannot_be_restored_by_approval(self):
        row, token = self.begin()
        pk = self.application()
        self.browser_post("cancel")
        self.assertEqual(self.review(pk).status_code, 200)
        self.assertEqual(self.approve(token).status_code, 410)
        self.assertFalse(WebLoginChallenge.objects.filter(pk=row.pk).exists())

    def test_private_license_only_owner_and_superadmin_can_download(self):
        upload_id = self.upload()
        path = "licenses/%s/" % upload_id
        self.assertEqual(self.api("get", path, self.admin).status_code, 404)
        application = self.api("post", "applications/", data={"organization_name": "证照权限厂家", "license_upload_id": upload_id,
                                                               "request_id": str(uuid.uuid4())})
        self.assertEqual(application.status_code, 200, application.content)
        for user in (self.guest, self.admin):
            response = self.api("get", path, user)
            self.assertEqual(response.status_code, 200, getattr(response, "content", b""))
            self.assertEqual(response["X-Content-Type-Options"], "nosniff")
            self.assertIn("no-store", response["Cache-Control"])
            response.close()
        self.assertEqual(self.api("get", path, self.other).status_code, 404)
        self.assertEqual(self.api("get", path, self.owner).status_code, 404)

    def test_foreign_license_cannot_be_submitted(self):
        upload_id = self.upload(self.other)
        result = self.api("post", "applications/", data={"organization_name": "越权厂家", "license_upload_id": upload_id,
                                                          "request_id": str(uuid.uuid4())})
        self.assertIn(result.status_code, (400, 403))
        self.assertFalse(RootTrialApplication.objects.exists())

    def test_only_superadmin_can_list_and_review(self):
        pk = self.application()
        for user in (self.guest, self.other, self.owner):
            self.assertEqual(self.api("get", "applications/", user).status_code, 403)
            self.assertEqual(self.review(pk, user=user).status_code, 403)
        self.assertEqual(self.api("get", "applications/", self.admin).status_code, 200)
        self.assertEqual(self.api("get", "applications/%s/" % pk).status_code, 200)
        self.assertEqual(self.api("get", "applications/%s/" % pk, self.other).status_code, 404)

    def test_malformed_requests_and_extreme_ids_do_not_raise_server_errors(self):
        for raw in ("{", "null", "[]", "3"):
            self.assertEqual(self.api("post", "applications/", raw=raw).status_code, 400)
        for value in (None, True, [], "1", 2 ** 100, -1):
            self.assertEqual(self.api("post", "applications/", data={"license_upload_id": value}).status_code, 400)
        self.assertEqual(self.api("get", "applications/%s/" % (2 ** 100)).status_code, 400)
        self.assertEqual(self.api("get", "applications/?status=wrong", self.admin).status_code, 400)
        self.assertEqual(self.api("get", "applications/?before_id=" + "9" * 100, self.admin).status_code, 400)

    def test_review_rejects_containers_and_invalid_decisions_without_server_error(self):
        pk = self.application()
        for decision in (None, True, [], {}, "APPROVED", ""):
            response = self.api("post", "applications/%s/review/" % pk, self.admin,
                                {"decision": decision, "reason": "malformed decision"})
            self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(RootTrialApplication.objects.get(pk=pk).status, "PENDING")

    def test_retries_recover_same_application_before_and_after_approval(self):
        data = {"organization_name": "幂等API厂家", "license_upload_id": self.upload(), "request_id": str(uuid.uuid4())}
        initial = self.api("post", "applications/", data=data)
        self.assertEqual(initial.status_code, 200, initial.content)
        pk = initial.json()["data"]["application"]["id"]
        repeated = self.api("post", "applications/", data=data)
        self.assertEqual(repeated.status_code, 200, repeated.content)
        self.assertFalse(repeated.json()["data"]["created"])
        self.assertEqual(repeated.json()["data"]["application"]["id"], pk)
        changed = self.api("post", "applications/", data=dict(data, organization_name="内容改变"))
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(self.review(pk).status_code, 200)
        approved_retry = self.api("post", "applications/", data=data)
        self.assertEqual(approved_retry.status_code, 200, approved_retry.content)
        self.assertEqual(approved_retry.json()["data"]["application"]["id"], pk)
        self.assertEqual(approved_retry.json()["data"]["application"]["status"], "APPROVED")
        self.assertEqual(RootTrialApplication.objects.filter(applicant=self.guest).count(), 1)
        self.assertEqual(UserOrganization.objects.filter(user=self.guest).count(), 1)
        self.assertNotIn(GRANT, self.browser.session)

