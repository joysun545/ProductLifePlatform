import json

from django.test import TestCase

from account.models import (
    Organization,
    OrganizationRole,
    OrganizationType,
    User,
    UserOrganization,
)
from account.services.auth_service import issue_access_token
from product.models import ProductCategory
from supply_chain.models import (
    Material,
    MaterialSpec,
    MaterialSpecSupplier,
    SupplierResponsibilityScope,
    SupplierSalesResponsibility,
)
from supply_chain.services.supplier_responsibility_service import (
    SupplierResponsibilityError,
    assign_supplier_sales_responsibilities,
    get_responsibility_options,
    get_responsible_material_ids,
    get_supplier_task_recipients,
)
from workflow.models import WorkItem
from workflow.services.workflow_service import publish_workitem


class SupplierResponsibilityTests(TestCase):
    url = "/pldp/api/supply-chain/sales-responsibilities/"

    def setUp(self):
        supplier_type = OrganizationType.objects.get(code="SUPPLIER")
        self.owner_role = OrganizationRole.objects.get(code="supplier_owner")
        self.sales_role = OrganizationRole.objects.get(code="supplier_sales")
        self.supplier = Organization.objects.create(
            name="责任范围测试供应商",
            code="RESP-SUPPLIER",
            organization_type=supplier_type,
        )
        self.owner = self._create_identity(
            "responsibility-owner",
            "openid-responsibility-owner",
            self.owner_role,
        )
        self.sales_a = self._create_identity(
            "responsibility-sales-a",
            "openid-responsibility-sales-a",
            self.sales_role,
        )
        self.sales_b = self._create_identity(
            "responsibility-sales-b",
            "openid-responsibility-sales-b",
            self.sales_role,
        )
        self.category = ProductCategory.objects.create(
            name="责任范围测试品类",
            code="RESP-CATEGORY",
        )
        self.material_a = self._create_supplied_material(
            "RESP-MATERIAL-A",
            "责任范围物料A",
        )
        self.material_b = self._create_supplied_material(
            "RESP-MATERIAL-B",
            "责任范围物料B",
        )
        self.owner_authorization = f"Bearer {issue_access_token(self.owner)}"
        self.sales_authorization = f"Bearer {issue_access_token(self.sales_a)}"

    def _create_identity(self, username, openid, role):
        user = User.objects.create_user(
            username=username,
            wx_openid=openid,
            wx_nickname=username,
        )
        UserOrganization.objects.create(
            user=user,
            organization=self.supplier,
            role=role,
        )
        return user

    def _create_supplied_material(self, code, name):
        material = Material.objects.create(
            code=code,
            name=name,
            product_category=self.category,
        )
        spec = MaterialSpec.objects.create(
            material=material,
            code=f"{code}-SPEC",
            name="测试规格",
        )
        MaterialSpecSupplier.objects.create(
            material_spec=spec,
            supplier=self.supplier,
        )
        return material

    def _assign(self, user, sales_user, scope_type, target_ids):
        return assign_supplier_sales_responsibilities(
            user=user,
            sales_identity_id=sales_user.organization_identity.pk,
            scope_type=scope_type,
            target_ids=target_ids,
        )

    def test_owner_can_load_only_current_supplier_options(self):
        payload = get_responsibility_options(user=self.owner)

        self.assertEqual(payload["supplier"]["id"], self.supplier.pk)
        self.assertSetEqual(
            {item["identity_id"] for item in payload["sales_managers"]},
            {
                self.sales_a.organization_identity.pk,
                self.sales_b.organization_identity.pk,
            },
        )
        self.assertSetEqual(
            {item["id"] for item in payload["materials"]},
            {self.material_a.pk, self.material_b.pk},
        )

    def test_sales_manager_cannot_change_responsibility(self):
        response = self.client.post(
            self.url,
            data=json.dumps(
                {
                    "sales_identity_id": self.sales_a.organization_identity.pk,
                    "scope_type": SupplierResponsibilityScope.MATERIAL,
                    "target_ids": [self.material_a.pk],
                }
            ),
            content_type="application/json",
            HTTP_AUTHORIZATION=self.sales_authorization,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1)
        self.assertFalse(SupplierSalesResponsibility.objects.exists())

    def test_multi_sales_uses_material_assignment_only(self):
        self._assign(
            self.owner,
            self.sales_b,
            SupplierResponsibilityScope.MATERIAL,
            [self.material_b.pk],
        )

        self.assertSetEqual(
            get_responsible_material_ids(
                sales_identity=self.sales_a.organization_identity,
            ),
            set(),
        )
        self.assertSetEqual(
            get_responsible_material_ids(
                sales_identity=self.sales_b.organization_identity,
            ),
            {self.material_b.pk},
        )

    def test_non_material_assignment_is_rejected(self):
        with self.assertRaises(SupplierResponsibilityError):
            self._assign(
                self.owner,
                self.sales_a,
                "CATEGORY",
                [self.category.pk],
            )

    def test_owner_can_reassign_same_material_to_another_sales_manager(self):
        self._assign(
            self.owner,
            self.sales_a,
            SupplierResponsibilityScope.MATERIAL,
            [self.material_a.pk],
        )
        self._assign(
            self.owner,
            self.sales_b,
            SupplierResponsibilityScope.MATERIAL,
            [self.material_a.pk],
        )

        responsibility = SupplierSalesResponsibility.objects.get(
            material=self.material_a,
        )
        self.assertEqual(
            responsibility.sales_identity,
            self.sales_b.organization_identity,
        )

    def test_cannot_assign_material_not_supplied_by_current_supplier(self):
        other_material = Material.objects.create(
            code="RESP-OTHER-MATERIAL",
            name="非本组织供应物料",
            product_category=self.category,
        )

        with self.assertRaises(SupplierResponsibilityError):
            self._assign(
                self.owner,
                self.sales_a,
                SupplierResponsibilityScope.MATERIAL,
                [other_material.pk],
            )

    def test_reassignment_keeps_one_workitem_and_syncs_user_tasks(self):
        self._assign(
            self.owner,
            self.sales_a,
            SupplierResponsibilityScope.MATERIAL,
            [self.material_b.pk],
        )
        recipients = get_supplier_task_recipients(
            supplier=self.supplier,
            material_ids=[self.material_b.pk],
        )
        workitem = publish_workitem(
            domain="SALES",
            type="ORDER_PLAN_SUPPLIER_PREPARE",
            biz_id="OP-RESP-001",
            biz_ref="sales.OrderPlan:1",
            uniq_key=f"sales:order-plan:1:supplier:{self.supplier.pk}",
            title="测试供应共享任务",
            summary="测试责任范围改派",
            recipients_openids=recipients["openids"],
            need_action=True,
            action_mode="ANYONE_DO",
            payload={
                "order_plan_id": 1,
                "supplier_id": self.supplier.pk,
                "materials": [{"material_id": self.material_b.pk}],
            },
            mark_workitem_done_immediately=False,
        )
        self.assertSetEqual(
            set(workitem.user_tasks.values_list("to_openid", flat=True)),
            {self.owner.wx_openid, self.sales_a.wx_openid},
        )

        result = self._assign(
            self.owner,
            self.sales_b,
            SupplierResponsibilityScope.MATERIAL,
            [self.material_b.pk],
        )
        workitem.refresh_from_db()

        self.assertEqual(WorkItem.objects.count(), 1)
        self.assertSetEqual(
            set(workitem.user_tasks.values_list("to_openid", flat=True)),
            {self.owner.wx_openid, self.sales_b.wx_openid},
        )
        self.assertEqual(result["open_task_sync"]["workitems"], 1)
        self.assertEqual(result["open_task_sync"]["tasks_added"], 1)
        self.assertEqual(result["open_task_sync"]["tasks_removed"], 1)

    def test_unassigned_sales_manager_receives_no_supplier_task(self):
        recipients = get_supplier_task_recipients(
            supplier=self.supplier,
            material_ids=[self.material_a.pk],
        )

        self.assertEqual(recipients["sales_openids"], [])
        self.assertEqual(recipients["openids"], [self.owner.wx_openid])

    def test_one_sales_manager_defaults_to_all_materials(self):
        self.sales_b.organization_identity.is_active = False
        self.sales_b.organization_identity.save(update_fields=("is_active", "updated_at"))

        payload = get_responsibility_options(user=self.owner)
        self.assertEqual(payload["assignment_mode"], "SINGLE_SALES_DEFAULT_ALL")
        self.assertTrue(payload["sales_managers"][0]["default_all_materials"])
        self.assertSetEqual(
            get_responsible_material_ids(
                sales_identity=self.sales_a.organization_identity,
            ),
            {self.material_a.pk, self.material_b.pk},
        )
        recipients = get_supplier_task_recipients(
            supplier=self.supplier,
            material_ids=[self.material_a.pk, self.material_b.pk],
        )
        self.assertSetEqual(
            set(recipients["openids"]),
            {self.owner.wx_openid, self.sales_a.wx_openid},
        )
