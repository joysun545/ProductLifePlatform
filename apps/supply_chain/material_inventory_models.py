"""Physical material receipts. Kept distinct from suppliers' production batch labels.

Quantities are integer thousandths of the stock item's fixed unit. This avoids
floating point balance drift on SQLite. Every receipt creates a new factory lot;
there is deliberately no maximum number of lots and no append-to-old-lot action.
"""
from django.conf import settings
from django.db import models
from django.db.models import F, Q


class FactoryMaterialStock(models.Model):
    class Unit(models.TextChoices):
        PIECE = "件", "件"
        EACH = "个", "个"
        SET = "套", "套"
        KG = "千克", "千克"
        METER = "米", "米"
        LITER = "升", "升"

    factory = models.ForeignKey("account.Organization", on_delete=models.PROTECT, related_name="material_stocks")
    supplier = models.ForeignKey("account.Organization", on_delete=models.PROTECT, related_name="factory_material_stocks")
    material_spec = models.ForeignKey("supply_chain.MaterialSpec", on_delete=models.PROTECT, related_name="factory_stocks")
    unit = models.CharField(max_length=8, choices=Unit.choices)
    revision = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "厂家物料库存"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(fields=("factory", "material_spec", "supplier"), name="material_stock_scope_unique"),
            models.CheckConstraint(condition=~Q(factory=F("supplier")), name="material_stock_different_orgs"),
        ]


class FactoryMaterialLot(models.Model):
    stock = models.ForeignKey(FactoryMaterialStock, on_delete=models.PROTECT, related_name="lots")
    batch_code = models.CharField(max_length=48, unique=True)
    supplier_batch_code = models.CharField(max_length=128, blank=True, default="")
    received_milli = models.PositiveBigIntegerField()
    on_hand_milli = models.PositiveBigIntegerField()
    reserved_milli = models.PositiveBigIntegerField(default=0)
    received_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="material_receipts")
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "厂家物料收货批次"
        verbose_name_plural = verbose_name
        ordering = ("received_at", "pk")
        constraints = [
            models.CheckConstraint(condition=Q(received_milli__gt=0), name="material_lot_received_positive"),
            models.CheckConstraint(condition=Q(on_hand_milli__lte=F("received_milli")), name="material_lot_balance_bounded"),
            models.CheckConstraint(condition=Q(reserved_milli__lte=F("on_hand_milli")), name="material_lot_reserved_bounded"),
        ]


class OrganizationMaterialStock(models.Model):
    """Accessory stock received by a downstream merchant from its factory."""

    organization = models.ForeignKey(
        "account.Organization",
        on_delete=models.PROTECT,
        related_name="organization_material_stocks",
        verbose_name="库存组织",
    )
    source_factory = models.ForeignKey(
        "account.Organization",
        on_delete=models.PROTECT,
        related_name="downstream_material_stocks",
        verbose_name="来源厂家",
    )
    supplier = models.ForeignKey(
        "account.Organization",
        on_delete=models.PROTECT,
        related_name="downstream_material_stock_sources",
        verbose_name="原供应商",
    )
    material_spec = models.ForeignKey(
        "supply_chain.MaterialSpec",
        on_delete=models.PROTECT,
        related_name="organization_stocks",
        verbose_name="物料规格",
    )
    unit = models.CharField(max_length=8, verbose_name="库存单位")
    revision = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("organization_id", "material_spec_id", "supplier_id")
        constraints = [
            models.UniqueConstraint(
                fields=("organization", "source_factory", "supplier", "material_spec"),
                name="supply_org_material_stock_scope_unique",
            ),
            models.CheckConstraint(
                condition=~Q(organization=F("source_factory")),
                name="supply_org_material_stock_distinct_factory",
            ),
        ]
        verbose_name = "组织配件库存"
        verbose_name_plural = verbose_name


