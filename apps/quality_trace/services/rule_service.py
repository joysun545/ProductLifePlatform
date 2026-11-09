from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from quality_trace.models import QualityAlertRule, QualityAlertRuleVersion
from product.models import ProductCategory, ProductModel
from supply_chain.models import Material, MaterialFaultCause, MaterialSpec, SupplierMaterialBatch

from .access_service import QualityTraceAccessError, get_quality_trace_identity


class QualityRuleError(Exception):
    pass


RULE_MANAGER_ROLES = {"factory_chief_engineer"}
TARGET_TYPES = {
    "SUPPLIER_BATCH",
    "PRODUCT_MODEL",
    "SUPPLIER",
    "MATERIAL",
    "MATERIAL_SPEC",
    "MATERIAL_CONTEXT",
}
DENOMINATOR_BASES = {"GENERATED_DEVICE", "ACTIVATED_DEVICE"}
SUPPORTED_METRICS = {
    QualityAlertRuleVersion.MetricType.FAULT_RATE,
    QualityAlertRuleVersion.MetricType.EARLY_FAULT_RATE,
    QualityAlertRuleVersion.MetricType.REPEAT_FAULT_RATE,
    QualityAlertRuleVersion.MetricType.FAULT_COUNT,
}


def _positive_int(value, field, *, allow_zero=False):
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise QualityRuleError(f"{field}必须为整数。") from exc
    minimum = 0 if allow_zero else 1
    if result < minimum:
        raise QualityRuleError(f"{field}必须大于等于{minimum}。")
    return result


def _datetime(value, field, *, required=True):
    if not value and not required:
        return None
    if hasattr(value, "tzinfo"):
        result = value
    else:
        result = parse_datetime(str(value or ""))
    if not result:
        raise QualityRuleError(f"{field}时间格式无效。")
    if timezone.is_naive(result):
        result = timezone.make_aware(result)
    return result


def _thresholds(value, metric_type):
    rows = value if isinstance(value, dict) else {}
    parsed = {}
    for name in ("warning", "urgent", "critical"):
        raw = rows.get(name)
        if raw in (None, ""):
            continue
        try:
            number = Decimal(str(raw))
        except (InvalidOperation, ValueError) as exc:
            raise QualityRuleError(f"{name}阈值格式无效。") from exc
        if number < 0:
            raise QualityRuleError(f"{name}阈值不能为负数。")
        parsed[name] = str(number)
    if "warning" not in parsed:
        raise QualityRuleError("至少设置预警阈值。")
    ordered = [Decimal(parsed[name]) for name in ("warning", "urgent", "critical") if name in parsed]
    if ordered != sorted(ordered):
        raise QualityRuleError("预警、紧急、重大阈值必须由低到高。")
    if metric_type != QualityAlertRuleVersion.MetricType.FAULT_COUNT:
        if any(number > 1 for number in ordered):
            raise QualityRuleError("故障率阈值使用0到1之间的小数。")
    return parsed


def _optional_id(value, field):
    if value in (None, ""):
        return None
    return _positive_int(value, field)


