"""A moved WeChat superadministrator is a platform account, not a tourist."""
from unittest.mock import patch

from django.test import TestCase

from account.models import Organization, OrganizationRole, OrganizationType, User, UserOrganization
from account.services import auth_service as auth
from web_portal.access import principal_for

REAL = 'oqHp-1' + 'A' * 18 + '0B0M'


class PlatformAdministratorIdentityTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(id=1, username='admin', password='test-only', wx_openid=REAL)
        self.guest = User.objects.create_user(username='ordinary-guest', wx_openid='ordinary-guest-openid')
        owner_type = OrganizationType.objects.get(code='OWNER')
        merchant_type = OrganizationType.objects.get(code='MERCHANT')
        self.root = Organization.objects.create(id=2, name='交互测试厂家', code='ADMIN-IDENTITY-FACTORY', organization_type=owner_type)
        self.merchant = Organization.objects.create(id=18, name='交互测试直属商家02-订单计划', code='ADMIN-IDENTITY-MERCHANT', organization_type=merchant_type, factory_root=self.root)
        self.merchant_user = User.objects.create_user(id=49, username='test_direct_merchant_02_owner_20260912', wx_openid='test-slot49-vacated-fixture')
        self.identity = UserOrganization.objects.create(user=self.merchant_user, organization=self.merchant, role=OrganizationRole.objects.get(code='merchant_owner', organization_type=merchant_type))

    def test_superadmin_without_organization_has_platform_role_and_no_fake_root(self):
        data = auth.build_user_payload(self.admin)
        self.assertEqual(data['role'], 'platform_admin')
        self.assertEqual(data['role_name'], '超级管理员')
        self.assertTrue(data['is_platform_admin'])
        for key in ('organization_id', 'factory_root_id'):
            self.assertIsNone(data[key])
        self.assertEqual(data['organization_name'], '')
        self.assertEqual(data['factory_root_name'], '')
        self.assertIsNone(auth.get_active_identity(self.admin))
        self.assertFalse(UserOrganization.objects.filter(user=self.admin).exists())

    def test_ordinary_guest_remains_a_guest(self):
        data = auth.build_user_payload(self.guest)
        self.assertEqual((data['role'], data['role_name'], data['is_platform_admin']), ('tourist', '游客', False))

    def test_staff_flag_alone_does_not_make_a_platform_administrator(self):
        self.guest.is_staff = True
        self.guest.save(update_fields=['is_staff'])
        data = auth.build_user_payload(self.guest)
        self.assertEqual(data['role'], 'tourist')
        self.assertFalse(data['is_platform_admin'])

    def test_inactive_superadmin_is_not_presented_as_a_platform_administrator(self):
        self.admin.is_active = False
        self.admin.save(update_fields=['is_active'])
        data = auth.build_user_payload(self.admin)
        self.assertEqual(data['role'], 'tourist')
        self.assertFalse(data['is_platform_admin'])

    def test_existing_business_identity_payload_is_preserved(self):
        data = auth.build_user_payload(self.merchant_user)
        self.assertEqual(data['role'], 'merchant_owner')
        self.assertEqual(data['organization_id'], 18)
        self.assertEqual(data['factory_root_id'], 2)
        self.assertFalse(data['is_platform_admin'])

    def test_superadmin_with_existing_business_identity_keeps_its_business_role(self):
        self.merchant_user.is_superuser = True
        self.merchant_user.save(update_fields=['is_superuser'])
        data = auth.build_user_payload(self.merchant_user)
        self.assertEqual(data['role'], 'merchant_owner')
        self.assertTrue(data['is_platform_admin'])
        self.assertEqual(data['organization_id'], 18)

    def test_moved_wechat_login_returns_existing_admin_and_new_role_over_http(self):
        before_users = User.objects.count()
        with patch('account.api.views._wx_code_to_openid', return_value=REAL):
            response = self.client.post('/pldp/api/account/login/', {'code': 'wechat-test-code'})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['code'], 0)
        self.assertEqual(data['role'], 'platform_admin')
        self.assertEqual(data['role_name'], '超级管理员')
        self.assertTrue(data['is_platform_admin'])
        self.assertIsNone(data['organization_id'])
        self.assertTrue(data['access_token'])
        self.assertEqual(User.objects.count(), before_users)
        self.assertEqual(User.objects.get(wx_openid=REAL).pk, 1)

    def test_profile_refresh_returns_platform_identity_over_http(self):
        response = self.client.get('/pldp/api/account/profile/', HTTP_AUTHORIZATION='Bearer ' + auth.issue_access_token(self.admin))
        self.assertEqual(response.status_code, 200)
        data = response.json()['data']
        self.assertEqual(data['role'], 'platform_admin')
        self.assertTrue(data['is_platform_admin'])
        self.assertIsNone(data['organization_id'])

    def test_client_profile_cannot_promote_an_ordinary_guest(self):
        response = self.client.post('/pldp/api/account/profile/', {
            'nickname': '普通游客', 'role': 'platform_admin', 'is_platform_admin': True,
            'is_staff': True, 'is_superuser': True,
        }, HTTP_AUTHORIZATION='Bearer ' + auth.issue_access_token(self.guest))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['role'], 'tourist')
        self.assertFalse(response.json()['data']['is_platform_admin'])
        self.guest.refresh_from_db()
        self.assertFalse(self.guest.is_staff)
        self.assertFalse(self.guest.is_superuser)

    def test_web_principal_remains_existing_platform_principal(self):
        data = principal_for(self.admin)
        self.assertEqual(data['kind'], 'superuser')
        self.assertEqual(data['user_id'], 1)
        self.assertEqual(data['modules'], ['admin', 'users'])

    def test_building_identity_does_not_change_slots_bindings_or_business_data(self):
        users = list(User.objects.order_by('pk').values())
        identities = list(UserOrganization.objects.order_by('pk').values())
        organizations = list(Organization.objects.order_by('pk').values())
        auth.build_user_payload(self.admin)
        auth.build_user_payload(self.merchant_user)
        self.assertEqual(users, list(User.objects.order_by('pk').values()))
        self.assertEqual(identities, list(UserOrganization.objects.order_by('pk').values()))
        self.assertEqual(organizations, list(Organization.objects.order_by('pk').values()))

    def test_handover_legacy_protection_is_retained(self):
        self.assertTrue({1, 49}.issubset(auth.LEGACY_TOKEN_RELOGIN_USER_IDS))
