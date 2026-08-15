# apps/product/models/category.py

from django.db import models


class ProductCategory(models.Model):
    """
    产品分类。

    例如：

    驻车发电机
    驻车空调
    启停锂电
    控制器
    """

    name = models.CharField(
        max_length=64,
        unique=True,
        verbose_name="分类名称"
    )


    code = models.CharField(
        max_length=32,
        unique=True,
        verbose_name="分类编码"
    )


    description = models.TextField(
        blank=True,
        default="",
        verbose_name="描述"
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

        verbose_name = "产品分类"

        verbose_name_plural = "产品分类"

        ordering = [
            "code"
        ]


    def __str__(self):

        return self.name
