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


class DeviceMaterialFaultRecord(models.Model):
    """生产现场选出的故障关联物料事实；创建后不在原记录上覆盖修订。"""

    class TraceSource(models.TextChoices):
        ACTUAL_BINDING = "ACTUAL_BINDING", "设备实际物料绑定"
        PRODUCTION_BATCH = "PRODUCTION_BATCH", "原生产批次确认BOM"
        BOM_INFERRED = "BOM_INFERRED", "BOM默认供应商推定"
        UNRESOLVED = "UNRESOLVED", "供应商待确认"
        LEGACY_TEXT = "LEGACY_TEXT", "历史拆机自由文本"

    inventory_item = models.ForeignKey(
        FactoryReturnInventoryItem,
        on_delete=models.PROTECT,
        related_name="material_fault_records",
        verbose_name="退厂库存设备",
    )
    return_record = models.OneToOneField(
        "lifecycle.FactoryReturnMaterialReturnRecord",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="fault_record",
        verbose_name="对应实际退回物料事实",
    )
    device = models.ForeignKey(
        "lifecycle.Device",
        on_delete=models.PROTECT,
        related_name="material_fault_records",
        verbose_name="设备",
    )
    return_transfer = models.ForeignKey(
        "lifecycle.GoodsTransfer",
        on_delete=models.PROTECT,
        related_name="material_fault_records",
        verbose_name="退货批次",
    )
    source_order_plan = models.ForeignKey(
        "sales.OrderPlan",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="device_material_fault_records",
        verbose_name="原订单计划",
    )
    source_order_plan_item = models.ForeignKey(
        "sales.OrderPlanItem",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="device_material_fault_records",
        verbose_name="原订单明细",
    )
    source_production_batch = models.ForeignKey(
        "manufacturing.ProductionBatch",
        on_delete=models.PROTECT,
        related_name="device_material_fault_records",
        verbose_name="原生产批次",
    )
    source_bom = models.ForeignKey(
        "product.BOM",
        on_delete=models.PROTECT,
        related_name="device_material_fault_records",
        verbose_name="原BOM",
    )
    source_bom_item = models.ForeignKey(
        "product.BOMItem",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="device_material_fault_records",
        verbose_name="原BOM物料行",
    )
    material = models.ForeignKey(
        "supply_chain.Material",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="device_fault_records",
        verbose_name="故障关联物料",
    )
    material_spec = models.ForeignKey(
        "supply_chain.MaterialSpec",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="device_fault_records",
        verbose_name="物料规格",
    )
    supplier = models.ForeignKey(
        "account.Organization",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="supplied_device_fault_records",
        verbose_name="追溯供应商",
    )
    trace_source = models.CharField(
        max_length=24,
        choices=TraceSource.choices,
        db_index=True,
        verbose_name="供应商追溯来源",
    )
    schema_version = models.PositiveSmallIntegerField(
        default=1,
        verbose_name="事实结构版本",
    )
    actual_binding_reference = models.CharField(
        max_length=128,
        blank=True,
        default="",
        verbose_name="实际物料绑定引用",
    )
    material_batch_snapshot = models.CharField(
        max_length=128,
        blank=True,
        default="",
        verbose_name="实际物料批次快照",
    )
    is_warranty_candidate = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name="系统自动判定是否在三包期内（兼容字段）",
    )
    original_note = models.CharField(
        max_length=500,
        blank=True,
        default="",
        verbose_name="生产现场原始说明",
    )
    dismantled_at = models.DateTimeField(
        db_index=True,
        verbose_name="拆机时间",
    )
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="recorded_device_material_faults",
        verbose_name="原始记录人",
    )
    recorded_organization = models.ForeignKey(
        "account.Organization",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="recorded_device_material_faults",
        verbose_name="原始记录组织",
    )
    recorded_role_snapshot = models.CharField(
        max_length=64,
        blank=True,
        default="",
        verbose_name="原始操作岗位快照",
    )

    # 名称、编码和业务号均做快照，基础资料以后改名也不影响历史质量事实。
    device_sn_snapshot = models.CharField(max_length=64, verbose_name="设备序列号快照")
    lifecycle_code_snapshot = models.CharField(
        max_length=128,
        blank=True,
        default="",
        verbose_name="原生命周期编码快照",
    )
    return_flow_no_snapshot = models.CharField(max_length=64, verbose_name="退货批次快照")
    order_plan_code_snapshot = models.CharField(
        max_length=64,
        blank=True,
        default="",
        verbose_name="原订单计划编码快照",
    )
    order_batch_code_snapshot = models.CharField(
        max_length=64,
        blank=True,
        default="",
        verbose_name="原订单批次编码快照",
    )
    production_batch_no_snapshot = models.CharField(max_length=64, verbose_name="原生产批次快照")
    bom_version_snapshot = models.CharField(max_length=32, verbose_name="原BOM版本快照")
    material_code_snapshot = models.CharField(
        max_length=64,
        blank=True,
        default="",
        verbose_name="物料编码快照",
    )
    material_name_snapshot = models.CharField(
        max_length=128,
        blank=True,
        default="",
        verbose_name="物料名称快照",
    )
    material_spec_code_snapshot = models.CharField(
        max_length=64,
        blank=True,
        default="",
        verbose_name="规格编码快照",
    )
    material_spec_name_snapshot = models.CharField(
        max_length=128,
        blank=True,
        default="",
        verbose_name="规格名称快照",
    )
    supplier_name_snapshot = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="供应商名称快照",
    )
    recorded_at = models.DateTimeField(db_index=True, verbose_name="原始记录时间")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="建档时间")

    class Meta:
        verbose_name = "设备故障物料事实"
        verbose_name_plural = "设备故障物料事实"
        ordering = ("inventory_item_id", "id")
        indexes = (
            models.Index(
                fields=("supplier", "is_warranty_candidate", "dismantled_at"),
                name="lifecycle_fault_supplier_idx",
            ),
            models.Index(
                fields=("material", "source_production_batch"),
                name="lifecycle_fault_material_idx",
            ),
        )
        constraints = (
            models.UniqueConstraint(
                fields=("inventory_item", "source_bom_item"),
                condition=models.Q(source_bom_item__isnull=False),
                name="lifecycle_unique_fault_bom_item",
            ),
        )

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValueError("故障物料事实创建后不可覆盖；修订请写入新的关联表。")
        return super().save(*args, **kwargs)

    def __str__(self):
        material_name = self.material_name_snapshot or "历史拆机文本"
        return f"{self.device_sn_snapshot}-{material_name}"


