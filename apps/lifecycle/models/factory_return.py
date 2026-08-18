from django.conf import settings
from django.db import models

from lifecycle.private_storage import goods_transfer_private_storage


class FactoryReturnProcess(models.Model):
    """退回厂家后的取货、下账和分类入库主流程。"""

    class Phase(models.TextChoices):
        WAITING_PICKUP = "WAITING_PICKUP", "待厂家物流取回"
        WAITING_POSTING = "WAITING_POSTING", "待销售助理下账入库"
        POSTED = "POSTED", "已分类入库"

    transfer = models.OneToOneField(
        "lifecycle.GoodsTransfer",
        on_delete=models.PROTECT,
        related_name="factory_return_process",
        verbose_name="退厂商品流转单",
    )
    phase = models.CharField(
        max_length=24,
        choices=Phase.choices,
        default=Phase.WAITING_PICKUP,
        db_index=True,
        verbose_name="流程阶段",
    )
    account_case = models.ForeignKey(
        "lifecycle.GoodsTransferAccountCase",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="factory_return_processes",
        verbose_name="关联退货冲抵批次",
    )
    picked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="picked_factory_returns",
        verbose_name="取回确认人",
    )
    picked_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="取回时间",
    )
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="posted_factory_returns",
        verbose_name="下账入库人",
    )
    posted_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="下账入库时间",
    )
    posting_remark = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="入库说明",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "厂家退货处理流程"
        verbose_name_plural = "厂家退货处理流程"
        ordering = ("-created_at", "-id")

    def __str__(self):
        return f"{self.transfer.flow_no}-{self.get_phase_display()}"


class FactoryReturnDocument(models.Model):
    """退厂退货最终提交或后续补传的私有货品/物流单据。"""

    class DocumentKind(models.TextChoices):
        LOGISTICS = "LOGISTICS", "物流单据"
        GOODS_LIST = "GOODS_LIST", "货品清单"

    class FileType(models.TextChoices):
        IMAGE = "IMAGE", "图片"
        PDF = "PDF", "PDF"

    process = models.ForeignKey(
        FactoryReturnProcess,
        on_delete=models.PROTECT,
        related_name="documents",
        verbose_name="厂家退货处理流程",
    )
    document_kind = models.CharField(
        max_length=16,
        choices=DocumentKind.choices,
        default=DocumentKind.LOGISTICS,
        db_index=True,
        verbose_name="单据类型",
    )
    sequence_no = models.PositiveIntegerField(verbose_name="同类型单据序号")
    client_request_id = models.CharField(
        max_length=64,
        verbose_name="客户端请求标识",
    )
    document_file = models.FileField(
        storage=goods_transfer_private_storage,
        upload_to="factory_return_documents/%Y/%m/",
        verbose_name="退厂退货单据",
    )
    original_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="原文件名",
    )
    file_type = models.CharField(
        max_length=12,
        choices=FileType.choices,
        verbose_name="文件类型",
    )
    content_type = models.CharField(
        max_length=100,
        blank=True,
        default="",
        verbose_name="文件内容类型",
    )
    file_size = models.PositiveBigIntegerField(default=0, verbose_name="文件大小")
    remark = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="备注",
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="uploaded_factory_return_documents",
        verbose_name="上传人",
    )
    uploaded_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        verbose_name="上传时间",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "厂家退货单据"
        verbose_name_plural = "厂家退货单据"
        ordering = ("document_kind", "sequence_no", "id")
        constraints = (
            models.UniqueConstraint(
                fields=("process", "document_kind", "sequence_no"),
                name="lifecycle_fr_document_sequence",
            ),
            models.UniqueConstraint(
                fields=("process", "document_kind", "client_request_id"),
                name="lifecycle_fr_document_request",
            ),
        )

    def __str__(self):
        return (
            f"{self.process.transfer.flow_no}-"
            f"{self.get_document_kind_display()}{self.sequence_no}"
        )


