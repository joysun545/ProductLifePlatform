from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def align_completed_stages(apps, schema_editor):
    PostDisposition = apps.get_model("quality_trace", "QualityRecallPostDisposition")
    PostDisposition.objects.filter(status="COMPLETED").update(
        execution_stage="COMPLETED",
    )


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("account", "0005_add_factory_chief_engineer_role"),
        ("lifecycle", "0021_aftersales_application_workflow"),
        ("manufacturing", "0001_initial"),
        ("product", "0006_productdocument_file_fields"),
        ("quality_trace", "0016_recall_post_disposition"),
    ]

    operations = [
        migrations.AlterField(
            model_name="qualityrecallpostdisposition",
            name="stock_result",
            field=models.CharField(
                choices=[("PENDING", "待处理"), ("DISASSEMBLED", "已拆机并结束生命周期"), ("READY_FOR_OUTBOUND", "已维修，入库待流转")],
                default="PENDING", max_length=24,
            ),
        ),
        migrations.AddField(
            model_name="qualityrecallpostdisposition",
            name="execution_stage",
            field=models.CharField(
                choices=[
                    ("WAITING_PRODUCTION", "待生产经理处理"),
                    ("WAITING_SUPPLIER", "待供应商负责人确认"),
                    ("WAITING_MATCHING", "待配套经理确认"),
                    ("WAITING_PRODUCTION_COMPLETION", "待生产经理完成维修"),
                    ("WAITING_CHIEF_CONFIRMATION", "待总工程师确认"),
                    ("COMPLETED", "处置完成"),
                ],
                db_index=True,
                default="WAITING_PRODUCTION",
                max_length=40,
            ),
        ),
        migrations.AddField(
            model_name="qualityrecallpostdisposition",
            name="execution_payload",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="qualityrecallpostdisposition",
            name="matching_completed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="qualityrecallpostdisposition",
            name="matching_completed_by",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="matching_completed_recall_dispositions",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="qualityrecallpostdisposition",
            name="production_completed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="qualityrecallpostdisposition",
            name="production_completed_by",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="production_completed_recall_dispositions",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(align_completed_stages, migrations.RunPython.noop),
        migrations.CreateModel(
            name="QualityRecallDispositionSupplierConfirmation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("supplier_name_snapshot", models.CharField(max_length=255)),
                ("confirmed_at", models.DateTimeField(blank=True, null=True)),
                ("note", models.CharField(blank=True, default="", max_length=1000)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("confirmed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="confirmed_recall_disposition_suppliers", to=settings.AUTH_USER_MODEL)),
                ("disposition", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="supplier_confirmations", to="quality_trace.qualityrecallpostdisposition")),
                ("supplier", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="recall_disposition_confirmations", to="account.organization")),
            ],
            options={"ordering": ("disposition_id", "supplier_id")},
        ),
        migrations.AddConstraint(
            model_name="qualityrecalldispositionsupplierconfirmation",
            constraint=models.UniqueConstraint(fields=("disposition", "supplier"), name="qtrace_unique_disposition_supplier"),
        ),
        migrations.CreateModel(
            name="QualityRecallReusableInventory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("stock_status", models.CharField(choices=[("PENDING_REISSUE", "入库待流转"), ("RESERVED_FOR_ORDER", "已被订单占用"), ("READY_TO_SHIP", "待重新发货"), ("REISSUED", "已重新流转")], db_index=True, default="PENDING_REISSUE", max_length=32)),
                ("available_at", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("device", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="quality_recall_reusable_inventory_rows", to="lifecycle.device")),
                ("disposition", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="reusable_inventory", to="quality_trace.qualityrecallpostdisposition")),
                ("factory_organization", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="quality_recall_reusable_inventory", to="account.organization")),
                ("source_bom", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="quality_recall_reusable_inventory", to="product.bom")),
                ("source_product_model", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="quality_recall_reusable_inventory", to="product.productmodel")),
                ("source_production_batch", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="quality_recall_reusable_inventory", to="manufacturing.productionbatch")),
            ],
            options={"ordering": ("available_at", "id")},
        ),
        migrations.AddIndex(
            model_name="qualityrecallreusableinventory",
            index=models.Index(fields=["factory_organization", "source_product_model", "stock_status"], name="qtrace_reuse_available_idx"),
        ),
    ]
