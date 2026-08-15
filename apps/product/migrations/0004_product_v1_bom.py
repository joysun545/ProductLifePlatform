import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("product", "0003_remove_productmodel_manufacturer"),
        ("supply_chain", "0003_material_product_category"),
    ]

    operations = [
        migrations.AlterField(
            model_name="productmodel",
            name="brand",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="products",
                to="product.brand",
                verbose_name="所属品牌",
            ),
        ),
        migrations.AddField(
            model_name="bomitem",
            name="material_spec",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="bom_items",
                to="supply_chain.materialspec",
                verbose_name="物料规格",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="bomitem",
            name="unique_bom_material_supplier",
        ),
        migrations.AddConstraint(
            model_name="bomitem",
            constraint=models.UniqueConstraint(
                fields=("bom", "material"),
                name="unique_bom_material",
            ),
        ),
    ]
