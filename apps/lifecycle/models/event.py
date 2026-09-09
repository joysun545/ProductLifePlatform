# apps/lifecycle/models/event.py


from django.db import models



class LifecycleEvent(models.Model):
    """
    产品生命周期事件。


    记录设备从生产、流转、安装、售后到报废的全过程。


    设计原则：

    1. 事件只新增，不覆盖历史。

    2. Device记录是什么设备。

    3. LifecycleEvent记录设备发生过什么。


    产品流转原则：

    发货：

        上游组织 -> 下游组织


    售后：

        下游组织 -> 上游组织


    调货：

        平级组织之间转移


    系统不定义：

        一级代理

        二级代理

        三级代理


    只记录真实发生的组织流转。
    """



    EVENT_TYPE_CHOICES = [

        (
            "CREATE",
            "设备创建"
        ),

        (
            "FACTORY_OUT",
            "生产出厂"
        ),

        (
            "SHIP",
            "发货"
        ),

        (
            "RECEIVE",
            "收货"
        ),

        (
            "RETURN",
            "退货"
        ),

        (
            "TRANSFER",
            "调货"
        ),

        (
            "AFTERSALE_TRANSFER",
            "售后转移"
        ),

        (
            "INSTALL",
            "设备安装"
        ),

        (
            "ACTIVATE",
            "设备激活"
        ),

        (
            "MAINTAIN",
            "维护保养"
        ),

        (
            "REPAIR",
            "维修"
        ),

        (
            "SCRAP",
            "设备报废"
        ),

        (
            "FACTORY_RETURN_PICKUP",
            "厂家物流取回"
        ),

        (
            "FACTORY_RETURN_INTAKE",
            "退货分类入库"
        ),

        (
            "FACTORY_RETURN_REPAIR_DIAGNOSIS",
            "退货设备修复诊断"
        ),

        (
            "FACTORY_RETURN_REPAIR_COMPLETE",
            "退货设备修复完成"
        ),

        (
            "FACTORY_RETURN_DISASSEMBLY_COMPLETE",
            "退货设备拆机完成"
        ),

        (
            "FACTORY_RETURN_MATCHING_COMPLETE",
            "退货设备配套处置完成"
        ),

        (
            "LIFECYCLE_CLOSE",
            "生命周期结束"
        ),

    ]



    device = models.ForeignKey(
        "lifecycle.Device",
        on_delete=models.CASCADE,
        related_name="lifecycle_events",
        verbose_name="设备"
    )



    event_type = models.CharField(
        max_length=64,
        choices=EVENT_TYPE_CHOICES,
        db_index=True,
        verbose_name="事件类型"
    )



    # 执行扫码操作的人
    operator = models.ForeignKey(
        "account.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="operated_lifecycle_events",
        verbose_name="操作用户"
    )



    # 实际执行该业务动作的组织
    #
    # 例如：
    # 商家A发货给商家B
    #
    # organization = 商家A
    #
    organization = models.ForeignKey(
        "account.Organization",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="lifecycle_events",
        verbose_name="操作组织"
    )



    # 流转起点
    #
    # 例如：
    # 厂家 -> 商家
    #
    # from_org = 厂家
    #
    from_organization = models.ForeignKey(
        "account.Organization",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="lifecycle_events_from",
        verbose_name="来源组织"
    )



    # 流转目标
    #
    # 例如：
    # 厂家 -> 商家
    #
    # to_org = 商家
    #
    to_organization = models.ForeignKey(
        "account.Organization",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="lifecycle_events_to",
        verbose_name="目标组织"
    )



    # 扩展业务数据
    #
    # 不同事件保存不同数据：
    #
    # 发货：
    # {
    #   "logistics_no":"SF123456"
    # }
    #
    # 安装：
    # {
    #   "vehicle_no":"渝A12345"
    # }
    #
    data = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="事件数据"
    )



    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        verbose_name="发生时间"
    )



    class Meta:

        verbose_name = "生命周期事件"

        verbose_name_plural = "生命周期事件"


        ordering = [
            "created_at"
        ]


        indexes = [

            models.Index(
                fields=[
                    "device",
                    "event_type"
                ]
            ),

            models.Index(
                fields=[
                    "from_organization",
                    "to_organization"
                ]
            ),

            models.Index(
                fields=[
                    "created_at"
                ]
            ),

        ]



    def __str__(self):

        return (
            f"{self.device.sn}"
            f"-"
            f"{self.event_type}"
        )