def _scope(value):
    result = dict(value) if isinstance(value, dict) else {}
    target_type = str(result.get("target_type") or "").strip().upper()
    if target_type not in TARGET_TYPES:
        raise QualityRuleError("规则目标类型无效。")
    if target_type == "MATERIAL_CONTEXT":
        material_id = _positive_int(result.get("material_id"), "物料")
        material = Material.objects.filter(pk=material_id).first()
        if not material:
            raise QualityRuleError("物料不存在。")
        product_category_id = _optional_id(result.get("product_category_id"), "产品品类")
        product_model_id = _optional_id(result.get("product_model_id"), "产品型号")
        material_spec_id = _optional_id(result.get("material_spec_id"), "物料规格")
        supplier_id = _optional_id(result.get("supplier_id"), "供应商")
        supplier_batch_id = _optional_id(result.get("supplier_batch_id"), "供应商批次")
        if product_category_id and not ProductCategory.objects.filter(pk=product_category_id).exists():
            raise QualityRuleError("产品品类不存在。")
        if product_model_id:
            model = ProductModel.objects.filter(pk=product_model_id).select_related("category").first()
            if not model:
                raise QualityRuleError("产品型号不存在。")
            if product_category_id and model.category_id != product_category_id:
                raise QualityRuleError("产品型号不属于所选品类。")
            product_category_id = product_category_id or model.category_id
        if material_spec_id and not MaterialSpec.objects.filter(pk=material_spec_id, material_id=material_id).exists():
            raise QualityRuleError("物料规格不属于所选物料。")
        if supplier_batch_id:
            batch = SupplierMaterialBatch.objects.filter(pk=supplier_batch_id).first()
            if not batch or batch.material_id != material_id:
                raise QualityRuleError("供应商批次不属于所选物料。")
            if material_spec_id and batch.material_spec_id != material_spec_id:
                raise QualityRuleError("供应商批次与物料规格不一致。")
            if supplier_id and batch.supplier_id != supplier_id:
                raise QualityRuleError("供应商批次与供应商不一致。")
            material_spec_id = material_spec_id or batch.material_spec_id
            supplier_id = supplier_id or batch.supplier_id
        return {
            "target_type": target_type,
            "material_id": material_id,
            "product_category_id": product_category_id,
            "product_model_id": product_model_id,
            "material_spec_id": material_spec_id,
            "supplier_id": supplier_id,
            "supplier_batch_id": supplier_batch_id,
        }

    raw_ids = result.get("target_ids") or []
    if not isinstance(raw_ids, list):
        raise QualityRuleError("target_ids必须为数组。")
    try:
        target_ids = sorted({int(item) for item in raw_ids})
    except (TypeError, ValueError) as exc:
        raise QualityRuleError("target_ids只能包含整数主键。") from exc
    if any(item <= 0 for item in target_ids):
        raise QualityRuleError("target_ids必须为正整数。")
    return {"target_type": target_type, "target_ids": target_ids}


def _fault_policy(value, scope):
    result = dict(value) if isinstance(value, dict) else {}
    result.setdefault("sources", ["AFTERSALES_CLOSED", "MATERIAL_FAULT_RECORD"])
    result.setdefault("final_fault_types", ["COMPONENT_SUSPECTED"])
    result.setdefault("require_material_replacement", False)
    result.setdefault("early_life_days", 90)
    result.setdefault("repeat_min_count", 2)
    raw_cause_ids = result.get("root_cause_ids") or []
    raw_grades = result.get("root_cause_grades") or []
    if not isinstance(raw_cause_ids, list) or not isinstance(raw_grades, list):
        raise QualityRuleError("根因筛选必须为数组。")
    cause_ids = sorted({_positive_int(item, "根因") for item in raw_cause_ids})
    grades = sorted({str(item).strip().upper() for item in raw_grades if str(item).strip()})
    if any(item not in {"A", "B", "C"} for item in grades):
        raise QualityRuleError("根因等级仅支持A、B、C。")
    if cause_ids:
        material_id = scope.get("material_id") if scope.get("target_type") == "MATERIAL_CONTEXT" else None
        if not material_id:
            raise QualityRuleError("按根因统计必须使用“物料范围”规则。")
        causes = MaterialFaultCause.objects.filter(pk__in=cause_ids)
        if causes.count() != len(cause_ids):
            raise QualityRuleError("存在不存在的根因。")
        if material_id and causes.exclude(material_id=material_id).exists():
            raise QualityRuleError("根因必须属于规则物料。")
    if (cause_ids or grades) and "AFTERSALES_CLOSED" not in set(result["sources"]):
        raise QualityRuleError("按根因统计必须包含已关闭售后来源。")
    result["root_cause_ids"] = cause_ids
    result["root_cause_grades"] = grades
    return result


