from django.urls import path

from . import views


app_name = "supply_chain"


urlpatterns = [
    path(
        "sales-responsibilities/",
        views.supplier_sales_responsibilities,
        name="supplier_sales_responsibilities",
    ),
]
