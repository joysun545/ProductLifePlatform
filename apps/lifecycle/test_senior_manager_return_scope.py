from types import SimpleNamespace

from django.test import TestCase

from account.models import (
    Organization,
    OrganizationRole,
    OrganizationType,
    SeniorManagerProfile,
    User,
    UserOrganization,
)
from lifecycle.services.factory_return_service import (
    PICKUP_WORK_TYPE,
    _role_task_access,
)
from workflow.models import UserTask, WorkItem


class SeniorManagerFactoryReturnTaskScopeTests(TestCase):
    def test_manager_can_act_only_on_the_return_task_copied_to_them(self):
        owner_type = OrganizationType.objects.get(code="OWNER")
        factory = Organization.objects.create(
            name="高级经理退货权限测试厂家",
            code="SENIOR-MANAGER-RETURN-SCOPE-FACTORY",
            organization_type=owner_type,
        )
        manager_role = OrganizationRole.objects.get(
            organization_type=owner_type,
            code="factory_senior_manager",
        )
        logistics_role = OrganizationRole.objects.get(
            organization_type=owner_type,
            code="factory_logistics",
        )
        manager = User.objects.create_user(
            username="return-scope-manager",
            password="unused",
            wx_openid="return-scope-manager-openid",
        )
        identity = UserOrganization.objects.create(
            user=manager,
            organization=factory,
            role=manager_role,
        )
        profile = SeniorManagerProfile.objects.create(identity=identity)
        profile.managed_roles.add(logistics_role)

        transfer = SimpleNamespace(pk=481)
        self.assertFalse(
            _role_task_access(
                identity,
                transfer,
                "factory_logistics",
                (PICKUP_WORK_TYPE,),
                require_action=True,
            )
        )

        workitem = WorkItem.objects.create(
            domain="LOGISTICS",
            type=PICKUP_WORK_TYPE,
            biz_id="RETURN-481",
            biz_ref="lifecycle.GoodsTransfer:481",
            title="退货待取回",
            action_mode="ANYONE_DO",
            status="OPEN",
            uniq_key="senior-manager-return-scope-481",
        )
        UserTask.objects.create(
            workitem=workitem,
            to_openid=manager.wx_openid,
            need_action=True,
            state="NEW",
        )

        self.assertTrue(
            _role_task_access(
                identity,
                transfer,
                "factory_logistics",
                (PICKUP_WORK_TYPE,),
                require_action=True,
            )
        )
        self.assertFalse(
            _role_task_access(
                identity,
                SimpleNamespace(pk=482),
                "factory_logistics",
                (PICKUP_WORK_TYPE,),
                require_action=True,
            )
        )
