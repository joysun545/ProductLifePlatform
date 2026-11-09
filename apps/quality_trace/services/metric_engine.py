from collections import Counter, defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db import models, transaction
from django.utils import timezone

from lifecycle.models import (
    ActivationRecord,
    AfterSalesApplication,
    Device,
    DeviceMaterialFaultRecord,
)
from product.models import BOMItem, ProductModel
from quality_trace.models import (
    QualityAlert,
    QualityAlertRule,
    QualityAlertRuleVersion,
    QualityMetricSnapshot,
    QualityMetricSnapshotDevice,
    QualityTraceEvent,
)
from sales.models import OrderPlanItemMaterialBatch
from sales.services.supplier_batch_trace_service import consumed_material_lots, with_supplier_batch_id
from supply_chain.models import Material, MaterialSpec, SupplierMaterialBatch
from account.models import Organization


class QualityMetricError(Exception):
    pass


SEVERITY_RANK = {"": 0, "WARNING": 1, "URGENT": 2, "CRITICAL": 3}


def _usage_queryset(factory_id):
    return OrderPlanItemMaterialBatch.objects.filter(
        order_plan_item__plan__factory_organization_id=factory_id,
        order_plan_item__production_quantity__gt=0,
    ).select_related(
        "order_plan_item__plan",
        "order_plan_item__product_model",
        "supplier_material_batch__supplier",
        "supplier_material_batch__material",
        "supplier_material_batch__material_spec",
    )


MATERIAL_SCOPE_FIELDS = {
    "SUPPLIER_BATCH": ("supplier_material_batch_id", "supplier_batch_id"),
    "SUPPLIER": ("supplier_material_batch__supplier_id", "requirement__supplier_id"),
    "MATERIAL": ("supplier_material_batch__material_id", "requirement__material_id"),
    "MATERIAL_SPEC": ("supplier_material_batch__material_spec_id", "requirement__material_spec_id"),
}
V2_BATCH = "requirement__production_subplan_item__production_batch"


def _used_target_ids(factory_id, usages, target_type):
    legacy_field, v2_field = MATERIAL_SCOPE_FIELDS[target_type]
    lots = consumed_material_lots(factory_id)
    if target_type == "SUPPLIER_BATCH":
        lots = with_supplier_batch_id(lots)
    return models.Q(pk__in=usages.values(legacy_field)) | models.Q(pk__in=lots.values(v2_field))


def _devices_from_material_use(factory_id, usages, lots):
    legacy_codes = usages.order_by().values("order_plan_item__order_batch_code")
    production_batches = lots.values(V2_BATCH + "_id")
    return Device.objects.filter(batch__manufacturer_id=factory_id).filter(
        models.Q(batch__batch_no__in=legacy_codes) | models.Q(batch_id__in=production_batches)
    ).select_related("batch", "product_model")


