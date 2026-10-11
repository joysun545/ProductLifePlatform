from django.test import TestCase

from account.models import (
    Organization,
    OrganizationRole,
    OrganizationType,
    SeniorManagerProfile,
    User,
    UserOrganization,
)
from workflow.services.workflow_service import _add_delegated_senior_manager_recipients


class SeniorManagerTaskCopyTests(TestCase):
    def test_only_direct_inviter_gets_role_task_copy(self):
        owner_type = OrganizationType.objects.get(code="OWNER")
        factory = Organization.objects.create(
            name="高级经理任务协同测试厂家",
            code="SENIOR-MANAGER-TASK-COPY-FACTORY",
            organization_type=owner_type,
        )
        manager_role = OrganizationRole.objects.get(
            organization_type=owner_type,
            code="factory_senior_manager",
        )
        sales_role = OrganizationRole.objects.get(
            organization_type=owner_type,
            code="factory_sales",
        )
        owner = User.objects.create_user(
            username="task-copy-owner",
            password="unused",
            wx_openid="task-copy-owner-openid",
        )
        manager = User.objects.create_user(
            username="task-copy-manager",
            password="unused",
            wx_openid="task-copy-manager-openid",
        )
        peer_manager = User.objects.create_user(
            username="task-copy-peer-manager",
            password="unused",
            wx_openid="task-copy-peer-manager-openid",
        )
        sales_user = User.objects.create_user(
            username="task-copy-sales",
            password="unused",
            wx_openid="task-copy-sales-openid",
        )
        manager_identity = UserOrganization.objects.create(
            user=manager,
            organization=factory,
            role=manager_role,
            inviter=owner,
        )
        peer_identity = UserOrganization.objects.create(
            user=peer_manager,
            organization=factory,
            role=manager_role,
            inviter=owner,
        )
        UserOrganization.objects.create(
            user=sales_user,
            organization=factory,
            role=sales_role,
            inviter=manager,
        )
        for identity in (manager_identity, peer_identity):
            profile = SeniorManagerProfile.objects.create(identity=identity)
            profile.managed_roles.add(sales_role)

        recipients = _add_delegated_senior_manager_recipients(
            [sales_user.wx_openid]
        )

        self.assertEqual(
            recipients,
            [sales_user.wx_openid, manager.wx_openid],
        )
