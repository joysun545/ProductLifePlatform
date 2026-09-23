import django.db.models.deletion

from django.db import migrations, models


def discard_legacy_category_responsibilities(apps, schema_editor):
    Responsibility = apps.get_model("supply_chain", "SupplierSalesResponsibility")
    Responsibility.objects.filter(scope_type="CATEGORY").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("supply_chain", "0007_material_fault_cause"),
    ]

    operations = [
        migrations.RunPython(
            discard_legacy_category_responsibilities,
            migrations.RunPython.noop,
        ),
        migrations.RemoveConstraint(
            model_name="suppliersalesresponsibility",
            name="supply_chain_supplier_responsibility_scope_consistent",
        ),
        migrations.RemoveConstraint(
            model_name="suppliersalesresponsibility",
            name="supply_chain_unique_supplier_category_responsibility",
        ),
        migrations.RemoveConstraint(
            model_name="suppliersalesresponsibility",
            name="supply_chain_unique_supplier_material_responsibility",
        ),
        migrations.RemoveField(
            model_name="suppliersalesresponsibility",
            name="product_category",
        ),
        migrations.AlterField(
            model_name="suppliersalesresponsibility",
            name="material",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="supplier_sales_responsibilities",
                to="supply_chain.material",
                verbose_name="负责物料",
            ),
        ),
        migrations.AlterField(
            model_name="suppliersalesresponsibility",
            name="scope_type",
            field=models.CharField(
                choices=[("MATERIAL", "按物料类型")],
                db_index=True,
                max_length=16,
                verbose_name="责任范围类型",
            ),
        ),
        migrations.AlterModelOptions(
            name="suppliersalesresponsibility",
            options={
                "ordering": ("supplier_id", "material_id", "id"),
                "verbose_name": "供应商销售责任范围",
                "verbose_name_plural": "供应商销售责任范围",
            },
        ),
        migrations.AddConstraint(
            model_name="suppliersalesresponsibility",
            constraint=models.UniqueConstraint(
                fields=("supplier", "material"),
                name="supply_chain_unique_supplier_material_responsibility",
            ),
        ),
    ]