def _target_rows(version):
    factory_id = version.rule.factory_organization_id
    scope = version.scope_snapshot or {}
    target_type = scope.get("target_type")
    target_ids = set(scope.get("target_ids") or [])
    usages = _usage_queryset(factory_id)

    if target_type == "MATERIAL_CONTEXT":
        material = Material.objects.filter(pk=scope.get("material_id")).first()
        if not material:
            return []
        snapshot = {
            "material_id": material.pk,
            "material_code": material.code,
            "material_name": material.name,
            "product_category_id": scope.get("product_category_id"),
            "product_model_id": scope.get("product_model_id"),
            "material_spec_id": scope.get("material_spec_id"),
            "supplier_id": scope.get("supplier_id"),
            "supplier_batch_id": scope.get("supplier_batch_id"),
        }
        key = ":".join(str(snapshot.get(name) or "*") for name in (
            "material_id", "product_category_id", "product_model_id",
            "material_spec_id", "supplier_id", "supplier_batch_id",
        ))
        return [{
            "type": target_type,
            "id": material.pk,
            "key": f"material-context:{key}",
            "snapshot": snapshot,
        }]

    if target_type == "SUPPLIER_BATCH":
        queryset = SupplierMaterialBatch.objects.filter(_used_target_ids(factory_id, usages, target_type))
        if target_ids:
            queryset = queryset.filter(pk__in=target_ids)
        return [{
            "type": target_type,
            "id": row.pk,
            "key": f"supplier-batch:{row.pk}",
            "snapshot": {
                "batch_code": row.batch_code,
                "supplier_id": row.supplier_id,
                "supplier_name": row.supplier.name,
                "material_id": row.material_id,
                "material_name": row.material.name,
                "material_spec_id": row.material_spec_id,
                "material_spec_name": row.material_spec.name,
            },
        } for row in queryset.select_related("supplier", "material", "material_spec")]

    if target_type == "PRODUCT_MODEL":
        ids = Device.objects.filter(
            batch__manufacturer_id=factory_id,
        ).values_list("product_model_id", flat=True).distinct()
        queryset = ProductModel.objects.filter(pk__in=ids)
        if target_ids:
            queryset = queryset.filter(pk__in=target_ids)
        return [{
            "type": target_type,
            "id": row.pk,
            "key": f"product-model:{row.pk}",
            "snapshot": {"product_model_id": row.pk, "code": row.code, "name": row.name},
        } for row in queryset]

    definitions = {
        "SUPPLIER": (
            Organization,
            lambda row: {"supplier_id": row.pk, "name": row.name, "code": row.code},
        ),
        "MATERIAL": (
            Material,
            lambda row: {"material_id": row.pk, "name": row.name, "code": row.code},
        ),
        "MATERIAL_SPEC": (
            MaterialSpec,
            lambda row: {
                "material_spec_id": row.pk,
                "material_id": row.material_id,
                "name": row.name,
                "code": row.code,
            },
        ),
    }
    if target_type not in definitions:
        raise QualityMetricError("规则目标类型不受支持。")
    model, serializer = definitions[target_type]
    queryset = model.objects.filter(_used_target_ids(factory_id, usages, target_type))
    if target_ids:
        queryset = queryset.filter(pk__in=target_ids)
    if target_type == "MATERIAL_SPEC":
        queryset = queryset.select_related("material")
    return [{
        "type": target_type,
        "id": row.pk,
        "key": f"{target_type.lower()}:{row.pk}",
        "snapshot": serializer(row),
    } for row in queryset]


def _material_context_devices(factory_id, snapshot):
    usages = _usage_queryset(factory_id).filter(
        supplier_material_batch__material_id=snapshot["material_id"],
    )
    lots = consumed_material_lots(factory_id).filter(requirement__material_id=snapshot["material_id"])
    filters = (
        ("product_category_id", "order_plan_item__product_model__category_id", V2_BATCH + "__product_model__category_id"),
        ("product_model_id", "order_plan_item__product_model_id", V2_BATCH + "__product_model_id"),
        ("material_spec_id", "supplier_material_batch__material_spec_id", "requirement__material_spec_id"),
        ("supplier_id", "supplier_material_batch__supplier_id", "requirement__supplier_id"),
        ("supplier_batch_id", "supplier_material_batch_id", "supplier_batch_id"),
    )
    if snapshot.get("supplier_batch_id"):
        lots = with_supplier_batch_id(lots)
    for key, legacy_field, v2_field in filters:
        value = snapshot.get(key)
        if value:
            usages = usages.filter(**{legacy_field: value})
            lots = lots.filter(**{v2_field: value})
    return _devices_from_material_use(factory_id, usages, lots)


