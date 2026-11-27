"""Regression coverage for all approved WeChat slot handovers."""

import hashlib

from django.core import signing
from django.test import RequestFactory, TestCase, override_settings

from account.models import User
from account.services.auth_service import (
    ACCESS_TOKEN_SALT,
    LEGACY_TOKEN_RELOGIN_USER_IDS,
    ApiAuthenticationError,
    authenticate_request,
    issue_access_token,
)


APPROVED_SLOTS = frozenset({1, 2, 3, 4, 7, 8, 9, 24, 26, 27, 33, 35, 49, 51, 55, 57, 59})
CONTROL_SLOT = 61


class WeChatBindingGuardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        for user_id in sorted(APPROVED_SLOTS | {CONTROL_SLOT}):
            User.objects.create_user(
                pk=user_id,
                username=f"handover-guard-test-{user_id}",
                wx_openid=f"guard-openid-{user_id}",
            )

    def setUp(self):
        self.factory = RequestFactory()

    def _sign(self, payload):
        return signing.dumps(payload, salt=ACCESS_TOKEN_SALT, compress=True)

    def _authenticate(self, token):
        request = self.factory.get("/", HTTP_AUTHORIZATION=f"Bearer {token}")
        return authenticate_request(request)

    def test_all_approved_handovers_reject_legacy_tokens(self):
        self.assertEqual(
            LEGACY_TOKEN_RELOGIN_USER_IDS,
            APPROVED_SLOTS,
        )
        for user_id in sorted(APPROVED_SLOTS):
            with self.subTest(user_id=user_id):
                with self.assertRaises(ApiAuthenticationError):
                    self._authenticate(self._sign({"user_id": user_id}))
        self.assertEqual(self._authenticate(self._sign({"user_id": 61})).pk, 61)

    def test_new_tokens_authenticate_all_handover_slots_and_unrelated_user(self):
        for user_id in sorted(APPROVED_SLOTS | {CONTROL_SLOT}):
            with self.subTest(user_id=user_id):
                user = User.objects.get(pk=user_id)
                self.assertEqual(self._authenticate(issue_access_token(user)).pk, user_id)

    def test_old_binding_fails_after_openid_moves_and_fresh_binding_works(self):
        for user_id in sorted(APPROVED_SLOTS | {CONTROL_SLOT}):
            with self.subTest(user_id=user_id):
                user = User.objects.get(pk=user_id)
                previous_token = issue_access_token(user)
                user.wx_openid = f"replacement-openid-{user_id}"
                user.save(update_fields=["wx_openid"])
                with self.assertRaises(ApiAuthenticationError):
                    self._authenticate(previous_token)
                self.assertEqual(self._authenticate(issue_access_token(user)).pk, user_id)

    def test_cleared_source_openid_denies_old_and_new_bound_tokens(self):
        for user_id in (4, 7, 8, 9, 51):
            with self.subTest(user_id=user_id):
                user = User.objects.get(pk=user_id)
                previous_token = issue_access_token(user)
                user.wx_openid = ""
                user.save(update_fields=["wx_openid"])
                with self.assertRaises(ApiAuthenticationError):
                    self._authenticate(previous_token)
                with self.assertRaises(ApiAuthenticationError):
                    self._authenticate(issue_access_token(user))
                user.wx_openid = f"restored-test-openid-{user_id}"
                user.save(update_fields=["wx_openid"])

    def test_actual_child_slot_exchanges_revoke_both_old_sessions(self):
        for source_id, target_id in ((4, 55), (9, 59), (51, 57), (3, 49)):
            with self.subTest(source_id=source_id, target_id=target_id):
                source = User.objects.get(pk=source_id)
                target = User.objects.get(pk=target_id)
                source_openid, target_openid = source.wx_openid, target.wx_openid
                source_token = issue_access_token(source)
                target_token = issue_access_token(target)
                # Use a temporary value only inside the rollback-only test DB
                # to satisfy the unique constraint while exchanging bindings.
                source.wx_openid = f"test-exchange-temporary-{source_id}"
                source.save(update_fields=["wx_openid"])
                target.wx_openid = source_openid
                target.save(update_fields=["wx_openid"])
                source.wx_openid = target_openid
                source.save(update_fields=["wx_openid"])
                for old_token in (source_token, target_token):
                    with self.assertRaises(ApiAuthenticationError):
                        self._authenticate(old_token)
                for current in (source, target):
                    self.assertEqual(
                        self._authenticate(issue_access_token(current)).pk,
                        current.pk,
                    )
                self.assertEqual(User.objects.get(wx_openid=source_openid).pk, target_id)
                self.assertEqual(User.objects.get(wx_openid=target_openid).pk, source_id)

    def test_factory_sales_and_tourist_exchanges_revoke_old_sessions(self):
        for target_id, source_id in ((4, 57), (26, 59)):
            with self.subTest(target_id=target_id, source_id=source_id):
                target = User.objects.get(pk=target_id)
                source = User.objects.get(pk=source_id)
                target_openid, source_openid = target.wx_openid, source.wx_openid
                target_token = issue_access_token(target)
                source_token = issue_access_token(source)
                target.wx_openid = f"test-exchange-temporary-{target_id}"
                target.save(update_fields=["wx_openid"])
                source.wx_openid = target_openid
                source.save(update_fields=["wx_openid"])
                target.wx_openid = source_openid
                target.save(update_fields=["wx_openid"])
                for old_token in (target_token, source_token):
                    with self.assertRaises(ApiAuthenticationError):
                        self._authenticate(old_token)
                for current in (target, source):
                    self.assertEqual(
                        self._authenticate(issue_access_token(current)).pk,
                        current.pk,
                    )
                self.assertEqual(User.objects.get(wx_openid=source_openid).pk, target_id)
                self.assertEqual(User.objects.get(wx_openid=target_openid).pk, source_id)

    def test_lower01_owner_and_material_stock_exchange_revokes_old_sessions(self):
        material_stock = User.objects.get(pk=35)
        lower_owner = User.objects.get(pk=55)
        stock_openid, owner_openid = material_stock.wx_openid, lower_owner.wx_openid
        old_tokens = (issue_access_token(material_stock), issue_access_token(lower_owner))
        material_stock.wx_openid = "test-exchange-temporary-35"
        material_stock.save(update_fields=["wx_openid"])
        lower_owner.wx_openid = stock_openid
        lower_owner.save(update_fields=["wx_openid"])
        material_stock.wx_openid = owner_openid
        material_stock.save(update_fields=["wx_openid"])
        for old_token in old_tokens:
            with self.assertRaises(ApiAuthenticationError):
                self._authenticate(old_token)
        self.assertEqual(
            self._authenticate(issue_access_token(material_stock)).pk,
            material_stock.pk,
        )
        self.assertEqual(
            self._authenticate(issue_access_token(lower_owner)).pk,
            lower_owner.pk,
        )
        self.assertEqual(User.objects.get(wx_openid=owner_openid).pk, 35)
        self.assertEqual(User.objects.get(wx_openid=stock_openid).pk, 55)

    def test_material_stock_and_tourist_user2_exchange_revokes_old_sessions(self):
        tourist = User.objects.get(pk=2)
        material_stock = User.objects.get(pk=35)
        tourist_openid, stock_openid = tourist.wx_openid, material_stock.wx_openid
        old_tokens = (issue_access_token(tourist), issue_access_token(material_stock))
        tourist.wx_openid = "test-exchange-temporary-2"
        tourist.save(update_fields=["wx_openid"])
        material_stock.wx_openid = tourist_openid
        material_stock.save(update_fields=["wx_openid"])
        tourist.wx_openid = stock_openid
        tourist.save(update_fields=["wx_openid"])
        for old_token in old_tokens:
            with self.assertRaises(ApiAuthenticationError):
                self._authenticate(old_token)
        self.assertEqual(self._authenticate(issue_access_token(tourist)).pk, 2)
        self.assertEqual(self._authenticate(issue_access_token(material_stock)).pk, 35)
        self.assertEqual(User.objects.get(wx_openid=stock_openid).pk, 2)
        self.assertEqual(User.objects.get(wx_openid=tourist_openid).pk, 35)

    def test_merchant02_owner_session_survives_child_handover(self):
        owner = User.objects.get(pk=49)
        token = issue_access_token(owner)
        for user_id in (4, 9, 51, 55, 57, 59):
            User.objects.filter(pk=user_id).update(wx_openid=f"handover-new-{user_id}")
        self.assertEqual(self._authenticate(token).pk, 49)
        # Keep the original handover's legacy revocation in force.
        with self.assertRaises(ApiAuthenticationError):
            self._authenticate(self._sign({"user_id": 49}))

    def test_binding_is_keyed_and_contains_no_raw_or_plainly_hashed_openid(self):
        user = User.objects.get(pk=49)
        payload = signing.loads(issue_access_token(user), salt=ACCESS_TOKEN_SALT)
        self.assertEqual(set(payload), {"user_id", "wechat_binding"})
        self.assertNotIn(user.wx_openid, str(payload))
        self.assertNotEqual(
            payload["wechat_binding"], hashlib.sha256(user.wx_openid.encode()).hexdigest()
        )
        with override_settings(SECRET_KEY="different-isolated-test-key"):
            other_payload = signing.loads(issue_access_token(user), salt=ACCESS_TOKEN_SALT)
        self.assertNotEqual(payload["wechat_binding"], other_payload["wechat_binding"])

    def test_signed_malformed_payloads_raise_controlled_authentication_error(self):
        for payload in (
            [], None, "text", 49, {}, {"user_id": None}, {"user_id": True},
            {"user_id": "49"}, {"user_id": []}, {"user_id": {}},
            {"user_id": 0}, {"user_id": -1}, {"user_id": 2 ** 100},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ApiAuthenticationError):
                    self._authenticate(self._sign(payload))

    def test_included_invalid_binding_is_rejected_even_for_unrelated_user(self):
        for user_id in (49, 61):
            for binding in (None, [], {}, 17, "", "0" * 64):
                with self.subTest(user_id=user_id, binding=binding):
                    with self.assertRaises(ApiAuthenticationError) as result:
                        self._authenticate(self._sign({
                            "user_id": user_id, "wechat_binding": binding,
                        }))
                    self.assertNotIn(f"guard-openid-{user_id}", str(result.exception))

    def test_inactive_account_and_bad_signature_still_fail(self):
        user = User.objects.get(pk=49)
        token = issue_access_token(user)
        user.is_active = False
        user.save(update_fields=["is_active"])
        with self.assertRaises(ApiAuthenticationError):
            self._authenticate(token)
        with self.assertRaises(ApiAuthenticationError):
            self._authenticate("invalid-signature")
