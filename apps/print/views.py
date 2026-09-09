# apps/print/views.py

from django.views.generic import TemplateView
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie
from rest_framework.response import Response
from rest_framework.views import APIView

from sales.services.order_qrcode_print_service import (
    OrderQRCodePrintError,
    confirm_item_printed,
    generate_item_qrcodes,
    get_print_center_data,
)


# =========================
# 页面
# =========================

@method_decorator(ensure_csrf_cookie, name="dispatch")
class PrintCenterPage(TemplateView):
    template_name = "print/print_center.html"


# =========================
# 初始化数据
# =========================

class PrintCenterDataAPI(APIView):
    def get(self, request):
        return Response({"code": 0, "msg": "", "data": get_print_center_data()})


# =========================
# 打印二维码
# =========================

class PrintQRCodeAPI(APIView):
    def post(self, request):
        try:
            result = generate_item_qrcodes(item_id=request.data.get("item_id"))
        except (OrderQRCodePrintError, TypeError, ValueError) as exc:
            return Response({"code": 1, "msg": str(exc), "data": {}})
        return Response({"code": 0, "msg": "二维码生成完成", "data": result})


class PrintConfirmAPI(APIView):
    def post(self, request):
        try:
            result = confirm_item_printed(item_id=request.data.get("item_id"))
        except (OrderQRCodePrintError, TypeError, ValueError) as exc:
            return Response({"code": 1, "msg": str(exc), "data": {}})
        return Response({"code": 0, "msg": "打印状态已确认", "data": result})
