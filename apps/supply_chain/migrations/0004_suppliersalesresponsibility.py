import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("account", "0003_add_factory_production_role"),
        ("product", "0006_productdocument_file_fields"),
        ("supply_chain", "0003_material_product_category"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SupplierSalesResponsibility",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "scope_type",
                    models.CharField(
                        choices=[("CATEGORY", "按产品品类"), ("MATERIAL", "按物料名称")],
                        db_index=True,
                        max_length=16,
                        verbose_name="责任范围类型",
                    ),
                ),
                ("is_active", models.BooleanField(default=True, verbose_name="是否有效")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                (
                    "assigned_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="assigned_supplier_sales_responsibilities",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="分配负责人账号",
                    ),
                ),
                (
                    "material",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="supplier_sales_responsibilities",
                        to="supply_chain.material",
                        verbose_name="负责物料",
                    ),
                ),
                (
                    "product_category",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="supplier_sales_responsibilities",
                        to="product.productcategory",
                        verbose_name="负责产品品类",
                    ),
                ),
                (
                    "sales_identity",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="supplier_sales_responsibilities",
                        to="account.userorganization",
                        verbose_name="负责销售经理身份",
                    ),
                ),
                (
                    "supplier",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="sales_responsibilities",
                        to="account.organization",
                        verbose_name="供应商组织",
                    ),
                ),
            ],
            options={
                "verbose_name": "供应商销售责任范围",
                "verbose_name_plural": "供应商销售责任范围",
                "ordering": ("supplier_id", "scope_type", "id"),
                "indexes": [
                    models.Index(fields=["supplier", "scope_type", "is_active"], name="supply_chai_supplie_2f12f2_idx"),
                    models.Index(fields=["sales_identity", "is_active"], name="supply_chai_sales_i_6fd16a_idx"),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=(
                            models.Q(("material__isnull", True), ("product_category__isnull", False), ("scope_type", "CATEGORY"))
                            | models.Q(("material__isnull", False), ("product_category__isnull", True), ("scope_type", "MATERIAL"))
                        ),
                        name="supply_chain_supplier_responsibility_scope_consistent",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("scope_type", "CATEGORY")),
                        fields=("supplier", "product_category"),
                        name="supply_chain_unique_supplier_category_responsibility",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("scope_type", "MATERIAL")),
                        fields=("supplier", "material"),
                        name="supply_chain_unique_supplier_material_responsibility",
                    ),
                ],
            },
        ),
    ]
