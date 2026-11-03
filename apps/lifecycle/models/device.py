# apps/lifecycle/models/device.py


from django.db import models



class Device(models.Model):
    """
    产品设备实例。


    表示真实世界中的一台设备。


    示例：


    产品型号：

    JL5000驻车发电机


    生产批次：

    JL20260728001


    设备：

    SN:
    JL5000-2026072800001


    后续：

    出厂

    发货

    装车

    激活

    售后

    维修

    全部通过 LifecycleEvent 记录。
    """



    sn = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        verbose_name="设备序列号"
    )

    physical_device_code = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        default=None,
        unique=True,
        verbose_name="设备编码",
        help_text="设备本体二维码编码；未绑定时为空。",
    )



    product_model = models.ForeignKey(
        "product.ProductModel",
        on_delete=models.PROTECT,
        related_name="devices",
        verbose_name="产品型号"
    )



    batch = models.ForeignKey(
        "manufacturing.ProductionBatch",
        on_delete=models.PROTECT,
        related_name="devices",
        verbose_name="生产批次"
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

        verbose_name = "设备"

        verbose_name_plural = "设备"


        ordering = [
            "-created_at"
        ]


        indexes = [

            models.Index(
                fields=[
                    "product_model"
                ]
            ),

            models.Index(
                fields=[
                    "batch"
                ]
            ),

        ]



    def __str__(self):

        return self.sn
