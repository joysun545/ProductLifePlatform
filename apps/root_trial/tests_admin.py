import tempfile
import uuid
from io import BytesIO
from unittest.mock import patch

from PIL import Image
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from account.models import Organization, OrganizationRole, OrganizationType, UserOrganization
from account.services.auth_service import issue_access_token

from .services import create_license_upload, submit_application


class RootTrialAdminTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings_patch = override_settings(ROOT_TRIAL_PRIVATE_ROOT=directory.name)
        settings_patch.enable()
        self.addCleanup(settings_patch.disable)
        User = get_user_model()
        self.guest = User.objects.create_user(username="rt_admin_guest", wx_openid="rt-admin-guest")
        self.admin = User.objects.create_user(
            username="rt_web_admin", wx_openid="rt-web-admin", is_staff=True, is_superuser=True,
        )
        self.staff = User.objects.create_user(
            username="rt_plain_staff", wx_openid="rt-plain-staff", is_staff=True,
        )
        owner_type, _ = OrganizationType.objects.get_or_create(code="OWNER", defaults={"name": "厂家"})
        role, _ = OrganizationRole.objects.get_or_create(
            organization_type=owner_type, code="factory_admin",
            defaults={"name": "厂家负责人", "is_owner_role": True},
        )
        role.is_active = True
        role.is_owner_role = True
        role.save(update_fields=("is_active", "is_owner_role"))
        staff_org = Organization.objects.create(
            name="普通工作人员厂家", code=f"RT_{uuid.uuid4().hex}", organization_type=owner_type,
        )
        UserOrganization.objects.create(user=self.staff, role=role, organization=staff_org)
        stream = BytesIO()
        Image.new("RGB", (8, 8), "white").save(stream, format="PNG")
        media = create_license_upload(
            self.guest, SimpleUploadedFile("license.png", stream.getvalue(), content_type="image/png"),
        )
        self.application, _ = submit_application(self.guest, "管理员审核测试企业", media.pk, uuid.uuid4())
        self.review_url = reverse(
            "admin:root_trial_roottrialapplication_review", args=(self.application.pk,),
        )

    def portal_login(self, user):
        token = "d" * 64
        with patch("web_portal.views.secrets.token_hex", return_value=token):
            start = self.client.post("/pldp/web/portal/api/start/")
        self.assertEqual(start.status_code, 200)
        approval = self.client.post(
            "/pldp/web/portal/api/approve/", data={"token": token}, content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {issue_access_token(user)}",
        )
        self.assertEqual(approval.status_code, 200)
        status = self.client.post("/pldp/web/portal/api/status/")
        self.assertEqual(status.json()["data"]["status"], "ready")

    def test_staff_without_superadmin_cannot_view_or_approve_application(self):
        self.portal_login(self.staff)
        self.assertEqual(self.client.get(self.review_url).status_code, 403)
        response = self.client.post(self.review_url, {"decision": "APPROVE", "reason": ""})
        self.assertEqual(response.status_code, 403)
        self.application.refresh_from_db()
        self.assertEqual(self.application.status, "PENDING")
        self.assertFalse(UserOrganization.objects.filter(user=self.guest).exists())

    def test_admin_review_post_requires_csrf_and_valid_post_uses_service(self):
        self.portal_login(self.admin)
        strict = Client(enforce_csrf_checks=True)
        strict.cookies = self.client.cookies.copy()
        page = strict.get(self.review_url)
        self.assertEqual(page.status_code, 200)
        blocked = strict.post(self.review_url, {"decision": "APPROVE", "reason": ""})
        self.assertEqual(blocked.status_code, 403)
        self.application.refresh_from_db()
        self.assertEqual(self.application.status, "PENDING")
        response = strict.post(
            self.review_url, {"decision": "APPROVE", "reason": "证照已核验"},
            HTTP_X_CSRFTOKEN=strict.cookies["csrftoken"].value,
        )
        self.assertEqual(response.status_code, 302)
        self.application.refresh_from_db()
        self.assertEqual(self.application.status, "APPROVED")
        self.assertEqual(self.application.reviewed_by_id, self.admin.pk)
        self.assertTrue(UserOrganization.objects.filter(user=self.guest).exists())

    def test_admin_change_form_cannot_directly_write_approved_status(self):
        self.portal_login(self.admin)
        change_url = reverse("admin:root_trial_roottrialapplication_change", args=(self.application.pk,))
        self.assertEqual(self.client.get(change_url).status_code, 200)
        response = self.client.post(change_url, {"status": "APPROVED", "_save": "Save"})
        self.assertEqual(response.status_code, 403)
        self.application.refresh_from_db()
        self.assertEqual(self.application.status, "PENDING")
        self.assertFalse(UserOrganization.objects.filter(user=self.guest).exists())

    def test_admin_private_license_requires_existing_portal_grant(self):
        license_url = reverse(
            "admin:root_trial_roottrialapplication_license", args=(self.application.license_upload_id,),
        )
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(license_url).status_code, 403)
        self.client.logout()
        self.portal_login(self.admin)
        response = self.client.get(license_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertIn("private", response["Cache-Control"])
        self.assertIn("no-store", response["Cache-Control"])
        self.assertTrue(b"".join(response.streaming_content).startswith(b"\x89PNG\r\n\x1a\n"))
