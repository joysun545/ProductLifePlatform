"""Browser-scoped QR login and the irreversible shipment print boundary."""
import json
from datetime import timedelta

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from account.models import Organization, UserOrganization
from account.services.auth_service import issue_access_token
from sales import test_order_plan_material_flow as fixture_module
from sales.models import OrderPlan, OrderPlanShipment, OrderPlanShipmentDevice
from sales.services.order_plan_material_flow_service import (
    OrderPlanMaterialFlowError, authorize_material_flow_shipment,
    get_material_flow_detail, prepare_material_flow_shipment_qrcodes,
)
from print.models import PrintLoginChallenge
from print.views import GATE_SESSION_KEY, CHALLENGE_SESSION_KEY, SESSION_SCOPE


class PrintCenterSecurityTests(TestCase):
    # Reuse setup helpers only; do not inherit the whole sales test suite.
    setUp = fixture_module.OrderPlanMaterialFlowTests.setUp
    _user_with_identity = fixture_module.OrderPlanMaterialFlowTests._user_with_identity
    _accepted_direct_relation = fixture_module.OrderPlanMaterialFlowTests._accepted_direct_relation
    _product_with_bom = fixture_module.OrderPlanMaterialFlowTests._product_with_bom
    _material_with_supplier = fixture_module.OrderPlanMaterialFlowTests._material_with_supplier
    _create_lot = fixture_module.OrderPlanMaterialFlowTests._create_lot
    _create_v2_plan = fixture_module.OrderPlanMaterialFlowTests._create_v2_plan
    _ready_production_shipment = fixture_module.OrderPlanMaterialFlowTests._ready_production_shipment

    def url(self, name):
        return reverse("pldp:web:print:" + name)

    def browser(self):
        browser = Client(enforce_csrf_checks=True)
        response = browser.get(self.url("center"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "扫一扫")
        self.assertIn("no-store", response.headers["Cache-Control"])
        return browser

    def post(self, browser, name, payload=None):
        return browser.post(
            self.url(name), data=json.dumps({} if payload is None else payload), content_type="application/json",
            HTTP_X_CSRFTOKEN=browser.cookies["csrftoken"].value,
        )

    def start(self, browser):
        response = self.post(browser, "login_start")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertTrue(data["qr_image"].startswith("data:image/png;base64,"))
        return data["qr_text"].split("|")[1]

    def approve(self, token, user=None):
        return Client(enforce_csrf_checks=True).post(
            self.url("login_approve"), data=json.dumps({"token": token}),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + issue_access_token(user or self.factory_stock),
        )

    def login(self, browser):
        token = self.start(browser)
        self.assertEqual(self.approve(token).status_code, 200)
        before = browser.session.session_key
        self.assertEqual(self.post(browser, "login_status").json()["data"]["status"], "ready")
        self.assertNotEqual(before, browser.session.session_key)
        self.assertEqual(browser.session[GATE_SESSION_KEY]["scope"], SESSION_SCOPE)
        return token

    def shipment(self):
        plan, subplan, shipment = self._ready_production_shipment(request_id="print-test", quantity=5)
        authorize_material_flow_shipment(user=self.sales_user, plan_id=plan.pk, shipment_id=shipment.pk)
        return plan, shipment

    def test_anonymous_direct_url_never_impersonates_stock_keeper(self):
        plan, shipment = self.shipment()
        browser = self.browser()
        response = browser.get(self.url("center"), {"access_token": issue_access_token(self.factory_stock)})
        self.assertNotContains(response, plan.plan_code)
        for endpoint in ("prepare", "confirm"):
            response = self.post(browser, endpoint, {"shipment_id": shipment.pk})
            self.assertEqual(response.status_code, 403)
        shipment.refresh_from_db()
        self.assertIsNone(shipment.qrcode_printed_at)
        self.assertFalse(OrderPlanShipmentDevice.objects.filter(shipment=shipment).exists())

    def test_django_login_alone_is_not_a_print_grant(self):
        browser = self.browser()
        browser.force_login(self.factory_stock)
        self.assertContains(browser.get(self.url("center")), "扫一扫")
        self.assertEqual(self.post(browser, "prepare", {"shipment_id": 1}).status_code, 403)

    def test_browser_mutations_require_csrf(self):
        browser = self.browser()
        for endpoint in ("login_start", "login_status", "logout", "prepare", "confirm"):
            self.assertEqual(browser.post(self.url(endpoint), data="{}", content_type="application/json").status_code, 403)
        token = self.start(browser)
        self.assertEqual(self.approve(token).status_code, 200)

    def test_only_bearer_factory_stock_can_approve(self):
        browser = self.browser()
        token = self.start(browser)
        self.assertEqual(self.approve(token, self.sales_user).status_code, 403)
        self.assertEqual(self.approve(token, self.material_keeper).status_code, 403)
        response = Client().post(
            self.url("login_approve") + "?access_token=" + issue_access_token(self.factory_stock),
            data=json.dumps({"token": token}), content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.post(browser, "login_status").json()["data"]["status"], "pending")

    def test_approval_is_single_use_and_bound_to_original_browser(self):
        browser = self.browser()
        token = self.start(browser)
        self.assertEqual(self.approve(token).status_code, 200)
        self.assertEqual(self.approve(token).status_code, 400)
        other_browser = self.browser()
        self.assertEqual(self.post(other_browser, "login_status", {"token": token}).status_code, 400)
        self.assertEqual(self.post(browser, "login_status").json()["data"]["status"], "ready")
        self.assertIsNotNone(PrintLoginChallenge.objects.get().consumed_at)
        self.assertEqual(self.approve(token).status_code, 400)
        self.assertContains(browser.get(self.url("center")), self.factory.name)
        self.post(browser, "logout")
        self.assertContains(browser.get(self.url("center")), "扫一扫")
        self.assertEqual(self.post(browser, "login_status", {"token": token}).status_code, 400)

    def test_expired_or_refreshed_qr_cannot_be_replayed(self):
        browser = self.browser()
        old_token = self.start(browser)
        token = self.start(browser)
        self.assertEqual(self.approve(old_token).status_code, 400)
        PrintLoginChallenge.objects.update(expires_at=timezone.now()-timedelta(seconds=1))
        self.assertEqual(self.approve(token).status_code, 400)
        self.assertEqual(self.post(browser, "login_status").status_code, 400)

    def test_identity_change_between_approval_and_consumption_is_rejected(self):
        browser = self.browser()
        token = self.start(browser)
        self.assertEqual(self.approve(token).status_code, 200)
        UserOrganization.objects.filter(user=self.factory_stock).update(role=self.factory_sales_role)
        self.assertEqual(self.post(browser, "login_status").status_code, 403)
        self.assertNotIn(GATE_SESSION_KEY, browser.session)

    def test_existing_grant_rechecks_active_identity_and_absolute_expiry(self):
        browser = self.browser()
        self.login(browser)
        UserOrganization.objects.filter(user=self.factory_stock).update(is_active=False)
        self.assertEqual(self.post(browser, "prepare", {"shipment_id": 1}).status_code, 403)
        UserOrganization.objects.filter(user=self.factory_stock).update(is_active=True)
        session = browser.session
        grant = session[GATE_SESSION_KEY]
        grant["expires_at"] = (timezone.now()-timedelta(seconds=1)).timestamp()
        session[GATE_SESSION_KEY] = grant
        session.save()
        self.assertEqual(self.post(browser, "prepare", {"shipment_id": 1}).status_code, 403)
        self.assertContains(browser.get(self.url("center")), "扫一扫")

    def test_existing_grant_is_revoked_by_role_or_organization_change(self):
        browser = self.browser()
        self.login(browser)
        identity = UserOrganization.objects.get(user=self.factory_stock)
        UserOrganization.objects.filter(pk=identity.pk).update(role=self.factory_sales_role)
        self.assertEqual(self.post(browser, "prepare", {"shipment_id": 1}).status_code, 403)
        other_factory = Organization.objects.create(
            name="换岗厂家", code="PRINT-SHIFT", organization_type=self.factory.organization_type,
        )
        UserOrganization.objects.filter(pk=identity.pk).update(
            role=self.factory_stock_role, organization=other_factory,
        )
        self.assertEqual(self.post(browser, "prepare", {"shipment_id": 1}).status_code, 403)

    def test_cross_factory_list_and_direct_batch_access_are_denied(self):
        plan, shipment = self.shipment()
        other_factory = Organization.objects.create(
            name="另一厂家", code="PRINT-OTHER", organization_type=self.factory.organization_type,
        )
        OrderPlan.objects.filter(pk=plan.pk).update(factory_organization=other_factory)
        browser = self.browser()
        self.login(browser)
        self.assertNotContains(browser.get(self.url("center")), plan.plan_code)
        self.assertEqual(self.post(browser, "prepare", {"shipment_id": shipment.pk}).status_code, 400)
        self.assertFalse(OrderPlanShipmentDevice.objects.filter(shipment=shipment).exists())

    def test_prepare_refresh_and_confirmation_are_one_way(self):
        plan, shipment = self.shipment()
        browser = self.browser()
        self.login(browser)
        self.assertContains(browser.get(self.url("center")), plan.plan_code)
        self.assertEqual(self.post(browser, "confirm", {"shipment_id": shipment.pk}).status_code, 400)
        first = self.post(browser, "prepare", {"shipment_id": shipment.pk})
        self.assertEqual(first.status_code, 200, first.content)
        first_codes = first.json()["data"]["qrcodes"]
        self.assertEqual(len(first_codes), 5)
        self.assertTrue(all(row["qr_image"].startswith("data:image/png;base64,") for row in first_codes))
        browser.get(self.url("center"))
        second = self.post(browser, "prepare", {"shipment_id": shipment.pk})
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first_codes, second.json()["data"]["qrcodes"])
        confirmation = self.post(browser, "confirm", {"shipment_id": shipment.pk})
        self.assertEqual(confirmation.status_code, 200, confirmation.content)
        shipment.refresh_from_db()
        printed_at = shipment.qrcode_printed_at
        self.assertIsNotNone(printed_at)
        self.assertNotContains(browser.get(self.url("center")), plan.plan_code)
        response = self.post(browser, "prepare", {"shipment_id": shipment.pk})
        self.assertEqual(response.status_code, 400)
        self.assertIn("不能再次生成", response.json()["msg"])
        self.assertEqual(self.post(browser, "confirm", {"shipment_id": shipment.pk}).status_code, 400)
        shipment.refresh_from_db()
        self.assertEqual(shipment.qrcode_printed_at, printed_at)
        with self.assertRaises(OrderPlanMaterialFlowError):
            prepare_material_flow_shipment_qrcodes(user=self.factory_stock, plan_id=plan.pk, shipment_id=shipment.pk)
        data = get_material_flow_detail(user=self.factory_stock, plan_id=plan.pk)
        self.assertNotIn(shipment.pk, [batch["shipment_id"] for batch in data.get("qrcode_batches", [])])

    def test_stale_browser_preparation_cannot_obtain_codes_after_other_browser_confirms(self):
        plan, shipment = self.shipment()
        first, second = self.browser(), self.browser()
        self.login(first)
        self.login(second)
        self.assertEqual(self.post(first, "prepare", {"shipment_id": shipment.pk}).status_code, 200)
        self.assertEqual(self.post(second, "prepare", {"shipment_id": shipment.pk}).status_code, 200)
        self.assertEqual(self.post(first, "confirm", {"shipment_id": shipment.pk}).status_code, 200)
        self.assertEqual(self.post(second, "prepare", {"shipment_id": shipment.pk}).status_code, 400)

    def test_legacy_direct_get_cannot_generate_or_confirm(self):
        browser = self.browser()
        self.login(browser)
        for endpoint in ("prepare", "confirm"):
            self.assertEqual(browser.get(self.url(endpoint), {"shipment_id": 1}).status_code, 405)

    def test_invalid_payload_returns_controlled_error(self):
        browser = self.browser()
        self.login(browser)
        for payload in ([], {"shipment_id": True}, {"shipment_id": "bad"}):
            self.assertEqual(self.post(browser, "prepare", payload).status_code, 400)
