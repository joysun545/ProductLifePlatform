from django.db import models
from django.utils import timezone


class ProductDocumentRead(models.Model):
    """某账号已阅读某台设备当前型号资料版本的事实记录。"""

    user = models.ForeignKey(
        "account.User",
        on_delete=models.PROTECT,
        related_name="product_document_reads",
        verbose_name="阅读账号",
    )
    device = models.ForeignKey(
        "lifecycle.Device",
        on_delete=models.PROTECT,
        related_name="product_document_reads",
        verbose_name="设备",
    )
    document = models.ForeignKey(
        "product.ProductDocument",
        on_delete=models.PROTECT,
        related_name="read_records",
        verbose_name="型号资料",
    )
    document_version = models.PositiveIntegerField(verbose_name="已读版本")
    read_at = models.DateTimeField(default=timezone.now, verbose_name="阅读时间")

    class Meta:
        ordering = ("-read_at", "-id")
        constraints = [
            models.UniqueConstraint(
                fields=("user", "device", "document", "document_version"),
                name="lifecycle_unique_device_document_read",
            )
        ]
        verbose_name = "设备型号资料阅读记录"
        verbose_name_plural = "设备型号资料阅读记录"

    def __str__(self):
        return (
            f"{self.user_id}-{self.device_id}-"
            f"{self.document.document_type}-v{self.document_version}"
        )
