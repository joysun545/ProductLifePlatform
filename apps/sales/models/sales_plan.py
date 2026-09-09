# app/sales/models/sales_plan.py

from django.db import models


class SalesPlan(models.Model):
    plan_id = models.CharField(max_length=64, unique=True)

    customer = models.CharField(max_length=128)
    product_model = models.ForeignKey("product.ProductModel", on_delete=models.PROTECT)

    quantity = models.IntegerField()

    price = models.DecimalField(max_digits=10, decimal_places=2)

    status = models.CharField(
        max_length=32,
        default="pending"
    )

    created_at = models.DateTimeField(auto_now_add=True)
