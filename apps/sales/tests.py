import json
from datetime import timedelta
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone

from account.models import (
    InvitationRecord,
    InvitationScope,
    Organization,
    OrganizationRelation,
    OrganizationRelationType,
    OrganizationRole,
    OrganizationType,
    RoleInvitationRule,
    User,
    UserOrganization,
)
from account.services.auth_service import issue_access_token
from channel.models import MerchantChannelAssignment
from lifecycle.models import (
    Device,
    DeviceState,
    FactoryReturnInventoryItem,
    FactoryReturnProcess,
    GoodsTransfer,
    GoodsTransferItem,
    LifecycleEvent,
    OrganizationDevice,
    QRCode,
)
from manufacturing.models import ProductionBatch
from product.models import BOM, BOMItem, Brand, ProductCategory, ProductModel
from quality_trace.models import (
    BatchQualityAffectedDevice,
    BatchQualityCase,
    QualityGateDecision,
)
from quality_trace.services import place_temporary_hold, revoke_temporary_hold
from sales.services.order_qrcode_print_service import (
    OrderQRCodePrintError,
    _validate_supplier_batch_coverage,
    confirm_item_printed,
    generate_item_qrcodes,
    get_print_center_data,
)
from sales.services.supplier_batch_trace_service import get_supplier_batch_trace
from sales.models import (
    OrderPlan,
    OrderPlanItem,
    OrderPlanItemBOMConfirmation,
    OrderPlanItemMaterialBatch,
    OrderPlanFinance,
    OrderPlanLogisticsDocument,
    OrderPlanPayment,
    OrderPlanReturnInventoryAllocation,
)
from supply_chain.models import (
    Material,
    MaterialSpec,
    MaterialSpecSupplier,
    SupplierMaterialBatch,
    SupplierResponsibilityScope,
    SupplierSalesResponsibility,
)
from workflow.models import UserTask, WorkItem
from workflow.services.task_query import get_task_list