def _candidate_devices(factory_id, target, *, excluded_contexts=None):
    target_type = target["type"]
    target_id = target["id"]
    if target_type == "MATERIAL_CONTEXT":
        queryset = _material_context_devices(factory_id, target["snapshot"])
        excluded_ids = set()
        for scope in excluded_contexts or []:
            excluded_ids.update(_material_context_devices(factory_id, scope).values_list("pk", flat=True))
        return queryset.exclude(pk__in=excluded_ids)
    if target_type == "PRODUCT_MODEL":
        return Device.objects.filter(
            batch__manufacturer_id=factory_id,
            product_model_id=target_id,
        ).select_related("batch", "product_model")

    legacy_field, v2_field = MATERIAL_SCOPE_FIELDS[target_type]
    usages = _usage_queryset(factory_id).filter(**{legacy_field: target_id})
    lots = consumed_material_lots(factory_id)
    if target_type == "SUPPLIER_BATCH":
        lots = with_supplier_batch_id(lots)
    return _devices_from_material_use(factory_id, usages, lots.filter(**{v2_field: target_id}))


def _material_matches(target, rows):
    if target["type"] == "PRODUCT_MODEL":
        return True
    snapshot = target["snapshot"]
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if target["type"] == "SUPPLIER_BATCH":
            if (
                row.get("material_id") == snapshot.get("material_id")
                and row.get("supplier_id") == snapshot.get("supplier_id")
            ):
                return True
        elif target["type"] == "MATERIAL_CONTEXT":
            if row.get("material_id") != snapshot.get("material_id"):
                continue
            if snapshot.get("material_spec_id") and row.get("material_spec_id") != snapshot.get("material_spec_id"):
                continue
            if snapshot.get("supplier_id") and row.get("supplier_id") != snapshot.get("supplier_id"):
                continue
            if snapshot.get("supplier_batch_id") and row.get("supplier_batch_id") != snapshot.get("supplier_batch_id"):
                continue
            return True
        elif target["type"] == "SUPPLIER" and row.get("supplier_id") == target["id"]:
            return True
        elif target["type"] == "MATERIAL" and row.get("material_id") == target["id"]:
            return True
        elif target["type"] == "MATERIAL_SPEC":
            if row.get("material_spec_id") == target["id"]:
                return True
            bom_item_id = row.get("bom_item_id")
            if bom_item_id and BOMItem.objects.filter(
                pk=bom_item_id,
                material_spec_id=target["id"],
            ).exists():
                return True
    return False


def _fault_record_matches(target, record):
    if target["type"] == "PRODUCT_MODEL":
        return True
    if target["type"] == "SUPPLIER_BATCH":
        snapshot = target["snapshot"]
        return (
            record.material_id == snapshot.get("material_id")
            and record.supplier_id == snapshot.get("supplier_id")
        )
    if target["type"] == "MATERIAL_CONTEXT":
        snapshot = target["snapshot"]
        return (
            record.material_id == snapshot.get("material_id")
            and (not snapshot.get("material_spec_id") or record.material_spec_id == snapshot.get("material_spec_id"))
            and (not snapshot.get("supplier_id") or record.supplier_id == snapshot.get("supplier_id"))
        )
    field = {
        "SUPPLIER": record.supplier_id,
        "MATERIAL": record.material_id,
        "MATERIAL_SPEC": record.material_spec_id,
    }[target["type"]]
    return field == target["id"]


def _root_cause_matches(policy, material):
    ids = set(policy.get("root_cause_ids") or [])
    grades = set(policy.get("root_cause_grades") or [])
    if not ids and not grades:
        return True
    cause_id = material.get("root_cause_id")
    cause_grade = str(material.get("root_cause_grade") or "").upper()
    if ids and cause_id not in ids:
        return False
    if grades and cause_grade not in grades:
        return False
    return True


