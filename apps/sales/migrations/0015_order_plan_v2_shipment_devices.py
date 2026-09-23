# Generated manually to preserve the immutable V2 baseline migration.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0014_material_stock_v2"),
        # Device and return-inventory allocation targets are already present
        # by this lifecycle migration (the same dependency used by sales 0011).
        ("lifecycle", "0016_factory_return_material_return_facts"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="orderplanshipment",
            name="client_request_id",
            field=models.CharField(
                help_text="用于销售拆分发货时的幂等提交；系统生成批次也写入稳定标识。",
                max_length=64,
                verbose_name="发货批次请求标识",
                default="",
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="orderplanshipment",
            name="qrcode_printed_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.CreateModel(
            name="OrderPlanShipmentDevice",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("qrcode_printed_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("device", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="order_plan_v2_shipment_allocations", to="lifecycle.device", verbose_name="设备")),
                ("order_plan", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="shipment_device_allocations", to="sales.orderplan", verbose_name="订单计划")),
                ("return_inventory_allocation", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="v2_shipment_device_allocation", to="sales.orderplanreturninventoryallocation", verbose_name="成品库存调用分配")),
                ("shipment", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="device_allocations", to="sales.orderplanshipment", verbose_name="发货批次")),
                ("shipment_item", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="device_allocations", to="sales.orderplanshipmentitem", verbose_name="发货批次明细")),
            ],
            options={
                "verbose_name": "订单发货设备分配",
                "verbose_name_plural": "订单发货设备分配",
                "ordering": ("shipment_id", "shipment_item_id", "id"),
            },
        ),
        migrations.AddConstraint(
            model_name="orderplanshipment",
            constraint=models.UniqueConstraint(fields=("order_plan", "client_request_id"), name="sales_plan_unique_shipment_request"),
        ),
        migrations.AddConstraint(
            model_name="orderplanshipmentdevice",
            constraint=models.UniqueConstraint(fields=("order_plan", "device"), name="sales_plan_unique_shipment_device"),
        ),
    ]
