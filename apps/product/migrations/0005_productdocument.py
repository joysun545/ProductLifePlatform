import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("product", "0004_product_v1_bom"),
    ]

    operations = [
        migrations.CreateModel(
            name="ProductDocument",
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
                    "document_type",
                    models.CharField(
                        choices=[
                            ("USER_MANUAL", "使用说明书"),
                            ("DAILY_MAINTENANCE", "日常维护"),
                            ("TROUBLESHOOTING_GUIDE", "自助故障排除指南"),
                        ],
                        max_length=32,
                        verbose_name="资料类型",
                    ),
                ),
                ("title", models.CharField(max_length=64, verbose_name="标题")),
                ("content", models.TextField(verbose_name="正文")),
                ("version", models.PositiveIntegerField(default=1, verbose_name="内容版本")),
                ("is_active", models.BooleanField(default=True, verbose_name="是否启用")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                (
                    "product_model",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="documents",
                        to="product.productmodel",
                        verbose_name="产品型号",
                    ),
                ),
            ],
            options={
                "verbose_name": "产品型号资料",
                "verbose_name_plural": "产品型号资料",
                "ordering": ("document_type", "id"),
            },
        ),
        migrations.AddConstraint(
            model_name="productdocument",
            constraint=models.UniqueConstraint(
                fields=("product_model", "document_type"),
                name="product_unique_model_document_type",
            ),
        ),
    ]
