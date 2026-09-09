import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("account", "0002_seed_account_reference_data"),
        ("product", "0006_productdocument_file_fields"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="MerchantChannelAssignment",
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
                ("region", models.CharField(max_length=255, verbose_name="负责区域")),
                ("is_active", models.BooleanField(default=True, verbose_name="是否有效")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                (
                    "assigned_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="assigned_merchant_channels",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="指定销售经理",
                    ),
                ),
                (
                    "brand",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="merchant_channel_assignments",
                        to="product.brand",
                        verbose_name="授权品牌",
                    ),
                ),
                (
                    "relation",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="merchant_channel_assignment",
                        to="account.organizationrelation",
                        verbose_name="经销组织关系",
                    ),
                ),
            ],
            options={
                "verbose_name": "商家渠道授权",
                "verbose_name_plural": "商家渠道授权",
                "ordering": ("relation__target_organization__name", "id"),
            },
        ),
    ]
