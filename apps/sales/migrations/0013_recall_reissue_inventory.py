from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("quality_trace", "0017_recall_disposition_execution"),
        ("sales", "0012_orderplanitemmaterialbatch"),
    ]

    operations = [
        migrations.AlterField(
            model_name="orderplanreturninventoryallocation",
            name="inventory_item",
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="order_allocation", to="lifecycle.factoryreturninventoryitem", verbose_name="退货待流转库存设备"),
        ),
        migrations.AddField(
            model_name="orderplanreturninventoryallocation",
            name="recall_inventory",
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="order_allocation", to="quality_trace.qualityrecallreusableinventory", verbose_name="召回维修待流转库存设备"),
        ),
        migrations.AddConstraint(
            model_name="orderplanreturninventoryallocation",
            constraint=models.CheckConstraint(condition=models.Q(models.Q(("inventory_item__isnull", False), ("recall_inventory__isnull", True)), models.Q(("inventory_item__isnull", True), ("recall_inventory__isnull", False)), _connector="OR"), name="sales_reissue_exactly_one_source"),
        ),
    ]
