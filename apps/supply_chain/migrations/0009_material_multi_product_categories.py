from django.db import migrations, models


def copy_single_category_to_multi_category(apps, schema_editor):
    Material = apps.get_model("supply_chain", "Material")
    through = Material.product_categories.through
    for material in Material.objects.exclude(product_category__isnull=True).iterator():
        through.objects.get_or_create(
            material_id=material.pk,
            productcategory_id=material.product_category_id,
        )


class Migration(migrations.Migration):
    dependencies = [("supply_chain", "0008_supplier_sales_material_only")]

    operations = [
        migrations.AddField(
            model_name="material",
            name="product_categories",
            field=models.ManyToManyField(
                blank=True,
                related_name="materials_multi_category_migration",
                to="product.ProductCategory",
                verbose_name="适用产品品类",
            ),
        ),
        migrations.RunPython(copy_single_category_to_multi_category, migrations.RunPython.noop),
        migrations.RemoveField(model_name="material", name="product_category"),
        migrations.AlterField(
            model_name="material",
            name="product_categories",
            field=models.ManyToManyField(
                blank=True,
                related_name="materials",
                to="product.ProductCategory",
                verbose_name="适用产品品类",
            ),
        ),
    ]
