# apps/product/models/bom.py

from django.conf import settings
from django.db import models



class BOM(models.Model):
    """
    产品物料清单版本。


    示例：

    JL5000

        BOM V1
        标准版


        BOM V2
        出口版
    """



    product_model = models.ForeignKey(
        "product.ProductModel",
        on_delete=models.CASCADE,
        related_name="boms",
        verbose_name="产品型号"
    )


    version_no = models.PositiveIntegerField(
        default=1,
        verbose_name="版本序号"
    )


    version = models.CharField(
        max_length=32,
        verbose_name="版本名称"
    )


    remark = models.TextField(
        blank=True,
        default="",
        verbose_name="备注"
    )


    is_active = models.BooleanField(
        default=True,
        verbose_name="是否启用"
    )


    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_boms",
        verbose_name="创建人"
    )


    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="创建时间"
    )



    class Meta:

        verbose_name = "BOM"

        verbose_name_plural = "BOM"

        constraints = [

            models.UniqueConstraint(
                fields=[
                    "product_model",
                    "version"
                ],
                name="unique_product_bom_version"
            )

        ]


    def __str__(self):

        return (
            f"{self.product_model}"
            f"-"
            f"{self.version}"
        )




class BOMItem(models.Model):
    """
    BOM明细。


    一个BOM由多个物料组成。

    示例：

    JL5000 BOM

        电池箱体
        控制器
        发电机组
    """



    bom = models.ForeignKey(
        BOM,
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name="BOM"
    )


    material = models.ForeignKey(
        "supply_chain.Material",
        on_delete=models.PROTECT,
        related_name="bom_items",
        verbose_name="物料"
    )


    material_spec = models.ForeignKey(
        "supply_chain.MaterialSpec",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="bom_items",
        verbose_name="物料规格"
    )


    supplier = models.ForeignKey(
        "account.Organization",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="bom_supplied_materials",
        verbose_name="指定供应商"
    )


    quantity = models.FloatField(
        default=1,
        verbose_name="数量"
    )


    remark = models.TextField(
        blank=True,
        default="",
        verbose_name="备注"
    )


    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="创建时间"
    )


    class Meta:

        verbose_name = "BOM明细"

        verbose_name_plural = "BOM明细"


        constraints = [

            models.UniqueConstraint(
                fields=[
                    "bom",
                    "material"
                ],
                name="unique_bom_material"
            )

        ]


    def __str__(self):

        return (
            f"{self.bom}"
            f"-"
            f"{self.material}"
        )
