from django.db import models
from django.db.models import Q

from lifecycle.private_storage import goods_transfer_private_storage


class GoodsTransferSettlement(models.Model):
    """Commercial receivable ledger independent of physical receipt."""

    transfer = models.OneToOneField(
        "lifecycle.GoodsTransfer",
        on_delete=models.PROTECT,
        related_name="settlement",
        verbose_name="商品流转单",
    )
    statement_image = models.FileField(
        storage=goods_transfer_private_storage,
        upload_to="statements/%Y/%m/",
        blank=True,
        verbose_name="货品清单图片",
    )
    statement_original_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="清单原文件名",
    )
    receivable_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name="应收货款",
    )
    statement_submitted_by = models.ForeignKey(
        "account.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="submitted_goods_transfer_statements",
        verbose_name="清单提交人",
    )
    statement_submitted_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="清单提交时间",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        ordering = ("-created_at", "-id")
        verbose_name = "商品流转应收主账"
        verbose_name_plural = "商品流转应收主账"
        constraints = (
            models.CheckConstraint(
                condition=(
                    Q(receivable_amount__isnull=True)
                    | Q(receivable_amount__gt=0)
                ),
                name="lifecycle_gt_receivable_positive",
            ),
        )

    def __str__(self):
        return f"{self.transfer.flow_no}-应收主账"


class GoodsTransferLogisticsDocument(models.Model):
    """One private logistics document uploaded by the sender."""

    class FileType(models.TextChoices):
        IMAGE = "IMAGE", "图片"
        PDF = "PDF", "PDF"

    settlement = models.ForeignKey(
        GoodsTransferSettlement,
        on_delete=models.PROTECT,
        related_name="logistics_documents",
        verbose_name="商品流转应收主账",
    )
    sequence_no = models.PositiveIntegerField(verbose_name="物流单据序号")
    client_request_id = models.CharField(
        max_length=64,
        verbose_name="客户端请求标识",
    )
    document_file = models.FileField(
        storage=goods_transfer_private_storage,
        upload_to="logistics_documents/%Y/%m/",
        verbose_name="物流单据",
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
    remark = models.CharField(max_length=255, blank=True, default="", verbose_name="备注")
    uploaded_by = models.ForeignKey(
        "account.User",
        on_delete=models.PROTECT,
        related_name="uploaded_goods_transfer_logistics_documents",
        verbose_name="上传人",
    )
    uploaded_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="上传时间")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        ordering = ("sequence_no", "id")
        verbose_name = "商品流转物流单据"
        verbose_name_plural = "商品流转物流单据"
        constraints = (
            models.UniqueConstraint(
                fields=("settlement", "sequence_no"),
                name="lifecycle_gt_logistics_sequence",
            ),
            models.UniqueConstraint(
                fields=("settlement", "client_request_id"),
                name="lifecycle_gt_logistics_request",
            ),
        )

    def __str__(self):
        return f"{self.settlement.transfer.flow_no}-物流单据{self.sequence_no}"


class GoodsTransferPayment(models.Model):
    """Receiver voucher and sender-confirmed amount for one payment batch."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "待确认收款"
        CONFIRMED = "CONFIRMED", "已确认收款"

    settlement = models.ForeignKey(
        GoodsTransferSettlement,
        on_delete=models.PROTECT,
        related_name="payments",
        verbose_name="商品流转应收主账",
    )
    sequence_no = models.PositiveIntegerField(verbose_name="付款批次")
    client_request_id = models.CharField(max_length=64, verbose_name="客户端请求标识")
    voucher_image = models.FileField(
        storage=goods_transfer_private_storage,
        upload_to="payment_vouchers/%Y/%m/",
        verbose_name="付款凭证",
    )
    voucher_original_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="凭证原文件名",
    )
    uploaded_by = models.ForeignKey(
        "account.User",
        on_delete=models.PROTECT,
        related_name="uploaded_goods_transfer_payment_vouchers",
        verbose_name="凭证上传人",
    )
    uploaded_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="上传时间")
    confirmed_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name="确认到账金额",
    )
    confirmed_by = models.ForeignKey(
        "account.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="confirmed_goods_transfer_payments",
        verbose_name="收款确认人",
    )
    confirmed_at = models.DateTimeField(null=True, blank=True, db_index=True, verbose_name="确认时间")
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
        verbose_name="收款状态",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        ordering = ("sequence_no", "id")
        verbose_name = "商品流转付款记录"
        verbose_name_plural = "商品流转付款记录"
        constraints = (
            models.UniqueConstraint(
                fields=("settlement", "sequence_no"),
                name="lifecycle_gt_payment_sequence",
            ),
            models.UniqueConstraint(
                fields=("settlement", "client_request_id"),
                name="lifecycle_gt_payment_request",
            ),
            models.CheckConstraint(
                condition=(
                    Q(confirmed_amount__isnull=True)
                    | Q(confirmed_amount__gt=0)
                ),
                name="lifecycle_gt_payment_positive",
            ),
        )

    def __str__(self):
        return f"{self.settlement.transfer.flow_no}-第{self.sequence_no}笔付款"
