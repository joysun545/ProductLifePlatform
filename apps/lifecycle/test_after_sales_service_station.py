from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from account.models import (
    Organization,
    OrganizationRelation,
    OrganizationRole,
    OrganizationType,
    SeniorManagerProfile,
    User,
    UserOrganization,
)
from lifecycle.models import (
    AfterSalesServiceStationApplication,
    AfterSalesServiceStationLiaison,
)
from workflow.models import UserTask, WorkItem
from lifecycle.services.after_sales_station_service import (
    AfterSalesServiceStationError,
    _publish_station_result_notice,
    get_service_station_application_context,
    get_service_station_organization_badge,
    get_service_station_review_context,
    review_service_station_application,
    submit_service_station_application,
    update_approved_service_station_receiving_address,
)


class AfterSalesServiceStationApplicationTests(TestCase):
    def setUp(self):
        factory_type, _ = OrganizationType.objects.get_or_create(
            code="OWNER", defaults={"name": "厂家"}
        )
        merchant_type, _ = OrganizationType.objects.get_or_create(
            code="MERCHANT", defaults={"name": "商家"}
        )
        self.factory_root = Organization.objects.create(
            code="FACTORY-STATION-TEST",
            name="测试厂家根组织",
            organization_type=factory_type,
        )
        self.factory_sales_role, _ = OrganizationRole.objects.get_or_create(
            organization_type=factory_type,
            code="factory_sales",
            defaults={"name": "厂家销售经理"},
        )
        self.factory_aftersales_role, _ = OrganizationRole.objects.get_or_create(
            organization_type=factory_type,
            code="factory_aftersales",
            defaults={"name": "厂家售后经理"},
        )
        self.owner_role, _ = OrganizationRole.objects.get_or_create(
            organization_type=merchant_type,
            code="merchant_owner",
            defaults={"name": "商家负责人账号", "is_owner_role": True},
        )
        self.sales_role, _ = OrganizationRole.objects.get_or_create(
            organization_type=merchant_type,
            code="merchant_sales",
            defaults={"name": "商家销售经理"},
        )
        self.electrician_role, _ = OrganizationRole.objects.get_or_create(
            organization_type=merchant_type,
            code="merchant_electrician",
            defaults={"name": "商家电工"},
        )
        self.factory_sales_user = User.objects.create_user(
            username="factory_station_sales",
            wx_openid="factory-station-sales-openid",
            wx_nickname="厂家销售经理甲",
        )
        self.factory_sales_identity = UserOrganization.objects.create(
            user=self.factory_sales_user,
            organization=self.factory_root,
            role=self.factory_sales_role,
        )
        self.factory_aftersales_user = User.objects.create_user(
            username="factory_station_aftersales",
            wx_openid="factory-station-aftersales-openid",
            wx_nickname="厂家售后经理甲",
        )
        self.factory_aftersales_identity = UserOrganization.objects.create(
            user=self.factory_aftersales_user,
            organization=self.factory_root,
            role=self.factory_aftersales_role,
        )
        self.organization = Organization.objects.create(
            code="MERCHANT-STATION-TEST",
            name="测试售后商家",
            organization_type=merchant_type,
            factory_root=self.factory_root,
            provides_after_sales=True,
            address="重庆市测试区标准收件地址",
        )
        self.owner = User.objects.create_user(
            username="station_owner",
            wx_openid="station-owner-openid",
            phone="13900000001",
        )
        self.sales = User.objects.create_user(
            username="station_sales",
            wx_openid="station-sales-openid",
            wx_nickname="测试销售",
            phone="13900000002",
        )
        self.electrician = User.objects.create_user(
            username="station_electrician",
            wx_openid="station-electrician-openid",
            wx_nickname="测试电工",
            phone="13900000003",
        )
        self.owner_identity = UserOrganization.objects.create(
            user=self.owner,
            organization=self.organization,
            role=self.owner_role,
        )
        self.sales_identity = UserOrganization.objects.create(
            user=self.sales,
            organization=self.organization,
            role=self.sales_role,
        )
        self.electrician_identity = UserOrganization.objects.create(
            user=self.electrician,
            organization=self.organization,
            role=self.electrician_role,
        )

    def _create_parent_chain(self, *, delegated_sales_manager=False):
        parent = Organization.objects.create(
            code=f"MERCHANT-STATION-PARENT-{int(delegated_sales_manager)}",
            name="上级测试商家",
            organization_type=self.organization.organization_type,
            factory_root=self.factory_root,
        )
        parent_owner = User.objects.create_user(
            username=f"station_parent_owner_{int(delegated_sales_manager)}",
            wx_openid=f"station-parent-owner-{int(delegated_sales_manager)}",
        )
        UserOrganization.objects.create(
            user=parent_owner,
            organization=parent,
            role=self.owner_role,
        )

        manager_identity = None
        inviter = None
        inviter_user = None
        if delegated_sales_manager:
            manager_role, _ = OrganizationRole.objects.get_or_create(
                organization_type=self.organization.organization_type,
                code="merchant_senior_manager",
                defaults={"name": "商家高级经理"},
            )
            manager_user = User.objects.create_user(
                username="station_parent_senior_manager",
                wx_openid="station-parent-senior-openid",
                wx_nickname="上级分管经理",
            )
            manager_identity = UserOrganization.objects.create(
                user=manager_user,
                organization=parent,
                role=manager_role,
            )
            profile = SeniorManagerProfile.objects.create(identity=manager_identity)
            profile.managed_roles.add(self.sales_role)

        inviter_user = User.objects.create_user(
            username=f"station_parent_sales_{int(delegated_sales_manager)}",
            wx_openid=f"station-parent-sales-{int(delegated_sales_manager)}",
            wx_nickname="上级销售经理",
        )
        inviter = UserOrganization.objects.create(
            user=inviter_user,
            organization=parent,
            role=self.sales_role,
            inviter=manager_identity.user if manager_identity else None,
        )
        self.owner_identity.inviter = inviter_user
        self.owner_identity.save(update_fields=("inviter", "updated_at"))
        OrganizationRelation.objects.create(
            source_organization=parent,
            target_organization=self.organization,
            relation_type="DISTRIBUTION",
        )
        return parent, parent_owner, inviter, manager_identity

    def _payload(self, liaison_ids=None):
        return {
            "service_location_name": "测试维修点",
            "service_location_address": "重庆市测试区测试路1号",
            "service_latitude": 29.56301,
            "service_longitude": 106.55156,
            "location_captured_at": timezone.now().isoformat(),
            "receiving_address": "重庆市测试区测试路1号售后收件处",
            "receiving_contact_name": "测试联系人",
            "receiving_contact_phone": "13800001234",
            "liaison_identity_ids": (
                [self.electrician_identity.pk] if liaison_ids is None else liaison_ids
            ),
            "notice_agreed": True,
        }

    def test_electricians_take_priority_and_receiving_contact_is_generated(self):
        context = get_service_station_application_context(user=self.owner)
        self.assertTrue(context["eligible"])
        self.assertEqual([row["identity_id"] for row in context["liaison_candidates"]], [self.electrician_identity.pk])
        self.assertTrue(context["liaison_candidates"][0]["selected"])
        self.assertEqual(context["liaison_candidates"][0]["phone"], "13900000003")
        self.assertEqual(context["organization"]["address"], "重庆市测试区标准收件地址")

        result = submit_service_station_application(
            user=self.owner,
            data=self._payload(),
        )
        application = AfterSalesServiceStationApplication.objects.get(
            organization=self.organization
        )
        self.assertEqual(application.status, "PENDING_REVIEW")
        self.assertEqual(application.receiving_contact_name, "测试电工")
        self.assertEqual(application.receiving_contact_phone, "13900000003")
        self.assertEqual(application.service_latitude, Decimal("29.5630100"))
        self.assertEqual(
            set(
                AfterSalesServiceStationLiaison.objects.filter(
                    application=application
                ).values_list("identity_id", flat=True)
            ),
            {self.electrician_identity.pk},
        )
        self.assertEqual(result["application"]["status"], "PENDING_REVIEW")

    def test_factory_sales_review_task_and_result_notice_audience(self):
        _parent, parent_owner, inviter, _manager = self._create_parent_chain()
        submit_service_station_application(user=self.owner, data=self._payload())
        application = AfterSalesServiceStationApplication.objects.get(
            organization=self.organization
        )
        workitem = WorkItem.objects.get(
            uniq_key=f"after-sales-station:{application.pk}:review"
        )

        self.assertEqual(workitem.action_mode, "ANYONE_DO")
        self.assertEqual(workitem.status, "OPEN")
        self.assertTrue(
            workitem.user_tasks.get(to_openid=self.factory_sales_user.wx_openid).need_action
        )
        self.assertTrue(
            get_service_station_review_context(
                user=self.factory_sales_user,
                application_id=application.pk,
            )["can_review"]
        )

        reviewed = review_service_station_application(
            user=self.factory_sales_user,
            application_id=application.pk,
            action="approve",
        )
        application.refresh_from_db()
        workitem.refresh_from_db()
        self.assertEqual(application.status, "APPROVED")
        self.assertEqual(application.reviewed_by_id, self.factory_sales_user.pk)
        self.assertEqual(workitem.status, "DONE")
        self.assertEqual(workitem.done_by_openid, self.factory_sales_user.wx_openid)
        self.assertFalse(reviewed["can_review"])

        result_item = workitem
        self.assertEqual(result_item.type, "AFTER_SALES_STATION_REVIEW")
        self.assertEqual(
            WorkItem.objects.filter(biz_id=str(application.pk)).count(),
            1,
        )
        self.assertTrue(all(
            not task.need_action
            for task in result_item.user_tasks.all()
        ))
        notified = set(result_item.user_tasks.values_list("to_openid", flat=True))
        self.assertTrue({
            self.owner.wx_openid,
            self.electrician.wx_openid,
            self.factory_aftersales_user.wx_openid,
            parent_owner.wx_openid,
            inviter.user.wx_openid,
        }.issubset(notified))
        self.assertEqual(result_item.payload["result"], "APPROVED")
        for notified_user in (
            self.owner,
            self.electrician,
            self.factory_aftersales_user,
            parent_owner,
            inviter.user,
        ):
            context = get_service_station_review_context(
                user=notified_user,
                application_id=application.pk,
            )
            self.assertEqual(context["application"]["status"], "APPROVED")
            self.assertFalse(context["can_review"])

    def test_reviewer_who_invited_applicant_gets_one_updating_task_card(self):
        self.owner_identity.inviter = self.factory_sales_user
        self.owner_identity.save(update_fields=("inviter", "updated_at"))
        submit_service_station_application(user=self.owner, data=self._payload())
        application = AfterSalesServiceStationApplication.objects.get(
            organization=self.organization
        )
        workitem = WorkItem.objects.get(
            uniq_key=f"after-sales-station:{application.pk}:review"
        )

        review_service_station_application(
            user=self.factory_sales_user,
            application_id=application.pk,
            action="approve",
        )
        application.refresh_from_db()

        self.assertEqual(
            WorkItem.objects.filter(biz_id=str(application.pk)).count(),
            1,
        )
        reviewer_card = workitem.user_tasks.get(
            to_openid=self.factory_sales_user.wx_openid
        )
        applicant_card = workitem.user_tasks.get(to_openid=self.owner.wx_openid)
        self.assertEqual(
            reviewer_card.display_title_snapshot,
            "售后服务站申请已通过",
        )
        self.assertIn("由你完成审核", reviewer_card.display_subtitle_snapshot)
        self.assertEqual(applicant_card.display_title_snapshot, "售后服务站申请已通过")
        self.assertFalse(applicant_card.need_action)
        self.assertEqual(applicant_card.state, "NEW")
        self.assertFalse(
            WorkItem.objects.filter(
                type="AFTER_SALES_STATION_RESULT",
                biz_id=str(application.pk),
            ).exists()
        )

        # Replaying the same decision push must neither create a second task nor
        # turn a result card that the applicant read back into an unread card.
        read_at = timezone.now()
        applicant_card.state = "READ"
        applicant_card.read_at = read_at
        applicant_card.save(update_fields=("state", "read_at", "updated_at"))
        _publish_station_result_notice(application, actor=self.factory_sales_user)
        applicant_card.refresh_from_db()
        self.assertEqual(
            WorkItem.objects.filter(biz_id=str(application.pk)).count(),
            1,
        )
        self.assertEqual(applicant_card.state, "READ")
        self.assertEqual(applicant_card.read_at, read_at)

    def test_approved_station_badge_is_shared_by_every_role_in_merchant_org_only(self):
        submit_service_station_application(user=self.owner, data=self._payload())
        application = AfterSalesServiceStationApplication.objects.get(
            organization=self.organization
        )
        application.status = AfterSalesServiceStationApplication.Status.APPROVED
        application.save(update_fields=("status", "updated_at"))

        for member in (self.owner, self.sales, self.electrician):
            with self.subTest(user=member.username):
                self.assertEqual(
                    get_service_station_organization_badge(user=member),
                    {"organization_id": self.organization.pk, "approved": True},
                )
        self.assertEqual(
            get_service_station_organization_badge(user=self.factory_sales_user),
            {"organization_id": self.factory_root.pk, "approved": False},
        )

    def test_factory_senior_manager_and_sales_share_one_review_completion(self):
        senior_role, _ = OrganizationRole.objects.get_or_create(
            organization_type=self.factory_root.organization_type,
            code="factory_senior_manager",
            defaults={"name": "厂家高级经理"},
        )
        manager = User.objects.create_user(
            username="station_factory_senior_manager",
            wx_openid="station-factory-senior-openid",
            wx_nickname="厂家分管高级经理",
        )
        manager_identity = UserOrganization.objects.create(
            user=manager,
            organization=self.factory_root,
            role=senior_role,
        )
        profile = SeniorManagerProfile.objects.create(identity=manager_identity)
        profile.managed_roles.add(self.factory_sales_role)

        submit_service_station_application(user=self.owner, data=self._payload())
        application = AfterSalesServiceStationApplication.objects.get(
            organization=self.organization
        )
        workitem = WorkItem.objects.get(
            uniq_key=f"after-sales-station:{application.pk}:review"
        )
        recipients = set(workitem.user_tasks.values_list("to_openid", flat=True))
        self.assertIn(self.factory_sales_user.wx_openid, recipients)
        self.assertIn(manager.wx_openid, recipients)
        self.assertTrue(
            get_service_station_review_context(
                user=self.factory_sales_user,
                application_id=application.pk,
            )["can_review"]
        )
        self.assertTrue(
            get_service_station_review_context(
                user=manager,
                application_id=application.pk,
            )["can_review"]
        )

        review_service_station_application(
            user=manager,
            application_id=application.pk,
            action="reject",
            review_note="请补充可签收故障设备的收件说明。",
        )
        application.refresh_from_db()
        workitem.refresh_from_db()
        self.assertEqual(application.status, "REJECTED")
        result_item = workitem
        self.assertIn("请补充可签收故障设备的收件说明。", result_item.summary)
        self.assertEqual(
            result_item.payload["review_note"],
            "请补充可签收故障设备的收件说明。",
        )
        applicant_notice = result_item.user_tasks.get(
            to_openid=self.owner.wx_openid
        )
        self.assertFalse(applicant_notice.need_action)
        sales_copy = workitem.user_tasks.get(to_openid=self.factory_sales_user.wx_openid)
        manager_copy = workitem.user_tasks.get(to_openid=manager.wx_openid)
        self.assertEqual(manager_copy.done_reason, "SELF_ACTION")
        self.assertEqual(sales_copy.done_reason, "OTHER_ACTION")
        self.assertFalse(sales_copy.need_action)
        self.assertEqual(
            sales_copy.display_title_snapshot,
            "售后服务站申请未通过",
        )
        self.assertIn("厂家分管高级经理", sales_copy.display_subtitle_snapshot)
        sales_context = get_service_station_review_context(
            user=self.factory_sales_user,
            application_id=application.pk,
        )
        self.assertFalse(sales_context["can_review"])
        self.assertEqual(
            sales_context["task_completed_by_name"],
            "厂家分管高级经理",
        )

    def test_delegated_upstream_sales_manager_replaces_parent_owner_notice(self):
        _parent, parent_owner, inviter, manager_identity = self._create_parent_chain(
            delegated_sales_manager=True
        )
        submit_service_station_application(user=self.owner, data=self._payload())
        application = AfterSalesServiceStationApplication.objects.get(
            organization=self.organization
        )
        review_service_station_application(
            user=self.factory_sales_user,
            application_id=application.pk,
            action="approve",
        )
        notice = WorkItem.objects.get(
            uniq_key=f"after-sales-station:{application.pk}:review"
        )
        recipients = set(notice.user_tasks.values_list("to_openid", flat=True))
        self.assertIn(inviter.user.wx_openid, recipients)
        self.assertIn(manager_identity.user.wx_openid, recipients)
        self.assertNotIn(parent_owner.wx_openid, recipients)

    def test_application_cannot_be_submitted_without_factory_sales_reviewer(self):
        self.factory_sales_identity.is_active = False
        self.factory_sales_identity.save(update_fields=("is_active",))
        with self.assertRaisesRegex(AfterSalesServiceStationError, "厂家销售经理"):
            submit_service_station_application(user=self.owner, data=self._payload())

    def test_two_electricians_are_offered_but_only_one_is_preselected(self):
        second = User.objects.create_user(
            username="station_electrician_2",
            wx_nickname="乙电工",
            phone="13900000004",
        )
        second_identity = UserOrganization.objects.create(
            user=second,
            organization=self.organization,
            role=self.electrician_role,
        )
        candidates = get_service_station_application_context(user=self.owner)["liaison_candidates"]
        self.assertEqual(
            {row["identity_id"] for row in candidates},
            {self.electrician_identity.pk, second_identity.pk},
        )
        self.assertEqual(sum(row["selected"] for row in candidates), 1)

    def test_sales_managers_take_priority_over_owner_when_no_electrician_exists(self):
        self.electrician_identity.is_active = False
        self.electrician_identity.save(update_fields=("is_active",))
        second = User.objects.create_user(
            username="station_sales_2",
            wx_nickname="乙销售",
            phone="13900000005",
        )
        second_identity = UserOrganization.objects.create(
            user=second,
            organization=self.organization,
            role=self.sales_role,
        )

        candidates = get_service_station_application_context(user=self.owner)[
            "liaison_candidates"
        ]
        self.assertEqual(
            {row["identity_id"] for row in candidates},
            {self.sales_identity.pk, second_identity.pk},
        )
        self.assertEqual(sum(row["selected"] for row in candidates), 1)
        self.assertTrue(all(row["role_code"] == "merchant_sales" for row in candidates))

    def test_sales_then_owner_are_fallbacks_when_higher_priority_roles_are_absent(self):
        self.electrician_identity.is_active = False
        self.electrician_identity.save(update_fields=("is_active",))
        context = get_service_station_application_context(user=self.owner)
        self.assertEqual([row["identity_id"] for row in context["liaison_candidates"]], [self.sales_identity.pk])

        submit_service_station_application(
            user=self.owner,
            data=self._payload([self.sales_identity.pk]),
        )
        application = AfterSalesServiceStationApplication.objects.get(
            organization=self.organization
        )
        self.assertEqual(application.receiving_contact_name, "测试销售")
        self.assertEqual(application.receiving_contact_phone, "13900000002")

        self.sales_identity.is_active = False
        self.sales_identity.save(update_fields=("is_active",))
        context = get_service_station_application_context(user=self.owner)
        self.assertEqual([row["identity_id"] for row in context["liaison_candidates"]], [self.owner_identity.pk])
        self.assertTrue(context["liaison_candidates"][0]["selected"])

    def test_application_rejects_a_liaison_from_another_organization(self):
        outsider_org = Organization.objects.create(
            code="MERCHANT-STATION-OUTSIDE",
            name="其他商家",
            organization_type=self.organization.organization_type,
            factory_root=self.factory_root,
            provides_after_sales=True,
        )
        outsider = User.objects.create_user(username="station_outsider")
        outsider_identity = UserOrganization.objects.create(
            user=outsider,
            organization=outsider_org,
            role=self.sales_role,
        )

        with self.assertRaises(AfterSalesServiceStationError):
            submit_service_station_application(
                user=self.owner,
                data=self._payload([outsider_identity.pk]),
            )

    def test_application_rejects_multiple_liaisons(self):
        with self.assertRaises(AfterSalesServiceStationError):
            submit_service_station_application(
                user=self.owner,
                data=self._payload([self.electrician_identity.pk, self.sales_identity.pk]),
            )

    def test_missing_member_phone_cannot_be_replaced_by_client_contact_data(self):
        self.electrician.phone = ""
        self.electrician.save(update_fields=("phone",))
        payload = self._payload()
        payload["receiving_contact_name"] = "旧联系人"
        payload["receiving_contact_phone"] = "13800009999"

        with self.assertRaises(AfterSalesServiceStationError):
            submit_service_station_application(user=self.owner, data=payload)

    def test_unqualified_merchant_cannot_submit(self):
        self.organization.provides_after_sales = False
        self.organization.save(update_fields=("provides_after_sales",))

        context = get_service_station_application_context(user=self.owner)
        self.assertFalse(context["eligible"])
        with self.assertRaises(AfterSalesServiceStationError):
            submit_service_station_application(user=self.owner, data=self._payload())

    def test_approved_station_owner_can_edit_only_the_receiving_address(self):
        submit_service_station_application(user=self.owner, data=self._payload())
        application = AfterSalesServiceStationApplication.objects.get(
            organization=self.organization
        )
        application.status = AfterSalesServiceStationApplication.Status.APPROVED
        application.save(update_fields=("status",))

        context = get_service_station_application_context(user=self.owner)
        self.assertTrue(context["can_edit_receiving_address"])
        self.assertFalse(context["can_submit"])

        result = update_approved_service_station_receiving_address(
            user=self.owner,
            data={"receiving_address": "重庆市测试区新收件地址"},
        )
        application.refresh_from_db()
        self.assertEqual(application.status, AfterSalesServiceStationApplication.Status.APPROVED)
        self.assertEqual(application.receiving_address, "重庆市测试区新收件地址")
        self.assertEqual(
            result["application"]["receiving"]["address"],
            "重庆市测试区新收件地址",
        )

    def test_approved_address_editor_rejects_unapproved_and_empty_values(self):
        with self.assertRaises(AfterSalesServiceStationError):
            update_approved_service_station_receiving_address(
                user=self.owner,
                data={"receiving_address": "新的收件地址"},
            )

        submit_service_station_application(user=self.owner, data=self._payload())
        application = AfterSalesServiceStationApplication.objects.get(
            organization=self.organization
        )
        application.status = AfterSalesServiceStationApplication.Status.APPROVED
        application.save(update_fields=("status",))
        with self.assertRaises(AfterSalesServiceStationError):
            update_approved_service_station_receiving_address(
                user=self.owner,
                data={"receiving_address": "   "},
            )