def _fault_occurrences(version, target, device_ids, window_start, window_end):
    policy = version.fault_policy_snapshot or {}
    sources = set(policy.get("sources") or [])
    occurrences = defaultdict(list)
    factory_id = version.rule.factory_organization_id

    if "AFTERSALES_CLOSED" in sources:
        applications = AfterSalesApplication.objects.filter(
            device_id__in=device_ids,
            factory_organization_id=factory_id,
            status=AfterSalesApplication.Status.CLOSED,
            closed_at__gte=window_start,
            closed_at__lte=window_end,
            final_fault_type__in=(policy.get("final_fault_types") or ["COMPONENT_SUSPECTED"]),
        ).order_by("closed_at", "id")
        if policy.get("require_material_replacement"):
            applications = applications.filter(repair_material_replaced=True)
        for row in applications:
            materials = row.final_fault_materials or row.repair_materials or []
            has_cause_filter = bool(policy.get("root_cause_ids") or policy.get("root_cause_grades"))
            matches = (
                target["type"] == "PRODUCT_MODEL" and not has_cause_filter
            ) or any(
                _material_matches(target, [material]) and _root_cause_matches(policy, material)
                for material in materials if isinstance(material, dict)
            )
            if matches:
                occurrences[row.device_id].append({
                    "source": "AFTERSALES_CLOSED",
                    "source_id": row.pk,
                    "occurred_at": row.closed_at,
                })

    if "MATERIAL_FAULT_RECORD" in sources:
        records = DeviceMaterialFaultRecord.objects.filter(
            device_id__in=device_ids,
            recorded_at__gte=window_start,
            recorded_at__lte=window_end,
        ).order_by("recorded_at", "id")
        for row in records:
            if _fault_record_matches(target, row):
                occurrences[row.device_id].append({
                    "source": "MATERIAL_FAULT_RECORD",
                    "source_id": row.pk,
                    "occurred_at": row.recorded_at,
                })
    return occurrences


def _severity(version, metric_value, exposure_count, fault_count):
    if exposure_count < version.minimum_exposure:
        return ""
    if fault_count < version.minimum_fault_devices:
        return ""
    thresholds = version.threshold_snapshot or {}
    severity = ""
    for key, value in (
        ("warning", QualityAlert.Severity.WARNING),
        ("urgent", QualityAlert.Severity.URGENT),
        ("critical", QualityAlert.Severity.CRITICAL),
    ):
        if key in thresholds and metric_value >= Decimal(str(thresholds[key])):
            severity = value
    return severity


def calculate_target_metric(version, target, *, at=None, excluded_contexts=None):
    at = at or timezone.now()
    window_start = at - timedelta(days=version.window_days)
    candidates = list(_candidate_devices(
        version.rule.factory_organization_id, target, excluded_contexts=excluded_contexts,
    ).distinct())
    candidate_ids = [row.pk for row in candidates]
    denominator = version.denominator_policy_snapshot or {}
    basis = denominator.get("basis", "ACTIVATED_DEVICE")
    activations = {
        row.device_id: row
        for row in ActivationRecord.objects.filter(
            device_id__in=candidate_ids,
            status="ACTIVATED",
            activation_time__lte=at,
        )
    }
    if basis == "ACTIVATED_DEVICE":
        exposure_ids = set(activations)
        if denominator.get("cohort_window_only"):
            exposure_ids = {
                device_id for device_id, row in activations.items()
                if row.activation_time >= window_start
            }
    else:
        exposure_ids = set(candidate_ids)

    occurrences = _fault_occurrences(
        version, target, exposure_ids, window_start, at,
    )
    if version.metric_type == QualityAlertRuleVersion.MetricType.EARLY_FAULT_RATE:
        early_days = int((version.fault_policy_snapshot or {}).get("early_life_days", 90))
        occurrences = {
            device_id: rows
            for device_id, rows in occurrences.items()
            if device_id in activations and any(
                row["occurred_at"] <= activations[device_id].activation_time + timedelta(days=early_days)
                for row in rows
            )
        }
    if version.metric_type == QualityAlertRuleVersion.MetricType.REPEAT_FAULT_RATE:
        minimum = int((version.fault_policy_snapshot or {}).get("repeat_min_count", 2))
        occurrences = {
            device_id: rows for device_id, rows in occurrences.items()
            if len(rows) >= minimum
        }

    fault_ids = set(occurrences)
    exposure_count = len(exposure_ids)
    fault_count = len(fault_ids)
    if version.metric_type == QualityAlertRuleVersion.MetricType.FAULT_COUNT:
        metric_value = Decimal(fault_count)
    else:
        metric_value = (
            Decimal(fault_count) / Decimal(exposure_count)
            if exposure_count else Decimal("0")
        )
    metric_value = metric_value.quantize(Decimal("0.000001"))
    severity = _severity(version, metric_value, exposure_count, fault_count)
    evidence_limit = 500
    return {
        "window_started_at": window_start,
        "window_ended_at": at,
        "exposure_device_count": exposure_count,
        "fault_device_count": fault_count,
        "metric_value": metric_value,
        "severity": severity,
        "evidence": {
            "denominator_basis": basis,
            "candidate_device_count": len(candidate_ids),
            "exposure_device_ids": sorted(exposure_ids)[:evidence_limit],
            "fault_device_ids": sorted(fault_ids)[:evidence_limit],
            "evidence_truncated": (
                len(exposure_ids) > evidence_limit or len(fault_ids) > evidence_limit
            ),
            "fault_occurrence_counts": {
                str(device_id): len(rows)
                for device_id, rows in list(sorted(occurrences.items()))[:evidence_limit]
            },
            "policy": version.fault_policy_snapshot,
        },
        "_exposure_device_ids": exposure_ids,
        "_fault_occurrences": occurrences,
    }


