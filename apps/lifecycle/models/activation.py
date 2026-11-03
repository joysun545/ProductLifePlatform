# apps/lifecycle/models/activation.py


from django.db import models



class ActivationRecord(models.Model):
    """
    设备激活记录。


    记录设备从库存进入使用状态的过程。


    示例：


    设备：

    JL5000-000001


    激活信息：


    激活时间：
    2026-08-01


    激活组织：
    重庆XX维修服务中心


    操作人员：
    张师傅


    安装车辆：
    渝A12345



    一个设备原则上只有一次有效激活。


    如果发生：

    换机

    重新激活

    二次安装


    可以通过增加新的记录扩展。
    """



    class ActivationMethod(models.TextChoices):
        VEHICLE_PLATE = "VEHICLE_PLATE", "车牌号"
        DEVICE_CODE = "DEVICE_CODE", "设备编码二维码"

    STATUS_CHOICES = [

        (
            "ACTIVATED",
            "已激活"
        ),

        (
            "CANCELLED",
            "已取消"
        ),

    ]



    device = models.OneToOneField(
        "lifecycle.Device",
        on_delete=models.PROTECT,
        related_name="activation",
        verbose_name="设备"
    )



    organization = models.ForeignKey(
        "account.Organization",
        on_delete=models.PROTECT,
        related_name="activated_devices",
        verbose_name="激活组织"
    )



    operator = models.ForeignKey(
        "account.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="activation_records",
        verbose_name="激活人员"
    )



    activation_time = models.DateTimeField(
        auto_now_add=True,
        verbose_name="激活时间"
    )



    activation_method = models.CharField(
        max_length=24,
        choices=ActivationMethod.choices,
        default=ActivationMethod.VEHICLE_PLATE,
        verbose_name="激活绑定方式",
    )

    physical_device_code_snapshot = models.CharField(
        max_length=64,
        blank=True,
        default="",
        verbose_name="激活时设备编码快照",
    )

    # 安装相关信息

    vehicle_no = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        verbose_name="车辆编号"
    )


    vehicle_info = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="车辆信息"
    )



    location = models.CharField(
        max_length=128,
        null=True,
        blank=True,
        verbose_name="安装地点"
    )



    status = models.CharField(
        max_length=32,
        choices=STATUS_CHOICES,
        default="ACTIVATED",
        verbose_name="激活状态"
    )



    lifecycle_event = models.OneToOneField(
        "lifecycle.LifecycleEvent",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="activation_record",
        verbose_name="关联生命周期事件"
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



    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name="更新时间"
    )



    class Meta:

        verbose_name = "设备激活记录"

        verbose_name_plural = "设备激活记录"


        indexes = [

            models.Index(
                fields=[
                    "organization",
                    "activation_time"
                ]
            ),

            models.Index(
                fields=[
                    "activation_time"
                ]
            ),

        ]



    def __str__(self):

        return (
            f"{self.device.sn}"
            f"-"
            f"{self.status}"
        )
