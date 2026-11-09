"""Current portal logout contract and isolated legacy print compatibility.

The V2 fixture supplies valid users/catalog and a normal production shipment.
Primary tests use the deployed routes and real portal QR APIs. Only the legacy
class adds a test-only URL outside PortalAccessMiddleware's protected prefix;
it verifies the existing standalone handlers without widening deployed access.
"""
from unittest.mock import patch

from django.apps import apps
from django.conf import settings
from django.contrib.sessions.models import Session
from django.test import Client, TestCase, override_settings
from django.urls import include, path

from account.models import UserOrganization
from account.services.auth_service import issue_access_token
from config.urls import urlpatterns as production_urlpatterns
from print import tests as print_fixture
from print.models import PrintLoginChallenge
from print.views import CHALLENGE_SESSION_KEY, GATE_SESSION_KEY, PREPARED_SESSION_KEY, SESSION_SCOPE
from web_portal.access import GRANT
from web_portal.models import WebLoginChallenge
from web_portal.views import CHALLENGE


urlpatterns = list(production_urlpatterns) + [path("_c06_legacy_print/", include("print.urls"))]


class PrintLogoutFixture:
    setUp = print_fixture.PrintCenterSecurityTests.setUp
    _user_with_identity = print_fixture.PrintCenterSecurityTests._user_with_identity
    _accepted_direct_relation = print_fixture.PrintCenterSecurityTests._accepted_direct_relation
    _product_with_bom = print_fixture.PrintCenterSecurityTests._product_with_bom
    _material_with_supplier = print_fixture.PrintCenterSecurityTests._material_with_supplier
    _create_lot = print_fixture.PrintCenterSecurityTests._create_lot
    _create_v2_plan = print_fixture.PrintCenterSecurityTests._create_v2_plan
    _ready_production_shipment = print_fixture.PrintCenterSecurityTests._ready_production_shipment
    shipment = print_fixture.PrintCenterSecurityTests.shipment
    print_url = "/pldp/web/print/"
    portal_url = "/pldp/web/portal/"

    def _browser(self):
        browser = Client(enforce_csrf_checks=True)
        browser.get(self.portal_url + "api/session/")
        return browser

    def _post(self, browser, url, data=None):
        return browser.post(
            url, data or {}, content_type="application/json",
            HTTP_X_CSRFTOKEN=browser.cookies[settings.CSRF_COOKIE_NAME].value,
        )

    def _portal_start(self, browser):
        number = WebLoginChallenge.objects.count() + 1
        token = f"{number:064x}"
        with patch("web_portal.views.secrets.token_hex", return_value=token):
            response = self._post(browser, self.portal_url + "api/start/")
        self.assertEqual(response.status_code, 200, response.content)
        return token, browser.session[CHALLENGE]

    def _portal_login(self, browser):
        token, challenge_id = self._portal_start(browser)
        approved = Client().post(
            self.portal_url + "api/approve/", {"token": token}, content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + issue_access_token(self.factory_stock),
        )
        self.assertEqual(approved.status_code, 200, approved.content)
        response = self._post(browser, self.portal_url + "api/status/")
        self.assertEqual(response.json()["data"]["status"], "ready", response.content)
        self.assertIsNotNone(WebLoginChallenge.objects.get(pk=challenge_id).consumed_at)
        self.assertTrue(browser.get(self.portal_url + "api/session/").json()["data"]["logged_in"])

    def _print_start(self, browser):
        response = self._post(browser, self.print_url + "api/login/start/")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["data"]["qr_text"].split("|")[1], browser.session[CHALLENGE_SESSION_KEY]

    def _print_approve(self, token):
        return Client().post(
            self.print_url + "api/login/approve/", {"token": token}, content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + issue_access_token(self.factory_stock),
        )

    def _facts(self):
        labels = {"account", "sales", "workflow", "lifecycle", "manufacturing", "inventory", "supply_chain", "product"}
        return {
            model._meta.label: list(model.objects.order_by("pk").values())
            for model in apps.get_models() if model._meta.app_label in labels
        }

    def _logout(self, browser):
        response = self._post(browser, self.print_url + "api/logout/")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {"code": 0, "data": {"redirect_url": "/?login=1"}})
        self.assertIn("no-store", response.headers["Cache-Control"])
        for key in (GRANT, GATE_SESSION_KEY, CHALLENGE_SESSION_KEY, PREPARED_SESSION_KEY, "_auth_user_id"):
            self.assertNotIn(key, browser.session)
        return response


