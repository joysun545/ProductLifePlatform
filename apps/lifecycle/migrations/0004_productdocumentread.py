import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("lifecycle", "0003_installationinvitation_customer_acceptance"),
        ("product", "0005_productdocument"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ProductDocumentRead",
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
                ("document_version", models.PositiveIntegerField(verbose_name="已读版本")),
                ("read_at", models.DateTimeField(default=django.utils.timezone.now, verbose_name="阅读时间")),
                (
                    "device",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="product_document_reads",
                        to="lifecycle.device",
                        verbose_name="设备",
                    ),
                ),
                (
                    "document",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="read_records",
                        to="product.productdocument",
                        verbose_name="型号资料",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="product_document_reads",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="阅读账号",
                    ),
                ),
            ],
            options={
                "verbose_name": "设备型号资料阅读记录",
                "verbose_name_plural": "设备型号资料阅读记录",
                "ordering": ("-read_at", "-id"),
            },
        ),
        migrations.AddConstraint(
            model_name="productdocumentread",
            constraint=models.UniqueConstraint(
                fields=("user", "device", "document", "document_version"),
                name="lifecycle_unique_device_document_read",
            ),
        ),
    ]
