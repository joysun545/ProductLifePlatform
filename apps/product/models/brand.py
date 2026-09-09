# apps/product/models/brand.py

from django.db import models


class Brand(models.Model):
    """
    产品品牌。

    产品基础资料。

    示例：

    劲龙
    本田
    雅马哈


    说明：

    Brand 只描述品牌本身。

    不负责：
    - 品牌所有权
    - OEM关系
    - 授权关系
    - 渠道关系

    这些属于商业关系模型。
    """

    name = models.CharField(
        max_length=64,
        unique=True,
        verbose_name="品牌名称"
    )


    code = models.CharField(
        max_length=32,
        unique=True,
        verbose_name="品牌编码"
    )


    description = models.TextField(
        blank=True,
        default="",
        verbose_name="品牌描述"
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

        verbose_name = "产品品牌"

        verbose_name_plural = "产品品牌"

        ordering = [
            "code"
        ]


    def __str__(self):

        return self.name