class FactoryReturnRepairMaterialRequirement(models.Model):
    """退货修复时由生产经理从原BOM勾选的结构化换料需求。"""

    inventory_item = models.ForeignKey(
        FactoryReturnInventoryItem,
        on_delete=models.PROTECT,
        related_name="repair_material_requirements",
        verbose_name="退厂库存设备",
    )
    returned_material_record = models.OneToOneField(
        "lifecycle.FactoryReturnMaterialReturnRecord",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="repair_requirement",
        verbose_name="修复完成后实际拆下退回物料",
    )
    device = models.ForeignKey(
        "lifecycle.Device",
        on_delete=models.PROTECT,
        related_name="factory_return_repair_material_requirements",
        verbose_name="设备",
    )
    source_order_plan = models.ForeignKey(
        "sales.OrderPlan",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="factory_return_repair_material_requirements",
        verbose_name="原订单计划",
    )
    source_order_plan_item = models.ForeignKey(
        "sales.OrderPlanItem",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="factory_return_repair_material_requirements",
        verbose_name="原订单明细",
    )
    source_production_batch = models.ForeignKey(
        "manufacturing.ProductionBatch",
        on_delete=models.PROTECT,
        related_name="factory_return_repair_material_requirements",
        verbose_name="原生产批次",
    )
    source_bom = models.ForeignKey(
        "product.BOM",
        on_delete=models.PROTECT,
        related_name="factory_return_repair_material_requirements",
        verbose_name="原BOM",
    )
    source_bom_item = models.ForeignKey(
        "product.BOMItem",
        on_delete=models.PROTECT,
        related_name="factory_return_repair_material_requirements",
        verbose_name="原BOM物料行",
    )
    material = models.ForeignKey(
        "supply_chain.Material",
        on_delete=models.PROTECT,
        related_name="factory_return_repair_requirements",
        verbose_name="更换物料",
    )
    material_spec = models.ForeignKey(
        "supply_chain.MaterialSpec",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="factory_return_repair_requirements",
        verbose_name="物料规格",
    )
    supplier = models.ForeignKey(
        "account.Organization",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="factory_return_repair_requirements",
        verbose_name="追溯原供应商",
    )
    trace_source = models.CharField(
        max_length=24,
        choices=DeviceMaterialFaultRecord.TraceSource.choices,
        db_index=True,
        verbose_name="供应商追溯来源",
    )
    schema_version = models.PositiveSmallIntegerField(default=1)
    requested_quantity = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name="按原BOM换料数量",
    )
    original_note = models.CharField(
        max_length=500,
        blank=True,
        default="",
        verbose_name="生产经理逐项说明",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="requested_factory_return_repair_materials",
        verbose_name="需求提交人",
    )
    requested_at = models.DateTimeField(db_index=True, verbose_name="需求提交时间")
    notified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="notified_factory_return_repair_materials",
        verbose_name="供应商通知人",
    )
    notified_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="供应商通知时间",
    )

    # 快照保证基础资料、BOM或供应关系调整后仍可还原当时的修复需求。
    device_sn_snapshot = models.CharField(max_length=64)
    return_flow_no_snapshot = models.CharField(max_length=64)
    production_batch_no_snapshot = models.CharField(max_length=64)
    bom_version_snapshot = models.CharField(max_length=32)
    material_code_snapshot = models.CharField(max_length=64, blank=True, default="")
    material_name_snapshot = models.CharField(max_length=128)
    material_spec_code_snapshot = models.CharField(max_length=64, blank=True, default="")
    material_spec_name_snapshot = models.CharField(max_length=128, blank=True, default="")
    supplier_name_snapshot = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "退货修复换料需求"
        verbose_name_plural = "退货修复换料需求"
        ordering = ("inventory_item_id", "id")
        constraints = (
            models.UniqueConstraint(
                fields=("inventory_item", "source_bom_item"),
                name="lifecycle_unique_repair_bom_item",
            ),
        )
        indexes = (
            models.Index(
                fields=("supplier", "notified_at"),
                name="lifecycle_repair_supplier_idx",
            ),
        )

    def __str__(self):
        return f"{self.device_sn_snapshot}-{self.material_name_snapshot}"


