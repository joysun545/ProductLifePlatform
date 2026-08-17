from django.db import models

from sales.private_storage import order_logistics_private_storage


class OrderPlanLogisticsDocument(models.Model):
    """A logistics document uploaded after the factory ships an order."""

    class FileType(models.TextChoices):
        IMAGE = "IMAGE", "图片"
        PDF = "PDF", "PDF"

    order_plan = models.ForeignKey(
        "sales.OrderPlan",
        on_delete=models.PROTECT,
        related_name="logistics_documents",
        verbose_name="订单计划",
    )
    sequence_no = models.PositiveIntegerField(verbose_name="单据序号")
    client_request_id = models.CharField(
        max_length=64,
        verbose_name="客户端请求标识",
    )
    document_file = models.FileField(
        storage=order_logistics_private_storage,
        upload_to="documents/%Y/%m/",
        verbose_name="物流单据文件",
    )
    original_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="原文件名",
    )
    file_type = models.CharField(
        max_length=16,
        choices=FileType.choices,
        verbose_name="文件类型",
    )
    content_type = models.CharField(
        max_length=100,
        blank=True,
        default="",
        verbose_name="内容类型",
    )
    file_size = models.PositiveIntegerField(verbose_name="文件大小")
    remark = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="单据备注",
    )
    uploaded_by = models.ForeignKey(
        "account.User",
        on_delete=models.PROTECT,
        related_name="uploaded_order_logistics_documents",
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
        ordering = ("sequence_no", "id")
        verbose_name = "订单物流单据"
        verbose_name_plural = "订单物流单据"
        constraints = [
            models.UniqueConstraint(
                fields=("order_plan", "sequence_no"),
                name="sales_unique_logistics_document_sequence",
            ),
            models.UniqueConstraint(
                fields=("order_plan", "client_request_id"),
                name="sales_unique_logistics_document_request",
            ),
        ]

    def __str__(self):
        return f"{self.order_plan.plan_code}-物流单据{self.sequence_no}"
