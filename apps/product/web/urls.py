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
    path("material-fault-causes/", views.material_fault_causes, name="material_fault_causes"),

]
