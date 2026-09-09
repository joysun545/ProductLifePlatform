from django.db import models


class ProductDocument(models.Model):
    """随产品型号发布、供用户端阅读的必读资料。"""

    class DocumentType(models.TextChoices):
        USER_MANUAL = "USER_MANUAL", "使用说明书"
        DAILY_MAINTENANCE = "DAILY_MAINTENANCE", "日常维护"
        TROUBLESHOOTING_GUIDE = "TROUBLESHOOTING_GUIDE", "自助故障排除指南"

    class FileType(models.TextChoices):
        TEXT = "TEXT", "TXT文本"
        MARKDOWN = "MARKDOWN", "Markdown文本"
        DOCX = "DOCX", "Word文档"
        PDF = "PDF", "PDF文档"

    product_model = models.ForeignKey(
        "product.ProductModel",
        on_delete=models.CASCADE,
        related_name="documents",
        verbose_name="产品型号",
    )
    document_type = models.CharField(
        max_length=32,
        choices=DocumentType.choices,
        verbose_name="资料类型",
    )
    title = models.CharField(max_length=64, verbose_name="标题")
    content = models.TextField(blank=True, default="", verbose_name="正文")
    document_file = models.FileField(
        upload_to="product/documents/%Y/%m/",
        blank=True,
        verbose_name="原始文档",
    )
    original_filename = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="原始文件名",
    )
    file_type = models.CharField(
        max_length=16,
        choices=FileType.choices,
        blank=True,
        default="",
        verbose_name="文件类型",
    )
    mime_type = models.CharField(
        max_length=128,
        blank=True,
        default="",
        verbose_name="媒体类型",
    )
    file_size = models.PositiveBigIntegerField(default=0, verbose_name="文件大小")
    version = models.PositiveIntegerField(default=1, verbose_name="内容版本")
    is_active = models.BooleanField(default=True, verbose_name="是否启用")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        ordering = ("document_type", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("product_model", "document_type"),
                name="product_unique_model_document_type",
            )
        ]
        verbose_name = "产品型号资料"
        verbose_name_plural = "产品型号资料"

    def __str__(self):
        return f"{self.product_model.code}-{self.title}-v{self.version}"