@transaction.atomic
def _save_result(version, target, result, source):
    snapshot = QualityMetricSnapshot.objects.create(
        rule_version=version,
        factory_organization=version.rule.factory_organization,
        target_type=target["type"],
        target_id=target["id"],
        target_key=target["key"],
        target_snapshot=target["snapshot"],
        calculation_source=source,
        window_started_at=result["window_started_at"],
        window_ended_at=result["window_ended_at"],
        exposure_device_count=result["exposure_device_count"],
        fault_device_count=result["fault_device_count"],
        metric_value=result["metric_value"],
        threshold_met=bool(result["severity"]),
        severity=result["severity"],
        evidence_snapshot=result["evidence"],
    )
    occurrences = result.get("_fault_occurrences") or {}
    QualityMetricSnapshotDevice.objects.bulk_create([
        QualityMetricSnapshotDevice(
            metric_snapshot=snapshot,
            device_id=device_id,
            is_exposure=True,
            is_fault=device_id in occurrences,
            fault_occurrence_count=len(occurrences.get(device_id, [])),
            evidence_snapshot={
                "occurrences": [
                    {
                        "source": row["source"],
                        "source_id": row["source_id"],
                        "occurred_at": row["occurred_at"].isoformat(),
                    }
                    for row in occurrences.get(device_id, [])
                ],
            },
        )
        for device_id in sorted(result.get("_exposure_device_ids") or [])
    ])
    if not result["severity"]:
        return snapshot, None

    now = result["window_ended_at"]
    key = (
        f"factory:{version.rule.factory_organization_id}:"
        f"rule:{version.rule_id}:target:{target['type']}:{target['id']}"
    )
    alert = QualityAlert.objects.select_for_update().filter(
        deduplication_key=key,
    ).first()
    if alert:
        cooldown = timedelta(hours=version.cooldown_hours)
        is_escalation = (
            SEVERITY_RANK[result["severity"]] > SEVERITY_RANK.get(alert.severity, 0)
        )
        if not is_escalation and now - alert.last_triggered_at < cooldown:
            return snapshot, alert
        alert.rule_version = version
        alert.target_snapshot = target["snapshot"]
        alert.window_started_at = result["window_started_at"]
        alert.window_ended_at = result["window_ended_at"]
        alert.exposure_device_count = result["exposure_device_count"]
        alert.fault_device_count = result["fault_device_count"]
        alert.metric_value = result["metric_value"]
        alert.metric_snapshot = result["evidence"]
        alert.severity = result["severity"]
        if alert.status in (QualityAlert.Status.CLOSED, QualityAlert.Status.DISMISSED):
            alert.status = QualityAlert.Status.OPEN
        alert.last_triggered_at = now
        alert.trigger_count += 1
        alert.save()
        event_type = "ALERT_RETRIGGERED"
    else:
        alert = QualityAlert.objects.create(
            rule_version=version,
            factory_organization=version.rule.factory_organization,
            deduplication_key=key,
            target_type=target["type"],
            target_id=target["id"],
            target_key=target["key"],
            target_snapshot=target["snapshot"],
            window_started_at=result["window_started_at"],
            window_ended_at=result["window_ended_at"],
            exposure_device_count=result["exposure_device_count"],
            fault_device_count=result["fault_device_count"],
            metric_value=result["metric_value"],
            metric_snapshot=result["evidence"],
            severity=result["severity"],
            first_triggered_at=now,
            last_triggered_at=now,
        )
        event_type = "ALERT_TRIGGERED"
    QualityTraceEvent.objects.create(
        alert=alert,
        event_type=event_type,
        payload={
            "metric_snapshot_id": snapshot.pk,
            "severity": result["severity"],
            "metric_value": str(result["metric_value"]),
        },
    )
    return snapshot, alert


