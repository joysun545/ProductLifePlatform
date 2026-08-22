import calendar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def _add_months(value, months):
    month_index = value.month - 1 + int(months)
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def backfill_warranty_evaluations_and_awareness(apps, schema_editor):
    FaultRecord = apps.get_model("lifecycle", "DeviceMaterialFaultRecord")
    Evaluation = apps.get_model(
        "lifecycle",
        "DeviceMaterialWarrantyEvaluation",
    )
    Dispatch = apps.get_model("lifecycle", "FactoryReturnWarrantyDispatch")
    MaterialSpecSupplier = apps.get_model(
        "supply_chain",
        "MaterialSpecSupplier",
    )
    WorkItem = apps.get_model("workflow", "WorkItem")
    UserTask = apps.get_model("workflow", "UserTask")

    confirmed_by_by_inventory = {
        row.inventory_item_id: row.confirmed_by_id
        for row in Dispatch.objects.order_by("inventory_item_id", "id")
    }
    records = (
        FaultRecord.objects.select_related("source_production_batch")
        .filter(is_warranty_candidate=True)
        .order_by("id")
    )
    for record in records.iterator():
        if Evaluation.objects.filter(fault_record_id=record.pk).exists():
            continue
        production_date = record.source_production_batch.production_date
        evaluated_date = record.dismantled_at.date()
        relation = None
        result = "UNKNOWN"
        warranty_months = None
        warranty_end_date = None
        if not record.material_spec_id or not record.supplier_id:
            rule_source = "MISSING_TRACE"
        else:
            relation = MaterialSpecSupplier.objects.filter(
                material_spec_id=record.material_spec_id,
                supplier_id=record.supplier_id,
            ).first()
            if not production_date:
                rule_source = "MISSING_PRODUCTION_DATE"
            elif not relation:
                rule_source = "MISSING_EXACT_RULE"
            else:
                rule_source = "EXACT_SUPPLIER_SPEC"
                warranty_months = relation.warranty_months
                warranty_end_date = _add_months(
                    production_date,
                    warranty_months,
                )
                result = (
                    "IN_WARRANTY"
                    if evaluated_date <= warranty_end_date
                    else "OUT_OF_WARRANTY"
                )
        Evaluation.objects.create(
            fault_record_id=record.pk,
            version=1,
            result=result,
            rule_source=rule_source,
            material_spec_supplier_id=relation.pk if relation else None,
            warranty_months=warranty_months,
            warranty_start_date=production_date,
            warranty_end_date=warranty_end_date,
            evaluated_date=evaluated_date,
            supplier_name_snapshot=record.supplier_name_snapshot,
            evaluated_by_id=confirmed_by_by_inventory.get(
                record.inventory_item_id
            ),
        )

    # 未接收的旧数据转为“已通知”；已发生的历史接收快照仍保留。
    Dispatch.objects.filter(status="PENDING").update(status="NOTIFIED")
    in_warranty_dispatch_ids = set(
        Dispatch.objects.filter(
            fault_records__warranty_evaluations__result="IN_WARRANTY"
        ).values_list("id", flat=True)
    )
    workitems = WorkItem.objects.filter(
        type="FACTORY_RETURN_SUPPLIER_WARRANTY"
    ).order_by("id")
    for workitem in workitems.iterator():
        payload = workitem.payload if isinstance(workitem.payload, dict) else {}
        payload["notice_mode"] = "EACH_ACK"
        payload["schema_version"] = max(int(payload.get("schema_version") or 1), 2)
        try:
            dispatch_id = int(workitem.biz_id)
        except (TypeError, ValueError):
            dispatch_id = 0
        workitem.title = (
            "厂家已推送三包期内拆机故障物料"
            if dispatch_id in in_warranty_dispatch_ids
            else "厂家已更新拆机物料三包评估"
        )
        workitem.link = (
            "/pages/supply_chain/warranty_list/warranty_list"
            f"?dispatch_id={workitem.biz_id}"
        )
        workitem.action_mode = "EACH_ACK"
        workitem.status = "DONE"
        workitem.done_at = workitem.done_at or workitem.last_activity_at
        workitem.payload = payload
        workitem.save(
            update_fields=(
                "title",
                "link",
                "action_mode",
                "status",
                "done_at",
                "payload",
                "updated_at",
            )
        )
        UserTask.objects.filter(workitem_id=workitem.pk).update(
            need_action=False,
            display_title_snapshot=workitem.title,
            display_subtitle_snapshot=workitem.summary,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("lifecycle", "0013_device_material_fault_records"),
        ("supply_chain", "0004_suppliersalesresponsibility"),
        ("workflow", "0003_alter_workitem_action_mode"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="DeviceMaterialWarrantyEvaluation",
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
                ("version", models.PositiveSmallIntegerField(default=1, verbose_name="评估版本")),
                (
                    "result",
                    models.CharField(
                        choices=[
                            ("IN_WARRANTY", "三包期内"),
                            ("OUT_OF_WARRANTY", "已超三包期"),
                            ("UNKNOWN", "待补充规则"),
                        ],
                        db_index=True,
                        max_length=24,
                        verbose_name="评估结果",
                    ),
                ),
                (
                    "rule_source",
                    models.CharField(
                        choices=[
                            ("EXACT_SUPPLIER_SPEC", "精确供应商规格质保规则"),
                            ("MISSING_PRODUCTION_DATE", "缺少生产日期"),
                            ("MISSING_EXACT_RULE", "缺少精确供应商质保规则"),
                            ("MISSING_TRACE", "缺少规格或供应商追溯"),
                        ],
                        max_length=32,
                        verbose_name="评估规则来源",
                    ),
                ),
                ("warranty_months", models.PositiveIntegerField(blank=True, null=True, verbose_name="评估时质保月数快照")),
                ("warranty_start_date", models.DateField(blank=True, null=True, verbose_name="质保起算日")),
                ("warranty_end_date", models.DateField(blank=True, null=True, verbose_name="质保截止日")),
                ("evaluated_date", models.DateField(verbose_name="评估基准日")),
                ("supplier_name_snapshot", models.CharField(blank=True, default="", max_length=255, verbose_name="供应商名称快照")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="评估时间")),
                (
                    "evaluated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="evaluated_device_material_warranties",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="评估触发人",
                    ),
                ),
                (
                    "fault_record",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="warranty_evaluations",
                        to="lifecycle.devicematerialfaultrecord",
                        verbose_name="故障物料事实",
                    ),
                ),
                (
                    "material_spec_supplier",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="device_warranty_evaluations",
                        to="supply_chain.materialspecsupplier",
                        verbose_name="精确规格供应关系",
                    ),
                ),
            ],
            options={
                "verbose_name": "设备物料三包评估",
                "verbose_name_plural": "设备物料三包评估",
                "ordering": ("fault_record_id", "-version"),
            },
        ),
        migrations.AlterField(
            model_name="factoryreturnwarrantydispatch",
            name="status",
            field=models.CharField(
                choices=[
                    ("NOTIFIED", "已通知供应商"),
                    ("PENDING", "待供应商接收"),
                    ("RECEIVED", "供应商已接收"),
                ],
                db_index=True,
                default="NOTIFIED",
                max_length=16,
            ),
        ),
        migrations.AddConstraint(
            model_name="devicematerialwarrantyevaluation",
            constraint=models.UniqueConstraint(
                fields=("fault_record", "version"),
                name="lifecycle_unique_fault_warranty_version",
            ),
        ),
        migrations.AddIndex(
            model_name="devicematerialwarrantyevaluation",
            index=models.Index(
                fields=["result", "evaluated_date"],
                name="lifecycle_warranty_result_idx",
            ),
        ),
        migrations.RunPython(
            backfill_warranty_evaluations_and_awareness,
            migrations.RunPython.noop,
        ),
    ]
