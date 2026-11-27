from io import BytesIO
from zipfile import ZipFile

from django.test import Client, TestCase
from django.urls import reverse

from account.models import (
    Organization,
    OrganizationRole,
    OrganizationType,
    SeniorManagerProfile,
    User,
    UserOrganization,
)
from account.services.auth_service import issue_access_token
from lifecycle.models import PhysicalDeviceCode
from lifecycle.services.physical_device_code_service import identity_can_issue_device_codes
from web_portal.access import GRANT


class DeviceCodeRegistryWebTests(TestCase):
    def setUp(self):
        self.factory_type, _ = OrganizationType.objects.get_or_create(
            code="OWNER", defaults={"name": "厂家", "is_active": True}
        )
        self.roles = {}
        for code, name, is_owner in (
            ("factory_admin", "厂家负责人账号", True),
            ("factory_production", "厂家生产经理", False),
            ("factory_senior_manager", "厂家高级经理", False),
            ("factory_sales", "厂家销售经理", False),
        ):
            self.roles[code], _ = OrganizationRole.objects.get_or_create(
                organization_type=self.factory_type,
                code=code,
                defaults={"name": name, "is_active": True, "is_owner_role": is_owner},
            )
        self.factory = Organization.objects.create(
            code="DEVICE-CODE-WEB-FACTORY",
            name="设备编码发行测试厂家",
            organization_type=self.factory_type,
        )
        self.users = {}
        self.identities = {}
        for code in self.roles:
            self.users[code] = User.objects.create_user(
                username=f"device-code-web-{code}",
                password="unused",
                wx_openid=f"device-code-web-openid-{code}",
            )
            self.identities[code] = UserOrganization.objects.create(
                user=self.users[code],
                organization=self.factory,
                role=self.roles[code],
            )

    def _url(self, name, **kwargs):
        return reverse(f"pldp:web:device_code_registry:{name}", kwargs=kwargs or None)

    def _logged_in_browser(self, user):
        from web_portal.tests import PortalTests

        browser = Client()
        started = browser.post("/pldp/web/portal/api/start/", data="{}", content_type="application/json")
        self.assertEqual(started.status_code, 200, started.content)
        from web_portal.views import CHALLENGE
        from web_portal.models import WebLoginChallenge

        challenge = WebLoginChallenge.objects.get(pk=browser.session[CHALLENGE])
        approved = PortalTests.approve(self, challenge, user)
        return browser, approved

    def test_web_route_is_separate_and_uses_the_mid_web_router(self):
        self.assertRedirects(Client().get(self._url("page")), "/?login=1", fetch_redirect_response=False)
        browser, approved = self._logged_in_browser(self.users["factory_production"])
        self.assertEqual(approved.status_code, 200, approved.content)
        browser.post("/pldp/web/portal/api/status/", data="{}", content_type="application/json")
        response = browser.get(self._url("page"))
        self.assertContains(response, "厂家设备编码发行")
        self.assertNotContains(response, "打印中心")

    def test_factory_owner_yields_when_production_is_delegated(self):
        owner = self.identities["factory_admin"]
        production = self.identities["factory_production"]
        senior = self.identities["factory_senior_manager"]
        self.assertTrue(identity_can_issue_device_codes(owner))

        profile = SeniorManagerProfile.objects.create(identity=senior)
        profile.managed_roles.add(self.roles["factory_production"])

        self.assertFalse(identity_can_issue_device_codes(owner))
        self.assertTrue(identity_can_issue_device_codes(production))
        self.assertTrue(identity_can_issue_device_codes(senior))
        self.assertFalse(identity_can_issue_device_codes(self.identities["factory_sales"]))

    def test_web_qr_challenge_grants_device_codes_without_print_access(self):
        browser, approved = self._logged_in_browser(self.users["factory_production"])
        self.assertEqual(approved.status_code, 200, approved.content)
        status = browser.post("/pldp/web/portal/api/status/", data="{}", content_type="application/json")
        self.assertEqual(status.status_code, 200, status.content)
        self.assertEqual(status.json()["data"]["status"], "ready")
        self.assertIn("device_codes", browser.session[GRANT]["principal"]["modules"])
        self.assertNotIn("print", browser.session[GRANT]["principal"]["modules"])
        self.assertEqual(browser.get("/pldp/web/print/").status_code, 403)

    def test_unauthorized_role_cannot_approve_and_code_download_has_no_qr_images(self):
        browser, approved = self._logged_in_browser(self.users["factory_sales"])
        self.assertEqual(approved.status_code, 403)

        browser, approved = self._logged_in_browser(self.users["factory_production"])
        self.assertEqual(approved.status_code, 200)
        browser.post("/pldp/web/portal/api/status/", data="{}", content_type="application/json")

        generated = browser.post(
            self._url("generate_batch"),
            data={"quantity": 2},
            content_type="application/json",
        )
        self.assertEqual(generated.status_code, 201, generated.content)
        batch_id = generated.json()["data"]["generation_batch_id"]
        self.assertEqual(
            PhysicalDeviceCode.objects.filter(generation_batch_id=batch_id).count(),
            2,
        )

        packaging = browser.post(
            self._url("save_packaging_info", batch_id=batch_id),
            data={
                "batch_name": "永久标签测试批次",
                "maker_name": "测试制作单位",
                "label_spec": "金属二维码标签",
                "notes": "用于第三方制作",
            },
            content_type="application/json",
        )
        self.assertEqual(packaging.status_code, 200, packaging.content)

        code_archive = browser.get(self._url("download_codes_package", batch_id=batch_id))
        self.assertEqual(code_archive.status_code, 200, code_archive.content[:300])
        with ZipFile(BytesIO(code_archive.content)) as archive:
            names = archive.namelist()
            self.assertIn("设备编码.txt", names)
            self.assertFalse(any(name.startswith("二维码图片/") for name in names))

        qr_archive = browser.get(self._url("download_qr_package", batch_id=batch_id))
        self.assertEqual(qr_archive.status_code, 200, qr_archive.content[:300])
        with ZipFile(BytesIO(qr_archive.content)) as archive:
            names = archive.namelist()
            self.assertEqual(len([name for name in names if name.endswith(".png")]), 2)
            self.assertIn("设备编码对应表.csv", names)

