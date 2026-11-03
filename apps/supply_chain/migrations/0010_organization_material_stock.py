import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import F, Q


class Migration(migrations.Migration):
    dependencies = [
        ("account", "0006_factory_warehouse_roles"),
        ("supply_chain", "0009_material_multi_product_categories"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="OrganizationMaterialStock",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("unit", models.CharField(max_length=8, verbose_name="库存单位")),
                ("revision", models.PositiveBigIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("material_spec", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="organization_stocks", to="supply_chain.materialspec", verbose_name="物料规格")),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="organization_material_stocks", to="account.organization", verbose_name="库存组织")),
                ("source_factory", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="downstream_material_stocks", to="account.organization", verbose_name="来源厂家")),
                ("supplier", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="downstream_material_stock_sources", to="account.organization", verbose_name="原供应商")),
            ],
            options={
                "verbose_name": "组织配件库存",
                "verbose_name_plural": "组织配件库存",
                "ordering": ("organization_id", "material_spec_id", "supplier_id"),
            },
        ),
        migrations.CreateModel(
            name="OrganizationMaterialLot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source_reference", models.CharField(max_length=128, unique=True, verbose_name="来源流转标识")),
                ("source_order_plan_id", models.PositiveBigIntegerField(db_index=True, verbose_name="来源订单计划ID")),
                ("source_shipment_id", models.PositiveBigIntegerField(db_index=True, verbose_name="来源发货批次ID")),
                ("source_accessory_item_id", models.PositiveBigIntegerField(db_index=True, verbose_name="来源订单配件ID")),
                ("source_batch_code_snapshot", models.CharField(max_length=48, verbose_name="厂家收货批次号快照")),
                ("received_milli", models.PositiveBigIntegerField(verbose_name="收货数量千分位")),
                ("on_hand_milli", models.PositiveBigIntegerField(verbose_name="现存数量千分位")),
                ("reserved_milli", models.PositiveBigIntegerField(default=0, verbose_name="预占数量千分位")),
                ("received_at", models.DateTimeField(db_index=True, verbose_name="收货时间")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("received_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="received_organization_material_lots", to=settings.AUTH_USER_MODEL, verbose_name="收货操作人")),
                ("source_factory_lot", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="downstream_lots", to="supply_chain.factorymateriallot", verbose_name="厂家原始收货批次")),
                ("stock", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="lots", to="supply_chain.organizationmaterialstock", verbose_name="组织配件库存")),
            ],
            options={
                "verbose_name": "组织配件库存批次",
                "verbose_name_plural": "组织配件库存批次",
                "ordering": ("received_at", "id"),
            },
        ),
        migrations.AddConstraint(
            model_name="organizationmaterialstock",
            constraint=models.UniqueConstraint(fields=("organization", "source_factory", "supplier", "material_spec"), name="supply_org_material_stock_scope_unique"),
        ),
        migrations.AddConstraint(
            model_name="organizationmaterialstock",
            constraint=models.CheckConstraint(condition=~Q(organization=F("source_factory")), name="supply_org_material_stock_distinct_factory"),
        ),
        migrations.AddConstraint(
            model_name="organizationmateriallot",
            constraint=models.CheckConstraint(condition=Q(received_milli__gt=0), name="supply_org_material_lot_received_positive"),
        ),
        migrations.AddConstraint(
            model_name="organizationmateriallot",
            constraint=models.CheckConstraint(condition=Q(on_hand_milli__lte=F("received_milli")), name="supply_org_material_lot_balance_bounded"),
        ),
        migrations.AddConstraint(
            model_name="organizationmateriallot",
            constraint=models.CheckConstraint(condition=Q(reserved_milli__lte=F("on_hand_milli")), name="supply_org_material_lot_reserved_bounded"),
        ),
        migrations.AlterField(
            model_name="materialwarehousedocument",
            name="kind",
            field=models.CharField(choices=[("RECEIPT", "正常入库"), ("SUPPLIER_RETURN", "退给供应商"), ("WARRANTY_REGISTER", "三包故障件登记"), ("WARRANTY_SEND", "三包退给供应商"), ("WARRANTY_REPLACE", "三包换回件入库"), ("ORDER_SHIPMENT", "订单配件出库")], max_length=24),
        ),
    ]