def _factory_rule_manager(user, *, web_authorized=False):
    try:
        identity = get_quality_trace_identity(user)
    except QualityTraceAccessError as exc:
        raise QualityRuleError(str(exc)) from exc
    if identity.organization.organization_type.code != "OWNER":
        raise QualityRuleError("只有厂家可以配置质量预警规则。")
    from web_portal.access import identity_can_access_module
    allowed = identity_can_access_module(identity, "quality") if web_authorized else identity.role.code in RULE_MANAGER_ROLES
    if not allowed:
        raise QualityRuleError("当前岗位无权发布质量预警规则。")
    return identity


@transaction.atomic
def publish_rule_version(*, user, data, web_authorized=False):
    identity = _factory_rule_manager(user, web_authorized=web_authorized)
    payload = data if isinstance(data, dict) else {}
    metric_type = str(payload.get("metric_type") or "").strip().upper()
    if metric_type not in SUPPORTED_METRICS:
        raise QualityRuleError("当前版本不支持该指标类型。")
    scope_snapshot = _scope(payload.get("scope"))
    denominator = dict(payload.get("denominator_policy") or {})
    basis = str(denominator.get("basis") or "ACTIVATED_DEVICE").strip().upper()
    if basis not in DENOMINATOR_BASES:
        raise QualityRuleError("分母口径仅支持已生成设备或已激活设备。")
    denominator["basis"] = basis
    denominator["cohort_window_only"] = bool(denominator.get("cohort_window_only", False))

    fault_policy = _fault_policy(payload.get("fault_policy"), scope_snapshot)

    effective_from = _datetime(payload.get("effective_from") or timezone.now(), "生效")
    effective_to = _datetime(payload.get("effective_to"), "失效", required=False)
    if effective_to and effective_to <= effective_from:
        raise QualityRuleError("失效时间必须晚于生效时间。")

    rule_id = payload.get("rule_id")
    if rule_id:
        rule = QualityAlertRule.objects.select_for_update().filter(
            pk=rule_id,
            factory_organization=identity.organization,
        ).first()
        if not rule:
            raise QualityRuleError("质量规则不存在或不属于当前厂家。")
        if rule.status == QualityAlertRule.Status.RETIRED:
            raise QualityRuleError("已停用规则不能发布新版本。")
        name = str(payload.get("name") or rule.name).strip()
        if not name:
            raise QualityRuleError("请填写规则名称。")
        rule.name = name
    else:
        code = str(payload.get("code") or "").strip().upper()
        name = str(payload.get("name") or "").strip()
        if not code or not name:
            raise QualityRuleError("请填写规则编码和名称。")
        rule = QualityAlertRule.objects.create(
            factory_organization=identity.organization,
            code=code,
            name=name,
            created_by=user,
        )

    next_version = rule.current_version + 1
    version = QualityAlertRuleVersion(
        rule=rule,
        version=next_version,
        metric_type=metric_type,
        scope_snapshot=scope_snapshot,
        denominator_policy_snapshot=denominator,
        fault_policy_snapshot=fault_policy,
        threshold_snapshot=_thresholds(payload.get("thresholds"), metric_type),
        window_days=_positive_int(payload.get("window_days", 30), "统计窗口"),
        minimum_exposure=_positive_int(
            payload.get("minimum_exposure", 10), "最小暴露台次", allow_zero=True,
        ),
        minimum_fault_devices=_positive_int(
            payload.get("minimum_fault_devices", 2), "最小故障台次", allow_zero=True,
        ),
        cooldown_hours=_positive_int(
            payload.get("cooldown_hours", 24), "冷却时间", allow_zero=True,
        ),
        allow_automatic_temporary_hold=bool(payload.get("allow_automatic_temporary_hold", False)),
        automatic_hold_hours=_positive_int(
            payload.get("automatic_hold_hours", 0), "自动拦截时限", allow_zero=True,
        ),
        effective_from=effective_from,
        effective_to=effective_to,
        published_by=user,
    )
    version.full_clean()
    version.save()
    rule.current_version = next_version
    rule.status = QualityAlertRule.Status.ACTIVE
    rule.full_clean()
    rule.save(update_fields=("name", "current_version", "status", "updated_at"))
    return serialize_rule(rule, version)