def evaluate_rule_version(version, *, source, at=None, excluded_contexts=None):
    at = at or timezone.now()
    results = []
    for target in _target_rows(version):
        result = calculate_target_metric(version, target, at=at, excluded_contexts=excluded_contexts)
        snapshot, alert = _save_result(version, target, result, source)
        results.append({
            "target_type": target["type"],
            "target_id": target["id"],
            "metric_snapshot_id": snapshot.pk,
            "exposure_device_count": result["exposure_device_count"],
            "fault_device_count": result["fault_device_count"],
            "metric_value": str(result["metric_value"]),
            "severity": result["severity"],
            "alert_id": alert.pk if alert else None,
        })
    return results


def active_rule_versions(*, factory_id=None, rule_id=None, at=None):
    at = at or timezone.now()
    rules = QualityAlertRule.objects.filter(status=QualityAlertRule.Status.ACTIVE)
    if factory_id:
        rules = rules.filter(factory_organization_id=factory_id)
    if rule_id:
        rules = rules.filter(pk=rule_id)
    versions = []
    for rule in rules.select_related("factory_organization"):
        version = rule.versions.filter(
            version=rule.current_version,
            effective_from__lte=at,
        ).filter(
            models.Q(effective_to__isnull=True) | models.Q(effective_to__gt=at)
        ).first()
        if version:
            versions.append(version)
    return versions


def _is_narrower_material_context(broad, candidate):
    names = ("product_category_id", "product_model_id", "material_spec_id", "supplier_id", "supplier_batch_id")
    if broad.get("material_id") != candidate.get("material_id"):
        return False
    strictly_narrower = False
    for name in names:
        broad_value, candidate_value = broad.get(name), candidate.get(name)
        if broad_value and broad_value != candidate_value:
            return False
        if not broad_value and candidate_value:
            strictly_narrower = True
    return strictly_narrower


def evaluate_active_rules(*, source, factory_id=None, rule_id=None, at=None):
    versions = active_rule_versions(factory_id=factory_id, rule_id=rule_id, at=at)
    results = []
    contexts = [version.scope_snapshot for version in versions if (version.scope_snapshot or {}).get("target_type") == "MATERIAL_CONTEXT"]
    for version in versions:
        scope = version.scope_snapshot or {}
        excluded_contexts = [item for item in contexts if _is_narrower_material_context(scope, item)] if scope.get("target_type") == "MATERIAL_CONTEXT" else []
        results.append({
            "rule_id": version.rule_id,
            "rule_version_id": version.pk,
            "targets": evaluate_rule_version(version, source=source, at=at, excluded_contexts=excluded_contexts),
        })
    return results
