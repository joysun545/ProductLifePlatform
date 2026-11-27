from django.test import TestCase

from account.models import OrganizationRole, SeniorManagerProfile, UserOrganization
from print import tests as print_fixture
from sales.models import OrderPlanShipmentDevice
from sales.services.order_plan_material_flow_service import (
    _shipment_task_key,
    authorize_material_flow_shipment,
)
from web_portal import tests as portal_fixture
from web_portal.access import principal_for
from workflow.models import WorkItem


class DelegatedPrintCenterTests(TestCase):
    _user_with_identity = print_fixture.PrintCenterSecurityTests._user_with_identity
    _accepted_direct_relation = print_fixture.PrintCenterSecurityTests._accepted_direct_relation
    _product_with_bom = print_fixture.PrintCenterSecurityTests._product_with_bom
    _material_with_supplier = print_fixture.PrintCenterSecurityTests._material_with_supplier
    _create_lot = print_fixture.PrintCenterSecurityTests._create_lot
    _create_v2_plan = print_fixture.PrintCenterSecurityTests._create_v2_plan
    _ready_production_shipment = print_fixture.PrintCenterSecurityTests._ready_production_shipment
    url = print_fixture.PrintCenterSecurityTests.url
    print_post = print_fixture.PrintCenterSecurityTests.post
    csrf = portal_fixture.PortalTests.csrf
    post = portal_fixture.PortalTests.post
    begin = portal_fixture.PortalTests.begin
    approve = portal_fixture.PortalTests.approve
    signin = portal_fixture.PortalTests.signin

    def setUp(self):
        print_fixture.PrintCenterSecurityTests.setUp(self)
        portal_fixture.PortalTests.setUp(self)

    def _manager(self):
        role = OrganizationRole.objects.get(code="factory_senior_manager")
        manager = self._user_with_identity(
            "print-delegated-manager", "print-delegated-manager-openid", self.factory, role,
        )
        profile = SeniorManagerProfile.objects.create(identity=manager.organization_identity)
        profile.managed_roles.add(self.factory_stock_role)
        return manager, profile

    def _shipment(self, request_id):
        plan, _, shipment = self._ready_production_shipment(request_id=request_id, quantity=5)
        authorize_material_flow_shipment(user=self.sales_user, plan_id=plan.pk, shipment_id=shipment.pk)
        task = WorkItem.objects.get(uniq_key=_shipment_task_key(plan.pk, shipment.pk, "qrcode-print"))
        return plan, shipment, task

    def test_staffed_delegation_includes_open_tasks_from_before_allocation(self):
        prior_plan, _, prior_task = self._shipment("print-before-manager-allocation")
        manager, _ = self._manager()
        plan, shipment, task = self._shipment("print-manager-task")
        self.assertTrue(task.user_tasks.filter(to_openid=manager.wx_openid, need_action=True).exists())

        self.signin(manager)
        self.assertEqual(principal_for(manager)["modules"], ["print"])
        page = self.client.get(self.url("center"))
        self.assertContains(page, plan.plan_code)
        self.assertContains(page, prior_plan.plan_code)
        self.assertTrue(prior_task.user_tasks.filter(to_openid=manager.wx_openid, need_action=True).exists())

        payload = {"shipment_id": shipment.pk}
        self.assertEqual(self.print_post(self.client, "confirm", payload).status_code, 400)
        prepared = self.print_post(self.client, "prepare", payload)
        self.assertEqual(prepared.status_code, 200, prepared.content)
        self.assertEqual(len(prepared.json()["data"]["qrcodes"]), 5)
        confirmed = self.print_post(self.client, "confirm", payload)
        self.assertEqual(confirmed.status_code, 200, confirmed.content)
        task.refresh_from_db()
        self.assertEqual(task.done_by_openid, manager.wx_openid)
        self.assertEqual(task.payload["completed_by_user_id"], manager.pk)
        self.assertNotContains(self.client.get(self.url("center")), plan.plan_code)

    def test_vacant_stock_role_can_print_with_its_grant_and_loses_access_on_revocation(self):
        manager, profile = self._manager()
        UserOrganization.objects.filter(user=self.factory_stock).update(is_active=False)
        plan, shipment, task = self._shipment("print-stock-vacant")
        self.assertEqual(
            list(task.user_tasks.filter(need_action=True).values_list("to_openid", flat=True)),
            [manager.wx_openid],
        )

        self.signin(manager)
        self.assertContains(self.client.get(self.url("center")), plan.plan_code)
        payload = {"shipment_id": shipment.pk}
        prepared = self.print_post(self.client, "prepare", payload)
        self.assertEqual(prepared.status_code, 200, prepared.content)

        profile.managed_roles.remove(self.factory_stock_role)
        self.assertEqual(self.client.get(self.url("center")).status_code, 403)
        self.assertEqual(self.print_post(self.client, "confirm", payload).status_code, 403)
        shipment.refresh_from_db()
        self.assertIsNone(shipment.qrcode_printed_at)
        self.assertEqual(self.approve(self.begin(), manager).status_code, 403)

        self.assertEqual(self.post("logout").status_code, 200)
        profile.managed_roles.add(self.factory_stock_role)
        self.signin(manager)
        prepared = self.print_post(self.client, "prepare", payload)
        self.assertEqual(prepared.status_code, 200, prepared.content)
        confirmed = self.print_post(self.client, "confirm", payload)
        self.assertEqual(confirmed.status_code, 200, confirmed.content)
        task.refresh_from_db()
        self.assertEqual(task.done_by_openid, manager.wx_openid)
