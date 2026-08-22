import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import F, Q
import django.utils.timezone


def backfill_production_quantity(apps, schema_editor):
    OrderPlanItem = apps.get_model("sales", "OrderPlanItem")
    OrderPlanItem.objects.update(production_quantity=F("quantity"))


def reverse_backfill(apps, schema_editor):
    OrderPlanItem = apps.get_model("sales", "OrderPlanItem")
    OrderPlanItem.objects.update(
        return_inventory_quantity=0,
        production_quantity=F("quantity"),
    )


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("lifecycle", "0016_factory_return_material_return_facts"),
        ("sales", "0010_logistics_notice_factory_sales"),
    ]

    operations = [
        # 服务器早期部署的 0008 与当前模型在这两个字段的迁移状态上
        # 存在细微差异。0011 尚未应用，因此在本迁移中统一状态，避免
        # makemigrations 继续生成无业务意义的 0012。
        migrations.AlterField(
            model_name="orderplan",
            name="shipment_authorized_at",
            field=models.DateTimeField(
                blank=True,
                db_index=True,
                null=True,
                verbose_name="发货确认时间",
            ),
        ),
        migrations.AlterField(
            model_name="orderplan",
            name="shipment_authorized_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="authorized_order_plan_shipments",
                to=settings.AUTH_USER_MODEL,
                verbose_name="发货确认人",
            ),
        ),
        migrations.AddField(
            model_name="orderplanitem",
            name="return_inventory_quantity",
            field=models.PositiveIntegerField(
                default=0,
                verbose_name="调用退货待流转库存数量",
            ),
        ),
        migrations.AddField(
            model_name="orderplanitem",
            name="production_quantity",
            field=models.PositiveIntegerField(
                default=0,
                verbose_name="本次新生产数量",
            ),
        ),
        migrations.RunPython(backfill_production_quantity, reverse_backfill),
        migrations.AddConstraint(
            model_name="orderplanitem",
            constraint=models.CheckConstraint(
                condition=Q(
                    quantity=(
                        F("return_inventory_quantity")
                        + F("production_quantity")
                    )
                ),
                name="sales_order_item_quantity_split_matches_total",
            ),
        ),
        migrations.CreateModel(
            name="OrderPlanReturnInventoryAllocation",
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
                    "source_order_batch_snapshot",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=64,
                        verbose_name="原订单批次快照",
                    ),
                ),
                (
                    "source_lifecycle_snapshot",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=64,
                        verbose_name="原生命周期编码快照",
                    ),
                ),
                (
                    "source_qr_code_snapshot",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=128,
                        verbose_name="原二维码快照",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("RESERVED", "订单已预占"),
                            ("READY_TO_SHIP", "生产完成待发货"),
                            ("REISSUED", "已重新流转"),
                            ("RELEASED", "已释放"),
                        ],
                        db_index=True,
                        default="RESERVED",
                        max_length=20,
                        verbose_name="占用状态",
                    ),
                ),
                (
                    "reserved_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now,
                        verbose_name="预占时间",
                    ),
                ),
                (
                    "ready_to_ship_at",
                    models.DateTimeField(
                        blank=True,
                        null=True,
                        verbose_name="转待发货时间",
                    ),
                ),
                (
                    "reissued_at",
                    models.DateTimeField(
                        blank=True,
                        null=True,
                        verbose_name="重新流转时间",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                (
                    "inventory_item",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="order_allocation",
                        to="lifecycle.factoryreturninventoryitem",
                        verbose_name="退货待流转库存设备",
                    ),
                ),
                (
                    "order_plan_item",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="return_inventory_allocations",
                        to="sales.orderplanitem",
                        verbose_name="订单计划明细",
                    ),
                ),
                (
                    "original_qr",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="reissue_source_allocations",
                        to="lifecycle.qrcode",
                        verbose_name="原二维码记录",
                    ),
                ),
                (
                    "replacement_qr",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="return_inventory_allocation",
                        to="lifecycle.qrcode",
                        verbose_name="新生命周期二维码",
                    ),
                ),
                (
                    "reserved_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="reserved_return_inventory_allocations",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="预占操作人",
                    ),
                ),
                (
                    "source_bom",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="return_inventory_order_allocations",
                        to="product.bom",
                        verbose_name="退货设备实际BOM",
                    ),
                ),
                (
                    "source_device",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="return_inventory_order_allocations",
                        to="lifecycle.device",
                        verbose_name="沿用实体设备",
                    ),
                ),
            ],
            options={
                "verbose_name": "订单退货库存逐台占用",
                "verbose_name_plural": "订单退货库存逐台占用",
                "ordering": ("order_plan_item_id", "id"),
                "indexes": [
                    models.Index(
                        fields=["order_plan_item", "status"],
                        name="sales_reissue_item_status_idx",
                    ),
                    models.Index(
                        fields=["source_device", "status"],
                        name="sales_reissue_dev_st_idx",
                    ),
                ],
            },
        ),
    ]
