"""V2 order-material planning records.

The original order-plan objects remain the audit source for already-created
orders.  This module deliberately adds a parallel, lot-aware material flow for
new orders instead of changing historical BOM confirmations or supplier batch
rows in place.
"""

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


class OrderPlanSalesBOMSelection(models.Model):
    """BOM frozen by the factory sales manager at V2 order creation time."""

    order_plan_item = models.OneToOneField(
        "sales.OrderPlanItem",
        on_delete=models.PROTECT,
        related_name="sales_bom_selection",
        verbose_name="订单计划明细",
    )
    source_bom = models.ForeignKey(
        "product.BOM",
        on_delete=models.PROTECT,
        related_name="sales_order_source_selections",
        verbose_name="销售选择前BOM",
    )
    frozen_bom = models.ForeignKey(
        "product.BOM",
        on_delete=models.PROTECT,
        related_name="sales_order_frozen_selections",
        verbose_name="订单冻结BOM",
    )
    selected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="selected_order_plan_boms",
        verbose_name="选择销售经理",
    )
    has_changes = models.BooleanField(default=False, verbose_name="是否创建新版本")
    selection_snapshot = models.JSONField(default=dict, blank=True, verbose_name="选择物料快照")
    selected_at = models.DateTimeField(default=timezone.now, db_index=True, verbose_name="选择时间")

    class Meta:
        ordering = ("-selected_at", "-id")
        verbose_name = "销售订单BOM冻结"
        verbose_name_plural = verbose_name

    def clean(self):
        super().clean()
        errors = {}
        model_id = self.order_plan_item.product_model_id
        if self.source_bom_id and self.source_bom.product_model_id != model_id:
            errors["source_bom"] = "来源BOM与订单产品型号不一致。"
        if self.frozen_bom_id and self.frozen_bom.product_model_id != model_id:
            errors["frozen_bom"] = "冻结BOM与订单产品型号不一致。"
        if errors:
            from django.core.exceptions import ValidationError

            raise ValidationError(errors)


class OrderPlanMaterialPlan(models.Model):
    """One V2 material-planning container for one order plan."""

    class Status(models.TextChoices):
        WAITING_WAREHOUSE_REVIEW = "WAITING_WAREHOUSE_REVIEW", "待物料库管核对"
        WAITING_SUPPLIER_DISPATCH = "WAITING_SUPPLIER_DISPATCH", "待供应配送推送"
        WAITING_RECEIPT = "WAITING_RECEIPT", "待物料入库"
        READY_FOR_PRODUCTION = "READY_FOR_PRODUCTION", "物料齐套待生产"
        PRODUCTION_IN_PROGRESS = "PRODUCTION_IN_PROGRESS", "分批生产中"
        PRODUCTION_COMPLETED = "PRODUCTION_COMPLETED", "生产已完成"
        CANCELLED = "CANCELLED", "已取消"

    order_plan = models.OneToOneField(
        "sales.OrderPlan",
        on_delete=models.PROTECT,
        related_name="material_plan",
        verbose_name="订单计划",
    )
    status = models.CharField(
        max_length=40,
        choices=Status.choices,
        default=Status.WAITING_WAREHOUSE_REVIEW,
        db_index=True,
        verbose_name="物料流程状态",
    )
    algorithm_version = models.CharField(max_length=32, default="v1", verbose_name="算法版本")
    warehouse_reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reviewed_order_material_plans",
        verbose_name="物料库管核对人",
    )
    warehouse_reviewed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    material_ready_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="released_ready_order_material_plans",
        verbose_name="物料齐套确认人",
    )
    material_ready_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "订单物料计划"
        verbose_name_plural = verbose_name


class OrderPlanMaterialPlanningRun(models.Model):
    """Explainable, immutable algorithm input/output snapshot."""

    material_plan = models.ForeignKey(
        OrderPlanMaterialPlan,
        on_delete=models.PROTECT,
        related_name="planning_runs",
        verbose_name="订单物料计划",
    )
    run_no = models.PositiveIntegerField(verbose_name="计算轮次")
    algorithm_version = models.CharField(max_length=32)
    input_snapshot = models.JSONField(default=dict, blank=True)
    output_snapshot = models.JSONField(default=dict, blank=True)
    score_snapshot = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_order_material_planning_runs",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("material_plan_id", "run_no")
        constraints = [
            models.UniqueConstraint(
                fields=("material_plan", "run_no"),
                name="sales_material_plan_unique_run_no",
            )
        ]
        verbose_name = "订单物料算法计算"
        verbose_name_plural = verbose_name