class OrderPlanApiTests(TestCase):
    list_url = "/pldp/api/sales/order-plans/"
    options_url = "/pldp/api/sales/order-plans/options/"
    configure_url = "/pldp/api/sales/direct-merchants/configure/"
    create_url = "/pldp/api/sales/order-plans/create/"

    def setUp(self):
        owner_type = OrganizationType.objects.get(code="OWNER")
        merchant_type = OrganizationType.objects.get(code="MERCHANT")
        supplier_type = OrganizationType.objects.get(code="SUPPLIER")
        self.factory_sales_role = OrganizationRole.objects.get(code="factory_sales")
        self.factory_matching_role = OrganizationRole.objects.get(code="factory_matching")
        self.factory_production_role = OrganizationRole.objects.get(
            code="factory_production"
        )
        self.factory_stock_role = OrganizationRole.objects.get(
            code="factory_stock"
        )
        self.factory_logistics_role = OrganizationRole.objects.get(
            code="factory_logistics"
        )
        self.factory_sales_assistant_role = OrganizationRole.objects.get(
            code="factory_sales_assistant"
        )
        self.factory_manager_role = OrganizationRole.objects.get(
            code="factory_manager"
        )
        self.merchant_owner_role = OrganizationRole.objects.get(code="merchant_owner")
        self.merchant_senior_manager_role = OrganizationRole.objects.get(
            code="merchant_senior_manager"
        )
        self.merchant_sales_role = OrganizationRole.objects.get(code="merchant_sales")
        self.merchant_stock_role = OrganizationRole.objects.get(code="merchant_stock")
        self.merchant_electrician_role = OrganizationRole.objects.get(
            code="merchant_electrician"
        )
        self.supplier_owner_role = OrganizationRole.objects.get(code="supplier_owner")
        self.supplier_sales_role = OrganizationRole.objects.get(code="supplier_sales")
        self.invitation_rule = RoleInvitationRule.objects.get(
            inviter_role=self.factory_sales_role,
            target_role=self.merchant_owner_role,
            scope=InvitationScope.CROSS_ORGANIZATION,
            relation_type=OrganizationRelationType.DISTRIBUTION,
        )

        self.factory = Organization.objects.create(
            name="测试厂家",
            code="ORDER-FACTORY",
            organization_type=owner_type,
            region="重庆",
        )
        self.merchant = Organization.objects.create(
            name="销售经理直属商家",
            code="ORDER-MERCHANT-1",
            organization_type=merchant_type,
            region="四川省",
        )
        self.other_merchant = Organization.objects.create(
            name="其他销售经理直属商家",
            code="ORDER-MERCHANT-2",
            organization_type=merchant_type,
            region="云南省",
        )
        self.supplier = Organization.objects.create(
            name="订单测试供应商",
            code="ORDER-SUPPLIER",
            organization_type=supplier_type,
            region="重庆",
        )

        self.sales_user = self._user_with_identity(
            "order-sales",
            "openid-order-sales",
            self.factory,
            self.factory_sales_role,
        )
        self.other_sales_user = self._user_with_identity(
            "order-other-sales",
            "openid-order-other-sales",
            self.factory,
            self.factory_sales_role,
        )
        self.matching_user = self._user_with_identity(
            "order-matching",
            "openid-order-matching",
            self.factory,
            self.factory_matching_role,
        )
        self.factory_production = self._user_with_identity(
            "order-factory-production",
            "openid-order-factory-production",
            self.factory,
            self.factory_production_role,
        )
        self.factory_stock = self._user_with_identity(
            "order-factory-stock",
            "openid-order-factory-stock",
            self.factory,
            self.factory_stock_role,
        )
        self.factory_logistics_1 = self._user_with_identity(
            "order-factory-logistics-1",
            "openid-order-factory-logistics-1",
            self.factory,
            self.factory_logistics_role,
        )
        self.factory_logistics_2 = self._user_with_identity(
            "order-factory-logistics-2",
            "openid-order-factory-logistics-2",
            self.factory,
            self.factory_logistics_role,
        )
        self.factory_manager = self._user_with_identity(
            "order-factory-manager",
            "openid-order-factory-manager",
            self.factory,
            self.factory_manager_role,
        )
        self.factory_assistant_1 = self._user_with_identity(
            "order-factory-assistant-1",
            "openid-order-factory-assistant-1",
            self.factory,
            self.factory_sales_assistant_role,
        )
        self.factory_assistant_2 = self._user_with_identity(
            "order-factory-assistant-2",
            "openid-order-factory-assistant-2",
            self.factory,
            self.factory_sales_assistant_role,
        )
        self.merchant_owner = self._user_with_identity(
            "order-merchant-owner",
            "openid-order-merchant-owner",
            self.merchant,
            self.merchant_owner_role,
        )
        self.merchant_senior_manager = self._user_with_identity(
            "order-merchant-senior-manager",
            "openid-order-merchant-senior-manager",
            self.merchant,
            self.merchant_senior_manager_role,
        )
        self.merchant_sales = self._user_with_identity(
            "order-merchant-sales",
            "openid-order-merchant-sales",
            self.merchant,
            self.merchant_sales_role,
        )
        self.merchant_stock_1 = self._user_with_identity(
            "order-merchant-stock-1",
            "openid-order-merchant-stock-1",
            self.merchant,
            self.merchant_stock_role,
        )
        self.merchant_stock_2 = self._user_with_identity(
            "order-merchant-stock-2",
            "openid-order-merchant-stock-2",
            self.merchant,
            self.merchant_stock_role,
        )
        self.merchant_electrician = self._user_with_identity(
            "order-merchant-electrician",
            "openid-order-merchant-electrician",
            self.merchant,
            self.merchant_electrician_role,
        )
        self.other_merchant_owner = self._user_with_identity(
            "order-other-merchant-owner",
            "openid-order-other-merchant-owner",
            self.other_merchant,
            self.merchant_owner_role,
        )
        self.supplier_owner = self._user_with_identity(
            "order-supplier-owner",
            "openid-order-supplier-owner",
            self.supplier,
            self.supplier_owner_role,
        )
        self.supplier_sales = self._user_with_identity(
            "order-supplier-sales",
            "openid-order-supplier-sales",
            self.supplier,
            self.supplier_sales_role,
        )

        self.relation = self._accepted_direct_relation(
            self.sales_user,
            self.merchant_owner,
            self.merchant,
        )
        self.other_relation = self._accepted_direct_relation(
            self.other_sales_user,
            self.other_merchant_owner,
            self.other_merchant,
        )

        self.brand = Brand.objects.create(name="劲龙", code="JL-ORDER")
        self.other_brand = Brand.objects.create(name="其他品牌", code="OTHER-ORDER")
        self.brand_without_product = Brand.objects.create(
            name="暂未添加型号的品牌",
            code="EMPTY-ORDER",
        )
        self.generator_category = ProductCategory.objects.create(
            name="驻车发电机订单测试",
            code="ORDER-GENERATOR",
        )
        self.air_category = ProductCategory.objects.create(
            name="驻车空调订单测试",
            code="ORDER-AIR",
        )
        self.generator = self._product_with_v1_bom(
            "ORDER-GEN-5000",
            "5000W驻车发电机",
            self.brand,
            self.generator_category,
        )
        self.air_conditioner = self._product_with_v1_bom(
            "ORDER-AIR-24",
            "24V驻车空调",
            self.brand,
            self.air_category,
        )
        self.other_brand_product = self._product_with_v1_bom(
            "ORDER-OTHER-MODEL",
            "其他品牌产品",
            self.other_brand,
            self.generator_category,
        )
        self.unbranded_product = self._product_with_v1_bom(
            "ORDER-UNBRANDED-MODEL",
            "不绑定品牌的产品型号",
            None,
            self.generator_category,
        )

        self.assignment = MerchantChannelAssignment.objects.create(
            relation=self.relation,
            brand=self.brand,
            region="四川省",
            assigned_by=self.sales_user,
        )
        self.generator_material = self._material_with_supplier(
            "ORDER-GENERATOR-CTRL",
            "订单测试控制器",
            self.generator_category,
        )
        self.generator_extra_material = self._material_with_supplier(
            "ORDER-GENERATOR-SENSOR",
            "订单测试传感器",
            self.generator_category,
        )
        self.air_material = self._material_with_supplier(
            "ORDER-AIR-COMPRESSOR",
            "订单测试压缩机",
            self.air_category,
        )
        BOMItem.objects.create(
            bom=self.generator.boms.get(version="v1"),
            material=self.generator_material[0],
            material_spec=self.generator_material[1],
            supplier=self.supplier,
            quantity=1,
            remark="发电机默认控制器",
        )
        BOMItem.objects.create(
            bom=self.air_conditioner.boms.get(version="v1"),
            material=self.air_material[0],
            material_spec=self.air_material[1],
            supplier=self.supplier,
            quantity=1,
        )
        for category in (self.generator_category, self.air_category):
            SupplierSalesResponsibility.objects.create(
                supplier=self.supplier,
                sales_identity=self.supplier_sales.organization_identity,
                scope_type=SupplierResponsibilityScope.CATEGORY,
                product_category=category,
                assigned_by=self.supplier_owner,
            )
        self.authorization = f"Bearer {issue_access_token(self.sales_user)}"
        self.other_sales_authorization = (
            f"Bearer {issue_access_token(self.other_sales_user)}"
        )
        self.matching_authorization = (
            f"Bearer {issue_access_token(self.matching_user)}"
        )
        self.production_authorization = (
            f"Bearer {issue_access_token(self.factory_production)}"
        )
        self.stock_authorization = (
            f"Bearer {issue_access_token(self.factory_stock)}"
        )
        self.factory_logistics_1_authorization = (
            f"Bearer {issue_access_token(self.factory_logistics_1)}"
        )
        self.factory_logistics_2_authorization = (
            f"Bearer {issue_access_token(self.factory_logistics_2)}"
        )
        self.factory_assistant_1_authorization = (
            f"Bearer {issue_access_token(self.factory_assistant_1)}"
        )
        self.factory_assistant_2_authorization = (
            f"Bearer {issue_access_token(self.factory_assistant_2)}"
        )
        self.merchant_owner_authorization = (
            f"Bearer {issue_access_token(self.merchant_owner)}"
        )
        self.merchant_senior_manager_authorization = (
            f"Bearer {issue_access_token(self.merchant_senior_manager)}"
        )
        self.merchant_sales_authorization = (
            f"Bearer {issue_access_token(self.merchant_sales)}"
        )
        self.merchant_stock_1_authorization = (
            f"Bearer {issue_access_token(self.merchant_stock_1)}"
        )
        self.merchant_electrician_authorization = (
            f"Bearer {issue_access_token(self.merchant_electrician)}"
        )
        self.supplier_owner_authorization = (
            f"Bearer {issue_access_token(self.supplier_owner)}"
        )
        self.supplier_sales_authorization = (
            f"Bearer {issue_access_token(self.supplier_sales)}"
        )

    def _user_with_identity(self, username, openid, organization, role):
        user = User.objects.create_user(
            username=username,
            wx_openid=openid,
            wx_nickname=username,
        )
        UserOrganization.objects.create(
            user=user,
            organization=organization,
            role=role,
        )
        return user

    def _accepted_direct_relation(self, inviter, invitee, target_organization):
        invitation = InvitationRecord.objects.create(
            inviter=inviter,
            source_organization=self.factory,
            inviter_role=self.factory_sales_role,
            rule=self.invitation_rule,
            target_role=self.merchant_owner_role,
            target_organization=target_organization,
            invitee=invitee,
            scope=InvitationScope.CROSS_ORGANIZATION,
            relation_type=OrganizationRelationType.DISTRIBUTION,
            status=InvitationRecord.Status.ACCEPTED,
            expires_at=timezone.now() + timedelta(days=1),
            accepted_at=timezone.now(),
        )
        return OrganizationRelation.objects.create(
            source_organization=self.factory,
            target_organization=target_organization,
            relation_type=OrganizationRelationType.DISTRIBUTION,
            created_by_invitation=invitation,
        )

    def _product_with_v1_bom(self, code, name, brand, category):
        product_model = ProductModel.objects.create(
            code=code,
            name=name,
            brand=brand,
            category=category,
        )
        BOM.objects.create(
            product_model=product_model,
            version_no=1,
            version="v1",
            is_active=True,
        )
        return product_model

    def _material_with_supplier(self, code, name, category):
        material = Material.objects.create(
            code=code,
            name=name,
            product_category=category,
        )
        spec = MaterialSpec.objects.create(
            material=material,
            code=f"{code}-SPEC",
            name="标准规格",
        )
        MaterialSpecSupplier.objects.create(
            material_spec=spec,
            supplier=self.supplier,
        )
        return material, spec

    def _create_return_inventory(self, count, product_model=None):
        """建立已完成退厂分类、可被新订单逐台预占的真实链路库存。"""

        product_model = product_model or self.generator
        bom = product_model.boms.get(version="v1")
        now = timezone.now()
        suffix = (
            f"{timezone.now().strftime('%H%M%S%f')}-"
            f"{GoodsTransfer.objects.count()}"
        )
        batch = ProductionBatch.objects.create(
            batch_no=f"RETURN-BATCH-{suffix}",
            product_model=product_model,
            bom=bom,
            manufacturer=self.factory,
            production_date=timezone.localdate(),
            quantity=count,
        )
        transfer = GoodsTransfer.objects.create(
            flow_no=f"RETURN-FLOW-{suffix}",
            client_request_id=f"return-request-{suffix}",
            flow_type=GoodsTransfer.FlowType.RETURN,
            status=GoodsTransfer.Status.COMPLETED,
            instruction_organization=self.merchant,
            from_organization=self.merchant,
            to_organization=self.factory,
            source_account=self.merchant_owner,
            target_account=self.sales_user,
            initiated_by=self.merchant_owner,
            received_by=self.sales_user,
            submitted_at=now,
            received_at=now,
            completed_at=now,
        )
        process = FactoryReturnProcess.objects.create(
            transfer=transfer,
            phase=FactoryReturnProcess.Phase.POSTED,
            picked_by=self.factory_logistics_1,
            picked_at=now,
            posted_by=self.factory_assistant_1,
            posted_at=now,
        )
        inventory_items = []
        for index in range(count):
            device = Device.objects.create(
                sn=f"RETURN-SN-{suffix}-{index + 1}",
                product_model=product_model,
                batch=batch,
            )
            QRCode.objects.create(
                code=f"RETURN-QR-{suffix}-{index + 1}",
                device=device,
                status="DISABLED",
                created_by=self.sales_user,
                disabled_at=now,
            )
            transfer_item = GoodsTransferItem.objects.create(
                transfer=transfer,
                device=device,
                status=GoodsTransferItem.Status.COMPLETED,
                scanned_by=self.merchant_owner,
                completed_at=now,
            )
            inventory_items.append(
                FactoryReturnInventoryItem.objects.create(
                    process=process,
                    transfer_item=transfer_item,
                    source_product_model=product_model,
                    source_production_batch=batch,
                    source_bom=bom,
                    stock_status=(
                        FactoryReturnInventoryItem.StockStatus.PENDING_REISSUE
                    ),
                    classified_by=self.factory_assistant_1,
                    classified_at=now,
                    production_status=(
                        FactoryReturnInventoryItem.ProductionStatus.NOT_REQUIRED
                    ),
                    matching_status=(
                        FactoryReturnInventoryItem.MatchingStatus.NOT_REQUIRED
                    ),
                )
            )
        return inventory_items

    def _create_payload(self, client_request_id="order-request-1"):
        return {
            "merchant_id": self.merchant.pk,
            "client_request_id": client_request_id,
            "remark": "八月销售订单",
            "items": [
                {
                    "category_id": self.generator_category.pk,
                    "product_model_id": self.generator.pk,
                    "quantity": 20,
                },
                {
                    "category_id": self.air_category.pk,
                    "product_model_id": self.air_conditioner.pk,
                    "quantity": 10,
                },
            ],
        }

    def _create_plan(self, client_request_id="order-request-1"):
        response = self.client.post(
            self.create_url,
            data=json.dumps(self._create_payload(client_request_id)),
            content_type="application/json",
            HTTP_AUTHORIZATION=self.authorization,
        )
        self.assertEqual(response.json()["code"], 0, response.json())
        return OrderPlan.objects.get(client_request_id=client_request_id)

    def _prepare_outbound_ready_plan(self, client_request_id):
        plan = self._create_plan(client_request_id)
        plan.status = OrderPlan.Status.PRODUCTION_COMPLETED
        plan.shipment_authorized_at = timezone.now()
        plan.shipment_authorized_by = self.sales_user
        plan.save(update_fields=(
            "status",
            "shipment_authorized_at",
            "shipment_authorized_by",
            "updated_at",
        ))
        plan.items.update(status=OrderPlan.Status.PRODUCTION_COMPLETED)

        for item in plan.items.select_related("product_model").order_by("id"):
            bom = BOM.objects.get(
                product_model=item.product_model,
                version="v1",
                is_active=True,
            )
            OrderPlanItemBOMConfirmation.objects.update_or_create(
                order_plan_item=item,
                defaults={
                    "source_bom": bom,
                    "confirmed_bom": bom,
                    "confirmed_by": self.matching_user,
                    "has_changes": False,
                    "confirmed_at": timezone.now(),
                },
            )
            result = generate_item_qrcodes(item_id=item.pk)
            self.assertEqual(len(result["qrcodes"]), item.quantity)

        plan.items.update(qrcode_printed_at=timezone.now())
        workitem = WorkItem.objects.create(
            domain="SALES",
            type="ORDER_PLAN_STOCK_OUTBOUND",
            biz_id=plan.plan_code,
            biz_ref=f"sales.OrderPlan:{plan.pk}",
            uniq_key=f"sales:order-plan:{plan.pk}:stock-outbound",
            title="确认订单发货",
            summary="订单生产完成，等待厂家库管发货",
            link=(
                "/pages/sales/order_plan_list/order_plan_list"
                f"?focus_plan_id={plan.pk}"
            ),
            action_mode="ANYONE_DO",
            payload={"order_plan_id": plan.pk, "plan_code": plan.plan_code},
        )
        UserTask.objects.create(
            workitem=workitem,
            to_openid=self.factory_stock.wx_openid,
            to_role_snapshot="factory_stock",
            need_action=True,
            display_title_snapshot="确认订单发货",
        )
        return plan

    def _create_other_sales_plan(self):
        assignment = MerchantChannelAssignment.objects.create(
            relation=self.other_relation,
            brand=self.brand,
            region="云南省",
            assigned_by=self.other_sales_user,
        )
        plan = OrderPlan.objects.create(
            plan_code="OP-OTHER-SALES-001",
            client_request_id="other-sales-request",
            factory_organization=self.factory,
            merchant_organization=self.other_merchant,
            merchant_relation=self.other_relation,
            channel_assignment=assignment,
            brand=self.brand,
            region_snapshot="云南省",
            created_by=self.other_sales_user,
            total_quantity=3,
        )
        OrderPlanItem.objects.create(
            plan=plan,
            category=self.generator_category,
            product_model=self.generator,
            quantity=3,
            order_batch_code="OB-OTHER-SALES-001",
            lifecycle_code="PL-OTHER-SALES-001",
        )
        return plan

    def _create_external_factory_plan(self):
        external_factory = Organization.objects.create(
            name="外部测试厂家",
            code="ORDER-EXTERNAL-FACTORY",
            organization_type=OrganizationType.objects.get(code="OWNER"),
            region="广东",
        )
        external_merchant = Organization.objects.create(
            name="外部厂家直属商家",
            code="ORDER-EXTERNAL-MERCHANT",
            organization_type=OrganizationType.objects.get(code="MERCHANT"),
            region="广西",
        )
        external_sales = self._user_with_identity(
            "external-order-sales",
            "openid-external-order-sales",
            external_factory,
            self.factory_sales_role,
        )
        relation = OrganizationRelation.objects.create(
            source_organization=external_factory,
            target_organization=external_merchant,
            relation_type=OrganizationRelationType.DISTRIBUTION,
        )
        assignment = MerchantChannelAssignment.objects.create(
            relation=relation,
            brand=self.brand,
            region="广西",
            assigned_by=external_sales,
        )
        plan = OrderPlan.objects.create(
            plan_code="OP-EXTERNAL-FACTORY-001",
            client_request_id="external-factory-request",
            factory_organization=external_factory,
            merchant_organization=external_merchant,
            merchant_relation=relation,
            channel_assignment=assignment,
            brand=self.brand,
            region_snapshot="广西",
            created_by=external_sales,
            total_quantity=5,
        )
        OrderPlanItem.objects.create(
            plan=plan,
            category=self.generator_category,
            product_model=self.generator,
            quantity=5,
            order_batch_code="OB-EXTERNAL-FACTORY-001",
            lifecycle_code="PL-EXTERNAL-FACTORY-001",
        )
        return plan

    def _get_list(self, authorization):
        return self.client.get(
            self.list_url,
            HTTP_AUTHORIZATION=authorization,
        )

    def _matching_detail(self, plan):
        response = self.client.get(
            f"/pldp/api/sales/order-plans/{plan.pk}/matching/",
            HTTP_AUTHORIZATION=self.matching_authorization,
        )
        self.assertEqual(response.json()["code"], 0, response.json())
        return response.json()["data"]

    def _bom_detail(self, plan, order_item, bom):
        response = self.client.get(
            (
                f"/pldp/api/sales/order-plans/{plan.pk}/matching/items/"
                f"{order_item.pk}/boms/{bom.pk}/"
            ),
            HTTP_AUTHORIZATION=self.matching_authorization,
        )
        self.assertEqual(response.json()["code"], 0, response.json())
        return response.json()["data"]

    def _confirmation_payload(self, detail):
        return {
            "items": [
                {
                    "order_plan_item_id": item["id"],
                    "source_bom_id": item["default_bom"]["id"],
                    "materials": [
                        {
                            "material_id": material["id"],
                            "material_spec_id": material["selected_spec_id"],
                            "supplier_id": material["selected_supplier_id"],
                            "deleted": material["deleted"],
                            "remark": material["remark"],
                        }
                        for material in item["materials"]
                    ],
                }
                for item in detail["items"]
            ]
        }

    def _confirm(self, plan, payload):
        return self.client.post(
            f"/pldp/api/sales/order-plans/{plan.pk}/matching/confirm/",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION=self.matching_authorization,
        )

    def _supplier_confirm(
        self,
        plan,
        authorization,
        *,
        supplier_id=None,
        usertask_id=None,
    ):
        payload = {
            "supplier_id": supplier_id or self.supplier.pk,
        }
        if usertask_id:
            payload["usertask_id"] = usertask_id
        workitem = WorkItem.objects.filter(
            uniq_key=(
                f"sales:order-plan:{plan.pk}:supplier:"
                f"{payload['supplier_id']}"
            )
        ).first()
        collaboration = workitem.payload if workitem else {}
        if collaboration.get("supplier_batch_capture_required"):
            payload["material_batches"] = [
                {
                    "order_plan_item_id": row["order_plan_item_id"],
                    "bom_item_id": row["bom_item_id"],
                    "batch_code": (
                        f"TEST-SUPPLIER-BATCH-"
                        f"{row['material_spec_id']}"
                    ),
                }
                for row in collaboration.get("materials", [])
            ]
        return self.client.post(
            f"/pldp/api/sales/order-plans/{plan.pk}/supplier-confirm/",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION=authorization,
        )

    def _create_confirmed_generator_v2(self):
        plan = self._create_plan("create-generator-v2")
        detail = self._matching_detail(plan)
        payload = self._confirmation_payload(detail)
        generator_payload = next(
            item
            for item in payload["items"]
            if item["order_plan_item_id"]
            == plan.items.get(product_model=self.generator).pk
        )
        extra_payload = next(
            material
            for material in generator_payload["materials"]
            if material["material_id"] == self.generator_extra_material[0].pk
        )
        extra_payload.update(
            {
                "deleted": False,
                "material_spec_id": self.generator_extra_material[1].pk,
                "supplier_id": self.supplier.pk,
                "remark": "v2新增传感器",
            }
        )
        response = self._confirm(plan, payload)
        self.assertEqual(response.json()["code"], 0, response.json())
        return self.generator.boms.get(version="v2")

    def test_options_only_include_merchants_invited_by_current_sales_user(self):
        response = self.client.get(
            self.options_url,
            HTTP_AUTHORIZATION=self.authorization,
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(
            [item["id"] for item in data["direct_merchants"]],
            [self.merchant.pk],
        )
        self.assertTrue(data["direct_merchants"][0]["configured"])
        self.assertEqual(data["direct_merchants"][0]["brand"]["id"], self.brand.pk)
        catalog_model_ids = {
            model["id"]
            for category in data["categories"]
            for model in category["models"]
        }
        self.assertIn(self.generator.pk, catalog_model_ids)
        self.assertIn(self.air_conditioner.pk, catalog_model_ids)
        self.assertIn(self.other_brand_product.pk, catalog_model_ids)
        self.assertIn(self.unbranded_product.pk, catalog_model_ids)

    def test_order_list_sales_manager_only_sees_plans_created_by_self(self):
        own_plan = self._create_plan("sales-list-own")
        self._create_other_sales_plan()

        response = self._get_list(self.authorization)

        self.assertEqual(response.json()["code"], 0, response.json())
        data = response.json()["data"]
        self.assertEqual(data["visibility"], "created_by_me")
        self.assertEqual([item["id"] for item in data["items"]], [own_plan.pk])
        self.assertEqual(len(data["items"][0]["items"]), 2)

    def test_order_list_matching_and_production_see_all_factory_plans(self):
        own_plan = self._create_plan("factory-list-own")
        other_plan = self._create_other_sales_plan()
        external_plan = self._create_external_factory_plan()

        matching_response = self._get_list(self.matching_authorization)
        production_response = self._get_list(self.production_authorization)

        self.assertEqual(matching_response.json()["code"], 0)
        self.assertEqual(production_response.json()["code"], 0)
        self.assertSetEqual(
            {item["id"] for item in matching_response.json()["data"]["items"]},
            {own_plan.pk, other_plan.pk},
        )
        self.assertSetEqual(
            {item["id"] for item in production_response.json()["data"]["items"]},
            {own_plan.pk, other_plan.pk},
        )
        self.assertNotIn(
            external_plan.pk,
            {
                item["id"]
                for item in production_response.json()["data"]["items"]
            },
        )
        self.assertTrue(
            all(
                item["detail_link"]
                for item in matching_response.json()["data"]["items"]
            )
        )
        self.assertTrue(
            all(
                not item["detail_link"]
                for item in production_response.json()["data"]["items"]
            )
        )

    def test_sales_authorization_gates_qrcode_printing_and_stock_outbound(self):
        plan = self._create_plan("shipment-authorization-flow")
        plan.status = OrderPlan.Status.PRODUCTION_PENDING
        plan.save(update_fields=("status", "updated_at"))
        plan.items.update(status=OrderPlan.Status.PRODUCTION_PENDING)

        production_workitem = WorkItem.objects.create(
            domain="SALES",
            type="ORDER_PLAN_PRODUCTION_CONFIRM",
            biz_id=plan.plan_code,
            biz_ref=f"sales.OrderPlan:{plan.pk}",
            uniq_key=f"sales:order-plan:{plan.pk}:production-confirm",
            title="确认生产完成",
            summary="物料已到齐，等待厂家生产经理确认",
            link="/pages/sales/order_plan_list/order_plan_list",
            action_mode="ANYONE_DO",
            payload={"order_plan_id": plan.pk, "plan_code": plan.plan_code},
        )
        UserTask.objects.create(
            workitem=production_workitem,
            to_openid=self.factory_production.wx_openid,
            to_role_snapshot="factory_production",
            need_action=True,
            display_title_snapshot="确认生产完成",
        )

        production_response = self.client.post(
            f"/pldp/api/sales/order-plans/{plan.pk}/production-confirm/",
            HTTP_AUTHORIZATION=self.production_authorization,
        )
        self.assertEqual(
            production_response.json()["code"],
            0,
            production_response.json(),
        )
        plan.refresh_from_db()
        self.assertEqual(plan.status, OrderPlan.Status.PRODUCTION_COMPLETED)
        self.assertIsNone(plan.shipment_authorized_at)
        self.assertIsNone(plan.shipment_authorized_by)

        authorization_workitem = WorkItem.objects.get(
            type="ORDER_PLAN_SHIPMENT_AUTHORIZATION",
            uniq_key=f"sales:order-plan:{plan.pk}:shipment-authorization",
        )
        self.assertEqual(authorization_workitem.status, "OPEN")
        authorization_task = authorization_workitem.user_tasks.get()
        self.assertEqual(authorization_task.to_openid, self.sales_user.wx_openid)
        self.assertTrue(authorization_task.need_action)

        self.assertNotIn(
            plan.pk,
            {item["id"] for item in get_print_center_data()["orders"]},
        )
        first_item = plan.items.order_by("id").first()
        with self.assertRaisesMessage(
            OrderQRCodePrintError,
            "尚未确认发货",
        ):
            generate_item_qrcodes(item_id=first_item.pk)

        stock_before_authorization = self.client.post(
            f"/pldp/api/sales/order-plans/{plan.pk}/stock-outbound/",
            HTTP_AUTHORIZATION=self.stock_authorization,
        )
        self.assertEqual(stock_before_authorization.json()["code"], 1)
        self.assertIn(
            "尚未确认发货",
            stock_before_authorization.json()["msg"],
        )

        forbidden = self.client.post(
            f"/pldp/api/sales/order-plans/{plan.pk}/shipment-authorization/",
            HTTP_AUTHORIZATION=(
                f"Bearer {issue_access_token(self.other_sales_user)}"
            ),
        )
        self.assertEqual(forbidden.json()["code"], 1)
        self.assertIn("创建该订单", forbidden.json()["msg"])

        sales_list = self._get_list(self.authorization).json()["data"]["items"]
        sales_plan = next(row for row in sales_list if row["id"] == plan.pk)
        self.assertTrue(sales_plan["shipment_authorization"]["can_confirm"])
        self.assertEqual(
            sales_plan["shipment_authorization"]["state"],
            "PENDING",
        )

        finance = OrderPlanFinance.objects.get(order_plan=plan)
        self.assertIsNone(finance.receivable_amount)
        self.assertFalse(finance.payments.exists())

        authorization_response = self.client.post(
            f"/pldp/api/sales/order-plans/{plan.pk}/shipment-authorization/",
            HTTP_AUTHORIZATION=self.authorization,
        )
        self.assertEqual(
            authorization_response.json()["code"],
            0,
            authorization_response.json(),
        )
        self.assertEqual(
            authorization_response.json()["msg"],
            "发货已确认",
        )
        plan.refresh_from_db()
        authorization_workitem.refresh_from_db()
        self.assertEqual(plan.shipment_authorized_by, self.sales_user)
        self.assertIsNotNone(plan.shipment_authorized_at)
        self.assertEqual(authorization_workitem.status, "DONE")
        self.assertEqual(
            authorization_workitem.done_by_openid,
            self.sales_user.wx_openid,
        )
        authorization_task.refresh_from_db()
        self.assertEqual(authorization_task.state, "DONE")
        self.assertFalse(authorization_task.need_action)

        stock_notice = WorkItem.objects.get(
            type="ORDER_PLAN_SHIPMENT_AUTHORIZED_STOCK",
            uniq_key=f"sales:order-plan:{plan.pk}:shipment-authorized:stock",
        )
        self.assertSetEqual(
            set(stock_notice.user_tasks.values_list("to_openid", flat=True)),
            {self.factory_stock.wx_openid},
        )
        self.assertFalse(
            WorkItem.objects.filter(
                uniq_key=f"sales:order-plan:{plan.pk}:stock-outbound"
            ).exists()
        )
        self.assertIn(
            plan.pk,
            {item["id"] for item in get_print_center_data()["orders"]},
        )

        for item in plan.items.select_related("product_model").order_by("id"):
            bom = BOM.objects.get(
                product_model=item.product_model,
                version="v1",
                is_active=True,
            )
            OrderPlanItemBOMConfirmation.objects.update_or_create(
                order_plan_item=item,
                defaults={
                    "source_bom": bom,
                    "confirmed_bom": bom,
                    "confirmed_by": self.matching_user,
                    "has_changes": False,
                    "confirmed_at": timezone.now(),
                },
            )
            generated = generate_item_qrcodes(item_id=item.pk)
            self.assertEqual(len(generated["qrcodes"]), item.quantity)
            if item.pk == first_item.pk:
                batch = ProductionBatch.objects.get(
                    batch_no=item.order_batch_code,
                )
                gated_device = batch.devices.order_by("id").first()
                quality_case = BatchQualityCase.objects.create(
                    case_no="QT-R135-QRCODE-PRINT-GATE",
                    factory_organization=self.factory,
                    scope_type="DEVICE",
                    scope_key=f"device:{gated_device.pk}",
                    scope_snapshot={"device_id": gated_device.pk},
                    reason="验证二维码确认打印质量门禁",
                    opened_by=self.factory_manager,
                )
                BatchQualityAffectedDevice.objects.create(
                    quality_case=quality_case,
                    device=gated_device,
                    sn_snapshot=gated_device.sn,
                    production_batch_snapshot=batch.batch_no,
                    product_model_snapshot={
                        "id": gated_device.product_model_id,
                        "code": gated_device.product_model.code,
                        "name": gated_device.product_model.name,
                    },
                    location_snapshot={},
                    trace_evidence_snapshot={"is_fault": True},
                )
                action = place_temporary_hold(
                    user=self.factory_manager,
                    case_id=quality_case.pk,
                    reason="阻断二维码确认打印",
                    duration_hours=1,
                )
                with self.assertRaisesMessage(
                    OrderQRCodePrintError,
                    quality_case.case_no,
                ):
                    confirm_item_printed(item_id=item.pk)
                item.refresh_from_db()
                self.assertIsNone(item.qrcode_printed_at)
                decision = QualityGateDecision.objects.get(
                    device=gated_device,
                    operation="QRCODE_PRINT",
                    allowed=False,
                )
                self.assertEqual(decision.control_action_id, action.pk)
                self.assertEqual(
                    decision.context_snapshot["source"],
                    "ORDER_QRCODE_PRINT_CONFIRM",
                )
                revoke_temporary_hold(
                    user=self.factory_manager,
                    action_id=action.pk,
                    reason="测试完成后恢复二维码打印",
                )
            printed = confirm_item_printed(item_id=item.pk)

        self.assertTrue(printed["order_printed"])
        self.assertTrue(printed["stock_task_created"])
        stock_workitem = WorkItem.objects.get(
            type="ORDER_PLAN_STOCK_OUTBOUND",
            uniq_key=f"sales:order-plan:{plan.pk}:stock-outbound",
        )
        self.assertEqual(stock_workitem.status, "OPEN")
        self.assertEqual(
            stock_workitem.actor_openid,
            self.sales_user.wx_openid,
        )

        outbound_hold = place_temporary_hold(
            user=self.factory_manager,
            case_id=quality_case.pk,
            reason="阻断厂家库存出库",
            duration_hours=1,
        )
        blocked_outbound = self.client.post(
            f"/pldp/api/sales/order-plans/{plan.pk}/stock-outbound/",
            HTTP_AUTHORIZATION=self.stock_authorization,
        )
        self.assertEqual(
            blocked_outbound.json()["code"],
            1,
            blocked_outbound.json(),
        )
        self.assertIn(quality_case.case_no, blocked_outbound.json()["msg"])
        plan.refresh_from_db()
        stock_workitem.refresh_from_db()
        self.assertEqual(plan.status, OrderPlan.Status.PRODUCTION_COMPLETED)
        self.assertEqual(stock_workitem.status, "OPEN")
        self.assertFalse(
            LifecycleEvent.objects.filter(
                device=gated_device,
                event_type="SHIP",
                data__order_plan_id=plan.pk,
            ).exists()
        )
        outbound_decision = QualityGateDecision.objects.get(
            device=gated_device,
            operation="STOCK_OUTBOUND",
            allowed=False,
        )
        self.assertEqual(
            outbound_decision.control_action_id,
            outbound_hold.pk,
        )
        self.assertEqual(
            outbound_decision.context_snapshot["source"],
            "ORDER_PLAN_STOCK_OUTBOUND",
        )
        revoke_temporary_hold(
            user=self.factory_manager,
            action_id=outbound_hold.pk,
            reason="测试完成后恢复厂家出库",
        )

        outbound_response = self.client.post(
            f"/pldp/api/sales/order-plans/{plan.pk}/stock-outbound/",
            HTTP_AUTHORIZATION=self.stock_authorization,
        )
        self.assertEqual(
            outbound_response.json()["code"],
            0,
            outbound_response.json(),
        )
        plan.refresh_from_db()
        self.assertEqual(plan.status, OrderPlan.Status.SHIPPED)
        finance.refresh_from_db()
        self.assertIsNone(finance.receivable_amount)
        self.assertFalse(finance.payments.exists())

        repeated_authorization = self.client.post(
            f"/pldp/api/sales/order-plans/{plan.pk}/shipment-authorization/",
            HTTP_AUTHORIZATION=self.authorization,
        )
        self.assertEqual(repeated_authorization.json()["code"], 0)
        self.assertEqual(
            repeated_authorization.json()["msg"],
            "该订单已经确认发货",
        )

    def test_factory_stock_confirms_outbound_in_order_list_and_updates_stages(self):
        plan = self._create_plan("stock-order-list")
        plan.status = OrderPlan.Status.PRODUCTION_COMPLETED
        plan.shipment_authorized_at = timezone.now()
        plan.shipment_authorized_by = self.sales_user
        plan.save(update_fields=(
            "status",
            "shipment_authorized_at",
            "shipment_authorized_by",
            "updated_at",
        ))
        plan.items.update(status=OrderPlan.Status.PRODUCTION_COMPLETED)

        # 按正式打印流程准备BOM、生产批次、设备及二维码。
        for item in plan.items.select_related("product_model").order_by("id"):
            bom = BOM.objects.get(
                product_model=item.product_model,
                version="v1",
                is_active=True,
            )
            OrderPlanItemBOMConfirmation.objects.update_or_create(
                order_plan_item=item,
                defaults={
                    "source_bom": bom,
                    "confirmed_bom": bom,
                    "confirmed_by": self.matching_user,
                    "has_changes": False,
                    "confirmed_at": timezone.now(),
                },
            )
            result = generate_item_qrcodes(item_id=item.pk)
            self.assertEqual(len(result["qrcodes"]), item.quantity)

        # 模拟网页已实际打印并确认；出库任务仍由本测试显式创建。
        plan.items.update(qrcode_printed_at=timezone.now())

        workitem = WorkItem.objects.create(
            domain="SALES",
            type="ORDER_PLAN_STOCK_OUTBOUND",
            biz_id=plan.plan_code,
            biz_ref=f"sales.OrderPlan:{plan.pk}",
            uniq_key=f"sales:order-plan:{plan.pk}:stock-outbound",
            title="确认订单发货",
            summary="订单生产完成，等待厂家库管发货",
            link=(
                "/pages/sales/order_plan_list/order_plan_list"
                f"?focus_plan_id={plan.pk}"
            ),
            action_mode="ANYONE_DO",
            payload={"order_plan_id": plan.pk, "plan_code": plan.plan_code},
        )
        UserTask.objects.create(
            workitem=workitem,
            to_openid=self.factory_stock.wx_openid,
            to_role_snapshot="factory_stock",
            need_action=True,
            display_title_snapshot="确认订单发货",
        )

        before = self._get_list(self.stock_authorization).json()["data"]
        self.assertEqual(before["visibility"], "factory_all")
        plan_data = next(item for item in before["items"] if item["id"] == plan.pk)
        self.assertTrue(plan_data["stock_confirmation"]["can_confirm"])
        self.assertEqual(
            [stage["state"] for stage in plan_data["fulfillment_stages"]],
            ["DONE", "DONE", "DONE", "CURRENT", "WAITING"],
        )

        response = self.client.post(
            f"/pldp/api/sales/order-plans/{plan.pk}/stock-outbound/",
            HTTP_AUTHORIZATION=self.stock_authorization,
        )

        self.assertEqual(response.json()["code"], 0, response.json())
        plan.refresh_from_db()
        self.assertEqual(plan.status, OrderPlan.Status.SHIPPED)
        workitem.refresh_from_db()
        self.assertEqual(workitem.status, "DONE")
        after = self._get_list(self.stock_authorization).json()["data"]
        plan_data = next(item for item in after["items"] if item["id"] == plan.pk)
        self.assertFalse(plan_data["stock_confirmation"]["can_confirm"])
        self.assertEqual(
            [stage["state"] for stage in plan_data["fulfillment_stages"]],
            ["DONE", "DONE", "DONE", "DONE", "CURRENT"],
        )

    def test_logistics_documents_are_parallel_to_receipt_and_notify_merchants(self):
        plan = self._prepare_outbound_ready_plan("parallel-logistics-documents")
        outbound_response = self.client.post(
            f"/pldp/api/sales/order-plans/{plan.pk}/stock-outbound/",
            HTTP_AUTHORIZATION=self.stock_authorization,
        )
        self.assertEqual(outbound_response.json()["code"], 0, outbound_response.json())
        self.assertTrue(
            outbound_response.json()["data"]["logistics_task_created"]
        )

        receipt_workitem = WorkItem.objects.get(
            type="ORDER_PLAN_MERCHANT_RECEIPT",
            uniq_key=f"sales:order-plan:{plan.pk}:merchant-receipt",
        )
        logistics_workitem = WorkItem.objects.get(
            type="ORDER_PLAN_LOGISTICS_DOCUMENT_UPLOAD",
            uniq_key=(
                f"sales:order-plan:{plan.pk}:logistics-document-upload"
            ),
        )
        self.assertEqual(receipt_workitem.status, "OPEN")
        self.assertEqual(logistics_workitem.status, "OPEN")
        self.assertSetEqual(
            set(logistics_workitem.user_tasks.values_list("to_openid", flat=True)),
            {
                self.factory_logistics_1.wx_openid,
                self.factory_logistics_2.wx_openid,
            },
        )

        # 商家实物入库不等待物流经理上传单据。
        receipt_response = self.client.post(
            f"/pldp/api/sales/order-plans/{plan.pk}/merchant-receipt/",
            HTTP_AUTHORIZATION=self.merchant_owner_authorization,
        )
        self.assertEqual(receipt_response.json()["code"], 0, receipt_response.json())
        plan.refresh_from_db()
        logistics_workitem.refresh_from_db()
        self.assertEqual(plan.status, OrderPlan.Status.RECEIVED)
        self.assertEqual(logistics_workitem.status, "OPEN")

        upload_response = self.client.post(
            f"/pldp/api/sales/order-plans/{plan.pk}/logistics/documents/",
            data={
                "client_request_id": "logistics-doc-request-1",
                "remark": "第一车物流单",
                "document_file": SimpleUploadedFile(
                    "waybill-1.pdf",
                    b"%PDF-1.4\nlogistics-test-document",
                    content_type="application/pdf",
                ),
            },
            HTTP_AUTHORIZATION=self.factory_logistics_1_authorization,
        )
        self.assertEqual(upload_response.json()["code"], 0, upload_response.json())
        self.assertEqual(upload_response.json()["data"]["sequence_no"], 1)
        self.assertEqual(OrderPlanLogisticsDocument.objects.filter(order_plan=plan).count(), 1)

        logistics_workitem.refresh_from_db()
        self.assertEqual(logistics_workitem.status, "DONE")
        self.assertEqual(
            logistics_workitem.done_by_openid,
            self.factory_logistics_1.wx_openid,
        )
        actor_task = logistics_workitem.user_tasks.get(
            to_openid=self.factory_logistics_1.wx_openid,
        )
        peer_task = logistics_workitem.user_tasks.get(
            to_openid=self.factory_logistics_2.wx_openid,
        )
        self.assertEqual(actor_task.done_reason, "SELF_ACTION")
        self.assertEqual(peer_task.done_reason, "OTHER_ACTION")
        self.assertIn(
            self.factory_logistics_1.wx_nickname,
            peer_task.display_title_snapshot,
        )

        first_document = OrderPlanLogisticsDocument.objects.get(order_plan=plan)
        first_notice = WorkItem.objects.get(
            type="ORDER_PLAN_LOGISTICS_DOCUMENT_AVAILABLE",
            uniq_key=(
                f"sales:order-plan:{plan.pk}:logistics-document:"
                f"{first_document.pk}:available"
            ),
        )
        self.assertSetEqual(
            set(first_notice.user_tasks.values_list("to_openid", flat=True)),
            {
                self.sales_user.wx_openid,
                self.merchant_owner.wx_openid,
                self.merchant_sales.wx_openid,
                self.merchant_stock_1.wx_openid,
                self.merchant_stock_2.wx_openid,
            },
        )
        self.assertFalse(
            first_notice.user_tasks.filter(
                to_openid=self.merchant_senior_manager.wx_openid,
            ).exists()
        )
        self.assertFalse(
            first_notice.user_tasks.filter(
                to_openid=self.other_sales_user.wx_openid,
            ).exists()
        )

        sales_detail = self.client.get(
            f"/pldp/api/sales/order-plans/{plan.pk}/logistics/",
            HTTP_AUTHORIZATION=self.authorization,
        )
        self.assertEqual(sales_detail.json()["code"], 0, sales_detail.json())
        self.assertFalse(sales_detail.json()["data"]["actions"]["can_upload"])
        self.assertEqual(len(sales_detail.json()["data"]["documents"]), 1)

        unrelated_sales_detail = self.client.get(
            f"/pldp/api/sales/order-plans/{plan.pk}/logistics/",
            HTTP_AUTHORIZATION=self.other_sales_authorization,
        )
        self.assertEqual(unrelated_sales_detail.json()["code"], 1)

        merchant_detail = self.client.get(
            f"/pldp/api/sales/order-plans/{plan.pk}/logistics/",
            HTTP_AUTHORIZATION=self.merchant_sales_authorization,
        )
        self.assertEqual(merchant_detail.json()["code"], 0, merchant_detail.json())
        self.assertFalse(merchant_detail.json()["data"]["actions"]["can_upload"])
        self.assertEqual(len(merchant_detail.json()["data"]["documents"]), 1)
        preview_path = merchant_detail.json()["data"]["documents"][0][
            "preview_path"
        ]
        document_response = self.client.get(
            f"/pldp/api{preview_path}",
            HTTP_AUTHORIZATION=self.merchant_sales_authorization,
        )
        self.assertEqual(document_response.status_code, 200)
        self.assertEqual(document_response["Content-Type"], "application/pdf")
        self.assertEqual(document_response["Cache-Control"], "private, no-store")

        senior_detail = self.client.get(
            f"/pldp/api/sales/order-plans/{plan.pk}/logistics/",
            HTTP_AUTHORIZATION=self.merchant_senior_manager_authorization,
        )
        self.assertEqual(senior_detail.json()["code"], 1)

        # 首份单据完成操作任务后，物流经理仍可补充后续车辆/批次单据。
        second_upload = self.client.post(
            f"/pldp/api/sales/order-plans/{plan.pk}/logistics/documents/",
            data={
                "client_request_id": "logistics-doc-request-2",
                "remark": "第二车物流单",
                "document_file": SimpleUploadedFile(
                    "waybill-2.png",
                    b"\x89PNG\r\n\x1a\nlogistics-test-image",
                    content_type="image/png",
                ),
            },
            HTTP_AUTHORIZATION=self.factory_logistics_2_authorization,
        )
        self.assertEqual(second_upload.json()["code"], 0, second_upload.json())
        self.assertEqual(second_upload.json()["data"]["sequence_no"], 2)
        self.assertEqual(OrderPlanLogisticsDocument.objects.filter(order_plan=plan).count(), 2)
        self.assertEqual(
            WorkItem.objects.filter(
                type="ORDER_PLAN_LOGISTICS_DOCUMENT_AVAILABLE",
                biz_ref=f"sales.OrderPlan:{plan.pk}",
            ).count(),
            2,
        )

        logistics_list = self._get_list(
            self.factory_logistics_1_authorization,
        ).json()["data"]
        logistics_plan = next(
            item for item in logistics_list["items"] if item["id"] == plan.pk
        )
        self.assertEqual(logistics_plan["logistics_summary"]["document_count"], 2)
        self.assertTrue(logistics_plan["logistics_summary"]["can_upload"])

        merchant_list = self._get_list(
            self.merchant_owner_authorization,
        ).json()["data"]
        merchant_plan = next(
            item for item in merchant_list["items"] if item["id"] == plan.pk
        )
        self.assertTrue(merchant_plan["logistics_summary"]["can_view"])

    def test_merchant_receipt_and_accounting_are_shared_and_idempotent(self):
        plan = self._prepare_outbound_ready_plan("merchant-shared-receipt")
        outbound_response = self.client.post(
            f"/pldp/api/sales/order-plans/{plan.pk}/stock-outbound/",
            HTTP_AUTHORIZATION=self.stock_authorization,
        )
        self.assertEqual(outbound_response.json()["code"], 0, outbound_response.json())

        receipt_workitem = WorkItem.objects.get(
            type="ORDER_PLAN_MERCHANT_RECEIPT",
            uniq_key=f"sales:order-plan:{plan.pk}:merchant-receipt",
        )
        expected_receivers = {
            self.merchant_owner.wx_openid,
            self.merchant_senior_manager.wx_openid,
            self.merchant_sales.wx_openid,
            self.merchant_stock_1.wx_openid,
            self.merchant_stock_2.wx_openid,
        }
        actual_receivers = set(
            receipt_workitem.user_tasks.values_list("to_openid", flat=True)
        )
        self.assertSetEqual(actual_receivers, expected_receivers)
        self.assertNotIn(
            self.merchant_electrician.wx_openid,
            actual_receivers,
        )

        forbidden_response = self.client.post(
            f"/pldp/api/sales/order-plans/{plan.pk}/merchant-receipt/",
            HTTP_AUTHORIZATION=self.merchant_electrician_authorization,
        )
        self.assertEqual(forbidden_response.json()["code"], 1)

        receipt_response = self.client.post(
            f"/pldp/api/sales/order-plans/{plan.pk}/merchant-receipt/",
            HTTP_AUTHORIZATION=self.merchant_senior_manager_authorization,
        )
        self.assertEqual(receipt_response.json()["code"], 0, receipt_response.json())
        self.assertEqual(receipt_response.json()["msg"], "入库完成")

        plan.refresh_from_db()
        receipt_workitem.refresh_from_db()
        self.assertEqual(plan.status, OrderPlan.Status.RECEIVED)
        self.assertEqual(plan.received_by, self.merchant_senior_manager)
        self.assertEqual(receipt_workitem.status, "DONE")
        self.assertEqual(
            receipt_workitem.done_by_openid,
            self.merchant_senior_manager.wx_openid,
        )
        self.assertFalse(
            receipt_workitem.user_tasks.exclude(state="DONE").exists()
        )
        self.assertFalse(receipt_workitem.user_tasks.filter(need_action=True).exists())

        actor_task = receipt_workitem.user_tasks.get(
            to_openid=self.merchant_senior_manager.wx_openid
        )
        self.assertEqual(actor_task.done_reason, "SELF_ACTION")
        self.assertEqual(actor_task.display_title_snapshot, "订单入库已完成")
        for peer_task in receipt_workitem.user_tasks.exclude(pk=actor_task.pk):
            self.assertEqual(peer_task.done_reason, "OTHER_ACTION")
            self.assertIn(
                self.merchant_senior_manager.wx_nickname,
                peer_task.display_title_snapshot,
            )

        device_count = plan.total_quantity
        self.assertEqual(
            DeviceState.objects.filter(
                current_organization=self.merchant,
                status="IN_STOCK",
            ).count(),
            device_count,
        )
        self.assertEqual(
            OrganizationDevice.objects.filter(
                organization=self.merchant,
                status="IN_STOCK",
            ).count(),
            device_count,
        )
        receive_events = [
            event
            for event in LifecycleEvent.objects.filter(
                event_type="RECEIVE",
                from_organization=self.factory,
                to_organization=self.merchant,
            )
            if (event.data or {}).get("order_plan_id") == plan.pk
        ]
        self.assertEqual(len(receive_events), device_count)

        counts_before_repeat = {
            "events": LifecycleEvent.objects.count(),
            "states": DeviceState.objects.count(),
            "ownership": OrganizationDevice.objects.count(),
            "workitems": WorkItem.objects.count(),
            "tasks": UserTask.objects.count(),
        }
        repeat_receipt = self.client.post(
            f"/pldp/api/sales/order-plans/{plan.pk}/merchant-receipt/",
            HTTP_AUTHORIZATION=self.merchant_stock_1_authorization,
        )
        self.assertEqual(repeat_receipt.json()["code"], 0, repeat_receipt.json())
        self.assertEqual(repeat_receipt.json()["msg"], "该订单已入库")
        self.assertDictEqual(
            counts_before_repeat,
            {
                "events": LifecycleEvent.objects.count(),
                "states": DeviceState.objects.count(),
                "ownership": OrganizationDevice.objects.count(),
                "workitems": WorkItem.objects.count(),
                "tasks": UserTask.objects.count(),
            },
        )

        received_notice = WorkItem.objects.get(
            type="ORDER_PLAN_RECEIVED",
            uniq_key=f"sales:order-plan:{plan.pk}:received:sales",
        )
        self.assertEqual(
            list(received_notice.user_tasks.values_list("to_openid", flat=True)),
            [self.sales_user.wx_openid],
        )

        accounting_workitem = WorkItem.objects.get(
            type="ORDER_PLAN_ACCOUNTING_CONFIRM",
            uniq_key=f"sales:order-plan:{plan.pk}:accounting-confirm",
        )
        self.assertSetEqual(
            set(accounting_workitem.user_tasks.values_list("to_openid", flat=True)),
            {
                self.factory_assistant_1.wx_openid,
                self.factory_assistant_2.wx_openid,
            },
        )
        self.assertEqual(
            accounting_workitem.user_tasks.filter(need_action=True).count(),
            2,
        )

        accounting_response = self.client.post(
            f"/pldp/api/sales/order-plans/{plan.pk}/accounting-confirm/",
            HTTP_AUTHORIZATION=self.factory_assistant_1_authorization,
        )
        self.assertEqual(
            accounting_response.json()["code"], 0, accounting_response.json()
        )
        self.assertEqual(accounting_response.json()["msg"], "入账完成")
        plan.refresh_from_db()
        accounting_workitem.refresh_from_db()
        self.assertEqual(plan.accounted_by, self.factory_assistant_1)
        plan.finance.refresh_from_db()
        self.assertIsNone(plan.finance.statement_submitted_at)
        self.assertFalse(plan.finance.payments.exists())
        self.assertEqual(accounting_workitem.status, "DONE")
        assistant_actor_task = accounting_workitem.user_tasks.get(
            to_openid=self.factory_assistant_1.wx_openid
        )
        assistant_peer_task = accounting_workitem.user_tasks.get(
            to_openid=self.factory_assistant_2.wx_openid
        )
        self.assertEqual(assistant_actor_task.done_reason, "SELF_ACTION")
        self.assertEqual(assistant_peer_task.done_reason, "OTHER_ACTION")
        self.assertFalse(assistant_actor_task.need_action)
        self.assertFalse(assistant_peer_task.need_action)
        self.assertIn(
            self.factory_assistant_1.wx_nickname,
            assistant_peer_task.display_title_snapshot,
        )

        counts_before_accounting_repeat = (
            WorkItem.objects.count(),
            UserTask.objects.count(),
        )
        repeat_accounting = self.client.post(
            f"/pldp/api/sales/order-plans/{plan.pk}/accounting-confirm/",
            HTTP_AUTHORIZATION=self.factory_assistant_2_authorization,
        )
        self.assertEqual(repeat_accounting.json()["code"], 0)
        self.assertEqual(repeat_accounting.json()["msg"], "该订单已入账")
        self.assertEqual(
            counts_before_accounting_repeat,
            (WorkItem.objects.count(), UserTask.objects.count()),
        )

        senior_list = self._get_list(
            self.merchant_senior_manager_authorization
        ).json()
        self.assertEqual(senior_list["code"], 0, senior_list)
        self.assertEqual(senior_list["data"]["visibility"], "merchant_receiving")
        self.assertIn(
            plan.pk,
            {item["id"] for item in senior_list["data"]["items"]},
        )

    def test_order_list_supplier_only_sees_confirmed_own_materials(self):
        other_supplier = Organization.objects.create(
            name="其他订单供应商",
            code="ORDER-SUPPLIER-OTHER",
            organization_type=OrganizationType.objects.get(code="SUPPLIER"),
            region="四川",
        )
        other_material = Material.objects.create(
            code="ORDER-OTHER-SUPPLIER-MATERIAL",
            name="其他供应商专属物料",
            product_category=self.generator_category,
        )
        other_spec = MaterialSpec.objects.create(
            material=other_material,
            code="ORDER-OTHER-SUPPLIER-SPEC",
            name="其他供应商规格",
        )
        MaterialSpecSupplier.objects.create(
            material_spec=other_spec,
            supplier=other_supplier,
        )
        BOMItem.objects.create(
            bom=self.generator.boms.get(version="v1"),
            material=other_material,
            material_spec=other_spec,
            supplier=other_supplier,
            quantity=2,
        )
        plan = self._create_plan("supplier-list")

        pending_response = self._get_list(self.supplier_owner_authorization)
        self.assertEqual(pending_response.json()["data"]["items"], [])

        detail = self._matching_detail(plan)
        self.assertEqual(
            self._confirm(plan, self._confirmation_payload(detail)).json()["code"],
            0,
        )

        owner_response = self._get_list(self.supplier_owner_authorization)
        sales_response = self._get_list(self.supplier_sales_authorization)
        for response in (owner_response, sales_response):
            self.assertEqual(response.json()["code"], 0, response.json())
            data = response.json()["data"]
            self.assertEqual([item["id"] for item in data["items"]], [plan.pk])
            visible_material_codes = {
                material["material_code"]
                for item in data["items"][0]["items"]
                for material in item["materials"]
            }
            self.assertIn(self.generator_material[0].code, visible_material_codes)
            self.assertNotIn(other_material.code, visible_material_codes)
        self.assertEqual(
            owner_response.json()["data"]["visibility"],
            "supplier_participating",
        )
        self.assertEqual(
            sales_response.json()["data"]["visibility"],
            "supplier_responsibility",
        )

    def test_supplier_sales_managers_share_one_task_but_only_see_own_materials(self):
        second_sales = self._user_with_identity(
            "order-supplier-sales-second",
            "openid-order-supplier-sales-second",
            self.supplier,
            self.supplier_sales_role,
        )
        SupplierSalesResponsibility.objects.create(
            supplier=self.supplier,
            sales_identity=second_sales.organization_identity,
            scope_type=SupplierResponsibilityScope.MATERIAL,
            material=self.generator_material[0],
            assigned_by=self.supplier_owner,
        )
        plan = self._create_plan("supplier-shared-task")
        detail = self._matching_detail(plan)

        response = self._confirm(plan, self._confirmation_payload(detail))

        self.assertEqual(response.json()["code"], 0, response.json())
        workitem = WorkItem.objects.get(
            uniq_key=f"sales:order-plan:{plan.pk}:supplier:{self.supplier.pk}"
        )
        self.assertSetEqual(
            set(workitem.user_tasks.values_list("to_openid", flat=True)),
            {
                self.supplier_owner.wx_openid,
                self.supplier_sales.wx_openid,
                second_sales.wx_openid,
            },
        )
        self.assertEqual(workitem.payload["responsibility_snapshot_version"], 1)
        self.assertEqual(
            workitem.payload["initial_responsible_sales_identity_ids"],
            workitem.payload["responsible_sales_identity_ids"],
        )
        self.assertEqual(
            workitem.payload["initial_recipient_scopes"],
            workitem.payload["recipient_scopes"],
        )

        first_response = self._get_list(self.supplier_sales_authorization)
        second_response = self._get_list(
            f"Bearer {issue_access_token(second_sales)}"
        )
        first_codes = {
            material["material_code"]
            for item in first_response.json()["data"]["items"][0]["items"]
            for material in item["materials"]
        }
        second_codes = {
            material["material_code"]
            for item in second_response.json()["data"]["items"][0]["items"]
            for material in item["materials"]
        }
        self.assertSetEqual(first_codes, {self.air_material[0].code})
        self.assertSetEqual(second_codes, {self.generator_material[0].code})

        tasks = {
            task.to_openid: task
            for task in workitem.user_tasks.all()
        }
        self.assertIn(
            "2项供应物料",
            tasks[self.supplier_owner.wx_openid].display_subtitle_snapshot,
        )
        self.assertIn(
            "1项供应物料",
            tasks[self.supplier_sales.wx_openid].display_subtitle_snapshot,
        )
        self.assertIn(
            "1项供应物料",
            tasks[second_sales.wx_openid].display_subtitle_snapshot,
        )

        task_data = get_task_list(self.supplier_sales.wx_openid)["items"][0]
        self.assertEqual(task_data["collaboration"]["material_count"], 1)
        self.assertEqual(
            task_data["collaboration"]["scope_material_ids"],
            [self.air_material[0].pk],
        )
        self.assertIn(f"focus_plan_id={plan.pk}", task_data["link"])
        self.assertIn(f"task_id={tasks[self.supplier_sales.wx_openid].pk}", task_data["link"])

    def test_supplier_owner_confirmation_finishes_collaboration_and_sends_one_receipt(self):
        plan = self._create_plan("supplier-owner-confirmation")
        detail = self._matching_detail(plan)
        self.assertEqual(
            self._confirm(plan, self._confirmation_payload(detail)).json()["code"],
            0,
        )
        workitem = WorkItem.objects.get(
            uniq_key=f"sales:order-plan:{plan.pk}:supplier:{self.supplier.pk}"
        )
        owner_task = workitem.user_tasks.get(
            to_openid=self.supplier_owner.wx_openid
        )

        response = self._supplier_confirm(
            plan,
            self.supplier_owner_authorization,
            usertask_id=owner_task.pk,
        )

        self.assertEqual(response.json()["code"], 0, response.json())
        self.assertTrue(response.json()["data"]["completed"])
        workitem.refresh_from_db()
        self.assertEqual(workitem.status, "DONE")
        self.assertEqual(workitem.done_by_openid, self.supplier_owner.wx_openid)
        self.assertFalse(
            workitem.user_tasks.exclude(state="DONE").exists()
        )
        self.assertEqual(
            OrderPlanItemMaterialBatch.objects.filter(
                order_plan_item__plan=plan,
                supplier_material_batch__supplier=self.supplier,
            ).count(),
            len(workitem.payload["materials"]),
        )
        self.assertEqual(
            SupplierMaterialBatch.objects.filter(
                supplier=self.supplier,
            ).count(),
            len(
                {
                    row["material_spec_id"]
                    for row in workitem.payload["materials"]
                }
            ),
        )
        self.assertEqual(
            WorkItem.objects.filter(
                uniq_key=(
                    f"sales:order-plan:{plan.pk}:supplier:{self.supplier.pk}:"
                    "matching-receipt"
                ),
                type="ORDER_PLAN_SUPPLIER_CONFIRMED",
            ).count(),
            1,
        )

        repeated = self._supplier_confirm(
            plan,
            self.supplier_owner_authorization,
            usertask_id=owner_task.pk,
        )
        self.assertEqual(repeated.json()["code"], 0)
        self.assertEqual(
            WorkItem.objects.filter(
                type="ORDER_PLAN_SUPPLIER_CONFIRMED",
                biz_ref=f"sales.OrderPlan:{plan.pk}",
            ).count(),
            1,
        )

    def test_legacy_supplier_task_can_save_material_batches(self):
        plan = self._create_plan("legacy-supplier-material-batch")
        detail = self._matching_detail(plan)
        self.assertEqual(
            self._confirm(plan, self._confirmation_payload(detail)).json()["code"],
            0,
        )
        workitem = WorkItem.objects.get(
            uniq_key=f"sales:order-plan:{plan.pk}:supplier:{self.supplier.pk}"
        )
        payload = dict(workitem.payload)
        payload.pop("supplier_batch_capture_required", None)
        payload["materials"] = [
            {
                key: value
                for key, value in row.items()
                if key not in {"bom_item_id", "supplier_id"}
            }
            for row in payload["materials"]
        ]
        legacy_scopes = {}
        for openid, scope in payload["recipient_scopes"].items():
            legacy_scopes[
                f"legacy-{scope['role']}-openid"
            ] = scope
        payload["recipient_scopes"] = legacy_scopes
        workitem.payload = payload
        workitem.save(update_fields=("payload", "updated_at"))

        material_batches = []
        for order_item in plan.items.select_related(
            "bom_confirmation",
            "bom_confirmation__confirmed_bom",
        ):
            confirmation = order_item.bom_confirmation
            for bom_item in BOMItem.objects.filter(
                bom_id=confirmation.confirmed_bom_id,
                supplier=self.supplier,
            ):
                material_batches.append({
                    "order_plan_item_id": order_item.pk,
                    "bom_item_id": bom_item.pk,
                    "batch_code": f"LEGACY-{bom_item.material_spec_id}",
                })

        owner_task = workitem.user_tasks.get(
            to_openid=self.supplier_owner.wx_openid
        )
        response = self.client.post(
            f"/pldp/api/sales/order-plans/{plan.pk}/supplier-confirm/",
            data=json.dumps({
                "supplier_id": self.supplier.pk,
                "usertask_id": owner_task.pk,
                "material_batches": material_batches,
            }),
            content_type="application/json",
            HTTP_AUTHORIZATION=self.supplier_owner_authorization,
        )

        self.assertEqual(response.json()["code"], 0, response.json())
        workitem.refresh_from_db()
        rebound_scopes = workitem.payload["recipient_scopes"]
        self.assertIn(self.supplier_owner.wx_openid, rebound_scopes)
        self.assertIn(self.supplier_sales.wx_openid, rebound_scopes)
        self.assertFalse(
            any(openid.startswith("legacy-") for openid in rebound_scopes)
        )
        self.assertEqual(
            OrderPlanItemMaterialBatch.objects.filter(
                order_plan_item__plan=plan,
                supplier_material_batch__supplier=self.supplier,
            ).count(),
            len(material_batches),
        )
        self.assertFalse(workitem.payload.get("waiting_for_owner"))
        self.assertFalse(workitem.payload.get("waiting_for_batches"))

        order_item = plan.items.order_by("id").first()
        OrderPlanItem.objects.filter(pk=order_item.pk).update(
            quantity=10,
            production_quantity=3,
            return_inventory_quantity=7,
        )
        order_item.refresh_from_db()
        _validate_supplier_batch_coverage(order_item)

        first_usage = OrderPlanItemMaterialBatch.objects.filter(
            order_plan_item=order_item,
        ).select_related("supplier_material_batch").first()
        trace = get_supplier_batch_trace(
            batch_id=first_usage.supplier_material_batch_id,
        )
        traced_order = next(
            row
            for row in trace["orders"]
            if row["order_plan_item_id"] == order_item.pk
        )
        self.assertEqual(traced_order["affected_new_production_quantity"], 3)
        self.assertEqual(traced_order["excluded_return_inventory_quantity"], 7)
        self.assertEqual(traced_order["generated_device_count"], 0)
        self.assertFalse(traced_order["device_trace_ready"])

        workitem.payload = {
            **workitem.payload,
            "supplier_batch_capture_required": True,
        }
        workitem.save(update_fields=("payload", "updated_at"))
        first_usage.delete()
        with self.assertRaisesMessage(
            OrderQRCodePrintError,
            "供应商生产批次尚未覆盖最终BOM全部供应物料",
        ):
            _validate_supplier_batch_coverage(order_item)

    def test_all_responsible_supplier_sales_can_collectively_finish(self):
        second_sales = self._user_with_identity(
            "order-supplier-sales-collective",
            "openid-order-supplier-sales-collective",
            self.supplier,
            self.supplier_sales_role,
        )
        SupplierSalesResponsibility.objects.create(
            supplier=self.supplier,
            sales_identity=second_sales.organization_identity,
            scope_type=SupplierResponsibilityScope.MATERIAL,
            material=self.generator_material[0],
            assigned_by=self.supplier_owner,
        )
        plan = self._create_plan("supplier-sales-collective")
        detail = self._matching_detail(plan)
        self.assertEqual(
            self._confirm(plan, self._confirmation_payload(detail)).json()["code"],
            0,
        )
        workitem = WorkItem.objects.get(
            uniq_key=f"sales:order-plan:{plan.pk}:supplier:{self.supplier.pk}"
        )
        first_task = workitem.user_tasks.get(
            to_openid=self.supplier_sales.wx_openid
        )
        second_task = workitem.user_tasks.get(
            to_openid=second_sales.wx_openid
        )

        first_response = self._supplier_confirm(
            plan,
            self.supplier_sales_authorization,
            usertask_id=first_task.pk,
        )
        self.assertEqual(first_response.json()["code"], 0)
        self.assertFalse(first_response.json()["data"]["completed"])
        workitem.refresh_from_db()
        self.assertEqual(workitem.status, "OPEN")

        second_view = self._get_list(
            f"Bearer {issue_access_token(second_sales)}"
        ).json()["data"]
        second_plan = next(
            item for item in second_view["items"] if item["id"] == plan.pk
        )
        self.assertIn(
            self.supplier_sales.wx_nickname,
            second_plan["supplier_confirmation"]["processed_by_text"],
        )
        self.assertEqual(
            second_plan["supplier_confirmation"]["state"],
            "PENDING",
        )

        second_response = self._supplier_confirm(
            plan,
            f"Bearer {issue_access_token(second_sales)}",
            usertask_id=second_task.pk,
        )
        self.assertEqual(second_response.json()["code"], 0)
        self.assertTrue(second_response.json()["data"]["completed"])
        self.assertEqual(
            len(second_response.json()["data"]["processed_actors"]),
            2,
        )
        workitem.refresh_from_db()
        self.assertEqual(workitem.status, "DONE")
        owner_task = workitem.user_tasks.get(
            to_openid=self.supplier_owner.wx_openid
        )
        self.assertEqual(owner_task.state, "DONE")
        self.assertEqual(owner_task.done_reason, "OTHER_ACTION")

        matching_list = self._get_list(self.matching_authorization).json()["data"]
        plan_data = next(
            item for item in matching_list["items"] if item["id"] == plan.pk
        )
        self.assertEqual(
            plan_data["supplier_confirmation_summary"],
            {"total": 1, "confirmed": 1},
        )
        self.assertEqual(
            plan_data["supplier_confirmations"][0]["material_count"],
            2,
        )

    def test_unassigned_supplier_material_requires_owner_confirmation(self):
        SupplierSalesResponsibility.objects.filter(
            supplier=self.supplier,
            product_category=self.generator_category,
        ).delete()
        plan = self._create_plan("supplier-unassigned-material-confirmation")
        detail = self._matching_detail(plan)
        self.assertEqual(
            self._confirm(plan, self._confirmation_payload(detail)).json()["code"],
            0,
        )
        workitem = WorkItem.objects.get(
            uniq_key=f"sales:order-plan:{plan.pk}:supplier:{self.supplier.pk}"
        )
        sales_task = workitem.user_tasks.get(
            to_openid=self.supplier_sales.wx_openid
        )

        sales_response = self._supplier_confirm(
            plan,
            self.supplier_sales_authorization,
            usertask_id=sales_task.pk,
        )

        self.assertEqual(sales_response.json()["code"], 0)
        self.assertFalse(sales_response.json()["data"]["completed"])
        workitem.refresh_from_db()
        self.assertEqual(workitem.status, "OPEN")
        self.assertTrue(workitem.payload["waiting_for_owner"])

        owner_task = workitem.user_tasks.get(
            to_openid=self.supplier_owner.wx_openid
        )
        owner_response = self._supplier_confirm(
            plan,
            self.supplier_owner_authorization,
            usertask_id=owner_task.pk,
        )
        self.assertEqual(owner_response.json()["code"], 0)
        self.assertTrue(owner_response.json()["data"]["completed"])
        workitem.refresh_from_db()
        self.assertEqual(workitem.status, "DONE")

    def test_unassigned_supplier_sales_manager_sees_no_order_plan(self):
        unassigned_sales = self._user_with_identity(
            "order-supplier-sales-unassigned",
            "openid-order-supplier-sales-unassigned",
            self.supplier,
            self.supplier_sales_role,
        )
        plan = self._create_plan("supplier-unassigned-list")
        detail = self._matching_detail(plan)
        self.assertEqual(
            self._confirm(plan, self._confirmation_payload(detail)).json()["code"],
            0,
        )

        response = self._get_list(
            f"Bearer {issue_access_token(unassigned_sales)}"
        )

        self.assertEqual(response.json()["code"], 0, response.json())
        self.assertEqual(
            response.json()["data"]["visibility"],
            "supplier_responsibility",
        )
        self.assertEqual(response.json()["data"]["items"], [])

    def test_order_list_rejects_non_participating_role(self):
        response = self._get_list(
            f"Bearer {issue_access_token(self.factory_manager)}"
        )

        self.assertEqual(response.json()["code"], 1)
        self.assertIn("没有查看", response.json()["msg"])

    def test_sales_user_can_configure_own_direct_merchant(self):
        self.assignment.delete()

        response = self.client.post(
            self.configure_url,
            data=json.dumps(
                {
                    "merchant_id": self.merchant.pk,
                    "brand_id": self.brand.pk,
                    "region": "四川省成都市",
                }
            ),
            content_type="application/json",
            HTTP_AUTHORIZATION=self.authorization,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0, response.json())
        assignment = MerchantChannelAssignment.objects.get(relation=self.relation)
        self.assertEqual(assignment.assigned_by, self.sales_user)
        self.assertEqual(assignment.brand, self.brand)
        self.assertEqual(assignment.region, "四川省成都市")

    def test_options_include_brand_without_product_or_bom_for_channel_authorization(self):
        response = self.client.get(
            self.options_url,
            HTTP_AUTHORIZATION=self.authorization,
        )

        self.assertEqual(response.status_code, 200)
        brand = next(
            item
            for item in response.json()["data"]["brands"]
            if item["id"] == self.brand_without_product.pk
        )
        self.assertEqual(brand["name"], self.brand_without_product.name)
        catalog_model_ids = {
            model["id"]
            for category in response.json()["data"]["categories"]
            for model in category["models"]
        }
        self.assertIn(self.unbranded_product.pk, catalog_model_ids)

    def test_sales_user_can_authorize_brand_before_product_and_bom_are_added(self):
        self.assignment.delete()

        response = self.client.post(
            self.configure_url,
            data=json.dumps(
                {
                    "merchant_id": self.merchant.pk,
                    "brand_id": self.brand_without_product.pk,
                    "region": "重庆市测试区域",
                }
            ),
            content_type="application/json",
            HTTP_AUTHORIZATION=self.authorization,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0, response.json())
        assignment = MerchantChannelAssignment.objects.get(relation=self.relation)
        self.assertEqual(assignment.brand, self.brand_without_product)

    def test_create_multiple_models_generates_codes_and_matching_task(self):
        response = self.client.post(
            self.create_url,
            data=json.dumps(self._create_payload()),
            content_type="application/json",
            HTTP_AUTHORIZATION=self.authorization,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0, response.json())
        plan = OrderPlan.objects.get()
        self.assertTrue(plan.plan_code.startswith("OP-"))
        self.assertEqual(plan.total_quantity, 30)
        self.assertEqual(plan.status, OrderPlan.Status.MATCHING_PENDING)
        self.assertEqual(plan.region_snapshot, "四川省")
        self.assertEqual(plan.brand, self.brand)
        items = list(OrderPlanItem.objects.order_by("id"))
        self.assertEqual(len(items), 2)
        self.assertEqual(len({item.order_batch_code for item in items}), 2)
        self.assertEqual(len({item.lifecycle_code for item in items}), 2)
        self.assertTrue(all(item.order_batch_code.startswith("OB-") for item in items))
        self.assertTrue(all(item.lifecycle_code.startswith("PL-") for item in items))

        workitem = WorkItem.objects.get(
            type="ORDER_PLAN_MATCHING",
            biz_id=plan.plan_code,
        )
        self.assertEqual(workitem.domain, "SALES")
        self.assertEqual(workitem.status, "OPEN")
        task = UserTask.objects.get(workitem=workitem)
        self.assertEqual(task.to_openid, self.matching_user.wx_openid)
        self.assertEqual(task.to_role_snapshot, "factory_matching")
        self.assertTrue(task.need_action)
        self.assertEqual(
            workitem.link,
            (
                "/pages/sales/matching_confirm/matching_confirm"
                f"?order_plan_id={plan.pk}"
            ),
        )

    def test_create_order_partially_uses_return_inventory_and_only_matches_new_production(self):
        return_items = self._create_return_inventory(2)
        payload = {
            "merchant_id": self.merchant.pk,
            "client_request_id": "order-partial-return-inventory",
            "items": [
                {
                    "category_id": self.generator_category.pk,
                    "product_model_id": self.generator.pk,
                    "quantity": 3,
                    "use_return_inventory": True,
                }
            ],
        }

        response = self.client.post(
            self.create_url,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION=self.authorization,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0, response.json())
        plan = OrderPlan.objects.get(client_request_id=payload["client_request_id"])
        item = plan.items.get()
        self.assertEqual(plan.total_quantity, 3)
        self.assertEqual(plan.status, OrderPlan.Status.MATCHING_PENDING)
        self.assertEqual(item.quantity, 3)
        self.assertEqual(item.return_inventory_quantity, 2)
        self.assertEqual(item.production_quantity, 1)
        allocations = OrderPlanReturnInventoryAllocation.objects.filter(
            order_plan_item=item
        )
        self.assertEqual(allocations.count(), 2)
        self.assertSetEqual(
            set(allocations.values_list("inventory_item_id", flat=True)),
            {row.pk for row in return_items},
        )
        self.assertEqual(
            FactoryReturnInventoryItem.objects.filter(
                pk__in=[row.pk for row in return_items],
                stock_status=(
                    FactoryReturnInventoryItem.StockStatus.RESERVED_FOR_ORDER
                ),
            ).count(),
            2,
        )
        matching_workitem = WorkItem.objects.get(
            type="ORDER_PLAN_MATCHING",
            biz_id=plan.plan_code,
        )
        self.assertEqual(matching_workitem.payload["total_quantity"], 3)
        self.assertEqual(matching_workitem.payload["return_inventory_quantity"], 2)
        self.assertEqual(matching_workitem.payload["production_quantity"], 1)

        options_response = self.client.get(
            (
                "/pldp/api/sales/order-plans/reissue-options/"
                f"?merchant_id={self.merchant.pk}"
                f"&product_model_id={self.generator.pk}"
            ),
            HTTP_AUTHORIZATION=self.authorization,
        )
        self.assertEqual(options_response.json()["code"], 0, options_response.json())
        self.assertEqual(options_response.json()["data"]["available_count"], 0)

    def test_create_order_fully_uses_return_inventory_and_bypasses_matching(self):
        return_items = self._create_return_inventory(2)
        # 该路径本身不需要生产经理参与；即使当前厂家暂时没有有效
        # 生产经理槽位，也应能直接进入新二维码打印和库房发货。
        UserOrganization.objects.filter(
            user=self.factory_production,
            role=self.factory_production_role,
        ).update(is_active=False)
        payload = {
            "merchant_id": self.merchant.pk,
            "client_request_id": "order-full-return-inventory",
            "items": [
                {
                    "category_id": self.generator_category.pk,
                    "product_model_id": self.generator.pk,
                    "quantity": 2,
                    "use_return_inventory": True,
                }
            ],
        }

        response = self.client.post(
            self.create_url,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION=self.authorization,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0, response.json())
        plan = OrderPlan.objects.get(client_request_id=payload["client_request_id"])
        item = plan.items.get()
        self.assertEqual(plan.total_quantity, 2)
        self.assertEqual(plan.status, OrderPlan.Status.PRODUCTION_COMPLETED)
        self.assertEqual(plan.shipment_authorized_by, self.sales_user)
        self.assertIsNotNone(plan.shipment_authorized_at)
        self.assertEqual(item.quantity, 2)
        self.assertEqual(item.return_inventory_quantity, 2)
        self.assertEqual(item.production_quantity, 0)
        self.assertFalse(
            WorkItem.objects.filter(
                type="ORDER_PLAN_MATCHING",
                biz_id=plan.plan_code,
            ).exists()
        )
        self.assertFalse(
            WorkItem.objects.filter(
                type__in=(
                    "ORDER_PLAN_PRODUCTION_CONFIRM",
                    "ORDER_PLAN_CREATED_PRODUCTION_NOTICE",
                ),
                biz_id=plan.plan_code,
            ).exists()
        )
        self.assertEqual(
            FactoryReturnInventoryItem.objects.filter(
                pk__in=[row.pk for row in return_items],
                stock_status=FactoryReturnInventoryItem.StockStatus.READY_TO_SHIP,
            ).count(),
            2,
        )
        stock_workitem = WorkItem.objects.get(
            type="ORDER_PLAN_STOCK_OUTBOUND",
            biz_id=plan.plan_code,
        )
        self.assertEqual(stock_workitem.status, "OPEN")
        self.assertFalse(stock_workitem.payload["qrcodes_ready"])
        self.assertIn("待打印新二维码", stock_workitem.title)
        stock_task = UserTask.objects.get(
            workitem=stock_workitem,
            to_openid=self.factory_stock.wx_openid,
        )
        self.assertTrue(stock_task.need_action)
        print_orders = get_print_center_data()["orders"]
        self.assertIn(plan.pk, {row["id"] for row in print_orders})

        # 全量退货库存订单在创建时已经建立库管任务。打印新二维码后
        # 必须更新同一母任务为“待发货”，不能再给库管制造第二条任务。
        generated = generate_item_qrcodes(item_id=item.pk)
        self.assertEqual(len(generated["qrcodes"]), 2)
        printed = confirm_item_printed(item_id=item.pk)
        self.assertTrue(printed["order_printed"])
        self.assertTrue(printed["stock_task_created"])
        self.assertEqual(
            WorkItem.objects.filter(
                type="ORDER_PLAN_STOCK_OUTBOUND",
                biz_id=plan.plan_code,
            ).count(),
            1,
        )
        stock_workitem.refresh_from_db()
        self.assertTrue(stock_workitem.payload["qrcodes_ready"])
        self.assertIn("待一键出库", stock_workitem.title)
        self.assertTrue(OrderPlanFinance.objects.filter(order_plan=plan).exists())

    def test_repeated_client_request_is_idempotent(self):
        payload = self._create_payload("same-client-request")
        first = self.client.post(
            self.create_url,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION=self.authorization,
        )
        second = self.client.post(
            self.create_url,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION=self.authorization,
        )

        self.assertEqual(first.json()["code"], 0)
        self.assertEqual(second.json()["code"], 0)
        self.assertEqual(first.json()["data"]["id"], second.json()["data"]["id"])
        self.assertEqual(OrderPlan.objects.count(), 1)
        self.assertEqual(WorkItem.objects.filter(type="ORDER_PLAN_MATCHING").count(), 1)

    def test_cannot_create_for_other_sales_users_direct_merchant(self):
        payload = self._create_payload()
        payload["merchant_id"] = self.other_merchant.pk
        response = self.client.post(
            self.create_url,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION=self.authorization,
        )

        self.assertEqual(response.json()["code"], 1)
        self.assertIn("不是您本人邀请", response.json()["msg"])
        self.assertFalse(OrderPlan.objects.exists())

    def test_can_create_unbranded_model_under_channel_authorized_brand(self):
        payload = self._create_payload()
        payload["items"] = [
            {
                "category_id": self.generator_category.pk,
                "product_model_id": self.unbranded_product.pk,
                "quantity": 1,
            }
        ]
        response = self.client.post(
            self.create_url,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION=self.authorization,
        )

        self.assertEqual(response.json()["code"], 0, response.json())
        plan = OrderPlan.objects.get()
        self.assertEqual(plan.brand, self.brand)
        self.assertEqual(plan.items.get().product_model, self.unbranded_product)

    def test_non_sales_role_cannot_use_options(self):
        response = self.client.get(
            self.options_url,
            HTTP_AUTHORIZATION=f"Bearer {issue_access_token(self.matching_user)}",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1)
        self.assertIn("厂家销售经理", response.json()["msg"])

    def test_matching_detail_defaults_to_v1_and_marks_extra_material_deleted(self):
        plan = self._create_plan()
        detail = self._matching_detail(plan)

        self.assertEqual(detail["sales_manager"]["name"], self.sales_user.wx_nickname)
        self.assertEqual(detail["merchant"]["name"], self.merchant.name)
        self.assertEqual(detail["brand"]["name"], self.brand.name)
        generator_item = next(
            item
            for item in detail["items"]
            if item["product_model"]["id"] == self.generator.pk
        )
        self.assertEqual(generator_item["default_bom"]["version"], "v1")
        default_material = next(
            material
            for material in generator_item["materials"]
            if material["id"] == self.generator_material[0].pk
        )
        extra_material = next(
            material
            for material in generator_item["materials"]
            if material["id"] == self.generator_extra_material[0].pk
        )
        self.assertFalse(default_material["deleted"])
        self.assertTrue(extra_material["deleted"])

    def test_unchanged_confirmation_reuses_bom_and_dispatches_followup_tasks(self):
        plan = self._create_plan()
        detail = self._matching_detail(plan)
        response = self._confirm(plan, self._confirmation_payload(detail))

        self.assertEqual(response.json()["code"], 0, response.json())
        task_dispatch = response.json()["data"]["task_dispatch"]
        self.assertEqual(task_dispatch["factory_production_accounts"], 0)
        self.assertNotIn("factory_manager_accounts", task_dispatch)
        plan.refresh_from_db()
        self.assertEqual(plan.status, OrderPlan.Status.MATCHING_COMPLETED)
        self.assertEqual(BOM.objects.filter(product_model=self.generator).count(), 1)
        confirmations = list(
            OrderPlanItemBOMConfirmation.objects.select_related(
                "source_bom",
                "confirmed_bom",
            ).filter(order_plan_item__plan=plan)
        )
        self.assertEqual(len(confirmations), 2)
        self.assertTrue(all(not item.has_changes for item in confirmations))
        self.assertTrue(
            all(item.source_bom_id == item.confirmed_bom_id for item in confirmations)
        )
        matching_workitem = WorkItem.objects.get(
            uniq_key=f"sales:order-plan:{plan.pk}:matching"
        )
        self.assertEqual(matching_workitem.status, "DONE")
        self.assertTrue(
            matching_workitem.user_tasks.filter(
                to_openid=self.matching_user.wx_openid,
                state="DONE",
                done_reason="SELF_ACTION",
            ).exists()
        )
        supplier_workitem = WorkItem.objects.get(
            uniq_key=f"sales:order-plan:{plan.pk}:supplier:{self.supplier.pk}"
        )
        self.assertSetEqual(
            set(supplier_workitem.user_tasks.values_list("to_openid", flat=True)),
            {self.supplier_owner.wx_openid, self.supplier_sales.wx_openid},
        )
        self.assertFalse(
            WorkItem.objects.filter(
                uniq_key=f"sales:order-plan:{plan.pk}:production",
                user_tasks__to_openid=self.factory_production.wx_openid,
            ).exists()
        )
        self.assertFalse(
            WorkItem.objects.filter(
                uniq_key=f"sales:order-plan:{plan.pk}:production",
                user_tasks__to_openid=self.factory_manager.wx_openid,
            ).exists()
        )
        self.assertTrue(
            WorkItem.objects.filter(
                uniq_key=f"sales:order-plan:{plan.pk}:sales-notice",
                user_tasks__to_openid=self.sales_user.wx_openid,
            ).exists()
        )

    def test_legacy_confirmation_without_source_bom_id_remains_compatible(self):
        plan = self._create_plan("legacy-confirmation")
        payload = self._confirmation_payload(self._matching_detail(plan))
        for item in payload["items"]:
            item.pop("source_bom_id")

        response = self._confirm(plan, payload)

        self.assertEqual(response.json()["code"], 0, response.json())
        self.assertEqual(
            OrderPlanItemBOMConfirmation.objects.filter(
                order_plan_item__plan=plan,
                has_changes=False,
            ).count(),
            2,
        )

    def test_changed_material_selection_creates_next_bom_version_once(self):
        plan = self._create_plan()
        detail = self._matching_detail(plan)
        payload = self._confirmation_payload(detail)
        generator_payload = next(
            item
            for item in payload["items"]
            if item["order_plan_item_id"]
            == plan.items.get(product_model=self.generator).pk
        )
        extra_payload = next(
            material
            for material in generator_payload["materials"]
            if material["material_id"] == self.generator_extra_material[0].pk
        )
        extra_payload.update(
            {
                "deleted": False,
                "material_spec_id": self.generator_extra_material[1].pk,
                "supplier_id": self.supplier.pk,
                "remark": "本订单新增传感器",
            }
        )

        first = self._confirm(plan, payload)
        second = self._confirm(plan, payload)

        self.assertEqual(first.json()["code"], 0)
        self.assertEqual(second.json()["code"], 0)
        self.assertEqual(BOM.objects.filter(product_model=self.generator).count(), 2)
        confirmation = OrderPlanItemBOMConfirmation.objects.get(
            order_plan_item__plan=plan,
            order_plan_item__product_model=self.generator,
        )
        self.assertTrue(confirmation.has_changes)
        self.assertEqual(confirmation.confirmed_bom.version_no, 2)
        self.assertEqual(confirmation.confirmed_bom.version, "v2")
        self.assertTrue(
            confirmation.confirmed_bom.items.filter(
                material=self.generator_extra_material[0],
                material_spec=self.generator_extra_material[1],
                supplier=self.supplier,
            ).exists()
        )

    def test_invalid_supplier_rejects_entire_confirmation_atomically(self):
        plan = self._create_plan()
        detail = self._matching_detail(plan)
        payload = self._confirmation_payload(detail)
        generator_payload = next(
            item
            for item in payload["items"]
            if item["order_plan_item_id"]
            == plan.items.get(product_model=self.generator).pk
        )
        extra_payload = next(
            material
            for material in generator_payload["materials"]
            if material["material_id"] == self.generator_extra_material[0].pk
        )
        extra_payload.update(
            {
                "deleted": False,
                "material_spec_id": self.generator_extra_material[1].pk,
                "supplier_id": self.merchant.pk,
            }
        )

        response = self._confirm(plan, payload)

        self.assertEqual(response.json()["code"], 1)
        self.assertIn("有效关系", response.json()["msg"])
        plan.refresh_from_db()
        self.assertEqual(plan.status, OrderPlan.Status.MATCHING_PENDING)
        self.assertFalse(
            OrderPlanItemBOMConfirmation.objects.filter(
                order_plan_item__plan=plan
            ).exists()
        )
        self.assertEqual(BOM.objects.filter(product_model=self.generator).count(), 1)
        self.assertFalse(
            WorkItem.objects.filter(
                uniq_key__startswith=f"sales:order-plan:{plan.pk}:supplier:"
            ).exists()
        )

    def test_next_order_for_same_merchant_inherits_previous_confirmed_version(self):
        first_plan = self._create_plan("first-order")
        detail = self._matching_detail(first_plan)
        payload = self._confirmation_payload(detail)
        generator_payload = next(
            item
            for item in payload["items"]
            if item["order_plan_item_id"]
            == first_plan.items.get(product_model=self.generator).pk
        )
        extra_payload = next(
            material
            for material in generator_payload["materials"]
            if material["material_id"] == self.generator_extra_material[0].pk
        )
        extra_payload.update(
            {
                "deleted": False,
                "material_spec_id": self.generator_extra_material[1].pk,
                "supplier_id": self.supplier.pk,
            }
        )
        self.assertEqual(self._confirm(first_plan, payload).json()["code"], 0)

        second_plan = self._create_plan("second-order")
        second_detail = self._matching_detail(second_plan)
        generator_item = next(
            item
            for item in second_detail["items"]
            if item["product_model"]["id"] == self.generator.pk
        )
        self.assertEqual(generator_item["default_bom"]["version"], "v2")
        self.assertEqual(
            generator_item["default_bom"]["source"],
            "previous_order",
        )
        extra_material = next(
            material
            for material in generator_item["materials"]
            if material["id"] == self.generator_extra_material[0].pk
        )
        self.assertFalse(extra_material["deleted"])

    def test_can_switch_to_historical_bom_and_reuse_it_without_changes(self):
        v2 = self._create_confirmed_generator_v2()
        v1 = self.generator.boms.get(version="v1")
        plan = self._create_plan("historical-version-reuse")
        detail = self._matching_detail(plan)
        generator_item = next(
            item
            for item in detail["items"]
            if item["product_model"]["id"] == self.generator.pk
        )

        self.assertEqual(generator_item["default_bom"]["id"], v2.pk)
        self.assertEqual(
            [bom["id"] for bom in generator_item["available_boms"]],
            [v2.pk, v1.pk],
        )

        order_item = plan.items.get(product_model=self.generator)
        historical = self._bom_detail(plan, order_item, v1)
        extra_material = next(
            material
            for material in historical["materials"]
            if material["id"] == self.generator_extra_material[0].pk
        )
        self.assertEqual(historical["bom"]["id"], v1.pk)
        self.assertTrue(extra_material["deleted"])

        payload = self._confirmation_payload(detail)
        generator_payload = next(
            item
            for item in payload["items"]
            if item["order_plan_item_id"] == order_item.pk
        )
        generator_payload["source_bom_id"] = v1.pk
        generator_payload["materials"] = [
            {
                "material_id": material["id"],
                "material_spec_id": material["selected_spec_id"],
                "supplier_id": material["selected_supplier_id"],
                "deleted": material["deleted"],
                "remark": material["remark"],
            }
            for material in historical["materials"]
        ]

        response = self._confirm(plan, payload)

        self.assertEqual(response.json()["code"], 0, response.json())
        self.assertEqual(BOM.objects.filter(product_model=self.generator).count(), 2)
        confirmation = OrderPlanItemBOMConfirmation.objects.get(
            order_plan_item=order_item
        )
        self.assertEqual(confirmation.source_bom_id, v1.pk)
        self.assertEqual(confirmation.confirmed_bom_id, v1.pk)
        self.assertFalse(confirmation.has_changes)

    def test_editing_historical_bom_creates_version_after_global_maximum(self):
        self._create_confirmed_generator_v2()
        v1 = self.generator.boms.get(version="v1")
        plan = self._create_plan("historical-version-edit")
        detail = self._matching_detail(plan)
        payload = self._confirmation_payload(detail)
        order_item = plan.items.get(product_model=self.generator)
        historical = self._bom_detail(plan, order_item, v1)
        generator_payload = next(
            item
            for item in payload["items"]
            if item["order_plan_item_id"] == order_item.pk
        )
        generator_payload["source_bom_id"] = v1.pk
        generator_payload["materials"] = [
            {
                "material_id": material["id"],
                "material_spec_id": material["selected_spec_id"],
                "supplier_id": material["selected_supplier_id"],
                "deleted": material["deleted"],
                "remark": material["remark"],
            }
            for material in historical["materials"]
        ]
        extra_payload = next(
            material
            for material in generator_payload["materials"]
            if material["material_id"] == self.generator_extra_material[0].pk
        )
        extra_payload.update(
            {
                "deleted": False,
                "material_spec_id": self.generator_extra_material[1].pk,
                "supplier_id": self.supplier.pk,
                "remark": "从历史v1重新编辑",
            }
        )

        response = self._confirm(plan, payload)

        self.assertEqual(response.json()["code"], 0, response.json())
        confirmation = OrderPlanItemBOMConfirmation.objects.get(
            order_plan_item=order_item
        )
        self.assertEqual(confirmation.source_bom_id, v1.pk)
        self.assertTrue(confirmation.has_changes)
        self.assertEqual(confirmation.confirmed_bom.version_no, 3)
        self.assertEqual(confirmation.confirmed_bom.version, "v3")

    def test_cannot_select_bom_from_another_product_model(self):
        plan = self._create_plan("invalid-history-version")
        detail = self._matching_detail(plan)
        payload = self._confirmation_payload(detail)
        generator_payload = next(
            item
            for item in payload["items"]
            if item["order_plan_item_id"]
            == plan.items.get(product_model=self.generator).pk
        )
        generator_payload["source_bom_id"] = self.air_conditioner.boms.get(
            version="v1"
        ).pk

        response = self._confirm(plan, payload)

        self.assertEqual(response.json()["code"], 1)
        self.assertIn("不属于", response.json()["msg"])
        self.assertFalse(
            OrderPlanItemBOMConfirmation.objects.filter(
                order_plan_item__plan=plan
            ).exists()
        )

    def test_historical_matching_task_without_link_gets_runtime_link(self):
        plan = self._create_plan()
        workitem = WorkItem.objects.get(
            uniq_key=f"sales:order-plan:{plan.pk}:matching"
        )
        workitem.link = ""
        workitem.save(update_fields=("link", "updated_at"))

        task_data = get_task_list(self.matching_user.wx_openid)

        self.assertEqual(
            task_data["items"][0]["link"],
            (
                "/pages/sales/matching_confirm/matching_confirm"
                f"?order_plan_id={plan.pk}"
            ),
        )

    def test_sales_manager_cannot_open_matching_confirmation_page(self):
        plan = self._create_plan()
        response = self.client.get(
            f"/pldp/api/sales/order-plans/{plan.pk}/matching/",
            HTTP_AUTHORIZATION=self.authorization,
        )

        self.assertEqual(response.json()["code"], 1)
        self.assertIn("厂家配套经理", response.json()["msg"])

    @staticmethod
    def _finance_image(name):
        return SimpleUploadedFile(
            name,
            b"\x89PNG\r\n\x1a\n" + b"order-finance-test-image",
            content_type="image/png",
        )

    def test_order_finance_supports_private_roles_and_multiple_payments(self):
        plan = self._create_plan("order-finance-flow")
        finance = OrderPlanFinance.objects.get(order_plan=plan)
        self.assertIsNone(finance.receivable_amount)

        statement_workitem = WorkItem.objects.get(
            type="ORDER_PLAN_FINANCE_STATEMENT",
            uniq_key=f"sales:order-plan:{plan.pk}:finance-statement",
        )
        self.assertSetEqual(
            set(statement_workitem.user_tasks.values_list("to_openid", flat=True)),
            {
                self.factory_assistant_1.wx_openid,
                self.factory_assistant_2.wx_openid,
            },
        )

        sales_list = self._get_list(self.authorization).json()["data"]["items"]
        sales_plan = next(item for item in sales_list if item["id"] == plan.pk)
        self.assertEqual(
            sales_plan["finance_summary"]["state"],
            "WAITING_STATEMENT",
        )
        senior_list = self._get_list(
            self.merchant_senior_manager_authorization
        ).json()["data"]["items"]
        senior_plan = next(item for item in senior_list if item["id"] == plan.pk)
        self.assertNotIn("finance_summary", senior_plan)

        statement_response = self.client.post(
            f"/pldp/api/sales/order-plans/{plan.pk}/finance/statement/",
            {
                "receivable_amount": "1000.00",
                "statement_image": self._finance_image("statement.png"),
            },
            HTTP_AUTHORIZATION=self.factory_assistant_1_authorization,
        )
        self.assertEqual(statement_response.json()["code"], 0, statement_response.json())
        finance.refresh_from_db()
        self.assertEqual(finance.receivable_amount, Decimal("1000.00"))
        self.assertEqual(finance.statement_submitted_by, self.factory_assistant_1)
        statement_workitem.refresh_from_db()
        self.assertEqual(statement_workitem.status, "DONE")
        assistant_peer_task = statement_workitem.user_tasks.get(
            to_openid=self.factory_assistant_2.wx_openid
        )
        self.assertEqual(assistant_peer_task.done_reason, "OTHER_ACTION")

        merchant_payment_task = WorkItem.objects.get(
            type="ORDER_PLAN_PAYMENT_UPLOAD",
            uniq_key=f"sales:order-plan:{plan.pk}:payment-upload:1",
        )
        self.assertEqual(merchant_payment_task.status, "OPEN")
        self.assertSetEqual(
            set(merchant_payment_task.user_tasks.values_list("to_openid", flat=True)),
            {self.merchant_owner.wx_openid},
        )

        forbidden_detail = self.client.get(
            f"/pldp/api/sales/order-plans/{plan.pk}/finance/",
            HTTP_AUTHORIZATION=self.merchant_senior_manager_authorization,
        )
        self.assertEqual(forbidden_detail.json()["code"], 1)
        other_sales_detail = self.client.get(
            f"/pldp/api/sales/order-plans/{plan.pk}/finance/",
            HTTP_AUTHORIZATION=(
                f"Bearer {issue_access_token(self.other_sales_user)}"
            ),
        )
        self.assertEqual(other_sales_detail.json()["code"], 1)

        first_upload = self.client.post(
            f"/pldp/api/sales/order-plans/{plan.pk}/finance/payments/",
            {
                "client_request_id": "payment-request-1",
                "voucher_image": self._finance_image("payment-1.png"),
            },
            HTTP_AUTHORIZATION=self.merchant_owner_authorization,
        )
        self.assertEqual(first_upload.json()["code"], 0, first_upload.json())
        first_payment = OrderPlanPayment.objects.get(
            finance=finance,
            sequence_no=1,
        )
        self.assertEqual(first_payment.status, OrderPlanPayment.Status.PENDING)
        self.assertTrue(
            WorkItem.objects.filter(
                type="ORDER_PLAN_PAYMENT_UPLOADED_NOTICE",
                user_tasks__to_openid=self.sales_user.wx_openid,
            ).exists()
        )
        first_confirm_task = WorkItem.objects.get(
            uniq_key=(
                f"sales:order-plan:{plan.pk}:payment:{first_payment.pk}:confirm"
            )
        )
        self.assertSetEqual(
            set(first_confirm_task.user_tasks.values_list("to_openid", flat=True)),
            {
                self.factory_assistant_1.wx_openid,
                self.factory_assistant_2.wx_openid,
            },
        )

        first_confirm = self.client.post(
            (
                f"/pldp/api/sales/order-plans/{plan.pk}/finance/payments/"
                f"{first_payment.pk}/confirm/"
            ),
            data=json.dumps({"confirmed_amount": "400.00"}),
            content_type="application/json",
            HTTP_AUTHORIZATION=self.factory_assistant_2_authorization,
        )
        self.assertEqual(first_confirm.json()["code"], 0, first_confirm.json())
        self.assertEqual(
            first_confirm.json()["data"]["confirmed_received_amount"],
            "400.00",
        )
        self.assertEqual(
            first_confirm.json()["data"]["outstanding_amount"],
            "600.00",
        )
        self.assertTrue(
            WorkItem.objects.filter(
                uniq_key=f"sales:order-plan:{plan.pk}:payment-upload:2",
                status="OPEN",
            ).exists()
        )

        second_upload = self.client.post(
            f"/pldp/api/sales/order-plans/{plan.pk}/finance/payments/",
            {
                "client_request_id": "payment-request-2",
                "voucher_image": self._finance_image("payment-2.png"),
            },
            HTTP_AUTHORIZATION=self.merchant_owner_authorization,
        )
        self.assertEqual(second_upload.json()["code"], 0, second_upload.json())
        second_payment = OrderPlanPayment.objects.get(
            finance=finance,
            sequence_no=2,
        )
        second_confirm = self.client.post(
            (
                f"/pldp/api/sales/order-plans/{plan.pk}/finance/payments/"
                f"{second_payment.pk}/confirm/"
            ),
            data=json.dumps({"confirmed_amount": "600.00"}),
            content_type="application/json",
            HTTP_AUTHORIZATION=self.factory_assistant_1_authorization,
        )
        self.assertEqual(second_confirm.json()["code"], 0, second_confirm.json())
        self.assertEqual(second_confirm.json()["data"]["state"], "FULLY_RECEIVED")
        self.assertEqual(second_confirm.json()["data"]["outstanding_amount"], "0.00")
        self.assertFalse(
            WorkItem.objects.filter(
                uniq_key=f"sales:order-plan:{plan.pk}:payment-upload:3"
            ).exists()
        )

        # Confirming money never performs or gates the existing shipment-statistics
        # accounting action.
        plan.refresh_from_db()
        self.assertIsNone(plan.accounted_at)
        self.assertIsNone(plan.accounted_by)
