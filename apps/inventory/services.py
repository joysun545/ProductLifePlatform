# apps/inventory/services.py

"""
PLDP - Inventory Service

库存业务服务层


职责：

1. 出库
2. 入库
3. 调货
4. 退货

所有库存变化必须经过 Service。

禁止：

直接修改 InventoryRecord.quantity


库存变化同时产生：

InventoryTransaction


"""


from django.db import transaction


from inventory.models import (
    InventoryRecord,
    InventoryTransaction,
)



class InventoryService:


    @staticmethod
    @transaction.atomic
    def outbound(
        *,
        from_org,
        product_model,
        quantity,
        operator=None,
        remark=""
    ):
        """
        出库


        例如：

        厂家

          ↓

        总代


        """

        inventory = InventoryRecord.objects.select_for_update().get(

            organization=from_org,

            product_model=product_model

        )


        if inventory.quantity < quantity:

            raise Exception(
                "库存不足"
            )


        inventory.quantity -= quantity

        inventory.save()



        return InventoryTransaction.objects.create(

            from_org=from_org,

            product_model=product_model,

            quantity=quantity,

            transaction_type="OUTBOUND",

            operator=operator,

            remark=remark

        )





    @staticmethod
    @transaction.atomic
    def inbound(
        *,
        to_org,
        product_model,
        quantity,
        operator=None,
        remark=""
    ):
        """
        入库


        例如：

        厂家发货后

        总代收货


        """


        inventory, created = InventoryRecord.objects.get_or_create(

            organization=to_org,

            product_model=product_model,

            defaults={

                "quantity": 0

            }

        )


        inventory.quantity += quantity

        inventory.save()



        return InventoryTransaction.objects.create(

            to_org=to_org,

            product_model=product_model,

            quantity=quantity,

            transaction_type="INBOUND",

            operator=operator,

            remark=remark

        )





    @staticmethod
    @transaction.atomic
    def transfer(
        *,
        from_org,
        to_org,
        product_model,
        quantity,
        operator=None,
        remark=""
    ):
        """
        调货


        平级组织之间。


        A:

        -10


        B:

        +10


        """


        InventoryService.outbound(

            from_org=from_org,

            product_model=product_model,

            quantity=quantity,

            operator=operator,

            remark=remark

        )



        InventoryService.inbound(

            to_org=to_org,

            product_model=product_model,

            quantity=quantity,

            operator=operator,

            remark=remark

        )



        return InventoryTransaction.objects.create(

            from_org=from_org,

            to_org=to_org,

            product_model=product_model,

            quantity=quantity,

            transaction_type="TRANSFER",

            operator=operator,

            remark=remark

        )





    @staticmethod
    @transaction.atomic
    def return_goods(
        *,
        from_org,
        to_org,
        product_model,
        quantity,
        operator=None,
        remark=""
    ):
        """
        退货


        下级

          ↓

        上级


        """

        InventoryService.outbound(

            from_org=from_org,

            product_model=product_model,

            quantity=quantity,

            operator=operator,

            remark=remark

        )


        InventoryService.inbound(

            to_org=to_org,

            product_model=product_model,

            quantity=quantity,

            operator=operator,

            remark=remark

        )


        return InventoryTransaction.objects.create(

            from_org=from_org,

            to_org=to_org,

            product_model=product_model,

            quantity=quantity,

            transaction_type="RETURN",

            operator=operator,

            remark=remark

        )
