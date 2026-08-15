# apps/inventory/views.py

"""
PLDP - Inventory API

库存接口层


职责：

- 接收请求
- 参数校验
- 调用 InventoryService
- 返回库存结果


不直接修改库存。


"""


from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status


from account.models import Organization

from product.models import ProductModel

from inventory.services import InventoryService

from inventory.models import InventoryRecord




class InventoryOutboundAPI(APIView):
    """
    出库接口


    上级组织向下级组织发货


    """

    def post(self, request):

        from_org_id = request.data.get(
            "from_org_id"
        )

        product_model_id = request.data.get(
            "product_model_id"
        )

        quantity = request.data.get(
            "quantity"
        )


        from_org = Organization.objects.get(

            id=from_org_id

        )


        product_model = ProductModel.objects.get(

            id=product_model_id

        )


        InventoryService.outbound(

            from_org=from_org,

            product_model=product_model,

            quantity=quantity,

            operator=request.user,

            remark=request.data.get(
                "remark",
                ""
            )

        )


        return Response({

            "message":
                "出库成功"

        })





class InventoryInboundAPI(APIView):
    """
    入库接口


    组织收货


    """


    def post(self, request):

        to_org_id = request.data.get(
            "to_org_id"
        )


        product_model_id = request.data.get(
            "product_model_id"
        )


        quantity = request.data.get(
            "quantity"
        )


        to_org = Organization.objects.get(

            id=to_org_id

        )


        product_model = ProductModel.objects.get(

            id=product_model_id

        )



        InventoryService.inbound(

            to_org=to_org,

            product_model=product_model,

            quantity=quantity,

            operator=request.user,

            remark=request.data.get(
                "remark",
                ""
            )

        )


        return Response({

            "message":
                "入库成功"

        })






class InventoryTransferAPI(APIView):
    """
    平级调货接口


    A组织

        ↓

    B组织


    """


    def post(self, request):


        from_org = Organization.objects.get(

            id=request.data.get(
                "from_org_id"
            )

        )


        to_org = Organization.objects.get(

            id=request.data.get(
                "to_org_id"
            )

        )


        product_model = ProductModel.objects.get(

            id=request.data.get(
                "product_model_id"
            )

        )


        quantity = request.data.get(
            "quantity"
        )



        InventoryService.transfer(

            from_org=from_org,

            to_org=to_org,

            product_model=product_model,

            quantity=quantity,

            operator=request.user,

            remark=request.data.get(
                "remark",
                ""
            )

        )


        return Response({

            "message":
                "调货成功"

        })







class InventoryReturnAPI(APIView):
    """
    退货接口


    下级

      ↓

    上级


    """


    def post(self, request):


        from_org = Organization.objects.get(

            id=request.data.get(
                "from_org_id"
            )

        )


        to_org = Organization.objects.get(

            id=request.data.get(
                "to_org_id"
            )

        )


        product_model = ProductModel.objects.get(

            id=request.data.get(
                "product_model_id"
            )

        )


        quantity = request.data.get(
            "quantity"
        )


        InventoryService.return_goods(

            from_org=from_org,

            to_org=to_org,

            product_model=product_model,

            quantity=quantity,

            operator=request.user,

            remark=request.data.get(
                "remark",
                ""
            )

        )


        return Response({

            "message":
                "退货成功"

        })







class InventoryQueryAPI(APIView):
    """
    查询组织库存


    用于：

    厂家查看渠道库存

    商家查看自身库存

    """


    def get(self, request):


        organization_id = request.query_params.get(
            "organization_id"
        )


        records = InventoryRecord.objects.filter(

            organization_id=organization_id

        ).select_related(

            "product_model"

        )


        data = []


        for item in records:


            data.append({

                "product_model":

                    item.product_model.name,


                "quantity":

                    item.quantity,


            })


        return Response(data)
