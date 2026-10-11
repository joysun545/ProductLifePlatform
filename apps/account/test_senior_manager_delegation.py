from django.test import TestCase

from account.models import (
    InvitationScope,
    Organization,
    OrganizationRelationType,
    OrganizationRole,
    OrganizationType,
    RoleInvitationRule,
    SeniorManagerProfile,
    User,
    UserOrganization,
)
from account.services.invite_service import (
    InvitationError,
    accept_staff_invitation,
    create_staff_invitation,
    get_available_invitation_roles,
)
from account.services.role_delegation_service import (
    effective_role_codes,
    has_open_action_task_copy,
    identity_can_perform_shared_operation,
    shared_operation_recipient_role_codes,
)
from workflow.models import UserTask, WorkItem


class SeniorManagerDelegationInvitationTests(TestCase):
    def setUp(self):
        self.owner_type = OrganizationType.objects.get(code="OWNER")
        self.owner_role = OrganizationRole.objects.get(
            organization_type=self.owner_type,
            code="factory_admin",
        )
        self.manager_role = OrganizationRole.objects.get(
            organization_type=self.owner_type,
            code="factory_senior_manager",
        )
        self.sales_role = OrganizationRole.objects.get(
            organization_type=self.owner_type,
            code="factory_sales",
        )
        self.stock_role = OrganizationRole.objects.get(
            organization_type=self.owner_type,
            code="factory_stock",
        )
        merchant_type = OrganizationType.objects.get(code="MERCHANT")
        merchant_owner_role = OrganizationRole.objects.get(
            organization_type=merchant_type,
            code="merchant_owner",
        )
        self.factory = Organization.objects.create(
            name="高级经理授权测试厂家",
            code="SENIOR-MANAGER-DELEGATION-FACTORY",
            organization_type=self.owner_type,
        )
        self.owner = self._user("senior-owner")
        UserOrganization.objects.create(
            user=self.owner,
            organization=self.factory,
            role=self.owner_role,
        )
        self.manager_rule = RoleInvitationRule.objects.get(
            inviter_role=self.owner_role,
            target_role=self.manager_role,
            scope=InvitationScope.SAME_ORGANIZATION,
        )
        self.sales_rule = RoleInvitationRule.objects.get(
            inviter_role=self.manager_role,
            target_role=self.sales_role,
            scope=InvitationScope.SAME_ORGANIZATION,
        )
        self.stock_rule = RoleInvitationRule.objects.get(
            inviter_role=self.manager_role,
            target_role=self.stock_role,
            scope=InvitationScope.SAME_ORGANIZATION,
        )
        self.cross_organization_rule, _ = RoleInvitationRule.objects.get_or_create(
            inviter_role=self.manager_role,
            target_role=merchant_owner_role,
            scope=InvitationScope.CROSS_ORGANIZATION,
            relation_type=OrganizationRelationType.DISTRIBUTION,
            defaults={"is_active": True},
        )

    def _user(self, suffix):
        return User.objects.create_user(
            username=f"senior-test-{suffix}",
            password="unused",
            wx_openid=f"senior-test-openid-{suffix}",
        )

    def test_owner_configures_manager_and_manager_invites_only_selected_role(self):
        owner_roles = get_available_invitation_roles(self.owner)
        manager_option = next(
            row for row in owner_roles if row["role"] == "factory_senior_manager"
        )
        offered_codes = {
            row["code"]
            for row in manager_option["senior_manager_setup"]["managed_role_options"]
        }
        self.assertIn("factory_sales", offered_codes)

        manager_invitation = create_staff_invitation(
            inviter=self.owner,
            rule_id=self.manager_rule.pk,
            senior_manager_job_title="销售分管",
            senior_manager_managed_role_codes=["factory_sales"],
        )
        manager_user = self._user("manager")
        accept_staff_invitation(
            invitee=manager_user,
            token=manager_invitation["token"],
        )
        manager_identity = UserOrganization.objects.get(user=manager_user)
        profile = SeniorManagerProfile.objects.get(identity=manager_identity)

        self.assertEqual(profile.job_title, "销售分管")
        self.assertEqual(set(profile.managed_roles.values_list("code", flat=True)), {"factory_sales"})
        self.assertEqual(effective_role_codes(manager_identity), {"factory_sales"})
        manager_roles = get_available_invitation_roles(manager_user)
        self.assertEqual({row["role"] for row in manager_roles}, {"factory_sales"})

        with self.assertRaises(InvitationError):
            create_staff_invitation(
                inviter=manager_user,
                rule_id=self.stock_rule.pk,
            )
        with self.assertRaises(InvitationError):
            create_staff_invitation(
                inviter=manager_user,
                rule_id=self.cross_organization_rule.pk,
            )

        sales_invitation = create_staff_invitation(
            inviter=manager_user,
            rule_id=self.sales_rule.pk,
        )
        sales_user = self._user("sales")
        accept_staff_invitation(
            invitee=sales_user,
            token=sales_invitation["token"],
        )
        sales_identity = UserOrganization.objects.get(user=sales_user)
        self.assertEqual(sales_identity.inviter_id, manager_user.pk)
        self.assertEqual(sales_identity.role.code, "factory_sales")

    def test_manager_must_have_the_exact_open_action_task_copy(self):
        manager = self._user("task-manager")
        identity = UserOrganization.objects.create(
            user=manager,
            organization=self.factory,
            role=self.manager_role,
            inviter=self.owner,
        )
        profile = SeniorManagerProfile.objects.create(identity=identity)
        profile.managed_roles.add(self.sales_role)
        key = "quality-recall:55:post-recall-disposition"
        workitem = WorkItem.objects.create(
            domain="SYSTEM",
            type="QUALITY_RECALL_POST_RECALL_DISPOSITION",
            biz_id="55",
            uniq_key=key,
            title="召回后处置待决策",
            status="OPEN",
            action_mode="ANYONE_DO",
        )
        task = UserTask.objects.create(
            workitem=workitem,
            to_openid=manager.wx_openid,
            need_action=True,
            state="NEW",
        )

        self.assertTrue(has_open_action_task_copy(identity, (key,)))
        self.assertFalse(has_open_action_task_copy(identity, ("other-task",)))

        task.state = "DONE"
        task.save(update_fields=("state",))
        self.assertFalse(has_open_action_task_copy(identity, (key,)))