class OrderPlanProductionSubplan(models.Model):
    """At most two production slices are created by the planning service."""

    class Status(models.TextChoices):
        PLANNED = "PLANNED", "已规划待齐套"
        PRODUCTION_PENDING = "PRODUCTION_PENDING", "待生产"
        PRODUCTION_COMPLETED = "PRODUCTION_COMPLETED", "生产完成待打码"
        QRCODE_COMPLETED = "QRCODE_COMPLETED", "已打码待发货"
        SHIPPED = "SHIPPED", "已发货"
        RECEIVED = "RECEIVED", "已签收"
        CANCELLED = "CANCELLED", "已取消"

    material_plan = models.ForeignKey(
        OrderPlanMaterialPlan,
        on_delete=models.PROTECT,
        related_name="production_subplans",
        verbose_name="订单物料计划",
    )
    sequence_no = models.PositiveSmallIntegerField(verbose_name="子计划序号")
    planned_total_quantity = models.PositiveIntegerField(verbose_name="子计划计划总数")
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.PLANNED,
        db_index=True,
    )
    production_released_at = models.DateTimeField(null=True, blank=True, db_index=True)
    production_completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="completed_order_production_subplans",
    )
    production_completed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    qrcode_completed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("material_plan_id", "sequence_no")
        constraints = [
            models.UniqueConstraint(
                fields=("material_plan", "sequence_no"),
                name="sales_material_plan_unique_subplan_sequence",
            ),
            models.CheckConstraint(
                condition=Q(planned_total_quantity__gt=0),
                name="sales_subplan_quantity_positive",
            ),
        ]
        verbose_name = "订单生产子计划"
        verbose_name_plural = verbose_name


class OrderPlanProductionSubplanItem(models.Model):
    """A product-model quantity in one production subplan."""

    production_subplan = models.ForeignKey(
        OrderPlanProductionSubplan,
        on_delete=models.PROTECT,
        related_name="items",
        verbose_name="生产子计划",
    )
    order_plan_item = models.ForeignKey(
        "sales.OrderPlanItem",
        on_delete=models.PROTECT,
        related_name="production_subplan_items",
        verbose_name="订单计划明细",
    )
    quantity = models.PositiveIntegerField(verbose_name="本子计划生产数量")
    production_batch_code = models.CharField(max_length=64, unique=True, verbose_name="子计划生产批次编码")
    production_batch = models.ForeignKey(
        "manufacturing.ProductionBatch",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="order_plan_subplan_items",
        verbose_name="实际生产批次",
    )
    qrcode_generated_at = models.DateTimeField(null=True, blank=True, db_index=True)
    qrcode_printed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("production_subplan_id", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("production_subplan", "order_plan_item"),
                name="sales_subplan_unique_order_item",
            ),
            models.CheckConstraint(
                condition=Q(quantity__gt=0),
                name="sales_subplan_item_quantity_positive",
            ),
        ]
        verbose_name = "订单生产子计划产品项"
        verbose_name_plural = verbose_name


