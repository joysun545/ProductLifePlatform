from django.db import models


class GoodsTransfer(models.Model):
    """独立于订单计划的逐台商品物权流转主单。"""

    class FlowType(models.TextChoices):
        SHIPMENT = "SHIPMENT", "向下级发货"
        RETURN = "RETURN", "向上级退货"
        TRANSFER = "TRANSFER", "下级之间调货"

    class Status(models.TextChoices):
        SCANNING = "SCANNING", "扫码中"
        PENDING_RECEIPT = "PENDING_RECEIPT", "待确认入库"
        PENDING_FACTORY_PICKUP = "PENDING_FACTORY_PICKUP", "待厂家物流取回"
        PENDING_FACTORY_POSTING = "PENDING_FACTORY_POSTING", "待退货分类入库"
        COMPLETED = "COMPLETED", "已完成"
        CANCELLED = "CANCELLED", "已取消"

    flow_no = models.CharField(max_length=64, unique=True, db_index=True)
    client_request_id = models.CharField(max_length=64, unique=True)
    flow_type = models.CharField(
        max_length=16,
        choices=FlowType.choices,
        db_index=True,
    )
    status = models.CharField(
        max_length=24,
        choices=Status.choices,
        default=Status.SCANNING,
        db_index=True,
    )
    instruction_organization = models.ForeignKey(
        "account.Organization",
        on_delete=models.PROTECT,
        related_name="instructed_goods_transfers",
    )
    from_organization = models.ForeignKey(
        "account.Organization",
        on_delete=models.PROTECT,
        related_name="outgoing_goods_transfers",
    )
    to_organization = models.ForeignKey(
        "account.Organization",
        on_delete=models.PROTECT,
        related_name="incoming_goods_transfers",
    )
    source_account = models.ForeignKey(
        "account.User",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="goods_transfers_as_source_account",
    )
    target_account = models.ForeignKey(
        "account.User",
        on_delete=models.PROTECT,
        related_name="goods_transfers_as_target_account",
    )
    initiated_by = models.ForeignKey(
        "account.User",
        on_delete=models.PROTECT,
        related_name="initiated_goods_transfers",
    )
    received_by = models.ForeignKey(
        "account.User",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="received_goods_transfers",
    )
    remark = models.CharField(max_length=255, blank=True, default="")
    receipt_required = models.BooleanField(default=False)
    factory_lifecycle_terminated = models.BooleanField(default=False)
    submitted_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = (
            models.Index(
                fields=("instruction_organization", "status"),
                name="lifecycle_g_instruc_9c5872_idx",
            ),
            models.Index(
                fields=("from_organization", "status"),
                name="lifecycle_g_from_or_629109_idx",
            ),
            models.Index(
                fields=("to_organization", "status"),
                name="lifecycle_g_to_orga_dccdea_idx",
            ),
        )
        constraints = (
            models.CheckConstraint(
                condition=~models.Q(
                    from_organization=models.F("to_organization")
                ),
                name="lifecycle_goods_transfer_orgs_differ",
            ),
        )

    def __str__(self):
        return f"{self.flow_no}-{self.get_flow_type_display()}"


class GoodsTransferItem(models.Model):
    """一次商品流转中逐台扫码形成的设备明细。"""

    class Status(models.TextChoices):
        SCANNED = "SCANNED", "已扫码"
        PENDING_RECEIPT = "PENDING_RECEIPT", "待确认入库"
        PENDING_FACTORY_PICKUP = "PENDING_FACTORY_PICKUP", "待厂家物流取回"
        PENDING_FACTORY_POSTING = "PENDING_FACTORY_POSTING", "待退货分类入库"
        COMPLETED = "COMPLETED", "已完成"

    transfer = models.ForeignKey(
        GoodsTransfer,
        on_delete=models.CASCADE,
        related_name="items",
    )
    device = models.ForeignKey(
        "lifecycle.Device",
        on_delete=models.PROTECT,
        related_name="goods_transfer_items",
    )
    status = models.CharField(
        max_length=24,
        choices=Status.choices,
        default=Status.SCANNED,
        db_index=True,
    )
    scanned_by = models.ForeignKey(
        "account.User",
        on_delete=models.PROTECT,
        related_name="scanned_goods_transfer_items",
    )
    lifecycle_event = models.ForeignKey(
        "lifecycle.LifecycleEvent",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="goods_transfer_items",
    )
    receipt_event = models.ForeignKey(
        "lifecycle.LifecycleEvent",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="received_goods_transfer_items",
    )
    scanned_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("scanned_at", "id")
        constraints = (
            models.UniqueConstraint(
                fields=("transfer", "device"),
                name="lifecycle_unique_goods_transfer_device",
            ),
        )

    def __str__(self):
        return f"{self.transfer.flow_no}-{self.device.sn}"
