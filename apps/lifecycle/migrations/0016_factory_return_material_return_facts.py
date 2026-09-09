import calendar
from decimal import Decimal

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def _add_months(value, months):
    if not value or months is None:
        return None
    target_index = value.month - 1 + int(months)
    year = value.year + target_index // 12
    month = target_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def backfill_material_return_facts(apps, schema_editor):
    """Preserve deployed structured history without inventing legacy text rows."""

    FaultRecord = apps.get_model("lifecycle", "DeviceMaterialFaultRecord")
    WarrantyEvaluation = apps.get_model(
        "lifecycle", "DeviceMaterialWarrantyEvaluation"
    )
    ReturnRecord = apps.get_model(
        "lifecycle", "FactoryReturnMaterialReturnRecord"
    )
    RepairRequirement = apps.get_model(
        "lifecycle", "FactoryReturnRepairMaterialRequirement"
    )
    MaterialSpecSupplier = apps.get_model(
        "supply_chain", "MaterialSpecSupplier"
    )

    for fault in FaultRecord.objects.exclude(source_bom_item_id=None).order_by("id"):
        latest = (
            WarrantyEvaluation.objects.filter(fault_record_id=fault.pk)
            .order_by("-version", "-id")
            .first()
        )
        bom_item = fault.source_bom_item
        evaluation_date = fault.dismantled_at.date()
        result = latest.result if latest else (
            "IN_WARRANTY" if fault.is_warranty_candidate else "UNKNOWN"
        )
        relation_id = latest.material_spec_supplier_id if latest else None
        record, _ = ReturnRecord.objects.get_or_create(
            inventory_item_id=fault.inventory_item_id,
            source_bom_item_id=fault.source_bom_item_id,
            return_kind="DISASSEMBLY",
            defaults={
                "device_id": fault.device_id,
                "return_transfer_id": fault.return_transfer_id,
                "source_order_plan_id": fault.source_order_plan_id,
                "source_order_plan_item_id": fault.source_order_plan_item_id,
                "source_production_batch_id": fault.source_production_batch_id,
                "source_bom_id": fault.source_bom_id,
                "material_id": fault.material_id,
                "material_spec_id": fault.material_spec_id,
                "supplier_id": fault.supplier_id,
                "material_spec_supplier_id": relation_id,
                "returned_quantity": Decimal(str(bom_item.quantity)),
                "is_fault_material": True,
                "trace_source": fault.trace_source,
                "data_origin": "LEGACY_FAULT_BACKFILL",
                "schema_version": 2,
                "actual_binding_reference": fault.actual_binding_reference,
                "material_batch_snapshot": fault.material_batch_snapshot,
                "original_note": fault.original_note,
                "returned_at": fault.dismantled_at,
                "recorded_by_id": fault.recorded_by_id,
                "recorded_organization_id": fault.recorded_organization_id,
                "recorded_role_snapshot": fault.recorded_role_snapshot,
                "warranty_result": result,
                "warranty_rule_source": (
                    latest.rule_source if latest else "MISSING_EXACT_RULE"
                ),
                "warranty_months": latest.warranty_months if latest else None,
                "warranty_start_date": (
                    latest.warranty_start_date if latest else None
                ),
                "warranty_end_date": latest.warranty_end_date if latest else None,
                "warranty_evaluated_date": (
                    latest.evaluated_date if latest else evaluation_date
                ),
                "device_sn_snapshot": fault.device_sn_snapshot,
                "return_flow_no_snapshot": fault.return_flow_no_snapshot,
                "production_batch_no_snapshot": (
                    fault.production_batch_no_snapshot
                ),
                "bom_version_snapshot": fault.bom_version_snapshot,
                "material_code_snapshot": fault.material_code_snapshot,
                "material_name_snapshot": fault.material_name_snapshot,
                "material_spec_code_snapshot": fault.material_spec_code_snapshot,
                "material_spec_name_snapshot": fault.material_spec_name_snapshot,
                "supplier_name_snapshot": fault.supplier_name_snapshot,
            },
        )
        FaultRecord.objects.filter(pk=fault.pk).update(return_record_id=record.pk)

    completed_repairs = RepairRequirement.objects.filter(
        inventory_item__production_status="COMPLETED",
    ).select_related(
        "inventory_item__process__transfer",
        "source_bom_item",
        "source_production_batch",
    )
    for requirement in completed_repairs.order_by("id"):
        item = requirement.inventory_item
        returned_at = item.production_processed_at or requirement.requested_at
        evaluated_date = returned_at.date()
        production_date = requirement.source_production_batch.production_date
        relation = None
        if requirement.material_spec_id and requirement.supplier_id:
            relation = MaterialSpecSupplier.objects.filter(
                material_spec_id=requirement.material_spec_id,
                supplier_id=requirement.supplier_id,
            ).first()
        if not requirement.material_spec_id or not requirement.supplier_id:
            result = "UNKNOWN"
            rule_source = "MISSING_TRACE"
        elif not production_date:
            result = "UNKNOWN"
            rule_source = "MISSING_PRODUCTION_DATE"
        elif not relation:
            result = "UNKNOWN"
            rule_source = "MISSING_EXACT_RULE"
        else:
            warranty_end = _add_months(production_date, relation.warranty_months)
            result = (
                "IN_WARRANTY"
                if evaluated_date <= warranty_end
                else "OUT_OF_WARRANTY"
            )
            rule_source = "EXACT_SUPPLIER_SPEC"
        warranty_end = (
            _add_months(production_date, relation.warranty_months)
            if relation and production_date
            else None
        )
        transfer = item.process.transfer
        record, _ = ReturnRecord.objects.get_or_create(
            inventory_item_id=item.pk,
            source_bom_item_id=requirement.source_bom_item_id,
            return_kind="REPAIR_REPLACED",
            defaults={
                "device_id": requirement.device_id,
                "return_transfer_id": transfer.pk,
                "source_order_plan_id": requirement.source_order_plan_id,
                "source_order_plan_item_id": requirement.source_order_plan_item_id,
                "source_production_batch_id": requirement.source_production_batch_id,
                "source_bom_id": requirement.source_bom_id,
                "material_id": requirement.material_id,
                "material_spec_id": requirement.material_spec_id,
                "supplier_id": requirement.supplier_id,
                "material_spec_supplier_id": relation.pk if relation else None,
                "returned_quantity": requirement.requested_quantity,
                "is_fault_material": False,
                "trace_source": requirement.trace_source,
                "data_origin": "LEGACY_REPAIR_BACKFILL",
                "schema_version": 2,
                "original_note": requirement.original_note,
                "returned_at": returned_at,
                "recorded_by_id": item.production_processed_by_id,
                "recorded_organization_id": transfer.to_organization_id,
                "recorded_role_snapshot": "factory_production",
                "warranty_result": result,
                "warranty_rule_source": rule_source,
                "warranty_months": relation.warranty_months if relation else None,
                "warranty_start_date": production_date,
                "warranty_end_date": warranty_end,
                "warranty_evaluated_date": evaluated_date,
                "device_sn_snapshot": requirement.device_sn_snapshot,
                "return_flow_no_snapshot": requirement.return_flow_no_snapshot,
                "production_batch_no_snapshot": (
                    requirement.production_batch_no_snapshot
                ),
                "bom_version_snapshot": requirement.bom_version_snapshot,
                "material_code_snapshot": requirement.material_code_snapshot,
                "material_name_snapshot": requirement.material_name_snapshot,
                "material_spec_code_snapshot": (
                    requirement.material_spec_code_snapshot
                ),
                "material_spec_name_snapshot": (
                    requirement.material_spec_name_snapshot
                ),
                "supplier_name_snapshot": requirement.supplier_name_snapshot,
            },
        )
        RepairRequirement.objects.filter(pk=requirement.pk).update(
            returned_material_record_id=record.pk
        )


