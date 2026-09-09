# apps/product/models/product.py

from django.db import models



class ProductModel(models.Model):
    """
    产品型号。

    表示具体销售和生产型号。

    示例：

    品牌：
    劲龙

    型号：
    JL5000


    OEM:

    品牌：
    客户A

    生产厂家：
    重庆航迪
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


    brand = models.ForeignKey(
        "product.Brand",
        on_delete=models.PROTECT,
        related_name="products",
        null=True,
        blank=True,
        verbose_name="所属品牌"
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
