import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("lifecycle", "0007_goodstransferaccountcase_goodstransferaccountpayment_and_more"),
        ("manufacturing", "0001_initial"),
        ("product", "0006_productdocument_file_fields"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="qrcode",
            name="disabled_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="禁用时间"),
        ),
        migrations.AlterField(
            model_name="goodstransfer",
            name="status",
            field=models.CharField(
                choices=[
                    ("SCANNING", "扫码中"),
                    ("PENDING_RECEIPT", "待确认入库"),
                    ("PENDING_FACTORY_PICKUP", "待厂家物流取回"),
                    ("PENDING_FACTORY_POSTING", "待退货分类入库"),
                    ("COMPLETED", "已完成"),
                    ("CANCELLED", "已取消"),
                ],
                db_index=True,
                default="SCANNING",
                max_length=24,
            ),
        ),
        migrations.AlterField(
            model_name="goodstransferitem",
            name="status",
            field=models.CharField(
                choices=[
                    ("SCANNED", "已扫码"),
                    ("PENDING_RECEIPT", "待确认入库"),
                    ("PENDING_FACTORY_PICKUP", "待厂家物流取回"),
                    ("PENDING_FACTORY_POSTING", "待退货分类入库"),
                    ("COMPLETED", "已完成"),
                ],
                db_index=True,
                default="SCANNED",
                max_length=24,
            ),
        ),
        migrations.AlterField(
            model_name="lifecycleevent",
            name="event_type",
            field=models.CharField(
                choices=[
                    ("CREATE", "设备创建"),
                    ("FACTORY_OUT", "生产出厂"),
                    ("SHIP", "发货"),
                    ("RECEIVE", "收货"),
                    ("RETURN", "退货"),
                    ("TRANSFER", "调货"),
                    ("AFTERSALE_TRANSFER", "售后转移"),
                    ("INSTALL", "设备安装"),
                    ("ACTIVATE", "设备激活"),
                    ("MAINTAIN", "维护保养"),
                    ("REPAIR", "维修"),
                    ("SCRAP", "设备报废"),
                    ("FACTORY_RETURN_PICKUP", "厂家物流取回"),
                    ("FACTORY_RETURN_INTAKE", "退货分类入库"),
                ],
                db_index=True,
                max_length=64,
                verbose_name="事件类型",
            ),
        ),
        migrations.CreateModel(
            name="FactoryReturnProcess",
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
                    "phase",
                    models.CharField(
                        choices=[
                            ("WAITING_PICKUP", "待厂家物流取回"),
                            ("WAITING_POSTING", "待销售助理下账入库"),
                            ("POSTED", "已分类入库"),
                        ],
                        db_index=True,
                        default="WAITING_PICKUP",
                        max_length=24,
                        verbose_name="流程阶段",
                    ),
                ),
                (
                    "picked_at",
                    models.DateTimeField(
                        blank=True,
                        db_index=True,
                        null=True,
                        verbose_name="取回时间",
                    ),
                ),
                (
                    "posted_at",
                    models.DateTimeField(
                        blank=True,
                        db_index=True,
                        null=True,
                        verbose_name="下账入库时间",
                    ),
                ),
                (
                    "posting_remark",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=255,
                        verbose_name="入库说明",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                (
                    "account_case",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="factory_return_processes",
                        to="lifecycle.goodstransferaccountcase",
                        verbose_name="关联退货冲抵批次",
                    ),
                ),
                (
                    "picked_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="picked_factory_returns",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="取回确认人",
                    ),
                ),
                (
                    "posted_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="posted_factory_returns",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="下账入库人",
                    ),
                ),
                (
                    "transfer",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="factory_return_process",
                        to="lifecycle.goodstransfer",
                        verbose_name="退厂商品流转单",
                    ),
                ),
            ],
            options={
                "verbose_name": "厂家退货处理流程",
                "verbose_name_plural": "厂家退货处理流程",
                "ordering": ("-created_at", "-id"),
            },
        ),
        migrations.CreateModel(
            name="FactoryReturnInventoryItem",
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
                            ("WAITING_POSTING", "待分类入库"),
                            ("PENDING_DISASSEMBLY", "入库待拆机"),
                            ("PENDING_REISSUE", "入库待流转"),
                            ("PENDING_REPAIR", "入库待修复"),
                            ("DISASSEMBLING", "拆机处理中"),
                            ("REPAIRING", "修复处理中"),
                            ("RESERVED_FOR_ORDER", "已被订单占用"),
                            ("READY_TO_SHIP", "待重新发货"),
                            ("REISSUED", "已重新流转"),
                            ("LIFECYCLE_ENDED", "生命周期已结束"),
                        ],
                        db_index=True,
                        default="WAITING_POSTING",
                        max_length=32,
                        verbose_name="退厂库存状态",
                    ),
                ),
                (
                    "classified_at",
                    models.DateTimeField(
                        blank=True,
                        db_index=True,
                        null=True,
                        verbose_name="分类时间",
                    ),
                ),
                (
                    "remark",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=255,
                        verbose_name="分类说明",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                (
                    "classified_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="classified_factory_return_inventory_items",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="分类人",
                    ),
                ),
                (
                    "process",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="inventory_items",
                        to="lifecycle.factoryreturnprocess",
                        verbose_name="退厂处理流程",
                    ),
                ),
                (
                    "source_bom",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="factory_return_inventory_items",
                        to="product.bom",
                        verbose_name="原BOM",
                    ),
                ),
                (
                    "source_product_model",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="factory_return_inventory_items",
                        to="product.productmodel",
                        verbose_name="原产品型号",
                    ),
                ),
                (
                    "source_production_batch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="factory_return_inventory_items",
                        to="manufacturing.productionbatch",
                        verbose_name="原生产批次",
                    ),
                ),
                (
                    "transfer_item",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="factory_return_inventory_item",
                        to="lifecycle.goodstransferitem",
                        verbose_name="退货逐台明细",
                    ),
                ),
            ],
            options={
                "verbose_name": "厂家退货分类库存",
                "verbose_name_plural": "厂家退货分类库存",
                "ordering": ("process_id", "transfer_item_id"),
            },
        ),
        migrations.AddIndex(
            model_name="factoryreturninventoryitem",
            index=models.Index(
                fields=["stock_status", "classified_at"],
                name="lifecycle_fr_stock_61f532_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="factoryreturninventoryitem",
            index=models.Index(
                fields=["source_product_model", "stock_status"],
                name="lifecycle_fr_model_33d2e7_idx",
            ),
        ),
    ]
