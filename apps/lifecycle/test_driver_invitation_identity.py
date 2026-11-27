"""Driver sharing respects the account's historical unique identity slot.

The fixture is an active customer owner and an empty customer vehicle; no
device, stock, installation or activation facts are manufactured. Invitations,
acceptances and driver grants are formed through the current HTTP APIs. Direct
admin deactivations deliberately model revocation between preview and POST.
"""
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

from django.db import IntegrityError, connections
from django.test import Client, TestCase, TransactionTestCase
from django.utils import timezone

from account.models import Organization, OrganizationRole, OrganizationType, User, UserOrganization
from account.services.auth_service import issue_access_token
from lifecycle.models import CustomerVehicle, DriverVehicleBinding, DriverVehicleInvitation
from lifecycle.services.customer_service import CustomerDeviceService


class DriverInvitationFixture:
    def make_fixture(self):
        self.owner_type, _ = OrganizationType.objects.get_or_create(
            code="OWNER", defaults={"name": "厂家"})
        self.customer_type, _ = OrganizationType.objects.get_or_create(
            code="CUSTOMER", defaults={"name": "用户"})
        self.owner_role, _ = OrganizationRole.objects.get_or_create(
            organization_type=self.customer_type, code="customer_owner",
            defaults={"name": "用户负责人", "is_owner_role": True})
        self.driver_role, _ = OrganizationRole.objects.get_or_create(
            organization_type=self.customer_type, code="driver", defaults={"name": "司机"})
        self.root = Organization.objects.create(
            code="driver-test-root", name="司机测试厂家", organization_type=self.owner_type)
        self.organization = Organization.objects.create(
            code="driver-test-customer", name="司机测试车队",
            organization_type=self.customer_type, factory_root=self.root)
        self.owner = self.make_user("driver-test-owner")
        UserOrganization.objects.create(user=self.owner, organization=self.organization,
            role=self.owner_role)
        self.vehicle = CustomerVehicle.objects.create(
            customer_organization=self.organization, vehicle_no="测试车辆一")
        self.viewer = self.make_user("driver-test-viewer")
        self.invitation = self.share()

    @staticmethod
    def make_user(name):
        return User.objects.create_user(username=name, wx_openid="openid-" + name)

    @staticmethod
    def auth(user):
        return {"HTTP_AUTHORIZATION": "Bearer " + issue_access_token(user)}

    def share(self, vehicle=None, owner=None):
        response = self.client.post(
            f"/pldp/api/lifecycle/customer/vehicles/{(vehicle or self.vehicle).pk}/driver-invitations/",
            **self.auth(owner or self.owner))
        self.assertEqual(response.status_code, 201, response.json())
        return DriverVehicleInvitation.objects.get(token=response.json()["data"]["token"])

    def url(self, invitation=None):
        return f"/pldp/api/lifecycle/customer/driver-invitations/{(invitation or self.invitation).token}/"

    def preview(self, user=None, invitation=None):
        return self.client.get(self.url(invitation), **self.auth(user or self.viewer))

    def accept(self, user=None, invitation=None):
        return self.client.post(self.url(invitation) + "accept/", **self.auth(user or self.viewer))

    def slot(self, *, organization=None, role=None, active=True):
        return UserOrganization.objects.create(user=self.viewer,
            organization=organization or self.organization,
            role=role or self.driver_role, is_active=active)

    def other_customer(self):
        organization = Organization.objects.create(code="driver-test-other", name="其他车队",
            organization_type=self.customer_type, factory_root=self.root)
        owner = self.make_user("driver-test-other-owner")
        UserOrganization.objects.create(user=owner, organization=organization, role=self.owner_role)
        vehicle = CustomerVehicle.objects.create(customer_organization=organization,
            vehicle_no="其他车辆")
        return organization, owner, vehicle

    @staticmethod
    def facts():
        return {
            "invitations": list(DriverVehicleInvitation.objects.order_by("pk").values()),
            "identities": list(UserOrganization.objects.order_by("pk").values()),
            "bindings": list(DriverVehicleBinding.objects.order_by("pk").values()),
            "vehicles": list(CustomerVehicle.objects.order_by("pk").values()),
        }

    def assert_blocked(self, *, user=None, invitation=None, reason=None):
        before = self.facts()
        detail = self.preview(user=user, invitation=invitation)
        self.assertEqual(detail.status_code, 200, detail.json())
        data = detail.json()["data"]
        self.assertFalse(data["acceptance_allowed"])
        self.assertFalse(data["current_access_allowed"])
        self.assertTrue(data["blocked_reason"])
        if reason:
            self.assertIn(reason, data["blocked_reason"])
        response = self.accept(user=user, invitation=invitation)
        self.assertEqual(response.status_code, 403, response.json())
        self.assertEqual(response.json()["msg"], data["blocked_reason"])
        self.assertEqual(self.facts(), before)
        return data