class OrderPlanMaterialRequirement(models.Model):
    """Frozen material requirement for one subplan product item and BOM line."""

    production_subplan_item = models.ForeignKey(
        OrderPlanProductionSubplanItem,
        on_delete=models.PROTECT,
        related_name="material_requirements",
        verbose_name="生产子计划产品项",
    )
    sales_bom_selection = models.ForeignKey(
        OrderPlanSalesBOMSelection,
        on_delete=models.PROTECT,
        related_name="material_requirements",
        verbose_name="销售BOM冻结",
    )
    bom_item = models.ForeignKey(
        "product.BOMItem",
        on_delete=models.PROTECT,
        related_name="order_material_requirements",
        verbose_name="冻结BOM物料行",
    )
    material = models.ForeignKey(
        "supply_chain.Material",
        on_delete=models.PROTECT,
        related_name="order_material_requirements",
    )
    material_spec = models.ForeignKey(
        "supply_chain.MaterialSpec",
        on_delete=models.PROTECT,
        related_name="order_material_requirements",
    )
    supplier = models.ForeignKey(
        "account.Organization",
        on_delete=models.PROTECT,
        related_name="order_material_requirements",
    )
    unit_snapshot = models.CharField(max_length=8, blank=True, default="")
    quantity_milli_per_device = models.PositiveBigIntegerField(verbose_name="单台需求千分位")
    required_milli = models.PositiveBigIntegerField(verbose_name="子计划总需求千分位")
    reserved_milli = models.PositiveBigIntegerField(default=0, verbose_name="已预占千分位")
    consumed_milli = models.PositiveBigIntegerField(default=0, verbose_name="已生产消耗千分位")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("production_subplan_item_id", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("production_subplan_item", "bom_item"),
                name="sales_subplan_unique_bom_requirement",
            ),
            models.CheckConstraint(
                condition=Q(quantity_milli_per_device__gt=0),
                name="sales_requirement_unit_quantity_positive",
            ),
            models.CheckConstraint(
                condition=Q(required_milli__gt=0),
                name="sales_requirement_quantity_positive",
            ),
            models.CheckConstraint(
                condition=Q(reserved_milli__lte=F("required_milli")),
                name="sales_requirement_reserved_bounded",
            ),
            models.CheckConstraint(
                condition=Q(consumed_milli__lte=F("reserved_milli")),
                name="sales_requirement_consumed_bounded",
            ),
        ]
        verbose_name = "订单物料需求"
        verbose_name_plural = verbose_name


class OrderPlanMaterialLotAllocation(models.Model):
    """Reservation of a real factory material lot to one V2 requirement."""

    class Status(models.TextChoices):
        RESERVED = "RESERVED", "已预占"
        CONSUMED = "CONSUMED", "已消耗"
        RELEASED = "RELEASED", "已释放"

    requirement = models.ForeignKey(
        OrderPlanMaterialRequirement,
        on_delete=models.PROTECT,
        related_name="lot_allocations",
        verbose_name="订单物料需求",
    )
    material_lot = models.ForeignKey(
        "supply_chain.FactoryMaterialLot",
        on_delete=models.PROTECT,
        related_name="order_plan_allocations",
        verbose_name="厂家物料批次",
    )
    reserved_milli = models.PositiveBigIntegerField(verbose_name="预占千分位")
    consumed_milli = models.PositiveBigIntegerField(default=0, verbose_name="消耗千分位")
    released_milli = models.PositiveBigIntegerField(default=0, verbose_name="释放千分位")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RESERVED, db_index=True)
    reserved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_order_material_lot_allocations",
    )
    reserved_at = models.DateTimeField(default=timezone.now, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("requirement_id", "material_lot_id")
        constraints = [
            models.UniqueConstraint(
                fields=("requirement", "material_lot"),
                name="sales_requirement_unique_lot_allocation",
            ),
            models.CheckConstraint(
                condition=Q(reserved_milli__gt=0),
                name="sales_lot_allocation_positive",
            ),
            models.CheckConstraint(
                condition=Q(consumed_milli__lte=F("reserved_milli")),
                name="sales_lot_allocation_consumed_bounded",
            ),
            models.CheckConstraint(
                condition=Q(released_milli__lte=F("reserved_milli")),
                name="sales_lot_allocation_released_bounded",
            ),
        ]
        verbose_name = "订单物料批次预占"
        verbose_name_plural = verbose_name


class FactoryMaterialSupplyPolicy(models.Model):
    """Optional normal delivery quantity for a factory/supplier/spec stock key."""

    factory = models.ForeignKey("account.Organization", on_delete=models.PROTECT, related_name="material_supply_policies")
    supplier = models.ForeignKey("account.Organization", on_delete=models.PROTECT, related_name="factory_supply_policies")
    material_spec = models.ForeignKey("supply_chain.MaterialSpec", on_delete=models.PROTECT, related_name="factory_supply_policies")
    normal_delivery_milli = models.PositiveBigIntegerField(default=0, verbose_name="常规配送量千分位")
    preferred_live_lot_count = models.PositiveSmallIntegerField(default=2, verbose_name="常态优先库存批次数")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="updated_material_supply_policies")
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("factory", "supplier", "material_spec"),
                name="sales_supply_policy_stock_key_unique",
            ),
            models.CheckConstraint(
                condition=~Q(factory=F("supplier")),
                name="sales_supply_policy_orgs_different",
            ),
        ]
        verbose_name = "厂家物料常规配送策略"
        verbose_name_plural = verbose_name


