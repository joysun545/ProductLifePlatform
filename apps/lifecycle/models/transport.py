# apps/lifecycle/models/transport.py


"""
=========================================================
PLDP - Lifecycle Module

模型：TransportOrder

职责：

点对点物流记录（售后/返厂）


说明：

TransportOrder记录运输过程。

设备事实状态变化由LifecycleEvent记录。


=========================================================
"""


from django.db import models



class TransportOrder(models.Model):
    """
    物流运输单
    """


    device = models.ForeignKey(
        'lifecycle.Device',
        on_delete=models.CASCADE,
        related_name="transport_orders",
        verbose_name="设备"
    )


    from_org = models.ForeignKey(
        'account.Organization',
        on_delete=models.PROTECT,
        related_name='transport_out',
        verbose_name="发出组织"
    )


    to_org = models.ForeignKey(
        'account.Organization',
        on_delete=models.PROTECT,
        related_name='transport_in',
        verbose_name="接收组织"
    )


    reason = models.CharField(
        max_length=32,
        default='repair',
        verbose_name="运输原因"
    )


    tracking_no = models.CharField(
        max_length=128,
        null=True,
        blank=True,
        verbose_name="物流单号"
    )


    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="创建时间"
    )


    def __str__(self):

        return (
            f"{self.device}"
            f": "
            f"{self.from_org}"
            f" -> "
            f"{self.to_org}"
        )
