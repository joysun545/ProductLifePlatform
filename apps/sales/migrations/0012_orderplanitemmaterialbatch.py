from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ("account", "0004_add_factory_aftersales_role"),
        ("product", "0004_product_v1_bom"),
        ("sales", "0011_order_plan_return_inventory"),
        ("supply_chain", "0005_suppliermaterialbatch"),
    ]

    operations = [
        migrations.CreateModel(
            name="OrderPlanItemMaterialBatch",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("confirmed_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("bom_item", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="order_plan_batch_usages", to="product.bomitem", verbose_name="确认BOM物料行")),
                ("confirmed_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="confirmed_order_material_batches", to="account.user", verbose_name="确认账号")),
                ("order_plan_item", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="material_batch_usages", to="sales.orderplanitem", verbose_name="订单计划明细")),
                ("supplier_material_batch", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="order_plan_usages", to="supply_chain.suppliermaterialbatch", verbose_name="供应商生产批次")),
            ],
            options={"ordering": ("order_plan_item_id", "bom_item_id")},
        ),
        migrations.AddConstraint(
            model_name="orderplanitemmaterialbatch",
            constraint=models.UniqueConstraint(fields=("order_plan_item", "bom_item"), name="sales_one_supplier_batch_per_order_bom_item"),
        ),
        migrations.AddIndex(
            model_name="orderplanitemmaterialbatch",
            index=models.Index(fields=["supplier_material_batch", "order_plan_item"], name="sales_batch_order_lookup"),
        ),
    ]
