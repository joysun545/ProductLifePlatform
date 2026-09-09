from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("lifecycle", "0014_supplier_warranty_awareness_lists"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="FactoryReturnRepairMaterialRequirement",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("trace_source", models.CharField(choices=[("ACTUAL_BINDING", "设备实际物料绑定"), ("PRODUCTION_BATCH", "原生产批次确认BOM"), ("BOM_INFERRED", "BOM默认供应商推定"), ("UNRESOLVED", "供应商待确认"), ("LEGACY_TEXT", "历史拆机自由文本")], db_index=True, max_length=24, verbose_name="供应商追溯来源")),
                ("schema_version", models.PositiveSmallIntegerField(default=1)),
                ("requested_quantity", models.DecimalField(decimal_places=2, max_digits=12, verbose_name="按原BOM换料数量")),
                ("original_note", models.CharField(blank=True, default="", max_length=500, verbose_name="生产经理逐项说明")),
                ("requested_at", models.DateTimeField(db_index=True, verbose_name="需求提交时间")),
                ("notified_at", models.DateTimeField(blank=True, db_index=True, null=True, verbose_name="供应商通知时间")),
                ("device_sn_snapshot", models.CharField(max_length=64)),
                ("return_flow_no_snapshot", models.CharField(max_length=64)),
                ("production_batch_no_snapshot", models.CharField(max_length=64)),
                ("bom_version_snapshot", models.CharField(max_length=32)),
                ("material_code_snapshot", models.CharField(blank=True, default="", max_length=64)),
                ("material_name_snapshot", models.CharField(max_length=128)),
                ("material_spec_code_snapshot", models.CharField(blank=True, default="", max_length=64)),
                ("material_spec_name_snapshot", models.CharField(blank=True, default="", max_length=128)),
                ("supplier_name_snapshot", models.CharField(blank=True, default="", max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("device", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="factory_return_repair_material_requirements", to="lifecycle.device", verbose_name="设备")),
                ("inventory_item", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="repair_material_requirements", to="lifecycle.factoryreturninventoryitem", verbose_name="退厂库存设备")),
                ("material", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="factory_return_repair_requirements", to="supply_chain.material", verbose_name="更换物料")),
                ("material_spec", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="factory_return_repair_requirements", to="supply_chain.materialspec", verbose_name="物料规格")),
                ("notified_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="notified_factory_return_repair_materials", to=settings.AUTH_USER_MODEL, verbose_name="供应商通知人")),
                ("requested_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="requested_factory_return_repair_materials", to=settings.AUTH_USER_MODEL, verbose_name="需求提交人")),
                ("source_bom", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="factory_return_repair_material_requirements", to="product.bom", verbose_name="原BOM")),
                ("source_bom_item", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="factory_return_repair_material_requirements", to="product.bomitem", verbose_name="原BOM物料行")),
                ("source_order_plan", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="factory_return_repair_material_requirements", to="sales.orderplan", verbose_name="原订单计划")),
                ("source_order_plan_item", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="factory_return_repair_material_requirements", to="sales.orderplanitem", verbose_name="原订单明细")),
                ("source_production_batch", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="factory_return_repair_material_requirements", to="manufacturing.productionbatch", verbose_name="原生产批次")),
                ("supplier", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="factory_return_repair_requirements", to="account.organization", verbose_name="追溯原供应商")),
            ],
            options={
                "verbose_name": "退货修复换料需求",
                "verbose_name_plural": "退货修复换料需求",
                "ordering": ("inventory_item_id", "id"),
                "indexes": [models.Index(fields=["supplier", "notified_at"], name="lifecycle_repair_supplier_idx")],
                "constraints": [models.UniqueConstraint(fields=("inventory_item", "source_bom_item"), name="lifecycle_unique_repair_bom_item")],
            },
        ),
    ]