class OrderPlanSupplyDelivery(models.Model):
    """One order/supplier delivery instruction, queued for a dispatch window."""

    class Status(models.TextChoices):
        QUEUED = "QUEUED", "待定时推送"
        DISPATCHED = "DISPATCHED", "已推送待供应商确认"
        SUPPLIER_CONFIRMED = "SUPPLIER_CONFIRMED", "供应商已确认配送"
        RECEIVING = "RECEIVING", "待厂家入库"
        SATISFIED = "SATISFIED", "关联物料已齐套"
        CANCELLED = "CANCELLED", "已取消"

    material_plan = models.ForeignKey(
        OrderPlanMaterialPlan,
        on_delete=models.PROTECT,
        related_name="supply_deliveries",
        verbose_name="订单物料计划",
    )
    supplier = models.ForeignKey(
        "account.Organization",
        on_delete=models.PROTECT,
        related_name="order_plan_supply_deliveries",
    )
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.QUEUED, db_index=True)
    scheduled_for = models.DateTimeField(db_index=True, verbose_name="计划推送时间")
    dispatched_at = models.DateTimeField(null=True, blank=True, db_index=True)
    supplier_confirmed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    supplier_confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="confirmed_order_plan_supply_deliveries")
    cancelled_at = models.DateTimeField(null=True, blank=True, db_index=True)
    cancelled_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="cancelled_order_plan_supply_deliveries")
    dispatch_window_key = models.CharField(max_length=32, blank=True, default="", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("scheduled_for", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("material_plan", "supplier"),
                name="sales_material_plan_unique_supplier_delivery",
            )
        ]
        verbose_name = "订单物料配送计划"
        verbose_name_plural = verbose_name


class OrderPlanSupplyDeliveryLine(models.Model):
    delivery = models.ForeignKey(OrderPlanSupplyDelivery, on_delete=models.PROTECT, related_name="lines")
    material = models.ForeignKey("supply_chain.Material", on_delete=models.PROTECT, related_name="order_plan_delivery_lines")
    material_spec = models.ForeignKey("supply_chain.MaterialSpec", on_delete=models.PROTECT, related_name="order_plan_delivery_lines")
    requested_milli = models.PositiveBigIntegerField(verbose_name="请求配送千分位")
    confirmed_milli = models.PositiveBigIntegerField(default=0, verbose_name="供应商确认千分位")
    received_milli = models.PositiveBigIntegerField(default=0, verbose_name="已入库对应千分位")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("delivery_id", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("delivery", "material_spec"),
                name="sales_delivery_unique_material_spec",
            ),
            models.CheckConstraint(
                condition=Q(requested_milli__gt=0),
                name="sales_delivery_line_requested_positive",
            ),
        ]
        verbose_name = "订单物料配送行"
        verbose_name_plural = verbose_name


class OrderPlanSupplyDeliveryRequirement(models.Model):
    delivery_line = models.ForeignKey(OrderPlanSupplyDeliveryLine, on_delete=models.PROTECT, related_name="requirement_allocations")
    requirement = models.ForeignKey(OrderPlanMaterialRequirement, on_delete=models.PROTECT, related_name="delivery_allocations")
    planned_milli = models.PositiveBigIntegerField(verbose_name="配送满足需求千分位")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("delivery_line", "requirement"),
                name="sales_delivery_line_unique_requirement",
            ),
            models.CheckConstraint(
                condition=Q(planned_milli__gt=0),
                name="sales_delivery_requirement_positive",
            ),
        ]
        verbose_name = "订单配送需求分配"
        verbose_name_plural = verbose_name