class DeviceMaterialWarrantyEvaluation(models.Model):
    """不覆盖的三包时间评估快照。

    以后修改质保规则或补齐生产数据时，必须递增 version 写入新记录，
    不允许覆盖当时向供应商发布通知所依据的原始结果。
    """

    class Result(models.TextChoices):
        IN_WARRANTY = "IN_WARRANTY", "三包期内"
        OUT_OF_WARRANTY = "OUT_OF_WARRANTY", "已超三包期"
        UNKNOWN = "UNKNOWN", "待补充规则"

    class RuleSource(models.TextChoices):
        EXACT_SUPPLIER_SPEC = "EXACT_SUPPLIER_SPEC", "精确供应商规格质保规则"
        MISSING_PRODUCTION_DATE = "MISSING_PRODUCTION_DATE", "缺少生产日期"
        MISSING_EXACT_RULE = "MISSING_EXACT_RULE", "缺少精确供应商质保规则"
        MISSING_TRACE = "MISSING_TRACE", "缺少规格或供应商追溯"

    fault_record = models.ForeignKey(
        DeviceMaterialFaultRecord,
        on_delete=models.PROTECT,
        related_name="warranty_evaluations",
        verbose_name="故障物料事实",
    )
    version = models.PositiveSmallIntegerField(default=1, verbose_name="评估版本")
    result = models.CharField(
        max_length=24,
        choices=Result.choices,
        db_index=True,
        verbose_name="评估结果",
    )
    rule_source = models.CharField(
        max_length=32,
        choices=RuleSource.choices,
        verbose_name="评估规则来源",
    )
    material_spec_supplier = models.ForeignKey(
        "supply_chain.MaterialSpecSupplier",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="device_warranty_evaluations",
        verbose_name="精确规格供应关系",
    )
    warranty_months = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name="评估时质保月数快照",
    )
    warranty_start_date = models.DateField(null=True, blank=True, verbose_name="质保起算日")
    warranty_end_date = models.DateField(null=True, blank=True, verbose_name="质保截止日")
    evaluated_date = models.DateField(verbose_name="评估基准日")
    supplier_name_snapshot = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="供应商名称快照",
    )
    evaluated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="evaluated_device_material_warranties",
        verbose_name="评估触发人",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="评估时间")

    class Meta:
        verbose_name = "设备物料三包评估"
        verbose_name_plural = "设备物料三包评估"
        ordering = ("fault_record_id", "-version")
        constraints = (
            models.UniqueConstraint(
                fields=("fault_record", "version"),
                name="lifecycle_unique_fault_warranty_version",
            ),
        )
        indexes = (
            models.Index(
                fields=("result", "evaluated_date"),
                name="lifecycle_warranty_result_idx",
            ),
        )

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValueError("三包评估快照不可覆盖；重新评估请递增版本。")
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.fault_record}-v{self.version}-{self.get_result_display()}"


