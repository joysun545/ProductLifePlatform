from django.db import transaction
from django.test import TestCase

from account.models import Organization, OrganizationRole, OrganizationType, SeniorManagerProfile, User, UserOrganization
from account.services.role_delegation_service import ManagedRoleConflict, vacant_role_manager_openids
from workflow.services.workflow_service import publish_workitem


class SeniorManagerTaskCopyTests(TestCase):
    def setUp(self):
        kind = OrganizationType.objects.get(code="OWNER")
        self.factory = Organization.objects.create(name="测试厂家", code="ROLE-DELIVERY", organization_type=kind)
        self.sales_role = OrganizationRole.objects.get(organization_type=kind, code="factory_sales")
        self.manager_role = OrganizationRole.objects.get(organization_type=kind, code="factory_senior_manager")
        self.owner = User.objects.create_user(username="delivery-owner")

    def member(self, name, role, inviter=None):
        user = User.objects.create_user(username=name, wx_openid=name)
        return UserOrganization.objects.create(user=user, organization=self.factory, role=role, inviter=inviter)

    def publish(self, openids):
        return publish_workitem(
            domain="SALES", type="ROLE_DELIVERY_TEST", biz_id="1", uniq_key="role-delivery:test",
            title="待办理", recipients_openids=openids, need_action=True,
            action_mode="ANYONE_DO", mark_workitem_done_immediately=False,
        )

    def test_staffed_role_uses_allocation_and_turns_into_fallback_on_departure(self):
        employee = self.member("delivery-sales", self.sales_role, inviter=self.owner)
        workitem = self.publish([employee.user.wx_openid])
        manager = self.member("delivery-manager", self.manager_role)
        profile = SeniorManagerProfile.objects.create(identity=manager)
        profile.managed_roles.add(self.sales_role)
        copy = workitem.user_tasks.get(to_openid=manager.user.wx_openid)
        self.assertTrue(copy.need_action)

        employee.is_active = False
        employee.save(update_fields=["is_active"])
        copy.refresh_from_db()
        self.assertTrue(copy.need_action)
        self.assertEqual(workitem.user_tasks.get(to_openid=employee.user.wx_openid).state, "DONE")

        self.member("delivery-sales-new", self.sales_role, inviter=self.owner)
        copy.refresh_from_db()
        self.assertTrue(copy.need_action)

        profile.managed_roles.clear()
        copy.refresh_from_db()
        self.assertEqual(copy.state, "DONE")

    def test_order_role_task_follows_replacement_without_manager(self):
        role = OrganizationRole.objects.get(organization_type=self.factory.organization_type,
                                            code="factory_material_stock")
        original = self.member("review-original", role)
        work = publish_workitem(
            domain="SALES", type="ORDER_PLAN_MATERIAL_REVIEW", biz_id="1",
            uniq_key="role-delivery:material-review", title="核对物料",
            recipients_openids=[original.user.wx_openid], need_action=True,
            action_mode="ANYONE_DO", mark_workitem_done_immediately=False,
        )
        original.role = self.sales_role
        original.save(update_fields=["role"])
        replacement = self.member("review-replacement", role)
        self.assertEqual(work.user_tasks.get(to_openid=original.user.wx_openid).done_reason, "ROLE_REASSIGNED")
        self.assertTrue(work.user_tasks.get(to_openid=replacement.user.wx_openid, state="NEW").need_action)

    def test_vacant_role_fallback_tracks_allocation_and_new_holder(self):
        manager = self.member("delivery-manager", self.manager_role)
        profile = SeniorManagerProfile.objects.create(identity=manager)
        profile.managed_roles.add(self.sales_role)
        workitem = self.publish(vacant_role_manager_openids(self.factory, ["factory_sales"]))
        self.assertTrue(workitem.user_tasks.get(to_openid=manager.user.wx_openid).need_action)
        employee = self.member("delivery-new-holder", self.sales_role, inviter=manager.user)
        self.assertTrue(workitem.user_tasks.get(to_openid=employee.user.wx_openid).need_action)
        self.assertTrue(workitem.user_tasks.get(to_openid=manager.user.wx_openid).need_action)

    def test_republishing_completed_workitem_reopens_manager_action(self):
        manager = self.member("delivery-reopen-manager", self.manager_role)
        SeniorManagerProfile.objects.create(identity=manager).managed_roles.add(self.sales_role)
        workitem = self.publish(vacant_role_manager_openids(self.factory, ["factory_sales"]))
        copy = workitem.user_tasks.get(to_openid=manager.user.wx_openid)
        copy.state = "DONE"
        copy.done_reason = "SELF_ACTION"
        copy.save(update_fields=["state", "done_reason"])
        workitem.status = "DONE"
        workitem.save(update_fields=["status"])

        self.publish(vacant_role_manager_openids(self.factory, ["factory_sales"]))

        copy.refresh_from_db()
        self.assertEqual(copy.state, "NEW")
        self.assertTrue(copy.need_action)
        self.assertEqual(copy.done_reason, "")

    def test_second_manager_cannot_manage_same_organization_role(self):
        manager = self.member("delivery-manager", self.manager_role)
        profile = SeniorManagerProfile.objects.create(identity=manager)
        profile.managed_roles.add(self.sales_role)
        other = self.member("delivery-peer", self.manager_role)
        other_profile = SeniorManagerProfile.objects.create(identity=other)
        with self.assertRaises(ManagedRoleConflict), transaction.atomic():
            other_profile.managed_roles.add(self.sales_role)
        self.assertFalse(other_profile.managed_roles.exists())

    def test_departed_manager_can_be_replaced_but_cannot_restore_conflicting_allocation(self):
        old = self.member("removed-manager", self.manager_role)
        SeniorManagerProfile.objects.create(identity=old).managed_roles.add(self.sales_role)
        workitem = self.publish(vacant_role_manager_openids(self.factory, ["factory_sales"]))

        old.is_active = False
        old.save(update_fields=["is_active"])
        replacement = self.member("replacement-manager", self.manager_role)
        SeniorManagerProfile.objects.create(identity=replacement).managed_roles.add(self.sales_role)

        self.assertEqual(workitem.user_tasks.get(to_openid=old.user.wx_openid).state, "DONE")
        self.assertTrue(workitem.user_tasks.get(to_openid=replacement.user.wx_openid).need_action)
        old.is_active = True
        with self.assertRaises(ManagedRoleConflict):
            old.save(update_fields=["is_active"])

    def test_retired_member_no_longer_receives_new_tasks(self):
        retired_role = OrganizationRole.objects.get(code="factory_purchase")
        retired = self.member("retired-purchase", retired_role)
        sales = self.member("current-sales", self.sales_role)
        workitem = self.publish([retired.user.wx_openid, sales.user.wx_openid])
        self.assertEqual(list(workitem.user_tasks.values_list("to_openid", flat=True)), [sales.user.wx_openid])
        old_task = workitem.user_tasks.create(
            to_openid=retired.user.wx_openid, to_role_snapshot=retired_role.code, need_action=True,
        )
        self.publish([retired.user.wx_openid, sales.user.wx_openid])
        old_task.refresh_from_db()
        self.assertTrue(old_task.need_action)