class OrderPlanShipment(models.Model):
    """Auditable V2 shipment batch; may contain a whole or partial subplan."""

    class Status(models.TextChoices):
        PLANNED = "PLANNED", "待发货"
        AUTHORIZED = "AUTHORIZED", "已授权待出库"
        OUTBOUNDED = "OUTBOUNDED", "已出库待发货"
        SHIPPED = "SHIPPED", "已发货"
        RECEIVED = "RECEIVED", "已签收"
        CANCELLED = "CANCELLED", "已取消"

    order_plan = models.ForeignKey("sales.OrderPlan", on_delete=models.PROTECT, related_name="shipments")
    production_subplan = models.ForeignKey(OrderPlanProductionSubplan, null=True, blank=True, on_delete=models.PROTECT, related_name="shipments")
    sequence_no = models.PositiveSmallIntegerField(verbose_name="发货批次序号")
    client_request_id = models.CharField(
        max_length=64,
        verbose_name="发货批次请求标识",
        help_text="用于销售拆分发货时的幂等提交；系统生成批次也写入稳定标识。",
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PLANNED, db_index=True)
    shipment_authorized_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="authorized_order_plan_shipments_v2")
    shipment_authorized_at = models.DateTimeField(null=True, blank=True, db_index=True)
    qrcode_printed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # Logistics is deliberately stored on the V2 *shipment*, rather than on
    # the parent order.  A production subplan may be split into several urgent
    # deliveries, each with a different carrier and tracking number.
    logistics_company = models.CharField(
        max_length=128,
        blank=True,
        default="",
        verbose_name="物流公司",
    )
    tracking_no = models.CharField(
        max_length=128,
        blank=True,
        default="",
        verbose_name="物流单号",
    )
    logistics_document = models.ForeignKey(
        "sales.OrderPlanLogisticsDocument",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="shipment_batches",
        verbose_name="本批物流凭证",
    )
    logistics_confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="confirmed_order_plan_shipment_logistics_v2",
        verbose_name="物流信息确认人",
    )
    logistics_confirmed_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="物流信息确认时间",
    )
    shipped_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="shipped_order_plan_shipments_v2")
    shipped_at = models.DateTimeField(null=True, blank=True, db_index=True)
    received_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="received_order_plan_shipments_v2")
    received_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("order_plan_id", "sequence_no")
        constraints = [
            models.UniqueConstraint(fields=("order_plan", "sequence_no"), name="sales_plan_unique_shipment_sequence"),
            models.UniqueConstraint(fields=("order_plan", "client_request_id"), name="sales_plan_unique_shipment_request"),
        ]
        verbose_name = "订单发货批次"
        verbose_name_plural = verbose_name


class OrderPlanShipmentItem(models.Model):
    shipment = models.ForeignKey(OrderPlanShipment, on_delete=models.PROTECT, related_name="items")
    production_subplan_item = models.ForeignKey(OrderPlanProductionSubplanItem, null=True, blank=True, on_delete=models.PROTECT, related_name="shipment_items")
    order_plan_item = models.ForeignKey("sales.OrderPlanItem", on_delete=models.PROTECT, related_name="shipment_items")
    quantity = models.PositiveIntegerField(verbose_name="本批发货数量")

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0), name="sales_shipment_item_quantity_positive"),
        ]
        verbose_name = "订单发货批次明细"
        verbose_name_plural = verbose_name


class OrderPlanShipmentDevice(models.Model):
    """The exact physical device included in a V2 shipment item.

    Quantity-only shipment rows are not sufficient once a customer asks for a
    partial shipment.  This mapping is created before QR print, then becomes
    the sole source for outbound and merchant receipt.  A return/recall item
    keeps its original allocation link, while a newly manufactured item is
    resolved from ``production_subplan_item.production_batch``.
    """

    shipment = models.ForeignKey(
        OrderPlanShipment,
        on_delete=models.PROTECT,
        related_name="device_allocations",
        verbose_name="发货批次",
    )
    shipment_item = models.ForeignKey(
        OrderPlanShipmentItem,
        on_delete=models.PROTECT,
        related_name="device_allocations",
        verbose_name="发货批次明细",
    )
    order_plan = models.ForeignKey(
        "sales.OrderPlan",
        on_delete=models.PROTECT,
        related_name="shipment_device_allocations",
        verbose_name="订单计划",
    )
    device = models.ForeignKey(
        "lifecycle.Device",
        on_delete=models.PROTECT,
        related_name="order_plan_v2_shipment_allocations",
        verbose_name="设备",
    )
    return_inventory_allocation = models.OneToOneField(
        "sales.OrderPlanReturnInventoryAllocation",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="v2_shipment_device_allocation",
        verbose_name="成品库存调用分配",
    )
    qrcode_printed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("shipment_id", "shipment_item_id", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("order_plan", "device"),
                name="sales_plan_unique_shipment_device",
            ),
        ]
        verbose_name = "订单发货设备分配"
        verbose_name_plural = verbose_name