class FactoryReturnMaterialReturnRecord(models.Model):
    """厂家退货中实际拆下并退回的物料事实。

    这张表只表达实物退回与未来结算口径，不等同于故障判定。
    故障件次通过 ``DeviceMaterialFaultRecord`` 单独记录；质保状态在
    退回发生时按原生产批次、退回时间和精确供应商规格规则自动固化。
    """

    class ReturnKind(models.TextChoices):
        DISASSEMBLY = "DISASSEMBLY", "拆机实际退回"
        REPAIR_REPLACED = "REPAIR_REPLACED", "修复换下实际退回"

    class DataOrigin(models.TextChoices):
        OPERATION = "OPERATION", "现场结构化操作"
        LEGACY_FAULT_BACKFILL = "LEGACY_FAULT_BACKFILL", "历史故障记录回填"
        LEGACY_REPAIR_BACKFILL = "LEGACY_REPAIR_BACKFILL", "历史修复记录回填"

    class SettlementStatus(models.TextChoices):
        PENDING = "PENDING", "待关联结算"
        LINKED = "LINKED", "已关联结算批次"
        SETTLED = "SETTLED", "已完成结算"
        NOT_APPLICABLE = "NOT_APPLICABLE", "无需结算"

    inventory_item = models.ForeignKey(
        FactoryReturnInventoryItem,
        on_delete=models.PROTECT,
        related_name="material_return_records",
        verbose_name="退厂库存设备",
    )
    device = models.ForeignKey(
        "lifecycle.Device",
        on_delete=models.PROTECT,
        related_name="factory_return_material_records",
        verbose_name="设备",
    )
    return_transfer = models.ForeignKey(
        "lifecycle.GoodsTransfer",
        on_delete=models.PROTECT,
        related_name="material_return_records",
        verbose_name="退货批次",
    )
    source_order_plan = models.ForeignKey(
        "sales.OrderPlan",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="factory_return_material_records",
        verbose_name="原订单计划",
    )
    source_order_plan_item = models.ForeignKey(
        "sales.OrderPlanItem",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="factory_return_material_records",
        verbose_name="原订单明细",
    )
    source_production_batch = models.ForeignKey(
        "manufacturing.ProductionBatch",
        on_delete=models.PROTECT,
        related_name="factory_return_material_records",
        verbose_name="原生产批次",
    )
    source_bom = models.ForeignKey(
        "product.BOM",
        on_delete=models.PROTECT,
        related_name="factory_return_material_records",
        verbose_name="原BOM",
    )
    source_bom_item = models.ForeignKey(
        "product.BOMItem",
        on_delete=models.PROTECT,
        related_name="factory_return_material_records",
        verbose_name="原BOM物料行",
    )
    material = models.ForeignKey(
        "supply_chain.Material",
        on_delete=models.PROTECT,
        related_name="factory_return_material_records",
        verbose_name="实际退回物料",
    )
    material_spec = models.ForeignKey(
        "supply_chain.MaterialSpec",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="factory_return_material_records",
        verbose_name="物料规格",
    )
    supplier = models.ForeignKey(
        "account.Organization",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="factory_return_material_records",
        verbose_name="追溯原供应商",
    )
    material_spec_supplier = models.ForeignKey(
        "supply_chain.MaterialSpecSupplier",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="factory_return_material_records",
        verbose_name="质保计算采用的精确供应关系",
    )
    return_kind = models.CharField(
        max_length=24,
        choices=ReturnKind.choices,
        db_index=True,
        verbose_name="实际退回类型",
    )
    returned_quantity = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name="实际退回数量",
    )
    is_fault_material = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name="是否为导致本次处置的故障物料",
    )
    trace_source = models.CharField(
        max_length=24,
        choices=DeviceMaterialFaultRecord.TraceSource.choices,
        db_index=True,
        verbose_name="供应商追溯来源",
    )
    data_origin = models.CharField(
        max_length=32,
        choices=DataOrigin.choices,
        default=DataOrigin.OPERATION,
        db_index=True,
        verbose_name="数据来源",
    )
    schema_version = models.PositiveSmallIntegerField(default=2)
    actual_binding_reference = models.CharField(max_length=128, blank=True, default="")
    material_batch_snapshot = models.CharField(max_length=128, blank=True, default="")
    original_note = models.CharField(max_length=500, blank=True, default="")
    returned_at = models.DateTimeField(db_index=True, verbose_name="实际退回/拆下时间")
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="recorded_factory_return_materials",
    )
    recorded_organization = models.ForeignKey(
        "account.Organization",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="recorded_factory_return_materials",
    )
    recorded_role_snapshot = models.CharField(max_length=64, blank=True, default="")

    warranty_result = models.CharField(
        max_length=24,
        choices=DeviceMaterialWarrantyEvaluation.Result.choices,
        db_index=True,
        verbose_name="自动质保计算结果",
    )
    warranty_rule_source = models.CharField(
        max_length=32,
        choices=DeviceMaterialWarrantyEvaluation.RuleSource.choices,
        verbose_name="自动质保计算规则来源",
    )
    warranty_months = models.PositiveIntegerField(null=True, blank=True)
    warranty_start_date = models.DateField(null=True, blank=True)
    warranty_end_date = models.DateField(null=True, blank=True)
    warranty_evaluated_date = models.DateField(verbose_name="质保计算基准日")

    settlement_status = models.CharField(
        max_length=20,
        choices=SettlementStatus.choices,
        default=SettlementStatus.PENDING,
        db_index=True,
        verbose_name="退回物料结算状态",
    )
    settlement_reference = models.CharField(
        max_length=128,
        blank=True,
        default="",
        verbose_name="结算关联引用",
    )

    device_sn_snapshot = models.CharField(max_length=64)
    return_flow_no_snapshot = models.CharField(max_length=64)
    production_batch_no_snapshot = models.CharField(max_length=64)
    bom_version_snapshot = models.CharField(max_length=32)
    material_code_snapshot = models.CharField(max_length=64, blank=True, default="")
    material_name_snapshot = models.CharField(max_length=128)
    material_spec_code_snapshot = models.CharField(max_length=64, blank=True, default="")
    material_spec_name_snapshot = models.CharField(max_length=128, blank=True, default="")
    supplier_name_snapshot = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "厂家退货实际退回物料事实"
        verbose_name_plural = "厂家退货实际退回物料事实"
        ordering = ("inventory_item_id", "return_kind", "id")
        constraints = (
            models.UniqueConstraint(
                fields=("inventory_item", "source_bom_item", "return_kind"),
                name="lifecycle_unique_return_material_kind",
            ),
        )
        indexes = (
            models.Index(
                fields=("supplier", "return_kind", "returned_at"),
                name="lifecycle_return_supplier_idx",
            ),
            models.Index(
                fields=("warranty_result", "is_fault_material"),
                name="lifecycle_return_warranty_idx",
            ),
        )

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValueError("实际退回物料事实创建后不可覆盖；结算请写关联记录。")
        return super().save(*args, **kwargs)

    def __str__(self):
        return (
            f"{self.device_sn_snapshot}-{self.material_name_snapshot}-"
            f"{self.get_return_kind_display()}"
        )