def serialize_rule(rule, version=None):
    version = version or rule.versions.filter(version=rule.current_version).first()
    return {
        "id": rule.pk,
        "root_organization": {
            "id": rule.factory_organization_id,
            "name": rule.factory_organization.name,
        },
        "code": rule.code,
        "name": rule.name,
        "status": rule.status,
        "current_version": rule.current_version,
        "version": ({
            "id": version.pk,
            "version": version.version,
            "metric_type": version.metric_type,
            "scope": version.scope_snapshot,
            "denominator_policy": version.denominator_policy_snapshot,
            "fault_policy": version.fault_policy_snapshot,
            "thresholds": version.threshold_snapshot,
            "window_days": version.window_days,
            "minimum_exposure": version.minimum_exposure,
            "minimum_fault_devices": version.minimum_fault_devices,
            "effective_from": version.effective_from.isoformat(),
            "effective_to": version.effective_to.isoformat() if version.effective_to else "",
        } if version else None),
    }


def simulate_rule(*, user, data, at=None, web_authorized=False):
    """Calculate a draft without creating a rule version, alert or case."""
    identity = _factory_rule_manager(user, web_authorized=web_authorized)
    payload = data if isinstance(data, dict) else {}
    metric_type = str(payload.get("metric_type") or "").strip().upper()
    if metric_type not in SUPPORTED_METRICS:
        raise QualityRuleError("当前版本不支持该指标类型。")
    scope = _scope(payload.get("scope"))
    denominator = dict(payload.get("denominator_policy") or {})
    basis = str(denominator.get("basis") or "ACTIVATED_DEVICE").upper()
    if basis not in DENOMINATOR_BASES:
        raise QualityRuleError("分母口径仅支持已生成设备或已激活设备。")
    denominator["basis"] = basis
    denominator["cohort_window_only"] = bool(denominator.get("cohort_window_only", False))
    now = at or timezone.now()
    rule = QualityAlertRule(factory_organization=identity.organization, code="SIMULATED", name="模拟规则")
    version = QualityAlertRuleVersion(
        rule=rule,
        version=0,
        metric_type=metric_type,
        scope_snapshot=scope,
        denominator_policy_snapshot=denominator,
        fault_policy_snapshot=_fault_policy(payload.get("fault_policy"), scope),
        threshold_snapshot=_thresholds(payload.get("thresholds"), metric_type),
        window_days=_positive_int(payload.get("window_days", 30), "统计窗口"),
        minimum_exposure=_positive_int(payload.get("minimum_exposure", 10), "最小暴露台次", allow_zero=True),
        minimum_fault_devices=_positive_int(payload.get("minimum_fault_devices", 2), "最小故障台次", allow_zero=True),
        cooldown_hours=0,
        allow_automatic_temporary_hold=False,
        automatic_hold_hours=0,
        effective_from=now,
        published_by=user,
    )
    from .metric_engine import calculate_target_metric, _target_rows
    results = []
    for target in _target_rows(version):
        row = calculate_target_metric(version, target, at=now)
        results.append({
            "target": target["snapshot"], "severity": row["severity"],
            "exposure_device_count": row["exposure_device_count"],
            "fault_device_count": row["fault_device_count"],
            "metric_value": str(row["metric_value"]), "evidence": row["evidence"],
        })
    return {"scope": scope, "metric_type": metric_type, "results": results}


def list_rules(*, user):
    identity = get_quality_trace_identity(user)
    if identity.organization.organization_type.code != "OWNER":
        return []
    return [
        serialize_rule(rule)
        for rule in QualityAlertRule.objects.filter(
            factory_organization=identity.organization,
        ).select_related("factory_organization").prefetch_related("versions")
    ]
