# apps/product/models/product.py

from django.db import models



class ProductModel(models.Model):
    """
    产品型号。

    表示具体销售和生产型号。

    示例：

    品牌不属于产品型号主数据。品牌在渠道授权和订单计划中管理，
    同一型号可按不同订单、渠道使用不同品牌。
    """



    code = models.CharField(
        max_length=64,
        unique=True,
        verbose_name="型号编码"
    )


    name = models.CharField(
        max_length=128,
        verbose_name="型号名称"
    )

    category = models.ForeignKey(
        "product.ProductCategory",
        on_delete=models.PROTECT,
        related_name="products",
        verbose_name="产品分类"
    )


    description = models.TextField(
        blank=True,
        default="",
        verbose_name="产品描述"
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

        verbose_name = "产品型号"

        verbose_name_plural = "产品型号"

        ordering = [
            "code"
        ]


    def __str__(self):

        return f"{self.code}-{self.name}"
