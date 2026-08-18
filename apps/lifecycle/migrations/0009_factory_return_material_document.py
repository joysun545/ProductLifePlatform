import django.db.models.deletion
import lifecycle.private_storage
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("lifecycle", "0008_factory_return_process"),
    ]

    operations = [
        migrations.CreateModel(
            name="FactoryReturnMaterialDocument",
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
                ("sequence_no", models.PositiveIntegerField(verbose_name="物料单据序号")),
                (
                    "client_request_id",
                    models.CharField(max_length=64, verbose_name="客户端请求标识"),
                ),
                (
                    "document_file",
                    models.FileField(
                        storage=lifecycle.private_storage.GoodsTransferPrivateStorage(),
                        upload_to="factory_return_material_documents/%Y/%m/",
                        verbose_name="退货物料单据",
                    ),
                ),
                (
                    "original_name",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=255,
                        verbose_name="原文件名",
                    ),
                ),
                (
                    "file_type",
                    models.CharField(
                        choices=[("IMAGE", "图片"), ("PDF", "PDF")],
                        max_length=12,
                        verbose_name="文件类型",
                    ),
                ),
                (
                    "content_type",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=100,
                        verbose_name="文件内容类型",
                    ),
                ),
                (
                    "file_size",
                    models.PositiveBigIntegerField(default=0, verbose_name="文件大小"),
                ),
                (
                    "remark",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=255,
                        verbose_name="备注",
                    ),
                ),
                (
                    "uploaded_at",
                    models.DateTimeField(
                        auto_now_add=True,
                        db_index=True,
                        verbose_name="上传时间",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                (
                    "process",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="material_documents",
                        to="lifecycle.factoryreturnprocess",
                        verbose_name="厂家退货处理流程",
                    ),
                ),
                (
                    "uploaded_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="uploaded_factory_return_material_documents",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="上传人",
                    ),
                ),
            ],
            options={
                "verbose_name": "厂家退货物料单据",
                "verbose_name_plural": "厂家退货物料单据",
                "ordering": ("sequence_no", "id"),
            },
        ),
        migrations.AddConstraint(
            model_name="factoryreturnmaterialdocument",
            constraint=models.UniqueConstraint(
                fields=("process", "sequence_no"),
                name="lifecycle_fr_material_sequence",
            ),
        ),
        migrations.AddConstraint(
            model_name="factoryreturnmaterialdocument",
            constraint=models.UniqueConstraint(
                fields=("process", "client_request_id"),
                name="lifecycle_fr_material_request",
            ),
        ),
    ]