class MerchantSeniorManagerOperationPermissionTests(TestCase):
    def setUp(self):
        self.merchant_type = OrganizationType.objects.get(code="MERCHANT")
        self.roles = {
            code: OrganizationRole.objects.get(
                organization_type=self.merchant_type,
                code=code,
            )
            for code in (
                "merchant_owner",
                "merchant_senior_manager",
                "merchant_sales",
                "merchant_stock",
                "merchant_electrician",
            )
        }
        self.organization = Organization.objects.create(
            name="商家高级经理操作权限测试",
            code="MERCHANT-SENIOR-OPERATION-PERMISSIONS",
            organization_type=self.merchant_type,
        )
        self.owner_identity = self._identity("owner", "merchant_owner")
        self.manager_identity = self._identity(
            "manager", "merchant_senior_manager", inviter=self.owner_identity.user
        )
        self.profile = SeniorManagerProfile.objects.create(
            identity=self.manager_identity
        )

    def _identity(self, suffix, role_code, inviter=None):
        user = User.objects.create_user(
            username=f"merchant-senior-op-{suffix}",
            password="unused",
            wx_openid=f"merchant-senior-op-openid-{suffix}",
        )
        return UserOrganization.objects.create(
            user=user,
            organization=self.organization,
            role=self.roles[role_code],
            inviter=inviter,
        )

    def test_sales_keeps_only_transfer_and_stock_manager_takes_over_goods_flow(self):
        sales_identity = self._identity("sales", "merchant_sales")
        stock_identity = self._identity("stock", "merchant_stock")
        electrician_identity = self._identity("electrician", "merchant_electrician")

        self.assertTrue(identity_can_perform_shared_operation(
            sales_identity, "goods_transfer_routing"
        ))
        for operation in (
            "goods_transfer_coordination",
            "goods_transfer_receipt",
            "install_device",
        ):
            self.assertFalse(identity_can_perform_shared_operation(
                sales_identity, operation
            ))

        self.assertTrue(identity_can_perform_shared_operation(
            self.owner_identity, "goods_transfer_coordination"
        ))
        self.assertTrue(identity_can_perform_shared_operation(
            self.owner_identity, "goods_transfer_receipt"
        ))
        self.assertTrue(identity_can_perform_shared_operation(
            self.owner_identity, "install_device"
        ))
        self.assertTrue(identity_can_perform_shared_operation(
            self.owner_identity, "goods_transfer_routing"
        ))
        self.assertTrue(identity_can_perform_shared_operation(
            stock_identity, "goods_transfer_coordination"
        ))
        self.assertTrue(identity_can_perform_shared_operation(
            stock_identity, "goods_transfer_receipt"
        ))
        self.assertFalse(identity_can_perform_shared_operation(
            stock_identity, "goods_transfer_routing"
        ))
        self.assertTrue(identity_can_perform_shared_operation(
            electrician_identity, "install_device"
        ))

        self.profile.managed_roles.add(self.roles["merchant_stock"])

        self.assertEqual(
            shared_operation_recipient_role_codes(
                self.organization.pk, "goods_transfer_receipt"
            ),
            {"merchant_stock"},
        )

        self.assertTrue(
            identity_can_perform_shared_operation(
                self.manager_identity,
                "goods_transfer_coordination",
            )
        )
        self.assertFalse(
            identity_can_perform_shared_operation(
                self.owner_identity,
                "goods_transfer_receipt",
            )
        )
        self.assertFalse(
            identity_can_perform_shared_operation(
                self.owner_identity,
                "goods_transfer_coordination",
            )
        )
        self.assertEqual(
            shared_operation_recipient_role_codes(
                self.organization.pk, "goods_transfer_receipt"
            ),
            {"merchant_stock"},
        )
        self.assertTrue(
            identity_can_perform_shared_operation(
                self.owner_identity,
                "install_device",
            )
        )
        # Stock delegation does not grant the separate sales-only routing action.
        self.assertFalse(
            identity_can_perform_shared_operation(
                self.manager_identity,
                "goods_transfer_routing",
            )
        )

    def test_electrician_delegation_alone_takes_over_owner_installation(self):
        self.profile.managed_roles.add(self.roles["merchant_sales"])
        electrician_manager = self._identity(
            "electrician_manager",
            "merchant_senior_manager",
            inviter=self.owner_identity.user,
        )
        electrician_profile = SeniorManagerProfile.objects.create(
            identity=electrician_manager
        )
        # A sales grant does not provide installation, and an unconfigured
        # electrician manager does not take the owner's authority.
        self.assertFalse(
            identity_can_perform_shared_operation(
                electrician_manager,
                "install_device",
            )
        )
        self.assertTrue(
            identity_can_perform_shared_operation(
                self.owner_identity,
                "install_device",
            )
        )

        self.assertFalse(
            identity_can_perform_shared_operation(
                self.manager_identity,
                "install_device",
            )
        )
        self.assertTrue(
            identity_can_perform_shared_operation(
                self.owner_identity,
                "install_device",
            )
        )

        electrician_profile.managed_roles.add(self.roles["merchant_electrician"])
        self.assertTrue(identity_can_perform_shared_operation(
            electrician_manager, "install_device"
        ))
        self.assertFalse(identity_can_perform_shared_operation(
            self.owner_identity, "install_device"
        ))
