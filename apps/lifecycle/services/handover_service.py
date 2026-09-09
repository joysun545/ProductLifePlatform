# apps/lifecycle/services/handover_service.py


"""
设备组织流转服务


职责：

- 记录设备组织间流转
- 支持：
    发货
    退货
    调货


所有流转最终进入 LifecycleEvent
"""


from lifecycle.services.event_service import EventService




class HandoverService:


    @staticmethod
    def transfer(
        device,
        from_org,
        to_org,
        operator=None,
        purpose="sale"
    ):
        """
        创建设备组织流转事件


        from_org:
            原组织


        to_org:
            目标组织


        purpose:

            sale      发货

            return    退货

            transfer  调货

        """


        return EventService.create_event(

            device=device,

            event_type="TRANSFER",

            operator=operator,

            organization=from_org,

            from_organization=from_org,

            to_organization=to_org,

            source="platform",

            data={

                "from_org_id": from_org.id,

                "to_org_id": to_org.id,

                "purpose": purpose,

            }

        )
