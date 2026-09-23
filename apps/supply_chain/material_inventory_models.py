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
