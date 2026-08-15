# apps/product/web/views.py

from django.shortcuts import render



def product_manage(request):
    """
    产品基础资料管理中心

    Web页面渲染入口

    数据通过：
    apps.product.api
    提供接口
    """

    context = {

        "title":
            "产品基础资料管理中心",

    }


    return render(
        request,
        "product/product_manage.html",
        context
    )
