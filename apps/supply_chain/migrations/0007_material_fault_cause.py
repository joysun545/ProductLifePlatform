# Generated for the quality-alert root-cause dictionary.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("supply_chain", "0006_factory_material_inventory"),
    ]

    operations = [
        migrations.CreateModel(
            name="MaterialFaultCause",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(max_length=64, verbose_name="根因编码")),
                ("name", models.CharField(max_length=128, verbose_name="根因名称")),
                ("grade", models.CharField(choices=[("A", "A 级"), ("B", "B 级"), ("C", "C 级")], max_length=1, verbose_name="根因等级")),
                ("description", models.TextField(blank=True, default="", verbose_name="根因说明")),
                ("is_active", models.BooleanField(default=True, verbose_name="启用")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("material", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="fault_causes", to="supply_chain.material", verbose_name="所属关键物料")),
            ],
            options={
                "verbose_name": "关键物料故障根因",
                "verbose_name_plural": "关键物料故障根因",
                "ordering": ("material_id", "grade", "code"),
            },
        ),
        migrations.AddConstraint(
            model_name="materialfaultcause",
            constraint=models.UniqueConstraint(fields=("material", "code"), name="supply_unique_material_fault_cause_code"),
        ),
    ]