class DriverInvitationIdentityTests(DriverInvitationFixture, TestCase):
    def setUp(self):
        self.make_fixture()

    def test_new_visitor_accepts_and_same_user_replay_is_read_only_even_after_expiry(self):
        detail = self.preview().json()["data"]
        self.assertTrue(detail["acceptance_allowed"])
        self.assertFalse(detail["accepted_by_me"])
        response = self.accept()
        self.assertEqual(response.status_code, 201, response.json())
        data = response.json()["data"]
        self.assertEqual(data["status"], "ACCEPTED")
        self.assertTrue(data["accepted_by_me"])
        self.assertTrue(data["current_access_allowed"])
        self.assertFalse(data["acceptance_allowed"])
        self.assertEqual(data["blocked_reason"], "")
        slot = UserOrganization.objects.get(user=self.viewer)
        self.assertEqual((slot.organization_id, slot.role_id),
            (self.organization.pk, self.driver_role.pk))
        self.assertTrue(DriverVehicleBinding.objects.get(driver=self.viewer).is_active)
        DriverVehicleInvitation.objects.filter(pk=self.invitation.pk).update(
            expires_at=timezone.now() - timedelta(days=1))
        before = self.facts()
        self.assertTrue(self.preview().json()["data"]["current_access_allowed"])
        self.assertEqual(self.accept().status_code, 201)
        self.assertEqual(self.facts(), before)

    def test_active_same_customer_driver_slot_is_reused_unchanged(self):
        identity = self.slot()
        before = UserOrganization.objects.get(pk=identity.pk).__dict__.copy()
        self.assertTrue(self.preview().json()["data"]["acceptance_allowed"])
        self.assertEqual(self.accept().status_code, 201)
        after = UserOrganization.objects.get(pk=identity.pk).__dict__.copy()
        before.pop("_state"); after.pop("_state")
        self.assertEqual(after, before)
        self.assertEqual(UserOrganization.objects.filter(user=self.viewer).count(), 1)

    def test_dormant_driver_slot_is_neither_recreated_nor_restored(self):
        identity = self.slot(active=False)
        self.assert_blocked(reason="停用")
        identity.refresh_from_db()
        self.assertFalse(identity.is_active)
        self.assertFalse(DriverVehicleBinding.objects.filter(driver=self.viewer).exists())

    def test_active_and_dormant_other_customer_slots_cannot_move_organizations(self):
        other, _, _ = self.other_customer()
        identity = self.slot(organization=other)
        self.assert_blocked(reason="其他业务岗位")
        identity.is_active = False
        identity.save(update_fields=("is_active",))
        self.assert_blocked(reason="停用")
        self.assertEqual(UserOrganization.objects.get(pk=identity.pk).organization_id, other.pk)

    def test_other_role_in_same_customer_is_not_converted_to_driver(self):
        identity = self.slot(role=self.owner_role)
        self.assert_blocked(reason="其他业务岗位")
        self.assertEqual(UserOrganization.objects.get(pk=identity.pk).role_id, self.owner_role.pk)

    def test_unavailable_identity_dimensions_are_blocked_without_creating_another_slot(self):
        self.slot()
        cases = [
            (self.organization, "is_active", False),
            (self.customer_type, "is_active", False),
            (self.driver_role, "is_active", False),
            (self.root, "is_active", False),
            # A missing root is an explicitly unusable legacy organization,
            # produced here by an admin bypass of normal model validation.
            (self.organization, "factory_root_id", None),
        ]
        for target, field, value in cases:
            with self.subTest(field=field, target=type(target).__name__):
                previous = getattr(target, field)
                type(target).objects.filter(pk=target.pk).update(**{field: value})
                self.assert_blocked()
                type(target).objects.filter(pk=target.pk).update(**{field: previous})

    def test_new_visitor_cannot_accept_when_driver_role_or_root_is_unavailable(self):
        OrganizationRole.objects.filter(pk=self.driver_role.pk).update(is_active=False)
        self.assert_blocked(reason="司机岗位")
        OrganizationRole.objects.filter(pk=self.driver_role.pk).update(is_active=True)
        Organization.objects.filter(pk=self.root.pk).update(is_active=False)
        self.assert_blocked(reason="所属组织")
        self.assertFalse(UserOrganization.objects.filter(user=self.viewer).exists())

    def test_identity_revoked_after_allowed_preview_is_rechecked_at_post(self):
        identity = self.slot()
        self.assertTrue(self.preview().json()["data"]["acceptance_allowed"])
        UserOrganization.objects.filter(pk=identity.pk).update(is_active=False)
        before = self.facts()
        response = self.accept()
        self.assertEqual(response.status_code, 403, response.json())
        self.assertEqual(self.facts(), before)
        self.assertFalse(self.preview().json()["data"]["acceptance_allowed"])

    def test_accepted_replay_cannot_restore_revoked_identity_binding_or_vehicle(self):
        self.assertEqual(self.accept().status_code, 201)
        identity = UserOrganization.objects.get(user=self.viewer)
        binding = DriverVehicleBinding.objects.get(driver=self.viewer)
        cases = [(identity, "is_active", False), (binding, "is_active", False),
            (self.vehicle, "status", CustomerVehicle.Status.INACTIVE)]
        for target, field, value in cases:
            with self.subTest(target=type(target).__name__):
                previous = getattr(target, field)
                type(target).objects.filter(pk=target.pk).update(**{field: value})
                detail = self.assert_blocked()
                self.assertTrue(detail["accepted_by_me"])
                type(target).objects.filter(pk=target.pk).update(**{field: previous})

    def test_only_owner_can_revoke_and_neither_old_share_link_restores_access(self):
        self.assertEqual(self.accept().status_code, 201)
        pending = self.share()
        url = f"/pldp/api/lifecycle/customer/vehicles/{self.vehicle.pk}/drivers/{self.viewer.pk}/revoke/"
        self.assertEqual(self.client.post(url, **self.auth(self.viewer)).status_code, 403)
        _, other_owner, _ = self.other_customer()
        self.assertEqual(self.client.post(url, **self.auth(other_owner)).status_code, 409)

        response = self.client.post(url, **self.auth(self.owner))
        self.assertEqual(response.status_code, 200, response.json())
        self.assertTrue(response.json()["data"]["changed"])
        self.assertFalse(DriverVehicleBinding.objects.get(driver=self.viewer, vehicle=self.vehicle).is_active)
        self.assertNotEqual(self.accept().status_code, 201)
        self.assertNotEqual(self.accept(invitation=pending).status_code, 201)

        new = self.share()
        repeat = self.client.post(url, **self.auth(self.owner))
        self.assertFalse(repeat.json()["data"]["changed"])
        self.assertEqual(self.accept(invitation=new).status_code, 201)

    def test_new_explicit_share_can_regrant_binding_but_never_inactive_identity(self):
        self.assertEqual(self.accept().status_code, 201)
        binding = DriverVehicleBinding.objects.get(driver=self.viewer)
        DriverVehicleBinding.objects.filter(pk=binding.pk).update(is_active=False)
        invitation = self.share()
        self.assertNotEqual(invitation.pk, self.invitation.pk)
        self.assertEqual(self.accept(invitation=invitation).status_code, 201)
        binding.refresh_from_db()
        self.assertTrue(binding.is_active)
        self.assertEqual(binding.invitation_id, invitation.pk)
        UserOrganization.objects.filter(user=self.viewer).update(is_active=False)
        next_invitation = self.share()
        self.assert_blocked(invitation=next_invitation, reason="停用")

    def test_self_other_recipient_and_expired_invitation_are_controlled(self):
        self.assert_blocked(user=self.owner, reason="自己的")
        self.assertEqual(self.accept().status_code, 201)
        other = self.make_user("driver-test-second-viewer")
        before = self.facts()
        self.assertEqual(self.preview(user=other).status_code, 409)
        self.assertEqual(self.accept(user=other).status_code, 409)
        self.assertEqual(self.facts(), before)
        pending = self.share()
        DriverVehicleInvitation.objects.filter(pk=pending.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.accept(user=other, invitation=pending).status_code, 409)
        self.assertFalse(UserOrganization.objects.filter(user=other).exists())

    def test_unique_failure_rolls_back_invitation_claim_before_returning_conflict(self):
        before = self.facts()
        with patch.object(UserOrganization.objects, "create", side_effect=IntegrityError("unique slot")):
            response = self.accept()
        self.assertEqual(response.status_code, 409, response.json())
        # These reads run after the failed atomic block, proving the connection
        # is usable and neither the CAS claim nor any grant leaked out.
        self.assertEqual(self.facts(), before)
        self.assertEqual(self.accept().status_code, 201)

    def test_disabled_login_is_401_and_does_not_create_any_driver_fact(self):
        User.objects.filter(pk=self.viewer.pk).update(is_active=False)
        before = self.facts()
        self.assertEqual(self.preview().status_code, 401)
        self.assertEqual(self.accept().status_code, 401)
        self.assertEqual(self.facts(), before)


