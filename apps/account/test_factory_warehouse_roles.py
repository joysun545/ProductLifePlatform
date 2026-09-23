"""Upgrade continuity, invitation flow and finished-goods permission boundaries."""
import importlib
import json
from types import SimpleNamespace

from django.apps import apps
from django.db import connection
from django.test import TestCase

from account.models import (
    Organization, OrganizationRole, OrganizationType, RoleInvitationRule,
    User, UserOrganization,
)
from account.services.auth_service import get_active_identity, issue_access_token
from workflow.models import UserTask, WorkItem


MIGRATION = importlib.import_module("account.migrations.0006_factory_warehouse_roles")


class FactoryWarehouseRoleTests(TestCase):
    def setUp(self):
        self.factory = Organization.objects.create(
            name="库管岗位测试厂家", code="WAREHOUSE-ROLE-FACTORY",
            organization_type=OrganizationType.objects.get(code="OWNER"),
        )
        self.stock_role = OrganizationRole.objects.get(code="factory_stock")
        self.material_role = OrganizationRole.objects.get(code="factory_material_stock")
        self.admin = self.make_user("factory_admin", "admin")
        self.stock = self.make_user("factory_stock", "stock")

    def make_user(self, role_code, suffix):
        user = User.objects.create_user(
            username="warehouse-" + suffix, wx_openid="warehouse-openid-" + suffix,
        )
        UserOrganization.objects.create(
            user=user, organization=self.factory,
            role=OrganizationRole.objects.get(code=role_code),
        )
        return user

    def auth(self, user):
        return {"HTTP_AUTHORIZATION": "Bearer " + issue_access_token(user)}

    def apply_migration(self, reverse=False):
        operation = MIGRATION.reverse_warehouse_roles if reverse else MIGRATION.add_warehouse_roles
        operation(apps, SimpleNamespace(connection=connection))

    def test_upgrade_preserves_existing_identity_wechat_and_task_references(self):
        RoleInvitationRule.objects.filter(target_role=self.material_role).delete()
        self.material_role.delete()
        self.stock_role.name = "厂家库管"
        self.stock_role.save(update_fields=("name",))
        for state in ("NEW", "DONE"):
            item = WorkItem.objects.create(
                domain="SALES", type="ORDER_PLAN_STOCK_OUTBOUND",
                biz_id="warehouse-" + state, uniq_key="warehouse-" + state,
                status="OPEN" if state == "NEW" else "DONE",
                assignee_openid=self.stock.wx_openid,
                done_by_openid=self.stock.wx_openid if state == "DONE" else "",
            )
            UserTask.objects.create(
                workitem=item, to_openid=self.stock.wx_openid,
                to_role_snapshot="factory_stock", state=state, need_action=True,
            )
        protected = (User, UserOrganization, UserTask, WorkItem)
        snapshots = [list(model.objects.order_by("pk").values()) for model in protected]
        old_role_id = self.stock_role.pk
        old_rules = list(RoleInvitationRule.objects.order_by("pk").values())
        self.apply_migration()
        self.apply_migration()
        self.stock_role.refresh_from_db()
        self.assertEqual(self.stock_role.pk, old_role_id)
        self.assertEqual(self.stock_role.code, "factory_stock")
        self.assertEqual(self.stock_role.name, "厂家成品库管")
        for model, snapshot in zip(protected, snapshots):
            self.assertEqual(list(model.objects.order_by("pk").values()), snapshot)
        self.assertEqual(list(RoleInvitationRule.objects.filter(
            pk__in=[row["id"] for row in old_rules],
        ).order_by("pk").values()), old_rules)
        self.assertEqual(OrganizationRole.objects.filter(code="factory_material_stock").count(), 1)
        self.assertEqual(RoleInvitationRule.objects.filter(target_role__code="factory_material_stock").count(), 1)
        self.assertFalse(UserOrganization.objects.filter(role__code="factory_material_stock").exists())
        profile = self.client.get("/pldp/api/account/profile/", **self.auth(self.stock)).json()["data"]
        self.assertEqual(profile["role"], "factory_stock")
        self.assertEqual(profile["role_name"], "厂家成品库管")

    def test_rename_does_not_reactivate_a_disabled_finished_goods_role(self):
        self.stock_role.is_active = False
        self.stock_role.save(update_fields=("is_active",))
        self.apply_migration()
        self.stock_role.refresh_from_db()
        self.assertFalse(self.stock_role.is_active)

    def test_factory_owner_invites_material_keeper_into_the_existing_factory(self):
        response = self.client.get("/pldp/api/account/invitation_roles/", **self.auth(self.admin))
        self.assertEqual(response.json()["code"], 0, response.content)
        options = {row["role"]: row for row in response.json()["data"]["items"]}
        self.assertEqual(options["factory_stock"]["name"], "厂家成品库管")
        option = options["factory_material_stock"]
        self.assertEqual(option["name"], "厂家物料库管")
        self.assertEqual(option["scope"], "same_organization")
        created = self.client.post(
            "/pldp/api/account/invitations/create/",
            data=json.dumps({"rule_id": option["rule_id"]}),
            content_type="application/json", **self.auth(self.admin),
        ).json()
        self.assertEqual(created["code"], 0, created)
        visitor = User.objects.create_user(username="material-visitor", wx_openid="material-visitor")
        org_count = Organization.objects.count()
        accepted = self.client.post(
            "/pldp/api/account/invitations/accept/",
            data=json.dumps({"token": created["data"]["token"]}),
            content_type="application/json", **self.auth(visitor),
        ).json()
        self.assertEqual(accepted["code"], 0, accepted)
        self.assertEqual(accepted["data"]["role"], "factory_material_stock")
        self.assertEqual(accepted["data"]["role_name"], "厂家物料库管")
        self.assertEqual(accepted["data"]["organization_id"], self.factory.pk)
        self.assertEqual(Organization.objects.count(), org_count)
        self.assertFalse(get_active_identity(visitor).role.is_owner_role)
        invitations = self.client.get("/pldp/api/account/invitation_roles/", **self.auth(visitor)).json()
        self.assertEqual(invitations["data"]["items"], [])

    def test_material_keeper_is_a_distinct_role_from_finished_goods_keeper(self):
        material = self.make_user("factory_material_stock", "material")
        identity = get_active_identity(material)
        self.assertEqual(identity.role.code, "factory_material_stock")
        self.assertNotEqual(identity.role_id, self.stock_role.pk)

    def test_reverse_and_reapply_keep_an_existing_material_membership(self):
        material = self.make_user("factory_material_stock", "material")
        identity_before = UserOrganization.objects.filter(user=material).values().get()
        material_id = self.material_role.pk
        self.apply_migration(reverse=True)
        self.assertIsNone(get_active_identity(material))
        self.assertEqual(UserOrganization.objects.filter(user=material).values().get(), identity_before)
        self.stock_role.refresh_from_db()
        self.assertEqual(self.stock_role.name, "厂家库管")
        self.apply_migration()
        identity = get_active_identity(material)
        self.assertEqual(identity.role_id, material_id)
        self.assertEqual(UserOrganization.objects.filter(user=material).values().get(), identity_before)
