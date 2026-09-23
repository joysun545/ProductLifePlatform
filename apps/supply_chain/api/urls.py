from django.urls import path

from . import views
from .material_inventory_views import material_inventory


app_name = "supply_chain"


urlpatterns = [
    path("material-inventory/", material_inventory, name="material_inventory"),
    path("material-inventory/catalog/", material_inventory, {"action": "catalog"}, name="material_inventory_catalog"),
    path("material-inventory/warranty/", material_inventory, {"action": "warranty"}, name="material_inventory_warranty"),
    path("material-inventory/form/", material_inventory, {"action": "form"}, name="material_inventory_form"),
    path("material-inventory/documents/", material_inventory, {"action": "submit"}, name="material_inventory_submit"),
    path("material-inventory/<int:stock_id>/", material_inventory, name="material_inventory_detail"),
    path(
        "sales-responsibilities/",
        views.supplier_sales_responsibilities,
        name="supplier_sales_responsibilities",
    ),
]