class DriverInvitationConcurrencyTests(DriverInvitationFixture, TransactionTestCase):
    def setUp(self):
        self.make_fixture()

    def race(self, requests):
        barrier = threading.Barrier(2)
        original = CustomerDeviceService._driver_invitation_eligibility.__func__

        def synchronized(cls, invitation, **kwargs):
            decision = original(cls, invitation, **kwargs)
            if kwargs.get("lock_identity"):
                # Both real decisions finish before either real CAS write.
                barrier.wait(timeout=10)
            return decision

        def worker(user, invitation):
            database = connections["default"]
            database.ensure_connection()
            connection_id = id(database.connection)
            try:
                response = Client().post(self.url(invitation) + "accept/", **self.auth(user))
                return response.status_code, response.json(), connection_id
            finally:
                database.close()

        with patch.object(CustomerDeviceService, "_driver_invitation_eligibility", classmethod(synchronized)):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(worker, *request) for request in requests]
                outcomes = [future.result(timeout=30) for future in futures]
        self.assertEqual(len({x[2] for x in outcomes}), 2)
        for status_code, body, _ in outcomes:
            self.assertIn(status_code, (201, 403, 409), body)
        return outcomes

    def test_two_users_cannot_both_claim_one_invitation_or_leak_loser_identity(self):
        other = self.make_user("driver-test-concurrent-other")
        outcomes = self.race([(self.viewer, self.invitation), (other, self.invitation)])
        self.assertLessEqual(sum(x[0] == 201 for x in outcomes), 1)
        # Shared-memory SQLite may reject both writers. A clean retry must then
        # make one claim, and the competing account must receive a business error.
        if not any(x[0] == 201 for x in outcomes):
            self.assertEqual(self.accept().status_code, 201)
        self.invitation.refresh_from_db()
        winner = User.objects.get(pk=self.invitation.invitee_id)
        loser = other if winner.pk == self.viewer.pk else self.viewer
        self.assertEqual(self.accept(user=winner).status_code, 201)
        self.assertEqual(self.accept(user=loser).status_code, 409)
        self.assertEqual(UserOrganization.objects.filter(user__in=[self.viewer, other]).count(), 1)
        self.assertFalse(UserOrganization.objects.filter(user=loser).exists())
        self.assertEqual(DriverVehicleBinding.objects.filter(vehicle=self.vehicle).count(), 1)

    def test_one_user_cannot_join_two_organizations_via_concurrent_shares(self):
        other_org, owner, vehicle = self.other_customer()
        second = self.share(vehicle=vehicle, owner=owner)
        outcomes = self.race([(self.viewer, self.invitation), (self.viewer, second)])
        self.assertLessEqual(sum(x[0] == 201 for x in outcomes), 1)
        if not any(x[0] == 201 for x in outcomes):
            self.assertEqual(self.accept().status_code, 201)
        identity = UserOrganization.objects.get(user=self.viewer)
        winner = self.invitation if identity.organization_id == self.organization.pk else second
        loser = second if winner.pk == self.invitation.pk else self.invitation
        self.assertEqual(self.accept(invitation=winner).status_code, 201)
        self.assertEqual(self.accept(invitation=loser).status_code, 403)
        loser.refresh_from_db()
        self.assertEqual(loser.status, "PENDING")
        self.assertIsNone(loser.invitee_id)
        self.assertEqual(UserOrganization.objects.filter(user=self.viewer).count(), 1)
        grants = DriverVehicleBinding.objects.filter(driver=self.viewer)
        self.assertEqual(grants.count(), 1)
        self.assertEqual(grants.get().vehicle.customer_organization_id, identity.organization_id)