class OrganizationMaterialLot(models.Model):
    """A merchant-side lot preserving its manufacturer receipt-lot trace."""

    stock = models.ForeignKey(
        OrganizationMaterialStock,
        on_delete=models.PROTECT,
        related_name="lots",
        verbose_name="组织配件库存",
    )
    source_factory_lot = models.ForeignKey(
        FactoryMaterialLot,
        on_delete=models.PROTECT,
        related_name="downstream_lots",
        verbose_name="厂家原始收货批次",
    )
    source_reference = models.CharField(max_length=128, unique=True, verbose_name="来源流转标识")
    source_order_plan_id = models.PositiveBigIntegerField(db_index=True, verbose_name="来源订单计划ID")
    source_shipment_id = models.PositiveBigIntegerField(db_index=True, verbose_name="来源发货批次ID")
    source_accessory_item_id = models.PositiveBigIntegerField(db_index=True, verbose_name="来源订单配件ID")
    source_batch_code_snapshot = models.CharField(max_length=48, verbose_name="厂家收货批次号快照")
    received_milli = models.PositiveBigIntegerField(verbose_name="收货数量千分位")
    on_hand_milli = models.PositiveBigIntegerField(verbose_name="现存数量千分位")
    reserved_milli = models.PositiveBigIntegerField(default=0, verbose_name="预占数量千分位")
    received_by = models.ForeignKey(
        "account.User",
        on_delete=models.PROTECT,
        related_name="received_organization_material_lots",
        verbose_name="收货操作人",
    )
    received_at = models.DateTimeField(db_index=True, verbose_name="收货时间")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("received_at", "id")
        constraints = [
            models.CheckConstraint(condition=Q(received_milli__gt=0), name="supply_org_material_lot_received_positive"),
            models.CheckConstraint(condition=Q(on_hand_milli__lte=F("received_milli")), name="supply_org_material_lot_balance_bounded"),
            models.CheckConstraint(condition=Q(reserved_milli__lte=F("on_hand_milli")), name="supply_org_material_lot_reserved_bounded"),
        ]
        verbose_name = "组织配件库存批次"
        verbose_name_plural = verbose_name


class MaterialWarrantyCase(models.Model):
    """Faulty pieces are isolated from normal stock, including offline returns."""
    stock = models.ForeignKey(FactoryMaterialStock, on_delete=models.PROTECT, related_name="warranty_cases")
    case_no = models.CharField(max_length=48, unique=True)
    source_lot = models.ForeignKey(FactoryMaterialLot, null=True, blank=True, on_delete=models.PROTECT, related_name="fault_cases")
    registered_milli = models.PositiveBigIntegerField()
    sent_milli = models.PositiveBigIntegerField(default=0)
    replaced_milli = models.PositiveBigIntegerField(default=0)
    registered_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="material_warranty_cases")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "物料三包故障件记录"
        verbose_name_plural = verbose_name
        ordering = ("-pk",)
        constraints = [
            models.CheckConstraint(condition=Q(registered_milli__gt=0), name="material_fault_registered_positive"),
            models.CheckConstraint(condition=Q(sent_milli__lte=F("registered_milli")), name="material_fault_sent_bounded"),
            models.CheckConstraint(condition=Q(replaced_milli__lte=F("sent_milli")), name="material_fault_replaced_bounded"),
        ]


class MaterialWarehouseDocument(models.Model):
    class Kind(models.TextChoices):
        RECEIPT = "RECEIPT", "正常入库"
        SUPPLIER_RETURN = "SUPPLIER_RETURN", "退给供应商"
        WARRANTY_REGISTER = "WARRANTY_REGISTER", "三包故障件登记"
        WARRANTY_SEND = "WARRANTY_SEND", "三包退给供应商"
        WARRANTY_REPLACE = "WARRANTY_REPLACE", "三包换回件入库"
        ORDER_SHIPMENT = "ORDER_SHIPMENT", "订单配件出库"

    factory = models.ForeignKey("account.Organization", on_delete=models.PROTECT, related_name="material_documents")
    stock = models.ForeignKey(FactoryMaterialStock, on_delete=models.PROTECT, related_name="documents")
    document_no = models.CharField(max_length=48, unique=True)
    request_id = models.UUIDField()
    request_hash = models.CharField(max_length=64)
    kind = models.CharField(max_length=24, choices=Kind.choices)
    quantity_milli = models.PositiveBigIntegerField()
    lot = models.ForeignKey(FactoryMaterialLot, null=True, blank=True, on_delete=models.PROTECT, related_name="documents")
    warranty_case = models.ForeignKey(MaterialWarrantyCase, null=True, blank=True, on_delete=models.PROTECT, related_name="documents")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="material_documents")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "物料出入库流水"
        verbose_name_plural = verbose_name
        ordering = ("-pk",)
        constraints = [
            models.UniqueConstraint(fields=("factory", "request_id"), name="material_document_request_unique"),
            models.CheckConstraint(condition=Q(quantity_milli__gt=0), name="material_document_qty_positive"),
        ]