class PrintLogoutSessionTests(PrintLogoutFixture, TestCase):
    def test_logout_ends_only_this_browser_and_preserves_print_facts_and_bearer(self):
        plan, shipment = self.shipment()
        first, second = self._browser(), self._browser()
        for browser in (first, second):
            self._portal_login(browser)
        first_token, first_challenge = self._print_start(first)
        _, second_challenge = self._print_start(second)
        self.assertEqual(self._print_approve(first_token).status_code, 200)
        for browser in (first, second):
            response = self._post(browser, self.print_url + "api/prepare/", {"shipment_id": shipment.pk})
            self.assertEqual(response.status_code, 200, response.content)
        old_cookie = first.cookies[settings.SESSION_COOKIE_NAME].value
        old_csrf = first.cookies[settings.CSRF_COOKIE_NAME].value
        bearer_token = issue_access_token(self.factory_stock)
        before = self._facts()
        self._logout(first)
        self.assertFalse(Session.objects.filter(session_key=old_cookie).exists())
        self.assertFalse(PrintLoginChallenge.objects.filter(pk=first_challenge).exists())
        self.assertTrue(PrintLoginChallenge.objects.filter(pk=second_challenge).exists())
        replay = Client(enforce_csrf_checks=True)
        replay.cookies[settings.SESSION_COOKIE_NAME] = old_cookie
        replay.cookies[settings.CSRF_COOKIE_NAME] = old_csrf
        for browser in (first, replay):
            self.assertFalse(browser.get(self.portal_url + "api/session/").json()["data"]["logged_in"])
            self.assertRedirects(browser.get(self.print_url), "/?login=1", fetch_redirect_response=False)
            for endpoint in ("prepare", "confirm", "login/status"):
                response = self._post(browser, self.print_url + f"api/{endpoint}/", {"shipment_id": shipment.pk})
                self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(self._print_approve(first_token).status_code, 400)
        self.assertEqual(self._facts(), before)
        bearer = Client().get(
            "/pldp/api/account/profile/", HTTP_AUTHORIZATION="Bearer " + bearer_token,
        )
        self.assertEqual(bearer.json()["data"]["role"], "factory_stock")
        self.assertContains(second.get(self.print_url), plan.plan_code)
        self.assertEqual(self._post(second, self.print_url + "api/prepare/", {"shipment_id": shipment.pk}).status_code, 200)
        self.assertEqual(self._post(second, self.print_url + "api/confirm/", {"shipment_id": shipment.pk}).status_code, 200)
        self._portal_login(first)
        self.assertEqual(first.get(self.print_url).status_code, 200)

    def test_logout_requires_post_and_csrf_without_changing_the_session(self):
        browser = self._browser()
        self._portal_login(browser)
        grant = browser.session[GRANT]
        self.assertEqual(browser.get(self.print_url + "api/logout/").status_code, 405)
        self.assertEqual(browser.post(self.print_url + "api/logout/", {}, content_type="application/json").status_code, 403)
        self.assertEqual(browser.session[GRANT], grant)
        self.assertTrue(browser.get(self.portal_url + "api/session/").json()["data"]["logged_in"])
        self._logout(browser)

    def test_suspended_factory_root_can_still_end_its_browser_session(self):
        browser = self._browser()
        self._portal_login(browser)
        self.factory.is_active = False
        self.factory.full_clean()
        self.factory.save(update_fields=("is_active", "updated_at"))
        before = self._facts()
        self._logout(browser)
        self.assertEqual(self._facts(), before)

    def test_disabled_role_slot_can_still_end_its_browser_session(self):
        browser = self._browser()
        self._portal_login(browser)
        identity = UserOrganization.objects.get(user=self.factory_stock)
        identity.is_active = False
        identity.full_clean()
        identity.save(update_fields=("is_active",))
        self._logout(browser)

    def test_expired_portal_grant_can_still_end_its_browser_session(self):
        browser = self._browser()
        self._portal_login(browser)
        expires_at = browser.session[GRANT]["expires_at"]
        with patch("web_portal.access.time.time", return_value=expires_at + 1):
            self._logout(browser)

    def test_logout_cancels_only_its_pending_portal_challenge(self):
        first, second = self._browser(), self._browser()
        first_token, first_id = self._portal_start(first)
        _, second_id = self._portal_start(second)
        self._logout(first)
        self.assertFalse(WebLoginChallenge.objects.filter(pk=first_id).exists())
        self.assertTrue(WebLoginChallenge.objects.filter(pk=second_id).exists())
        approved = Client().post(
            self.portal_url + "api/approve/", {"token": first_token}, content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + issue_access_token(self.factory_stock),
        )
        self.assertEqual(approved.status_code, 410)


@override_settings(ROOT_URLCONF=__name__)
class LegacyPrintLogoutCompatibilityTests(PrintLogoutFixture, TestCase):
    print_url = "/_c06_legacy_print/"

    def test_legacy_gate_and_challenge_cannot_revive_after_current_session_logout(self):
        first, second = self._browser(), self._browser()
        for browser in (first, second):
            token, _ = self._print_start(browser)
            self.assertEqual(self._print_approve(token).status_code, 200)
            self.assertEqual(self._post(browser, self.print_url + "api/login/status/").json()["data"]["status"], "ready")
            self.assertEqual(browser.session[GATE_SESSION_KEY]["scope"], SESSION_SCOPE)
        token, challenge_id = self._print_start(first)
        self.assertEqual(self._print_approve(token).status_code, 200)
        old_cookie = first.cookies[settings.SESSION_COOKIE_NAME].value
        old_csrf = first.cookies[settings.CSRF_COOKIE_NAME].value
        self._logout(first)
        self.assertFalse(PrintLoginChallenge.objects.filter(pk=challenge_id).exists())
        self.assertEqual(self._print_approve(token).status_code, 400)
        replay = Client(enforce_csrf_checks=True)
        replay.cookies[settings.SESSION_COOKIE_NAME] = old_cookie
        replay.cookies[settings.CSRF_COOKIE_NAME] = old_csrf
        for browser in (first, replay):
            self.assertContains(browser.get(self.print_url), "扫一扫")
            self.assertEqual(self._post(browser, self.print_url + "api/login/status/").status_code, 400)
            self.assertEqual(self._post(browser, self.print_url + "api/prepare/", {"shipment_id": 1}).status_code, 403)
        self.assertContains(second.get(self.print_url), self.factory.name)
        fresh_token, _ = self._print_start(first)
        self.assertEqual(self._print_approve(fresh_token).status_code, 200)
        self.assertEqual(self._post(first, self.print_url + "api/login/status/").json()["data"]["status"], "ready")
