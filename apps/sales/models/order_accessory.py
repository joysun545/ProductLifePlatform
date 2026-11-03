from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


class OrderPlanAccessoryCompatibility(models.Model):
    """Manufacturer-owned catalogue mapping an exact part spec to a model."""

    factory_organization = models.ForeignKey(
        "account.Organization",
        on_delete=models.PROTECT,
        related_name="order_plan_accessory_compatibilities",
        verbose_name="厂家",
    )
    material_spec = models.ForeignKey(
        "supply_chain.MaterialSpec",
        on_delete=models.PROTECT,
        related_name="order_plan_accessory_compatibilities",
        verbose_name="配件物料规格",
    )
    product_model = models.ForeignKey(
        "product.ProductModel",
        on_delete=models.PROTECT,
        related_name="order_plan_accessory_compatibilities",
        verbose_name="适配产品型号",
    )
    is_active = models.BooleanField(default=True, verbose_name="启用")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = (
            "factory_organization__name",
            "product_model__category__code",
            "material_spec__material__code",
            "material_spec__code",
        )
        constraints = [
            models.UniqueConstraint(
                fields=("factory_organization", "material_spec", "product_model"),
                name="sales_unique_factory_accessory_model_spec",
            ),
        ]
        indexes = [
            models.Index(
                fields=("factory_organization", "is_active"),
                name="sales_acc_compat_factory_idx",
            ),
        ]
        verbose_name = "厂家配件型号适配关系"
        verbose_name_plural = "厂家配件型号适配关系"

    def clean(self):
        super().clean()
        if self.factory_organization_id and self.factory_organization.organization_type.code != "OWNER":
            raise ValidationError({"factory_organization": "适配关系必须归属厂家组织。"})

    def __str__(self):
        return (
            f"{self.factory_organization.name} · {self.material_spec} · "
            f"{self.product_model}"
        )


class OrderPlanAccessoryItem(models.Model):
    """A separately accounted material line on a device order plan."""

    class Status(models.TextChoices):
        RESERVED = "RESERVED", "已预占待发货"
        SHIPPED = "SHIPPED", "已发货待收货"
        RECEIVED = "RECEIVED", "已收货"
        RELEASED = "RELEASED", "已释放"

    order_plan = models.ForeignKey(
        "sales.OrderPlan",
        on_delete=models.PROTECT,
        related_name="accessory_items",
        verbose_name="订单计划",
    )
    category = models.ForeignKey(
        "product.ProductCategory",
        on_delete=models.PROTECT,
        related_name="order_plan_accessory_items",
        verbose_name="产品品类",
    )
    product_model = models.ForeignKey(
        "product.ProductModel",
        on_delete=models.PROTECT,
        related_name="order_plan_accessory_items",
        verbose_name="适配产品型号",
    )
    material_spec = models.ForeignKey(
        "supply_chain.MaterialSpec",
        on_delete=models.PROTECT,
        related_name="order_plan_accessory_items",
        verbose_name="配件物料规格",
    )
    quantity_milli = models.PositiveBigIntegerField(verbose_name="计划数量千分位")
    unit_snapshot = models.CharField(max_length=8, verbose_name="数量单位快照")
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.RESERVED,
        db_index=True,
        verbose_name="配件状态",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("order_plan_id", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("order_plan", "material_spec", "product_model"),
                name="sales_unique_order_accessory_spec_model",
            ),
            models.CheckConstraint(
                condition=Q(quantity_milli__gt=0),
                name="sales_order_accessory_quantity_positive",
            ),
        ]
        verbose_name = "订单配件明细"
        verbose_name_plural = "订单配件明细"

    def clean(self):
        super().clean()
        errors = {}
        if self.category_id and self.product_model_id and self.category_id != self.product_model.category_id:
            errors["category"] = "配件适配型号必须属于所选品类。"
        if errors:
            raise ValidationError(errors)


class OrderPlanShipmentAccessoryItem(models.Model):
    """Accessory quantities carried in the same physical batch as device rows."""

    shipment = models.ForeignKey(
        "sales.OrderPlanShipment",
        on_delete=models.PROTECT,
        related_name="accessory_items",
        verbose_name="发货批次",
    )
    accessory_item = models.ForeignKey(
        OrderPlanAccessoryItem,
        on_delete=models.PROTECT,
        related_name="shipment_items",
        verbose_name="订单配件明细",
    )
    quantity_milli = models.PositiveBigIntegerField(verbose_name="本批发货数量千分位")
    unit_snapshot = models.CharField(max_length=8, verbose_name="数量单位快照")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("shipment_id", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("accessory_item",),
                name="sales_unique_shipment_accessory_item",
            ),
            models.CheckConstraint(
                condition=Q(quantity_milli__gt=0),
                name="sales_shipment_accessory_quantity_positive",
            ),
        ]
        verbose_name = "发货批次配件明细"
        verbose_name_plural = "发货批次配件明细"

    def clean(self):
        super().clean()
        if (
            self.shipment_id
            and self.accessory_item_id
            and self.shipment.order_plan_id != self.accessory_item.order_plan_id
        ):
            raise ValidationError({"accessory_item": "配件明细不属于当前发货订单。"})


class OrderPlanAccessoryLotAllocation(models.Model):
    """Traceable reservation and movement of manufacturer material lots."""

    class Status(models.TextChoices):
        RESERVED = "RESERVED", "已预占"
        SHIPPED = "SHIPPED", "已出库"
        RECEIVED = "RECEIVED", "已收货"
        RELEASED = "RELEASED", "已释放"

    accessory_item = models.ForeignKey(
        OrderPlanAccessoryItem,
        on_delete=models.PROTECT,
        related_name="lot_allocations",
        verbose_name="订单配件明细",
    )
    material_lot = models.ForeignKey(
        "supply_chain.FactoryMaterialLot",
        on_delete=models.PROTECT,
        related_name="order_accessory_allocations",
        verbose_name="厂家收货批次",
    )
    shipment_item = models.ForeignKey(
        OrderPlanShipmentAccessoryItem,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="lot_allocations",
        verbose_name="发货批次配件明细",
    )
    reserved_milli = models.PositiveBigIntegerField(verbose_name="预占数量千分位")
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.RESERVED,
        db_index=True,
        verbose_name="批次流转状态",
    )
    reserved_by = models.ForeignKey(
        "account.User",
        on_delete=models.PROTECT,
        related_name="reserved_order_accessory_lots",
        verbose_name="预占操作人",
    )
    shipped_by = models.ForeignKey(
        "account.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="shipped_order_accessory_lots",
        verbose_name="出库操作人",
    )
    shipped_at = models.DateTimeField(null=True, blank=True, db_index=True)
    received_by = models.ForeignKey(
        "account.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="received_order_accessory_lots",
        verbose_name="收货操作人",
    )
    received_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("accessory_item_id", "material_lot__received_at", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("accessory_item", "material_lot"),
                name="sales_unique_order_accessory_lot",
            ),
            models.CheckConstraint(
                condition=Q(reserved_milli__gt=0),
                name="sales_accessory_lot_reservation_positive",
            ),
        ]
        verbose_name = "订单配件批次预占"
        verbose_name_plural = verbose_name
