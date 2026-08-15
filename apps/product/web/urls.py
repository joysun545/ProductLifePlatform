# apps/product/web/urls.py

from django.urls import path

from . import views


app_name = "product"


urlpatterns = [

    path(
        "",
        views.product_manage,
        name="product_manage"
    ),

]