class FactoryReturnInventoryItem(models.Model):
    """退厂设备逐台分类库存；保留原型号、批次和BOM快照外键。"""

    class StockStatus(models.TextChoices):
        WAITING_POSTING = "WAITING_POSTING", "待分类入库"
        PENDING_DISASSEMBLY = "PENDING_DISASSEMBLY", "入库待拆机"
        PENDING_REISSUE = "PENDING_REISSUE", "入库待流转"
        PENDING_REPAIR = "PENDING_REPAIR", "入库待修复"
        DISASSEMBLING = "DISASSEMBLING", "拆机处理中"
        REPAIRING = "REPAIRING", "修复处理中"
        RESERVED_FOR_ORDER = "RESERVED_FOR_ORDER", "已被订单占用"
        READY_TO_SHIP = "READY_TO_SHIP", "待重新发货"
        REISSUED = "REISSUED", "已重新流转"
        LIFECYCLE_ENDED = "LIFECYCLE_ENDED", "生命周期已结束"

    class ProductionStatus(models.TextChoices):
        NOT_REQUIRED = "NOT_REQUIRED", "无需生产处置"
        PENDING = "PENDING", "待生产处置"
        WAITING_MATCHING = "WAITING_MATCHING", "等待配套处置"
        READY_TO_COMPLETE = "READY_TO_COMPLETE", "可完成修复"
        COMPLETED = "COMPLETED", "生产处置已完成"

    class MatchingStatus(models.TextChoices):
        NOT_REQUIRED = "NOT_REQUIRED", "无需配套处置"
        BLOCKED = "BLOCKED", "等待生产结果"
        PENDING = "PENDING", "待配套处置"
        COMPLETED = "COMPLETED", "配套处置已完成"

    process = models.ForeignKey(
        FactoryReturnProcess,
        on_delete=models.PROTECT,
        related_name="inventory_items",
        verbose_name="退厂处理流程",
    )
    transfer_item = models.OneToOneField(
        "lifecycle.GoodsTransferItem",
        on_delete=models.PROTECT,
        related_name="factory_return_inventory_item",
        verbose_name="退货逐台明细",
    )
    source_product_model = models.ForeignKey(
        "product.ProductModel",
        on_delete=models.PROTECT,
        related_name="factory_return_inventory_items",
        verbose_name="原产品型号",
    )
    source_production_batch = models.ForeignKey(
        "manufacturing.ProductionBatch",
        on_delete=models.PROTECT,
        related_name="factory_return_inventory_items",
        verbose_name="原生产批次",
    )
    source_bom = models.ForeignKey(
        "product.BOM",
        on_delete=models.PROTECT,
        related_name="factory_return_inventory_items",
        verbose_name="原BOM",
    )
    stock_status = models.CharField(
        max_length=32,
        choices=StockStatus.choices,
        default=StockStatus.WAITING_POSTING,
        db_index=True,
        verbose_name="退厂库存状态",
    )
    classified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="classified_factory_return_inventory_items",
        verbose_name="分类人",
    )
    classified_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="分类时间",
    )
    remark = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="分类说明",
    )
    production_status = models.CharField(
        max_length=24,
        choices=ProductionStatus.choices,
        default=ProductionStatus.NOT_REQUIRED,
        db_index=True,
        verbose_name="生产处置状态",
    )
    matching_status = models.CharField(
        max_length=24,
        choices=MatchingStatus.choices,
        default=MatchingStatus.NOT_REQUIRED,
        db_index=True,
        verbose_name="配套处置状态",
    )
    needs_replacement_material = models.BooleanField(
        null=True,
        blank=True,
        verbose_name="是否需要更换物料",
    )
    production_remark = models.CharField(
        max_length=1000,
        blank=True,
        default="",
        verbose_name="生产诊断或修复说明",
    )
    material_requirement = models.CharField(
        max_length=1000,
        blank=True,
        default="",
        verbose_name="更换物料需求",
    )
    material_plan = models.CharField(
        max_length=1000,
        blank=True,
        default="",
        verbose_name="配套物料方案",
    )
    disassembly_result = models.CharField(
        max_length=1000,
        blank=True,
        default="",
        verbose_name="拆机结果和物料清单",
    )
    has_warranty_material = models.BooleanField(
        null=True,
        blank=True,
        verbose_name="是否包含三包物料",
    )
    warranty_disposition = models.CharField(
        max_length=1000,
        blank=True,
        default="",
        verbose_name="三包物料处置意见",
    )
    matching_remark = models.CharField(
        max_length=1000,
        blank=True,
        default="",
        verbose_name="配套处置说明",
    )
    production_processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="processed_factory_return_production_items",
        verbose_name="生产处置人",
    )
    production_processed_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="生产处置时间",
    )
    matching_processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="processed_factory_return_matching_items",
        verbose_name="配套处置人",
    )
    matching_processed_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="配套处置时间",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "厂家退货分类库存"
        verbose_name_plural = "厂家退货分类库存"
        ordering = ("process_id", "transfer_item_id")
        indexes = (
            models.Index(
                fields=("stock_status", "classified_at"),
                name="lifecycle_fr_stock_61f532_idx",
            ),
            models.Index(
                fields=("source_product_model", "stock_status"),
                name="lifecycle_fr_model_33d2e7_idx",
            ),
        )

    def __str__(self):
        return (
            f"{self.process.transfer.flow_no}-"
            f"{self.transfer_item.device.sn}-"
            f"{self.get_stock_status_display()}"
        )
