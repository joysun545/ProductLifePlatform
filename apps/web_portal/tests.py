import json
import time
from datetime import timedelta
from django.conf import settings
from django.test import TestCase, Client
from django.utils import timezone
from account.models import User, Organization, OrganizationRelation, OrganizationType, OrganizationRole, UserOrganization
from account.services.auth_service import issue_access_token
from product.models import ProductCategory
from supply_chain.models import Material, MaterialSpec, MaterialSpecSupplier
from .models import WebLoginChallenge
from .access import principal_for, ROLE_MODULES, MODULES, GRANT

class PortalTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org=Organization.objects.create(name='测试厂家',code='PORTAL_TEST',organization_type=OrganizationType.objects.get(code='OWNER'))
        cls.users={}
        for role in (*ROLE_MODULES,'factory_sales','factory_senior_manager'):
            u=User.objects.create_user(username=role,wx_openid='wx_'+role)
            r=OrganizationRole.objects.get(code=role,organization_type=cls.org.organization_type)
            UserOrganization.objects.create(user=u,organization=cls.org,role=r)
            cls.users[role]=u
        cls.admin=User.objects.create_superuser(username='admin',password='Test-Only-Password',wx_openid='wx_admin')
        cls.tourist=User.objects.create_user(username='tourist',wx_openid='wx_tourist')

    def setUp(self):self.client=Client(enforce_csrf_checks=True)
    def csrf(self,c):
        c.get('/pldp/web/portal/api/session/')
        return c.cookies['csrftoken'].value
    def post(self,path,data=None,c=None):
        c=c or self.client
        return c.post('/pldp/web/portal/api/'+path+'/',data=json.dumps(data or {}),content_type='application/json',HTTP_X_CSRFTOKEN=self.csrf(c))
    def begin(self,c=None):
        c=c or self.client
        r=self.post('start',c=c);self.assertEqual(r.status_code,200)
        row=WebLoginChallenge.objects.get(pk=c.session['pldp_web_challenge_v1'])
        return row
    def approve(self,row,user=None):
        # Tokens are opaque; tests insert a known digest without exposing production tokens.
        from .views import digest
        token=f'{row.pk:064x}';row.token_digest=digest(token);row.save(update_fields=['token_digest'])
        return Client().post('/pldp/web/portal/api/approve/',data=json.dumps({'token':token}),content_type='application/json',HTTP_AUTHORIZATION='Bearer '+issue_access_token(user or self.users['factory_admin']))
    def signin(self,user=None,c=None):
        row=self.begin(c);self.assertEqual(self.approve(row,user).status_code,200)
        r=self.post('status',c=c);self.assertEqual(r.status_code,200,r.content)
        self.assertEqual(r.json()['data']['status'],'ready');return row

    def test_role_matrix(self):
        for role,u in self.users.items():
            if role in ROLE_MODULES:self.assertEqual(set(principal_for(u)['modules']),set(ROLE_MODULES[role]))
            else:
                with self.assertRaises(Exception):principal_for(u)
        self.assertEqual(principal_for(self.admin)['modules'],['admin','users'])
    def test_only_manager_delegated_to_sales_assistant_can_open_merchant_accounts(self):
        from account.models import SeniorManagerProfile

        manager = self.users["factory_senior_manager"]
        self.assertEqual(self.approve(self.begin(), manager).status_code, 403)
        profile = SeniorManagerProfile.objects.create(identity=manager.organization_identity)
        profile.managed_roles.add(self.users["factory_sales_assistant"].organization_identity.role)
        self.signin(manager)
        address = MODULES["merchant_accounts"][1]
        self.assertContains(self.client.get(address), "直属商家往来账")
        self.assertContains(self.client.get(address + "?section=prices"), "产品型号价格")
        profile.managed_roles.clear()
        self.assertEqual(self.client.get(address).status_code, 403)

    def test_no_csrf_cannot_start(self):
        self.assertEqual(self.client.post('/pldp/web/portal/api/start/').status_code,403)
    def test_cookie_not_bearer_cannot_approve(self):
        self.assertEqual(self.client.post('/pldp/web/portal/api/approve/').status_code,401)
    def test_approval_requires_explicit_consumption(self):
        row=self.begin();self.assertEqual(self.approve(row).status_code,200)
        self.assertNotIn(GRANT,self.client.session)
        self.assertEqual(self.post('status').json()['data']['status'],'ready')
        self.assertEqual(self.client.get('/pldp/web/portal/').status_code,200)
    def test_other_browser_cannot_consume(self):
        row=self.begin();self.approve(row)
        self.assertEqual(self.post('status',c=Client(enforce_csrf_checks=True)).status_code,410)
    def test_qr_cannot_be_approved_twice(self):
        row=self.begin();self.assertEqual(self.approve(row).status_code,200)
        self.assertEqual(self.approve(row,self.users['factory_stock']).status_code,410)
    def test_expired_qr(self):
        row=self.begin();row.expires_at=timezone.now()-timedelta(seconds=1);row.save()
        self.assertEqual(self.approve(row).status_code,410)
    def test_refresh_invalidates_old_qr(self):
        old=self.begin();new=self.begin();self.assertFalse(WebLoginChallenge.objects.filter(pk=old.pk).exists());self.assertNotEqual(old.pk,new.pk)
    def test_cancel_invalidates_qr(self):
        row=self.begin();self.post('cancel');self.assertFalse(WebLoginChallenge.objects.filter(pk=row.pk).exists())
    def test_role_change_between_approval_and_consumption(self):
        row=self.begin();u=self.users['factory_admin'];self.approve(row,u)
        UserOrganization.objects.filter(user=u).update(is_active=False)
        self.assertEqual(self.post('status').status_code,403)
    def test_wechat_change_revokes_approval(self):
        row=self.begin();u=self.users['factory_stock'];self.approve(row,u)
        User.objects.filter(pk=u.pk).update(wx_openid='other_wechat')
        self.assertEqual(self.post('status').status_code,403)
    def test_role_change_revokes_browser_grant(self):
        u=self.users['factory_stock'];self.signin(u)
        UserOrganization.objects.filter(user=u).update(is_active=False)
        self.assertEqual(self.client.get('/pldp/web/portal/api/session/').json()['data']['logged_in'],False)
        self.assertEqual(self.client.get('/pldp/web/print/').status_code,403)
    def test_login_expiry(self):
        self.signin();s=self.client.session;g=s[GRANT];g['expires_at']=time.time()-1;s[GRANT]=g;s.save()
        self.assertFalse(self.client.get('/pldp/web/portal/api/session/').json()['data']['logged_in'])
    def test_logout(self):
        self.signin();self.assertEqual(self.post('logout').status_code,200)
        self.assertNotIn(GRANT,self.client.session)

    def test_device_registry_logout_ends_the_unified_browser_session(self):
        self.signin(self.users['factory_production'])
        page_url = '/pldp/web/device-codes/'
        logout_url = page_url + 'api/logout/'
        page = self.client.get(page_url)
        self.assertContains(page, 'id="logoutButton"')
        self.assertContains(page, logout_url)
        self.assertEqual(self.client.get(page_url + 'api/batches/').status_code, 200)
        session_cookie = self.client.cookies[settings.SESSION_COOKIE_NAME].value

        self.assertEqual(self.client.post(logout_url).status_code, 403)
        response = self.client.post(logout_url, HTTP_X_CSRFTOKEN=self.csrf(self.client))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'code': 0, 'data': {}})

        replay = Client()
        replay.cookies[settings.SESSION_COOKIE_NAME] = session_cookie
        for browser in (self.client, replay):
            self.assertNotIn(GRANT, browser.session)
            info = browser.get('/pldp/web/portal/api/session/').json()['data']
            self.assertFalse(info['logged_in'])
            self.assertRedirects(browser.get(page_url), '/?login=1', fetch_redirect_response=False)
            self.assertEqual(browser.get(page_url + 'api/batches/').status_code, 403)
    def test_eligible_tourist_is_offered_trial_without_authorizing_browser(self):
        challenge = self.begin()
        response = self.approve(challenge, self.tourist)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["status"], "trial_required")
        challenge.refresh_from_db()
        self.assertIsNone(challenge.approved_at)
        self.assertIsNone(challenge.consumed_at)
        self.assertEqual(self.post("status").json()["data"]["status"], "pending")
        self.assertNotIn(GRANT, self.client.session)
    def test_superuser_without_wechat_denied(self):
        self.admin.wx_openid='';self.admin.save()
        with self.assertRaises(Exception):principal_for(self.admin)
    def test_owner_pages(self):
        self.signin()
        for m in ROLE_MODULES['factory_admin']:
            r=self.client.get(MODULES[m][1]);self.assertEqual(r.status_code,200,(m,r.content[:300]))
        self.assertEqual(self.client.get('/pldp/admin/').status_code,403)
        self.assertEqual(self.client.get('/pldp/web/account/users/').status_code,403)
    def test_admin_pages_and_edit(self):
        self.signin(self.admin)
        for path in ('/pldp/admin/','/pldp/web/account/users/',f'/pldp/web/account/users/{self.tourist.pk}/edit/'):
            self.assertEqual(self.client.get(path).status_code,200,path)
        self.assertEqual(self.client.get('/pldp/web/product/').status_code,403)
    def test_production_cannot_open_other_modules(self):
        self.signin(self.users['factory_production'])
        self.assertEqual(self.client.get('/pldp/web/product/').status_code,200)
        self.assertEqual(self.client.get('/pldp/web/quality-trace/').status_code,403)
        self.assertEqual(self.client.get('/pldp/web/print/').status_code,403)
    def test_browser_product_write_requires_csrf(self):
        self.signin(self.users['factory_production'])
        self.assertEqual(self.client.post('/pldp/api/product/category/add/',data={'name':'Blocked'}).status_code,403)
    def test_wrong_role_write_and_legacy_paths(self):
        self.signin(self.users['factory_stock'])
        self.assertEqual(self.post_product().status_code,403)
        self.assertEqual(self.client.get('/api/product/categories/').status_code,404)
    def post_product(self):
        return self.client.post('/pldp/api/product/category/add/',data={'name':'Blocked'},HTTP_X_CSRFTOKEN=self.csrf(self.client))
    def test_mini_product_reads_preserved(self):
        r=Client().get('/pldp/api/product/categories/',HTTP_AUTHORIZATION='Bearer '+issue_access_token(self.tourist))
        self.assertEqual(r.status_code,200)

    def test_web_supplier_binding_and_options_stay_in_signed_in_root(self):
        other_factory = Organization.objects.create(
            code="PORTAL_OTHER_ROOT", name="另一厂家", organization_type=self.org.organization_type,
        )
        supplier_type = OrganizationType.objects.get(code="SUPPLIER")
        suppliers = [
            Organization.objects.create(code=code, name=code, organization_type=supplier_type)
            for code in ("PORTAL_LOCAL_SUPPLIER", "PORTAL_FOREIGN_SUPPLIER")
        ]
        for factory, supplier in zip((self.org, other_factory), suppliers):
            OrganizationRelation.objects.create(
                source_organization=factory, target_organization=supplier, relation_type="SUPPLY",
            )
        local, foreign = suppliers
        category = ProductCategory.objects.create(code="PORTAL_SUPPLY", name="配套测试品类")
        material = Material.objects.create(code="PORTAL_SUPPLY_PART", name="配套物料")
        material.product_categories.add(category)
        spec = MaterialSpec.objects.create(material=material, code="PORTAL_SPEC", name="测试规格")

        self.signin(self.users["factory_admin"])
        url = "/pldp/web/supply-chain/api/"
        self.assertEqual([row["id"] for row in self.client.get(url + "suppliers/").json()], [local.pk])
        headers = {"HTTP_X_CSRFTOKEN": self.csrf(self.client)}
        add_url = url + "material-spec-supplier/add/"
        for supplier, expected in ((local, 200), (foreign, 403)):
            response = self.client.post(
                add_url, {"material_spec_id": spec.pk, "supplier_id": supplier.pk},
                content_type="application/json", **headers,
            )
            self.assertEqual(response.status_code, expected, response.content)
            self.assertEqual(response.json()["success"], supplier == local)
        self.assertFalse(MaterialSpecSupplier.objects.filter(material_spec=spec, supplier=foreign).exists())

        foreign_link = MaterialSpecSupplier.objects.create(material_spec=spec, supplier=foreign)
        options = self.client.get(f"/pldp/api/product/category/{category.pk}/bom-options/").json()
        self.assertEqual([row["id"] for row in options["materials"][0]["specs"][0]["suppliers"]], [local.pk])
        removed = self.client.post(
            url + "material-spec-supplier/delete/", {"id": foreign_link.pk},
            content_type="application/json", **headers,
        )
        self.assertFalse(removed.json()["success"])
        self.assertTrue(MaterialSpecSupplier.objects.filter(pk=foreign_link.pk).exists())
    def test_owner_can_generate_codes_web(self):
        from lifecycle.services.physical_device_code_service import generate_device_code_batch
        self.assertEqual(generate_device_code_batch(user=self.users['factory_admin'],quantity=1,web_authorized=True)['count'],1)
    def test_supplier_cannot_generate_codes_web(self):
        from lifecycle.services.physical_device_code_service import generate_device_code_batch,PhysicalDeviceCodePermissionError
        with self.assertRaises(PhysicalDeviceCodePermissionError):generate_device_code_batch(user=self.tourist,quantity=1,web_authorized=True)

    def test_all_workbenches_only_offer_allowed_entries(self):
        for role in ROLE_MODULES:
            with self.subTest(role=role):
                c=Client(enforce_csrf_checks=True)
                self.signin(self.users[role],c=c)
                page=c.get('/pldp/web/portal/')
                self.assertEqual(page.status_code,200)
                for module,(_,url) in MODULES.items():
                    if module in ROLE_MODULES[role]:self.assertContains(page,'href="'+url+'"')
                    else:self.assertNotContains(page,'href="'+url+'"')
    def test_edit_cannot_create_business_role(self):
        self.signin(self.admin)
        u=self.tourist
        r=self.client.post(f'/pldp/web/account/users/{u.pk}/edit/',data={'phone':'123','first_name':'Test','last_name':'','is_active':'on','role':'factory_admin','organization':self.org.pk},HTTP_X_CSRFTOKEN=self.csrf(self.client))
        self.assertEqual(r.status_code,302)
        self.assertFalse(UserOrganization.objects.filter(user=u).exists())
        u.refresh_from_db();self.assertFalse(u.is_superuser)
    def test_factory_print_and_issuance_are_scoped(self):
        from django.test import RequestFactory
        from django.contrib.auth.models import AnonymousUser
        from print.views import _gate as print_gate
        from lifecycle.web.device_code_registry.views import _gate as code_gate
        self.signin()
        request=RequestFactory().get('/');request.session=self.client.session;request.user=self.users['factory_admin']
        self.assertEqual(print_gate(request)[1].organization_id,self.org.pk)
        self.assertEqual(code_gate(request)[1].organization_id,self.org.pk)
    def test_anonymous_management_apis_denied(self):
        for path in ('/pldp/api/product/categories/','/pldp/web/supply-chain/api/materials/','/pldp/web/quality-trace/rules/'):
            self.assertEqual(self.client.get(path).status_code,403,path)
