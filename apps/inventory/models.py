# apps/inventory/models.py


"""
PLDP - Inventory Module

库存管理模型

职责：

1. 记录各组织当前库存状态
2. 记录库存变化过程
3. 支撑：

   - 厂家库存查询
   - 渠道库存查询
   - 各级组织库存统计
   - 出库/入库/调货/退货追踪


设计原则：

库存是统计数据。

真实设备状态由：

Device

+

OrganizationDevice

维护。


关系：

Organization

        |

InventoryRecord

        |

ProductModel



库存变化：

InventoryTransaction


"""


from django.db import models



from account.models import Organization

from product.models import ProductModel

from lifecycle.models import Device



class InventoryRecord(models.Model):
    """
    组织库存汇总


    示例：

    重庆航迪制造

    JL5000

    quantity = 1000


    """



    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="inventory_records",
        verbose_name="所属组织"
    )



    product_model = models.ForeignKey(
        ProductModel,
        on_delete=models.PROTECT,
        related_name="inventory_records",
        verbose_name="产品型号"
    )



    quantity = models.IntegerField(
        default=0,
        verbose_name="库存数量"
    )



    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name="更新时间"
    )



    class Meta:

        verbose_name = "库存记录"

        verbose_name_plural = "库存记录"


        constraints = [

            models.UniqueConstraint(

                fields=[

                    "organization",

                    "product_model"

                ],

                name="unique_inventory_record"

            )

        ]



    def __str__(self):

        return (

            f"{self.organization}"

            f"-"

            f"{self.product_model}"

            f":"

            f"{self.quantity}"

        )





class InventoryTransaction(models.Model):
    """
    库存变化流水


    所有库存变化必须产生记录。


    示例：


    航迪制造

          ↓

    重庆总代


    数量：

    100台



    """


    TYPE_CHOICES = [


        (
            "OUTBOUND",
            "出库"
        ),


        (
            "INBOUND",
            "入库"
        ),


        (
            "TRANSFER",
            "调货"
        ),


        (
            "RETURN",
            "退货"
        ),


    ]



    from_org = models.ForeignKey(

        Organization,

        null=True,

        blank=True,

        on_delete=models.PROTECT,

        related_name="inventory_out_transactions",

        verbose_name="发出组织"

    )



    to_org = models.ForeignKey(

        Organization,

        null=True,

        blank=True,

        on_delete=models.PROTECT,

        related_name="inventory_in_transactions",

        verbose_name="接收组织"

    )



    product_model = models.ForeignKey(

        ProductModel,

        on_delete=models.PROTECT,

        related_name="inventory_transactions",

        verbose_name="产品型号"

    )



    quantity = models.IntegerField(

        default=0,

        verbose_name="数量"

    )



    transaction_type = models.CharField(

        max_length=32,

        choices=TYPE_CHOICES,

        db_index=True,

        verbose_name="交易类型"

    )



    operator = models.ForeignKey(

        "account.User",

        null=True,

        blank=True,

        on_delete=models.SET_NULL,

        verbose_name="操作人"

    )



    remark = models.TextField(

        blank=True,

        verbose_name="备注"

    )



    created_at = models.DateTimeField(

        auto_now_add=True,

        verbose_name="创建时间"

    )



    class Meta:

        verbose_name = "库存流水"

        verbose_name_plural = "库存流水"



        ordering = [

            "-created_at"

        ]



    def __str__(self):

        return (

            f"{self.product_model}"

            f"-"

            f"{self.quantity}"

        )
