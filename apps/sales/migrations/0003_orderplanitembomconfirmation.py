import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("product", "0006_productdocument_file_fields"),
        ("sales", "0002_order_plan"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="OrderPlanItemBOMConfirmation",
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
                    "has_changes",
                    models.BooleanField(default=False, verbose_name="是否生成新版本"),
                ),
                (
                    "confirmed_at",
                    models.DateTimeField(
                        db_index=True,
                        default=django.utils.timezone.now,
                        verbose_name="确认时间",
                    ),
                ),
                (
                    "confirmed_bom",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="order_plan_confirmations",
                        to="product.bom",
                        verbose_name="订单确认BOM",
                    ),
                ),
                (
                    "confirmed_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="confirmed_order_plan_boms",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="确认配套经理",
                    ),
                ),
                (
                    "order_plan_item",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="bom_confirmation",
                        to="sales.orderplanitem",
                        verbose_name="订单计划明细",
                    ),
                ),
                (
                    "source_bom",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="order_plan_source_confirmations",
                        to="product.bom",
                        verbose_name="默认继承BOM",
                    ),
                ),
            ],
            options={
                "verbose_name": "订单明细BOM确认",
                "verbose_name_plural": "订单明细BOM确认",
                "ordering": ("-confirmed_at", "-id"),
            },
        ),
    ]