class FactoryReturnWarrantyDispatch(models.Model):
    """配套经理确认后，按供应商分组形成的最小三包协同批次。"""

    class Status(models.TextChoices):
        NOTIFIED = "NOTIFIED", "已通知供应商"
        PENDING = "PENDING", "待供应商接收"
        RECEIVED = "RECEIVED", "供应商已接收"

    inventory_item = models.ForeignKey(
        FactoryReturnInventoryItem,
        on_delete=models.PROTECT,
        related_name="warranty_dispatches",
        verbose_name="退厂库存设备",
    )
    supplier = models.ForeignKey(
        "account.Organization",
        on_delete=models.PROTECT,
        related_name="factory_return_warranty_dispatches",
        verbose_name="供应商",
    )
    supplier_name_snapshot = models.CharField(max_length=255, verbose_name="供应商名称快照")
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="confirmed_factory_return_warranty_dispatches",
        verbose_name="配套确认人",
    )
    confirmed_role_snapshot = models.CharField(max_length=64, default="factory_matching")
    confirmed_at = models.DateTimeField(db_index=True, verbose_name="配套确认时间")
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.NOTIFIED,
        db_index=True,
    )
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="received_factory_return_warranty_dispatches",
    )
    received_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    fault_records = models.ManyToManyField(
        DeviceMaterialFaultRecord,
        through="FactoryReturnWarrantyDispatchItem",
        related_name="warranty_dispatches",
    )

    class Meta:
        ordering = ("-confirmed_at", "-id")
        constraints = (
            models.UniqueConstraint(
                fields=("inventory_item", "supplier"),
                name="lifecycle_unique_warranty_dispatch",
            ),
        )

    def __str__(self):
        return f"{self.inventory_item}-{self.supplier_name_snapshot}"


class FactoryReturnWarrantyDispatchItem(models.Model):
    dispatch = models.ForeignKey(
        FactoryReturnWarrantyDispatch,
        on_delete=models.PROTECT,
        related_name="dispatch_items",
    )
    fault_record = models.OneToOneField(
        DeviceMaterialFaultRecord,
        on_delete=models.PROTECT,
        related_name="warranty_dispatch_item",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("dispatch_id", "fault_record_id")

    def __str__(self):
        return f"{self.dispatch_id}-{self.fault_record_id}"
