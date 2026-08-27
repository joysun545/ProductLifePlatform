from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("account", "0004_add_factory_aftersales_role"),
        ("supply_chain", "0004_suppliersalesresponsibility"),
    ]

    operations = [
        migrations.CreateModel(
            name="SupplierMaterialBatch",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("batch_code", models.CharField(max_length=128, verbose_name="供应商生产批次编码")),
                ("normalized_batch_code", models.CharField(editable=False, max_length=128)),
                ("production_date", models.DateField(blank=True, null=True, verbose_name="生产日期")),
                ("remark", models.TextField(blank=True, default="", verbose_name="备注")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="created_supplier_material_batches", to="account.user", verbose_name="首次录入账号")),
                ("material", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="supplier_batches", to="supply_chain.material", verbose_name="物料")),
                ("material_spec", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="supplier_batches", to="supply_chain.materialspec", verbose_name="物料规格")),
                ("supplier", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="material_batches", to="account.organization", verbose_name="供应商")),
            ],
            options={"ordering": ("-production_date", "-id")},
        ),
        migrations.AddConstraint(
            model_name="suppliermaterialbatch",
            constraint=models.UniqueConstraint(fields=("supplier", "material_spec", "normalized_batch_code"), name="supply_unique_supplier_spec_batch_code"),
        ),
        migrations.AddIndex(
            model_name="suppliermaterialbatch",
            index=models.Index(fields=["supplier", "material", "material_spec"], name="supply_batch_lookup"),
        ),
    ]
