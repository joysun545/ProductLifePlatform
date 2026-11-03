import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ("account", "0006_factory_warehouse_roles"),
        ("product", "0007_remove_productmodel_brand"),
        ("sales", "0022_order_rolling_receivables"),
        ("supply_chain", "0010_organization_material_stock"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="OrderPlanAccessoryCompatibility",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("is_active", models.BooleanField(default=True, verbose_name="启用")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("factory_organization", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="order_plan_accessory_compatibilities", to="account.organization", verbose_name="厂家")),
                ("material_spec", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="order_plan_accessory_compatibilities", to="supply_chain.materialspec", verbose_name="配件物料规格")),
                ("product_model", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="order_plan_accessory_compatibilities", to="product.productmodel", verbose_name="适配产品型号")),
            ],
            options={
                "verbose_name": "厂家配件型号适配关系",
                "verbose_name_plural": "厂家配件型号适配关系",
                "ordering": ("factory_organization__name", "product_model__category__code", "material_spec__material__code", "material_spec__code"),
            },
        ),
        migrations.CreateModel(
            name="OrderPlanAccessoryItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quantity_milli", models.PositiveBigIntegerField(verbose_name="计划数量千分位")),
                ("unit_snapshot", models.CharField(max_length=8, verbose_name="数量单位快照")),
                ("status", models.CharField(choices=[("RESERVED", "已预占待发货"), ("SHIPPED", "已发货待收货"), ("RECEIVED", "已收货"), ("RELEASED", "已释放")], db_index=True, default="RESERVED", max_length=16, verbose_name="配件状态")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("category", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="order_plan_accessory_items", to="product.productcategory", verbose_name="产品品类")),
                ("material_spec", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="order_plan_accessory_items", to="supply_chain.materialspec", verbose_name="配件物料规格")),
                ("order_plan", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="accessory_items", to="sales.orderplan", verbose_name="订单计划")),
                ("product_model", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="order_plan_accessory_items", to="product.productmodel", verbose_name="适配产品型号")),
            ],
            options={
                "verbose_name": "订单配件明细",
                "verbose_name_plural": "订单配件明细",
                "ordering": ("order_plan_id", "id"),
            },
        ),
        migrations.CreateModel(
            name="OrderPlanShipmentAccessoryItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quantity_milli", models.PositiveBigIntegerField(verbose_name="本批发货数量千分位")),
                ("unit_snapshot", models.CharField(max_length=8, verbose_name="数量单位快照")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("accessory_item", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="shipment_items", to="sales.orderplanaccessoryitem", verbose_name="订单配件明细")),
                ("shipment", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="accessory_items", to="sales.orderplanshipment", verbose_name="发货批次")),
            ],
            options={
                "verbose_name": "发货批次配件明细",
                "verbose_name_plural": "发货批次配件明细",
                "ordering": ("shipment_id", "id"),
            },
        ),
        migrations.CreateModel(
            name="OrderPlanAccessoryLotAllocation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("reserved_milli", models.PositiveBigIntegerField(verbose_name="预占数量千分位")),
                ("status", models.CharField(choices=[("RESERVED", "已预占"), ("SHIPPED", "已出库"), ("RECEIVED", "已收货"), ("RELEASED", "已释放")], db_index=True, default="RESERVED", max_length=16, verbose_name="批次流转状态")),
                ("shipped_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("received_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("accessory_item", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="lot_allocations", to="sales.orderplanaccessoryitem", verbose_name="订单配件明细")),
                ("material_lot", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="order_accessory_allocations", to="supply_chain.factorymateriallot", verbose_name="厂家收货批次")),
                ("received_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="received_order_accessory_lots", to=settings.AUTH_USER_MODEL, verbose_name="收货操作人")),
                ("reserved_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="reserved_order_accessory_lots", to=settings.AUTH_USER_MODEL, verbose_name="预占操作人")),
                ("shipment_item", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="lot_allocations", to="sales.orderplanshipmentaccessoryitem", verbose_name="发货批次配件明细")),
                ("shipped_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="shipped_order_accessory_lots", to=settings.AUTH_USER_MODEL, verbose_name="出库操作人")),
            ],
            options={
                "verbose_name": "订单配件批次预占",
                "verbose_name_plural": "订单配件批次预占",
                "ordering": ("accessory_item_id", "material_lot__received_at", "id"),
            },
        ),
        migrations.AddConstraint(
            model_name="orderplanaccessorycompatibility",
            constraint=models.UniqueConstraint(fields=("factory_organization", "material_spec", "product_model"), name="sales_unique_factory_accessory_model_spec"),
        ),
        migrations.AddIndex(
            model_name="orderplanaccessorycompatibility",
            index=models.Index(fields=["factory_organization", "is_active"], name="sales_acc_compat_factory_idx"),
        ),
        migrations.AddConstraint(
            model_name="orderplanaccessoryitem",
            constraint=models.UniqueConstraint(fields=("order_plan", "material_spec", "product_model"), name="sales_unique_order_accessory_spec_model"),
        ),
        migrations.AddConstraint(
            model_name="orderplanaccessoryitem",
            constraint=models.CheckConstraint(condition=Q(quantity_milli__gt=0), name="sales_order_accessory_quantity_positive"),
        ),
        migrations.AddConstraint(
            model_name="orderplanshipmentaccessoryitem",
            constraint=models.UniqueConstraint(fields=("accessory_item",), name="sales_unique_shipment_accessory_item"),
        ),
        migrations.AddConstraint(
            model_name="orderplanshipmentaccessoryitem",
            constraint=models.CheckConstraint(condition=Q(quantity_milli__gt=0), name="sales_shipment_accessory_quantity_positive"),
        ),
        migrations.AddConstraint(
            model_name="orderplanaccessorylotallocation",
            constraint=models.UniqueConstraint(fields=("accessory_item", "material_lot"), name="sales_unique_order_accessory_lot"),
        ),
        migrations.AddConstraint(
            model_name="orderplanaccessorylotallocation",
            constraint=models.CheckConstraint(condition=Q(reserved_milli__gt=0), name="sales_accessory_lot_reservation_positive"),
        ),
    ]