class FactoryFinishedGoodsInventoryItem(models.Model):
    """A factory-owned finished device made available for a future V2 order.

    This is intentionally separate from ``FactoryReturnInventoryItem`` and
    recall inventory: a cancelled order can have real, newly manufactured
    devices even though no return transfer or recall disposition exists.  The
    source plan/subplan remains an audit fact, while the stock record itself
    carries no customer or sales-manager ownership.
    """

    class StockStatus(models.TextChoices):
        PENDING_REISSUE = "PENDING_REISSUE", "成品库存待调用"
        RESERVED_FOR_ORDER = "RESERVED_FOR_ORDER", "已被订单占用"
        READY_TO_SHIP = "READY_TO_SHIP", "待重新发货"
        REISSUED = "REISSUED", "已重新流转"

    factory_organization = models.ForeignKey(
        "account.Organization",
        on_delete=models.PROTECT,
        related_name="finished_goods_inventory_items",
        verbose_name="厂家",
    )
    device = models.OneToOneField(
        "lifecycle.Device",
        on_delete=models.PROTECT,
        related_name="factory_finished_goods_inventory_item",
        verbose_name="成品设备",
    )
    source_order_plan = models.ForeignKey(
        "sales.OrderPlan",
        on_delete=models.PROTECT,
        related_name="cancelled_finished_goods_inventory_items",
        verbose_name="来源订单计划",
    )
    source_subplan_item = models.ForeignKey(
        OrderPlanProductionSubplanItem,
        on_delete=models.PROTECT,
        related_name="finished_goods_inventory_items",
        verbose_name="来源生产子计划产品项",
    )
    source_product_model = models.ForeignKey(
        "product.ProductModel",
        on_delete=models.PROTECT,
        related_name="factory_finished_goods_inventory_items",
        verbose_name="产品型号",
    )
    source_production_batch = models.ForeignKey(
        "manufacturing.ProductionBatch",
        on_delete=models.PROTECT,
        related_name="factory_finished_goods_inventory_items",
        verbose_name="生产批次",
    )
    source_bom = models.ForeignKey(
        "product.BOM",
        on_delete=models.PROTECT,
        related_name="factory_finished_goods_inventory_items",
        verbose_name="实际BOM",
    )
    stock_status = models.CharField(
        max_length=32,
        choices=StockStatus.choices,
        default=StockStatus.PENDING_REISSUE,
        db_index=True,
        verbose_name="成品库存状态",
    )
    stored_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="stored_finished_goods_inventory_items",
        verbose_name="入成品库存确认人",
    )
    stored_at = models.DateTimeField(default=timezone.now, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("stored_at", "id")
        indexes = [
            models.Index(
                fields=("factory_organization", "source_product_model", "stock_status"),
                name="sales_fg_available_idx",
            ),
        ]
        verbose_name = "厂家成品库存设备"
        verbose_name_plural = verbose_name


class OrderPlanCancellationAudit(models.Model):
    """Explicit cancellation decision and the offline assembly fact."""

    class Status(models.TextChoices):
        CANCELLED = "CANCELLED", "已取消"
        WAITING_PRODUCTION_COMPLETION = "WAITING_PRODUCTION_COMPLETION", "待生产完成后取消"
        WAITING_FINISHED_STOCK = "WAITING_FINISHED_STOCK", "待转入成品库存后取消"
        REJECTED_AFTER_SHIPMENT = "REJECTED_AFTER_SHIPMENT", "已发货不可取消"

    order_plan = models.OneToOneField("sales.OrderPlan", on_delete=models.PROTECT, related_name="cancellation_audit")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="requested_order_plan_cancellations")
    status = models.CharField(max_length=40, choices=Status.choices, db_index=True)
    assembly_started_offline = models.BooleanField(default=False, verbose_name="线下已开始装配")
    reason = models.TextField(blank=True, default="")
    released_reservations_at = models.DateTimeField(null=True, blank=True)
    requested_at = models.DateTimeField(default=timezone.now, db_index=True)
    resolved_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "订单计划取消审计"
        verbose_name_plural = verbose_name
