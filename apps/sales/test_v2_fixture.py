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
from sales.services.supplier_batch_trace_service import get_supplier_batch_trace
from sales.models import (
    OrderPlan,
    OrderPlanItem,
    OrderPlanItemBOMConfirmation,
    OrderPlanItemMaterialBatch,
    OrderPlanFinance,
    OrderPlanLogisticsDocument,
    OrderPlanMaterialPlan,
    OrderPlanPayment,
    OrderPlanSupplyDelivery,
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


class V2OrderPlanFixture(TestCase):
    list_url = "/pldp/api/sales/order-plans/"
    options_url = "/pldp/api/sales/order-plans/options/"
    configure_url = "/pldp/api/sales/direct-merchants/configure/"

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
        self.factory_material_stock_role = OrganizationRole.objects.get(
            code="factory_material_stock"
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
        self.factory_chief_engineer_role = OrganizationRole.objects.get(
            code="factory_chief_engineer"
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
        self.factory_material_stock = self._user_with_identity(
            "order-factory-material-stock",
            "openid-order-factory-material-stock",
            self.factory,
            self.factory_material_stock_role,
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
        self.factory_chief_engineer = self._user_with_identity(
            "order-factory-chief-engineer",
            "openid-order-factory-chief-engineer",
            self.factory,
            self.factory_chief_engineer_role,
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
        self.generator = self._product_with_bom(
            "ORDER-GEN-5000",
            "5000W驻车发电机",
            self.brand,
            self.generator_category,
        )
        self.air_conditioner = self._product_with_bom(
            "ORDER-AIR-24",
            "24V驻车空调",
            self.brand,
            self.air_category,
        )
        self.other_brand_product = self._product_with_bom(
            "ORDER-OTHER-MODEL",
            "其他品牌产品",
            self.other_brand,
            self.generator_category,
        )
        self.unbranded_product = self._product_with_bom(
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
            bom=self.generator.boms.get(version="active-v2"),
            material=self.generator_material[0],
            material_spec=self.generator_material[1],
            supplier=self.supplier,
            quantity=1,
            remark="发电机默认控制器",
        )
        BOMItem.objects.create(
            bom=self.air_conditioner.boms.get(version="active-v2"),
            material=self.air_material[0],
            material_spec=self.air_material[1],
            supplier=self.supplier,
            quantity=1,
        )
        for material in (
            self.generator_material[0], self.generator_extra_material[0], self.air_material[0]
        ):
            SupplierSalesResponsibility.objects.create(
                supplier=self.supplier,
                sales_identity=self.supplier_sales.organization_identity,
                scope_type=SupplierResponsibilityScope.MATERIAL,
                material=material,
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
        self.material_stock_authorization = (
            f"Bearer {issue_access_token(self.factory_material_stock)}"
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

    def _product_with_bom(self, code, name, brand, category):
        product_model = ProductModel.objects.create(
            code=code,
            name=name,
            category=category,
        )
        BOM.objects.create(
            product_model=product_model,
            version_no=1,
            version="active-v2",
            is_active=True,
        )
        return product_model

    def _material_with_supplier(self, code, name, category):
        material = Material.objects.create(
            code=code,
            name=name,
        )
        material.product_categories.add(category)
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
        bom = product_model.boms.get(version="active-v2")
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
