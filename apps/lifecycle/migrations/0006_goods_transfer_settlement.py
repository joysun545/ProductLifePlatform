import django.db.models.deletion
import lifecycle.private_storage
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("account", "0003_add_factory_production_role"),
        ("lifecycle", "0005_goods_transfer"),
    ]

    operations = [
        migrations.CreateModel(
            name="GoodsTransferSettlement",
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
                    "statement_image",
                    models.FileField(
                        blank=True,
                        storage=lifecycle.private_storage.GoodsTransferPrivateStorage(),
                        upload_to="statements/%Y/%m/",
                        verbose_name="货品清单图片",
                    ),
                ),
                (
                    "statement_original_name",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=255,
                        verbose_name="清单原文件名",
                    ),
                ),
                (
                    "receivable_amount",
                    models.DecimalField(
                        blank=True,
                        decimal_places=2,
                        max_digits=14,
                        null=True,
                        verbose_name="应收货款",
                    ),
                ),
                (
                    "statement_submitted_at",
                    models.DateTimeField(
                        blank=True,
                        db_index=True,
                        null=True,
                        verbose_name="清单提交时间",
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
                    "statement_submitted_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="submitted_goods_transfer_statements",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="清单提交人",
                    ),
                ),
                (
                    "transfer",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="settlement",
                        to="lifecycle.goodstransfer",
                        verbose_name="商品流转单",
                    ),
                ),
            ],
            options={
                "verbose_name": "商品流转应收主账",
                "verbose_name_plural": "商品流转应收主账",
                "ordering": ("-created_at", "-id"),
            },
        ),
        migrations.CreateModel(
            name="GoodsTransferLogisticsDocument",
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
                ("sequence_no", models.PositiveIntegerField(verbose_name="物流单据序号")),
                ("client_request_id", models.CharField(max_length=64, verbose_name="客户端请求标识")),
                (
                    "document_file",
                    models.FileField(
                        storage=lifecycle.private_storage.GoodsTransferPrivateStorage(),
                        upload_to="logistics_documents/%Y/%m/",
                        verbose_name="物流单据",
                    ),
                ),
                ("original_name", models.CharField(blank=True, default="", max_length=255, verbose_name="原文件名")),
                ("file_type", models.CharField(choices=[("IMAGE", "图片"), ("PDF", "PDF")], max_length=12, verbose_name="文件类型")),
                ("content_type", models.CharField(blank=True, default="", max_length=100, verbose_name="文件内容类型")),
                ("file_size", models.PositiveBigIntegerField(default=0, verbose_name="文件大小")),
                ("remark", models.CharField(blank=True, default="", max_length=255, verbose_name="备注")),
                ("uploaded_at", models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="上传时间")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                (
                    "settlement",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="logistics_documents",
                        to="lifecycle.goodstransfersettlement",
                        verbose_name="商品流转应收主账",
                    ),
                ),
                (
                    "uploaded_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="uploaded_goods_transfer_logistics_documents",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="上传人",
                    ),
                ),
            ],
            options={
                "verbose_name": "商品流转物流单据",
                "verbose_name_plural": "商品流转物流单据",
                "ordering": ("sequence_no", "id"),
            },
        ),
        migrations.CreateModel(
            name="GoodsTransferPayment",
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
                ("sequence_no", models.PositiveIntegerField(verbose_name="付款批次")),
                ("client_request_id", models.CharField(max_length=64, verbose_name="客户端请求标识")),
                (
                    "voucher_image",
                    models.FileField(
                        storage=lifecycle.private_storage.GoodsTransferPrivateStorage(),
                        upload_to="payment_vouchers/%Y/%m/",
                        verbose_name="付款凭证",
                    ),
                ),
                ("voucher_original_name", models.CharField(blank=True, default="", max_length=255, verbose_name="凭证原文件名")),
                ("uploaded_at", models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="上传时间")),
                ("confirmed_amount", models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True, verbose_name="确认到账金额")),
                ("confirmed_at", models.DateTimeField(blank=True, db_index=True, null=True, verbose_name="确认时间")),
                ("status", models.CharField(choices=[("PENDING", "待确认收款"), ("CONFIRMED", "已确认收款")], db_index=True, default="PENDING", max_length=16, verbose_name="收款状态")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                (
                    "confirmed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="confirmed_goods_transfer_payments",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="收款确认人",
                    ),
                ),
                (
                    "settlement",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="payments",
                        to="lifecycle.goodstransfersettlement",
                        verbose_name="商品流转应收主账",
                    ),
                ),
                (
                    "uploaded_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="uploaded_goods_transfer_payment_vouchers",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="凭证上传人",
                    ),
                ),
            ],
            options={
                "verbose_name": "商品流转付款记录",
                "verbose_name_plural": "商品流转付款记录",
                "ordering": ("sequence_no", "id"),
            },
        ),
        migrations.AddConstraint(
            model_name="goodstransfersettlement",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(("receivable_amount__isnull", True))
                    | models.Q(("receivable_amount__gt", 0))
                ),
                name="lifecycle_gt_receivable_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="goodstransferlogisticsdocument",
            constraint=models.UniqueConstraint(
                fields=("settlement", "sequence_no"),
                name="lifecycle_gt_logistics_sequence",
            ),
        ),
        migrations.AddConstraint(
            model_name="goodstransferlogisticsdocument",
            constraint=models.UniqueConstraint(
                fields=("settlement", "client_request_id"),
                name="lifecycle_gt_logistics_request",
            ),
        ),
        migrations.AddConstraint(
            model_name="goodstransferpayment",
            constraint=models.UniqueConstraint(
                fields=("settlement", "sequence_no"),
                name="lifecycle_gt_payment_sequence",
            ),
        ),
        migrations.AddConstraint(
            model_name="goodstransferpayment",
            constraint=models.UniqueConstraint(
                fields=("settlement", "client_request_id"),
                name="lifecycle_gt_payment_request",
            ),
        ),
        migrations.AddConstraint(
            model_name="goodstransferpayment",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(("confirmed_amount__isnull", True))
                    | models.Q(("confirmed_amount__gt", 0))
                ),
                name="lifecycle_gt_payment_positive",
            ),
        ),
    ]
