# apps/manufacturing/models.py


from django.core.exceptions import ValidationError
from django.db import models



class ProductionBatch(models.Model):
    """
    产品生产批次。

    记录一次实际生产行为。


    示例：

    批次号：
    JL20260728001


    产品：
    JL5000驻车发电机


    BOM：
    V2.0标准版


    生产厂家：
    重庆航迪制造有限公司


    数量：
    500台
    """



    batch_no = models.CharField(
        max_length=64,
        unique=True,
        verbose_name="生产批次号"
    )


    product_model = models.ForeignKey(
        "product.ProductModel",
        on_delete=models.PROTECT,
        related_name="production_batches",
        verbose_name="产品型号"
    )


    bom = models.ForeignKey(
        "product.BOM",
        on_delete=models.PROTECT,
        related_name="production_batches",
        verbose_name="生产BOM"
    )


    manufacturer = models.ForeignKey(
        "account.Organization",
        on_delete=models.PROTECT,
        related_name="production_batches",
        verbose_name="生产厂家"
    )


    production_date = models.DateField(
        verbose_name="生产日期"
    )


    quantity = models.PositiveIntegerField(
        verbose_name="生产数量"
    )


    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="创建时间"
    )


    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name="更新时间"
    )



    class Meta:

        verbose_name = "生产批次"

        verbose_name_plural = "生产批次"


        ordering = [
            "-production_date",
            "batch_no"
        ]

        indexes = [
            models.Index(
                fields=["product_model", "production_date"]
            ),
            models.Index(
                fields=["manufacturer", "production_date"]
            ),
        ]

    def _validate_manufacturer_root(self):
        if not self.manufacturer_id:
            return
        manufacturer = self.manufacturer
        root = manufacturer.get_factory_root()
        if (
            manufacturer.organization_type.code != "OWNER"
            or not root
            or root.pk != manufacturer.pk
        ):
            raise ValidationError(
                {"manufacturer": "生产批次必须归属厂家负责人根组织。"}
            )

    def clean(self):
        super().clean()
        self._validate_manufacturer_root()

    def save(self, *args, **kwargs):
        self._validate_manufacturer_root()
        super().save(*args, **kwargs)

    def __str__(self):

        return (
            f"{self.batch_no}"
            f"-"
            f"{self.product_model}"
        )
