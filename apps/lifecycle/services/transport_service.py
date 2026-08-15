# apps/lifecycle/services/transport_service.py


"""
物流服务


职责：

- 售后返厂运输
- 点对点物流记录


说明：

物流单据 ≠ 生命周期事件

TransportOrder负责运输过程，

LifecycleEvent负责设备事实记录。

"""


from lifecycle.models.transport import TransportOrder




class TransportService:


    @staticmethod
    def create_transport(
        device,
        from_org,
        to_org,
        reason="repair",
        operator=None
    ):

        """
        创建运输单


        from_org:
            发出组织


        to_org:
            接收组织


        reason:

            repair:
                售后维修

            return:
                退货

            transfer:
                调货

        """


        return TransportOrder.objects.create(

            device=device,

            from_org=from_org,

            to_org=to_org,

            reason=reason,

        )