class Migration(migrations.Migration):

    dependencies = [
        ("lifecycle", "0015_factory_return_repair_material_requirements"),
        ("supply_chain", "0004_suppliersalesresponsibility"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="FactoryReturnMaterialReturnRecord",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("return_kind", models.CharField(choices=[("DISASSEMBLY", "拆机实际退回"), ("REPAIR_REPLACED", "修复换下实际退回")], db_index=True, max_length=24, verbose_name="实际退回类型")),
                ("returned_quantity", models.DecimalField(decimal_places=2, max_digits=12, verbose_name="实际退回数量")),
                ("is_fault_material", models.BooleanField(db_index=True, default=False, verbose_name="是否为导致本次处置的故障物料")),
                ("trace_source", models.CharField(choices=[("ACTUAL_BINDING", "设备实际物料绑定"), ("PRODUCTION_BATCH", "原生产批次确认BOM"), ("BOM_INFERRED", "BOM默认供应商推定"), ("UNRESOLVED", "供应商待确认"), ("LEGACY_TEXT", "历史拆机自由文本")], db_index=True, max_length=24, verbose_name="供应商追溯来源")),
                ("data_origin", models.CharField(choices=[("OPERATION", "现场结构化操作"), ("LEGACY_FAULT_BACKFILL", "历史故障记录回填"), ("LEGACY_REPAIR_BACKFILL", "历史修复记录回填")], db_index=True, default="OPERATION", max_length=32, verbose_name="数据来源")),
                ("schema_version", models.PositiveSmallIntegerField(default=2)),
                ("actual_binding_reference", models.CharField(blank=True, default="", max_length=128)),
                ("material_batch_snapshot", models.CharField(blank=True, default="", max_length=128)),
                ("original_note", models.CharField(blank=True, default="", max_length=500)),
                ("returned_at", models.DateTimeField(db_index=True, verbose_name="实际退回/拆下时间")),
                ("recorded_role_snapshot", models.CharField(blank=True, default="", max_length=64)),
                ("warranty_result", models.CharField(choices=[("IN_WARRANTY", "三包期内"), ("OUT_OF_WARRANTY", "已超三包期"), ("UNKNOWN", "待补充规则")], db_index=True, max_length=24, verbose_name="自动质保计算结果")),
                ("warranty_rule_source", models.CharField(choices=[("EXACT_SUPPLIER_SPEC", "精确供应商规格质保规则"), ("MISSING_PRODUCTION_DATE", "缺少生产日期"), ("MISSING_EXACT_RULE", "缺少精确供应商质保规则"), ("MISSING_TRACE", "缺少规格或供应商追溯")], max_length=32, verbose_name="自动质保计算规则来源")),
                ("warranty_months", models.PositiveIntegerField(blank=True, null=True)),
                ("warranty_start_date", models.DateField(blank=True, null=True)),
                ("warranty_end_date", models.DateField(blank=True, null=True)),
                ("warranty_evaluated_date", models.DateField(verbose_name="质保计算基准日")),
                ("settlement_status", models.CharField(choices=[("PENDING", "待关联结算"), ("LINKED", "已关联结算批次"), ("SETTLED", "已完成结算"), ("NOT_APPLICABLE", "无需结算")], db_index=True, default="PENDING", max_length=20, verbose_name="退回物料结算状态")),
                ("settlement_reference", models.CharField(blank=True, default="", max_length=128, verbose_name="结算关联引用")),
                ("device_sn_snapshot", models.CharField(max_length=64)),
                ("return_flow_no_snapshot", models.CharField(max_length=64)),
                ("production_batch_no_snapshot", models.CharField(max_length=64)),
                ("bom_version_snapshot", models.CharField(max_length=32)),
                ("material_code_snapshot", models.CharField(blank=True, default="", max_length=64)),
                ("material_name_snapshot", models.CharField(max_length=128)),
                ("material_spec_code_snapshot", models.CharField(blank=True, default="", max_length=64)),
                ("material_spec_name_snapshot", models.CharField(blank=True, default="", max_length=128)),
                ("supplier_name_snapshot", models.CharField(blank=True, default="", max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("device", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="factory_return_material_records", to="lifecycle.device", verbose_name="设备")),
                ("inventory_item", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="material_return_records", to="lifecycle.factoryreturninventoryitem", verbose_name="退厂库存设备")),
                ("material", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="factory_return_material_records", to="supply_chain.material", verbose_name="实际退回物料")),
                ("material_spec", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="factory_return_material_records", to="supply_chain.materialspec", verbose_name="物料规格")),
                ("material_spec_supplier", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="factory_return_material_records", to="supply_chain.materialspecsupplier", verbose_name="质保计算采用的精确供应关系")),
                ("recorded_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="recorded_factory_return_materials", to=settings.AUTH_USER_MODEL)),
                ("recorded_organization", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="recorded_factory_return_materials", to="account.organization")),
                ("return_transfer", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="material_return_records", to="lifecycle.goodstransfer", verbose_name="退货批次")),
                ("source_bom", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="factory_return_material_records", to="product.bom", verbose_name="原BOM")),
                ("source_bom_item", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="factory_return_material_records", to="product.bomitem", verbose_name="原BOM物料行")),
                ("source_order_plan", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="factory_return_material_records", to="sales.orderplan", verbose_name="原订单计划")),
                ("source_order_plan_item", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="factory_return_material_records", to="sales.orderplanitem", verbose_name="原订单明细")),
                ("source_production_batch", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="factory_return_material_records", to="manufacturing.productionbatch", verbose_name="原生产批次")),
                ("supplier", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="factory_return_material_records", to="account.organization", verbose_name="追溯原供应商")),
            ],
            options={
                "verbose_name": "厂家退货实际退回物料事实",
                "verbose_name_plural": "厂家退货实际退回物料事实",
                "ordering": ("inventory_item_id", "return_kind", "id"),
                "indexes": [
                    models.Index(fields=["supplier", "return_kind", "returned_at"], name="lifecycle_return_supplier_idx"),
                    models.Index(fields=["warranty_result", "is_fault_material"], name="lifecycle_return_warranty_idx"),
                ],
                "constraints": [models.UniqueConstraint(fields=("inventory_item", "source_bom_item", "return_kind"), name="lifecycle_unique_return_material_kind")],
            },
        ),
        migrations.AddField(
            model_name="devicematerialfaultrecord",
            name="return_record",
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="fault_record", to="lifecycle.factoryreturnmaterialreturnrecord", verbose_name="对应实际退回物料事实"),
        ),
        migrations.AlterField(
            model_name="devicematerialfaultrecord",
            name="is_warranty_candidate",
            field=models.BooleanField(db_index=True, default=False, verbose_name="系统自动判定是否在三包期内（兼容字段）"),
        ),
        migrations.AddField(
            model_name="factoryreturnrepairmaterialrequirement",
            name="returned_material_record",
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="repair_requirement", to="lifecycle.factoryreturnmaterialreturnrecord", verbose_name="修复完成后实际拆下退回物料"),
        ),
        migrations.RunPython(backfill_material_return_facts, migrations.RunPython.noop),
    ]
