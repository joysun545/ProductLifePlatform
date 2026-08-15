# apps/print/urls.py

from django.urls import path

from .views import (
    PrintCenterPage,
    PrintCenterDataAPI,
    PrintQRCodeAPI,
    PrintConfirmAPI,
)


app_name = "print"


urlpatterns = [

    # ==========================
    # 打印中心首页
    # ==========================

    path(
        "",
        PrintCenterPage.as_view(),
        name="print_center"
    ),


    path(
        "generate/",
        PrintCenterPage.as_view(),
        name="generate"
    ),


    # ==========================
    # 数据接口
    # ==========================

    path(
        "api/data/",
        PrintCenterDataAPI.as_view(),
        name="data"
    ),


    # ==========================
    # 打印二维码接口
    # ==========================

    path(
        "api/print/",
        PrintQRCodeAPI.as_view(),
        name="print_qrcode"
    ),
    path(
        "api/print/confirm/",
        PrintConfirmAPI.as_view(),
        name="print_confirm",
    ),

]
