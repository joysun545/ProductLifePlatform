import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("account", "0011_merchant_after_sales_qualification_management"),
        ("channel", "0001_initial"),
        ("product", "0007_remove_productmodel_brand"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="MerchantProductCategoryScope",
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
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="创建时间"),
                ),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True, verbose_name="更新时间"),
                ),
                (
                    "assigned_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="assigned_merchant_product_scopes",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="授权人",
                    ),
                ),
                (
                    "brand",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="merchant_product_category_scopes",
                        to="product.brand",
                        verbose_name="授权品牌",
                    ),
                ),
                (
                    "category",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="merchant_product_category_scopes",
                        to="product.productcategory",
                        verbose_name="经营品类",
                    ),
                ),
                (
                    "relation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="merchant_product_category_scopes",
                        to="account.organizationrelation",
                        verbose_name="经销组织关系",
                    ),
                ),
            ],
            options={
                "verbose_name": "商家品牌品类范围",
                "verbose_name_plural": "商家品牌品类范围",
                "ordering": (
                    "relation__target_organization__name",
                    "brand__code",
                    "category__code",
                ),
            },
        ),
        migrations.CreateModel(
            name="MerchantProductModelScope",
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
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="创建时间"),
                ),
                (
                    "category_scope",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="model_scopes",
                        to="channel.merchantproductcategoryscope",
                        verbose_name="品牌品类范围",
                    ),
                ),
                (
                    "product_model",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="merchant_scope_grants",
                        to="product.productmodel",
                        verbose_name="授权产品型号",
                    ),
                ),
            ],
            options={
                "verbose_name": "商家授权产品型号",
                "verbose_name_plural": "商家授权产品型号",
                "ordering": ("product_model__code", "id"),
            },
        ),
        migrations.AddConstraint(
            model_name="merchantproductcategoryscope",
            constraint=models.UniqueConstraint(
                fields=("relation", "brand", "category"),
                name="channel_unique_merchant_brand_category_scope",
            ),
        ),
        migrations.AddConstraint(
            model_name="merchantproductmodelscope",
            constraint=models.UniqueConstraint(
                fields=("category_scope", "product_model"),
                name="channel_unique_merchant_product_model_scope",
            ),
        ),
    ]
