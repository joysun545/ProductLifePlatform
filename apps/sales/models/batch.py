# app/sales/models/batch.py

from django.db import models


class Batch(models.Model):

    batch_id = models.CharField(max_length=64, unique=True)

    sales_plan = models.ForeignKey('sales.SalesPlan', on_delete=models.PROTECT)

    product_model = models.ForeignKey('product.ProductModel', on_delete=models.PROTECT)

    quantity = models.IntegerField()

    status = models.CharField(
        max_length=32,
        default="generated"
    )

    created_at = models.DateTimeField(auto_now_add=True)
