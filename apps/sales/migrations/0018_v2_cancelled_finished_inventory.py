"""Keep completed cancelled V2 goods as reusable finished inventory.

The V2 cancellation path must never delete completed production.  Instead the
factory finished-goods keeper records the physical devices as neutral factory
stock, so a later order can reserve them without retaining the cancelled
order's merchant or sales-manager responsibility.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0017_backfill_v2_shipment_logistics_tasks"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="FactoryFinishedGoodsInventoryItem",
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
                    "stock_status",
                    models.CharField(
                        choices=[
                            ("PENDING_REISSUE", "成品库存待调用"),
                            ("RESERVED_FOR_ORDER", "已被订单占用"),
                            ("READY_TO_SHIP", "待重新发货"),
                            ("REISSUED", "已重新流转"),
                        ],
                        db_index=True,
                        default="PENDING_REISSUE",
                        max_length=32,
                        verbose_name="成品库存状态",
                    ),
                ),
                (
                    "stored_at",
                    models.DateTimeField(
                        db_index=True,
                        default=django.utils.timezone.now,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "device",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="factory_finished_goods_inventory_item",
                        to="lifecycle.device",
                        verbose_name="成品设备",
                    ),
                ),
                (
                    "factory_organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="finished_goods_inventory_items",
                        to="account.organization",
                        verbose_name="厂家",
                    ),
                ),
                (
                    "source_bom",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="factory_finished_goods_inventory_items",
                        to="product.bom",
                        verbose_name="实际BOM",
                    ),
                ),
                (
                    "source_order_plan",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="cancelled_finished_goods_inventory_items",
                        to="sales.orderplan",
                        verbose_name="来源订单计划",
                    ),
                ),
                (
                    "source_product_model",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="factory_finished_goods_inventory_items",
                        to="product.productmodel",
                        verbose_name="产品型号",
                    ),
                ),
                (
                    "source_production_batch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="factory_finished_goods_inventory_items",
                        to="manufacturing.productionbatch",
                        verbose_name="生产批次",
                    ),
                ),
                (
                    "source_subplan_item",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="finished_goods_inventory_items",
                        to="sales.orderplanproductionsubplanitem",
                        verbose_name="来源生产子计划产品项",
                    ),
                ),
                (
                    "stored_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="stored_finished_goods_inventory_items",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="入成品库存确认人",
                    ),
                ),
            ],
            options={
                "verbose_name": "厂家成品库存设备",
                "verbose_name_plural": "厂家成品库存设备",
                "ordering": ("stored_at", "id"),
                "indexes": [
                    models.Index(
                        fields=[
                            "factory_organization",
                            "source_product_model",
                            "stock_status",
                        ],
                        name="sales_fg_available_idx",
                    ),
                ],
            },
        ),
        migrations.AddField(
            model_name="orderplanreturninventoryallocation",
            name="finished_inventory_item",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="order_allocation",
                to="sales.factoryfinishedgoodsinventoryitem",
                verbose_name="厂家成品库存设备",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="orderplanreturninventoryallocation",
            name="sales_reissue_exactly_one_source",
        ),
        migrations.AddConstraint(
            model_name="orderplanreturninventoryallocation",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        ("finished_inventory_item__isnull", True),
                        ("inventory_item__isnull", False),
                        ("recall_inventory__isnull", True),
                    )
                    | models.Q(
                        ("finished_inventory_item__isnull", True),
                        ("inventory_item__isnull", True),
                        ("recall_inventory__isnull", False),
                    )
                    | models.Q(
                        ("finished_inventory_item__isnull", False),
                        ("inventory_item__isnull", True),
                        ("recall_inventory__isnull", True),
                    )
                ),
                name="sales_reissue_exactly_one_source",
            ),
        ),
        migrations.AlterField(
            model_name="orderplancancellationaudit",
            name="status",
            field=models.CharField(
                choices=[
                    ("CANCELLED", "已取消"),
                    ("WAITING_PRODUCTION_COMPLETION", "待生产完成后取消"),
                    ("WAITING_FINISHED_STOCK", "待转入成品库存后取消"),
                    ("REJECTED_AFTER_SHIPMENT", "已发货不可取消"),
                ],
                db_index=True,
                max_length=40,
            ),
        ),
    ]
