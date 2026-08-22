# Generated for the structured factory-return material-fault facts.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def preserve_legacy_disassembly_text(apps, schema_editor):
    """Keep old free-text results as versioned facts without guessing materials."""

    InventoryItem = apps.get_model("lifecycle", "FactoryReturnInventoryItem")
    FaultRecord = apps.get_model("lifecycle", "DeviceMaterialFaultRecord")
    OrderPlanItem = apps.get_model("sales", "OrderPlanItem")

    rows = InventoryItem.objects.exclude(disassembly_result="").order_by("id")
    for item in rows.iterator():
        if FaultRecord.objects.filter(inventory_item_id=item.pk).exists():
            continue
        transfer_item = item.transfer_item
        process = item.process
        transfer = process.transfer
        device = transfer_item.device
        batch = item.source_production_batch
        bom = item.source_bom
        order_item = (
            OrderPlanItem.objects.filter(order_batch_code=batch.batch_no)
            .order_by("id")
            .first()
        )
        recorded_at = (
            item.production_processed_at
            or item.classified_at
            or item.created_at
        )
        FaultRecord.objects.create(
            inventory_item_id=item.pk,
            device_id=device.pk,
            return_transfer_id=transfer.pk,
            source_order_plan_id=order_item.plan_id if order_item else None,
            source_order_plan_item_id=order_item.pk if order_item else None,
            source_production_batch_id=batch.pk,
            source_bom_id=bom.pk,
            trace_source="LEGACY_TEXT",
            schema_version=1,
            is_warranty_candidate=bool(item.has_warranty_material),
            original_note=item.disassembly_result[:500],
            dismantled_at=recorded_at,
            recorded_by_id=item.production_processed_by_id,
            recorded_organization_id=transfer.to_organization_id,
            recorded_role_snapshot="factory_production",
            device_sn_snapshot=device.sn,
            lifecycle_code_snapshot=(
                order_item.lifecycle_code if order_item else ""
            ),
            return_flow_no_snapshot=transfer.flow_no,
            order_plan_code_snapshot=(
                order_item.plan.plan_code if order_item else ""
            ),
            order_batch_code_snapshot=(
                order_item.order_batch_code if order_item else ""
            ),
            production_batch_no_snapshot=batch.batch_no,
            bom_version_snapshot=bom.version,
            recorded_at=recorded_at,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("lifecycle", "0012_factory_return_internal_disposition"),
        ("sales", "0010_logistics_notice_factory_sales"),
        ("supply_chain", "0004_suppliersalesresponsibility"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="DeviceMaterialFaultRecord",
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
                    "trace_source",
                    models.CharField(
                        choices=[
                            ("ACTUAL_BINDING", "设备实际物料绑定"),
                            ("PRODUCTION_BATCH", "原生产批次确认BOM"),
                            ("BOM_INFERRED", "BOM默认供应商推定"),
                            ("UNRESOLVED", "供应商待确认"),
                            ("LEGACY_TEXT", "历史拆机自由文本"),
                        ],
                        db_index=True,
                        max_length=24,
                        verbose_name="供应商追溯来源",
                    ),
                ),
                (
                    "schema_version",
                    models.PositiveSmallIntegerField(
                        default=1,
                        verbose_name="事实结构版本",
                    ),
                ),
                (
                    "actual_binding_reference",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=128,
                        verbose_name="实际物料绑定引用",
                    ),
                ),
                (
                    "material_batch_snapshot",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=128,
                        verbose_name="实际物料批次快照",
                    ),
                ),
                (
                    "is_warranty_candidate",
                    models.BooleanField(
                        db_index=True,
                        default=False,
                        verbose_name="是否需要三包处置",
                    ),
                ),
                (
                    "original_note",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=500,
                        verbose_name="生产现场原始说明",
                    ),
                ),
                (
                    "dismantled_at",
                    models.DateTimeField(db_index=True, verbose_name="拆机时间"),
                ),
                (
                    "recorded_role_snapshot",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=64,
                        verbose_name="原始操作岗位快照",
                    ),
                ),
                (
                    "device_sn_snapshot",
                    models.CharField(max_length=64, verbose_name="设备序列号快照"),
                ),
                (
                    "lifecycle_code_snapshot",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=128,
                        verbose_name="原生命周期编码快照",
                    ),
                ),
                (
                    "return_flow_no_snapshot",
                    models.CharField(max_length=64, verbose_name="退货批次快照"),
                ),
                (
                    "order_plan_code_snapshot",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=64,
                        verbose_name="原订单计划编码快照",
                    ),
                ),
                (
                    "order_batch_code_snapshot",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=64,
                        verbose_name="原订单批次编码快照",
                    ),
                ),
                (
                    "production_batch_no_snapshot",
                    models.CharField(max_length=64, verbose_name="原生产批次快照"),
                ),
                (
                    "bom_version_snapshot",
                    models.CharField(max_length=32, verbose_name="原BOM版本快照"),
                ),
                (
                    "material_code_snapshot",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=64,
                        verbose_name="物料编码快照",
                    ),
                ),
                (
                    "material_name_snapshot",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=128,
                        verbose_name="物料名称快照",
                    ),
                ),
                (
                    "material_spec_code_snapshot",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=64,
                        verbose_name="规格编码快照",
                    ),
                ),
                (
                    "material_spec_name_snapshot",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=128,
                        verbose_name="规格名称快照",
                    ),
                ),
                (
                    "supplier_name_snapshot",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=255,
                        verbose_name="供应商名称快照",
                    ),
                ),
                (
                    "recorded_at",
                    models.DateTimeField(db_index=True, verbose_name="原始记录时间"),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="建档时间")),
                (
                    "device",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="material_fault_records",
                        to="lifecycle.device",
                        verbose_name="设备",
                    ),
                ),
                (
                    "inventory_item",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="material_fault_records",
                        to="lifecycle.factoryreturninventoryitem",
                        verbose_name="退厂库存设备",
                    ),
                ),
                (
                    "material",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="device_fault_records",
                        to="supply_chain.material",
                        verbose_name="故障关联物料",
                    ),
                ),
                (
                    "material_spec",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="device_fault_records",
                        to="supply_chain.materialspec",
                        verbose_name="物料规格",
                    ),
                ),
                (
                    "recorded_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="recorded_device_material_faults",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="原始记录人",
                    ),
                ),
                (
                    "recorded_organization",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="recorded_device_material_faults",
                        to="account.organization",
                        verbose_name="原始记录组织",
                    ),
                ),
                (
                    "return_transfer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="material_fault_records",
                        to="lifecycle.goodstransfer",
                        verbose_name="退货批次",
                    ),
                ),
                (
                    "source_bom",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="device_material_fault_records",
                        to="product.bom",
                        verbose_name="原BOM",
                    ),
                ),
                (
                    "source_bom_item",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="device_material_fault_records",
                        to="product.bomitem",
                        verbose_name="原BOM物料行",
                    ),
                ),
                (
                    "source_order_plan",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="device_material_fault_records",
                        to="sales.orderplan",
                        verbose_name="原订单计划",
                    ),
                ),
                (
                    "source_order_plan_item",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="device_material_fault_records",
                        to="sales.orderplanitem",
                        verbose_name="原订单明细",
                    ),
                ),
                (
                    "source_production_batch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="device_material_fault_records",
                        to="manufacturing.productionbatch",
                        verbose_name="原生产批次",
                    ),
                ),
                (
                    "supplier",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="supplied_device_fault_records",
                        to="account.organization",
                        verbose_name="追溯供应商",
                    ),
                ),
            ],
            options={
                "verbose_name": "设备故障物料事实",
                "verbose_name_plural": "设备故障物料事实",
                "ordering": ("inventory_item_id", "id"),
            },
        ),
        migrations.CreateModel(
            name="FactoryReturnWarrantyDispatch",
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
                ("supplier_name_snapshot", models.CharField(max_length=255, verbose_name="供应商名称快照")),
                ("confirmed_role_snapshot", models.CharField(default="factory_matching", max_length=64)),
                ("confirmed_at", models.DateTimeField(db_index=True, verbose_name="配套确认时间")),
                (
                    "status",
                    models.CharField(
                        choices=[("PENDING", "待供应商接收"), ("RECEIVED", "供应商已接收")],
                        db_index=True,
                        default="PENDING",
                        max_length=16,
                    ),
                ),
                ("received_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "confirmed_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="confirmed_factory_return_warranty_dispatches",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="配套确认人",
                    ),
                ),
                (
                    "inventory_item",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="warranty_dispatches",
                        to="lifecycle.factoryreturninventoryitem",
                        verbose_name="退厂库存设备",
                    ),
                ),
                (
                    "received_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="received_factory_return_warranty_dispatches",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "supplier",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="factory_return_warranty_dispatches",
                        to="account.organization",
                        verbose_name="供应商",
                    ),
                ),
            ],
            options={"ordering": ("-confirmed_at", "-id")},
        ),
        migrations.CreateModel(
            name="FactoryReturnWarrantyDispatchItem",
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
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "dispatch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="dispatch_items",
                        to="lifecycle.factoryreturnwarrantydispatch",
                    ),
                ),
                (
                    "fault_record",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="warranty_dispatch_item",
                        to="lifecycle.devicematerialfaultrecord",
                    ),
                ),
            ],
            options={"ordering": ("dispatch_id", "fault_record_id")},
        ),
        migrations.AddField(
            model_name="factoryreturnwarrantydispatch",
            name="fault_records",
            field=models.ManyToManyField(
                related_name="warranty_dispatches",
                through="lifecycle.FactoryReturnWarrantyDispatchItem",
                to="lifecycle.devicematerialfaultrecord",
            ),
        ),
        migrations.AddIndex(
            model_name="devicematerialfaultrecord",
            index=models.Index(
                fields=["supplier", "is_warranty_candidate", "dismantled_at"],
                name="lifecycle_fault_supplier_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="devicematerialfaultrecord",
            index=models.Index(
                fields=["material", "source_production_batch"],
                name="lifecycle_fault_material_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="devicematerialfaultrecord",
            constraint=models.UniqueConstraint(
                condition=models.Q(source_bom_item__isnull=False),
                fields=("inventory_item", "source_bom_item"),
                name="lifecycle_unique_fault_bom_item",
            ),
        ),
        migrations.AddConstraint(
            model_name="factoryreturnwarrantydispatch",
            constraint=models.UniqueConstraint(
                fields=("inventory_item", "supplier"),
                name="lifecycle_unique_warranty_dispatch",
            ),
        ),
        migrations.RunPython(
            preserve_legacy_disassembly_text,
            migrations.RunPython.noop,
        ),
    ]
