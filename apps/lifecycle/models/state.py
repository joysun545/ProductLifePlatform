# apps/lifecycle/models/state.py


from django.db import models



class DeviceState(models.Model):
    """
    设备当前状态。


    用于实时查询设备当前情况。


    示例：


    设备：

    JL5000-000001


    当前组织：

    重庆XX商贸有限公司


    当前状态：

    已激活


    最后事件：

    ACTIVATE



    注意：

    该模型不是历史记录。

    历史变化由 LifecycleEvent 保存。


    LifecycleEvent:
        发生过什么


    DeviceState:
        现在是什么
    """



    STATUS_CHOICES = [

        (
            "CREATED",
            "已创建"
        ),

        (
            "PRODUCED",
            "已生产"
        ),

        (
            "IN_STOCK",
            "库存中"
        ),

        (
            "SHIPPED",
            "已发货"
        ),

        (
            "INSTALLED",
            "已安装"
        ),

        (
            "ACTIVATED",
            "已激活"
        ),

        (
            "RUNNING",
            "运行中"
        ),

        (
            "REPAIRING",
            "维修中"
        ),

        (
            "SCRAPPED",
            "已报废"
        ),

    ]



    ACTIVATION_STATUS_CHOICES = [

        (
            "NOT_ACTIVATED",
            "未激活"
        ),

        (
            "ACTIVATED",
            "已激活"
        ),

    ]



    INSTALL_STATUS_CHOICES = [

        (
            "NOT_INSTALLED",
            "未安装"
        ),

        (
            "INSTALLED",
            "已安装"
        ),

    ]



    device = models.OneToOneField(
        "lifecycle.Device",
        on_delete=models.CASCADE,
        related_name="state",
        verbose_name="设备"
    )



    # 当前责任组织
    #
    # 例如：
    #
    # 当前库存：
    # 重庆XX商贸
    #
    # 或：
    #
    # 当前安装服务主体
    #
    current_organization = models.ForeignKey(
        "account.Organization",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="current_devices",
        verbose_name="当前所属组织"
    )



    status = models.CharField(
        max_length=32,
        choices=STATUS_CHOICES,
        default="CREATED",
        db_index=True,
        verbose_name="设备状态"
    )



    activation_status = models.CharField(
        max_length=32,
        choices=ACTIVATION_STATUS_CHOICES,
        default="NOT_ACTIVATED",
        db_index=True,
        verbose_name="激活状态"
    )



    install_status = models.CharField(
        max_length=32,
        choices=INSTALL_STATUS_CHOICES,
        default="NOT_INSTALLED",
        db_index=True,
        verbose_name="安装状态"
    )



    last_event = models.ForeignKey(
        "lifecycle.LifecycleEvent",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="current_states",
        verbose_name="最后事件"
    )



    last_event_time = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="最后事件时间"
    )



    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name="更新时间"
    )



    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="创建时间"
    )



    class Meta:

        verbose_name = "设备状态"

        verbose_name_plural = "设备状态"


        indexes = [

            models.Index(
                fields=[
                    "current_organization",
                    "status"
                ]
            ),

            models.Index(
                fields=[
                    "activation_status"
                ]
            ),

            models.Index(
                fields=[
                    "install_status"
                ]
            ),

        ]



    def __str__(self):

        return (
            f"{self.device.sn}"
            f"-"
            f"{self.status}"
        )
