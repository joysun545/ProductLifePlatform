import uuid
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from lifecycle.models import Device
from quality_trace.models import (
    BatchQualityAffectedDevice,
    BatchQualityCase,
    QualityAlert,
    QualityAssessment,
    QualityControlAction,
    QualityMetricSnapshot,
    QualityTraceEvent,
)
from supply_chain.models import SupplierMaterialBatch

from .access_service import QualityTraceAccessError, get_quality_trace_identity


class QualityCaseError(Exception):
    pass


ASSESSOR_ROLES = {"factory_admin", "factory_chief_engineer"}
CASE_MANAGER_ROLES = {"factory_admin", "factory_chief_engineer"}
CONTROL_MANAGER_ROLES = {"factory_admin", "factory_chief_engineer"}
MAX_TEMPORARY_HOLD_HOURS = 168


def _identity(user, allowed_roles):
    try:
        identity = get_quality_trace_identity(user)
    except QualityTraceAccessError as exc:
        raise QualityCaseError(str(exc)) from exc
    if identity.organization.organization_type.code != "OWNER":
        raise QualityCaseError("只有厂家可以执行质量研判与管控。")
    if identity.role.code not in allowed_roles:
        raise QualityCaseError("当前岗位无权执行该质量操作。")
    return identity


def _reason(value):
    result = str(value or "").strip()
    if not result:
        raise QualityCaseError("请填写研判或操作理由。")
    return result


def _alert_for_update(identity, alert_id):
    alert = (
        QualityAlert.objects.select_for_update()
        .select_related("rule_version__rule")
        .filter(pk=alert_id, factory_organization=identity.organization)
        .first()
    )
    if not alert:
        raise QualityCaseError("质量预警不存在或不属于当前厂家。")
    return alert


def _event(*, event_type, actor, identity, alert=None, quality_case=None, payload=None):
    return QualityTraceEvent.objects.create(
        alert=alert,
        quality_case=quality_case,
        event_type=event_type,
        actor=actor,
        actor_role_snapshot=identity.role.code if identity else "",
        payload=payload or {},
    )


@transaction.atomic
def assess_alert(*, user, alert_id, conclusion, reason, evidence=None):
    identity = _identity(user, ASSESSOR_ROLES)
    alert = _alert_for_update(identity, alert_id)
    if alert.status in (QualityAlert.Status.DISMISSED, QualityAlert.Status.CLOSED):
        raise QualityCaseError("该预警已经结束，不能继续追加研判。")
    conclusion = str(conclusion or "").strip().upper()
    valid = {value for value, _ in QualityAssessment.Conclusion.choices}
    if conclusion not in valid:
        raise QualityCaseError("研判结论无效。")
    if (
        conclusion == QualityAssessment.Conclusion.FALSE_ALARM
        and alert.quality_cases.exists()
    ):
        raise QualityCaseError("该预警已经立案，不能再按误报排除。")
    assessment = QualityAssessment.objects.create(
        alert=alert,
        conclusion=conclusion,
        reason=_reason(reason),
        evidence_snapshot=dict(evidence) if isinstance(evidence, dict) else {},
        assessed_by=user,
        assessed_role_snapshot=identity.role.code,
    )
    if alert.status == QualityAlert.Status.ESCALATED:
        next_status = QualityAlert.Status.ESCALATED
    elif conclusion == QualityAssessment.Conclusion.FALSE_ALARM:
        next_status = QualityAlert.Status.DISMISSED
    else:
        next_status = QualityAlert.Status.ASSESSING
    if alert.status != next_status:
        alert.status = next_status
        alert.save(update_fields=("status", "updated_at"))
    _event(
        event_type="ALERT_ASSESSED",
        actor=user,
        identity=identity,
        alert=alert,
        payload={
            "assessment_id": assessment.pk,
            "conclusion": conclusion,
            "reason": assessment.reason,
            "evidence": assessment.evidence_snapshot,
        },
    )
    return assessment


def _latest_metric_snapshot(alert):
    return (
        QualityMetricSnapshot.objects.filter(
            rule_version=alert.rule_version,
            target_type=alert.target_type,
            target_id=alert.target_id,
            window_ended_at__lte=alert.last_triggered_at,
        )
        .order_by("-window_ended_at", "-id")
        .first()
    )


def _snapshot_device_rows(alert, metric_snapshot):
    if metric_snapshot and metric_snapshot.device_evidence.exists():
        return [
            {
                "device_id": row.device_id,
                "is_fault": row.is_fault,
                "fault_occurrence_count": row.fault_occurrence_count,
                "metric_device_evidence": row.evidence_snapshot,
            }
            for row in metric_snapshot.device_evidence.order_by("device_id")
        ]
    evidence = (metric_snapshot.evidence_snapshot if metric_snapshot else None) or alert.metric_snapshot or {}
    if evidence.get("evidence_truncated"):
        raise QualityCaseError("历史指标证据已截断，不能据此立案；请先重新计算规则。")
    exposure_ids = {int(value) for value in evidence.get("exposure_device_ids", [])}
    fault_ids = {int(value) for value in evidence.get("fault_device_ids", [])}
    counts = evidence.get("fault_occurrence_counts") or {}
    return [
        {
            "device_id": device_id,
            "is_fault": device_id in fault_ids,
            "fault_occurrence_count": int(counts.get(str(device_id), 0)),
            "metric_device_evidence": {},
        }
        for device_id in sorted(exposure_ids)
    ]


def _location_snapshot(device):
    activation = getattr(device, "activation", None)
    if not activation:
        return {"activation_status": "NOT_ACTIVATED"}
    return {
        "activation_status": activation.status,
        "activation_time": activation.activation_time.isoformat(),
        "organization_id": activation.organization_id,
        "organization_name": activation.organization.name,
        "vehicle_no": activation.vehicle_no or "",
    }


@transaction.atomic
def open_case_from_alert(*, user, alert_id, reason):
    identity = _identity(user, CASE_MANAGER_ROLES)
    alert = _alert_for_update(identity, alert_id)
    if alert.status in (QualityAlert.Status.DISMISSED, QualityAlert.Status.CLOSED):
        raise QualityCaseError("已排除或已关闭的预警不能立案。")
    if not alert.assessments.exists():
        raise QualityCaseError("请先完成人工研判，再决定是否立案。")
    if BatchQualityCase.objects.filter(source_alert=alert).exists():
        raise QualityCaseError("该预警已经建立质量案件。")
    metric_snapshot = _latest_metric_snapshot(alert)
    device_rows = _snapshot_device_rows(alert, metric_snapshot)
    if not device_rows:
        raise QualityCaseError("预警没有可冻结的暴露设备证据。")
    supplier_batch = None
    if alert.target_type == "SUPPLIER_BATCH" and alert.target_id:
        supplier_batch = SupplierMaterialBatch.objects.filter(pk=alert.target_id).first()
    quality_case = BatchQualityCase.objects.create(
        case_no=f"QT-{timezone.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6].upper()}",
        factory_organization=identity.organization,
        source_alert=alert,
        supplier_material_batch=supplier_batch,
        scope_type=alert.target_type,
        scope_key=alert.target_key,
        scope_snapshot={
            "target": alert.target_snapshot,
            "rule_id": alert.rule_version.rule_id,
            "rule_version_id": alert.rule_version_id,
            "metric_snapshot_id": metric_snapshot.pk if metric_snapshot else None,
            "window_started_at": alert.window_started_at.isoformat(),
            "window_ended_at": alert.window_ended_at.isoformat(),
            "exposure_device_count": len(device_rows),
            "fault_device_count": sum(1 for row in device_rows if row["is_fault"]),
        },
        reason=_reason(reason),
        opened_by=user,
    )
    devices = {
        row.pk: row
        for row in Device.objects.filter(
            pk__in=[item["device_id"] for item in device_rows],
            batch__manufacturer=identity.organization,
        ).select_related(
            "batch", "product_model", "activation__organization",
        )
    }
    if len(devices) != len(device_rows):
        raise QualityCaseError("部分指标设备不属于当前厂家，已停止立案。")
    BatchQualityAffectedDevice.objects.bulk_create([
        BatchQualityAffectedDevice(
            quality_case=quality_case,
            device=devices[row["device_id"]],
            sn_snapshot=devices[row["device_id"]].sn,
            production_batch_snapshot=devices[row["device_id"]].batch.batch_no,
            product_model_snapshot={
                "id": devices[row["device_id"]].product_model_id,
                "code": devices[row["device_id"]].product_model.code,
                "name": devices[row["device_id"]].product_model.name,
            },
            location_snapshot=_location_snapshot(devices[row["device_id"]]),
            trace_evidence_snapshot={
                "metric_snapshot_id": metric_snapshot.pk if metric_snapshot else None,
                "is_fault": row["is_fault"],
                "fault_occurrence_count": row["fault_occurrence_count"],
                "metric_device_evidence": row["metric_device_evidence"],
            },
        )
        for row in device_rows
    ])
    alert.status = QualityAlert.Status.ESCALATED
    alert.save(update_fields=("status", "updated_at"))
    _event(
        event_type="QUALITY_CASE_OPENED",
        actor=user,
        identity=identity,
        alert=alert,
        quality_case=quality_case,
        payload={
            "case_no": quality_case.case_no,
            "affected_device_count": len(device_rows),
            "fault_device_count": quality_case.scope_snapshot["fault_device_count"],
            "metric_snapshot_id": metric_snapshot.pk if metric_snapshot else None,
            "reason": quality_case.reason,
        },
    )
    return quality_case


@transaction.atomic
def place_temporary_hold(*, user, case_id, reason, duration_hours):
    identity = _identity(user, CONTROL_MANAGER_ROLES)
    quality_case = (
        BatchQualityCase.objects.select_for_update()
        .filter(pk=case_id, factory_organization=identity.organization)
        .first()
    )
    if not quality_case:
        raise QualityCaseError("质量案件不存在或不属于当前厂家。")
    if quality_case.status == BatchQualityCase.Status.CLOSED:
        raise QualityCaseError("已结案质量案件不能执行临时拦截。")
    if quality_case.control_actions.filter(
        action_type=QualityControlAction.ActionType.TEMPORARY_HOLD,
        status=QualityControlAction.Status.ACTIVE,
    ).exists():
        raise QualityCaseError("该案件已经存在生效中的临时拦截。")
    if quality_case.control_actions.filter(
        action_type__in=(
            QualityControlAction.ActionType.FORMAL_HOLD,
            QualityControlAction.ActionType.RECALL,
        ),
        status=QualityControlAction.Status.ACTIVE,
    ).exists():
        raise QualityCaseError("正式拦截或召回已经生效，不能降级追加临时拦截。")
    try:
        duration_hours = int(duration_hours)
    except (TypeError, ValueError) as exc:
        raise QualityCaseError("临时拦截时长必须为整数小时。") from exc
    if duration_hours < 1 or duration_hours > MAX_TEMPORARY_HOLD_HOURS:
        raise QualityCaseError("临时拦截时长必须为1至168小时。")
    now = timezone.now()
    device_ids = list(
        quality_case.affected_devices.order_by("device_id").values_list("device_id", flat=True)
    )
    action = QualityControlAction(
        quality_case=quality_case,
        action_type=QualityControlAction.ActionType.TEMPORARY_HOLD,
        operation_scope={
            "scope_type": quality_case.scope_type,
            "scope_key": quality_case.scope_key,
            "affected_device_source": "CASE_FROZEN_SET",
            "quality_case_id": quality_case.pk,
            "affected_device_count": len(device_ids),
        },
        status=QualityControlAction.Status.ACTIVE,
        reason=_reason(reason),
        effective_at=now,
        expires_at=now + timedelta(hours=duration_hours),
        acted_by=user,
    )
    action.full_clean()
    action.save()
    quality_case.status = BatchQualityCase.Status.HELD
    quality_case.save(update_fields=("status", "updated_at"))
    _event(
        event_type="TEMPORARY_HOLD_PLACED",
        actor=user,
        identity=identity,
        quality_case=quality_case,
        alert=quality_case.source_alert,
        payload={
            "control_action_id": action.pk,
            "duration_hours": duration_hours,
            "expires_at": action.expires_at.isoformat(),
            "affected_device_count": len(device_ids),
            "reason": action.reason,
        },
    )
    return action


def _synchronize_case_control_status(quality_case):
    """Keep the case projection in sync without weakening another active control."""
    active_types = set(quality_case.control_actions.filter(
        status=QualityControlAction.Status.ACTIVE,
    ).values_list("action_type", flat=True))
    if QualityControlAction.ActionType.RECALL in active_types:
        next_status = BatchQualityCase.Status.RECALLING
    elif active_types.intersection({
        QualityControlAction.ActionType.TEMPORARY_HOLD,
        QualityControlAction.ActionType.FORMAL_HOLD,
    }):
        next_status = BatchQualityCase.Status.HELD
    elif quality_case.status not in {
        BatchQualityCase.Status.REMEDIATING,
        BatchQualityCase.Status.PENDING_RELEASE,
        BatchQualityCase.Status.CLOSED,
    }:
        next_status = BatchQualityCase.Status.OPEN
    else:
        next_status = quality_case.status
    if quality_case.status != next_status:
        quality_case.status = next_status
        quality_case.save(update_fields=("status", "updated_at"))


@transaction.atomic
def revoke_temporary_hold(*, user, action_id, reason):
    identity = _identity(user, CONTROL_MANAGER_ROLES)
    action = (
        QualityControlAction.objects.select_for_update()
        .select_related("quality_case__source_alert")
        .filter(
            pk=action_id,
            quality_case__factory_organization=identity.organization,
            action_type=QualityControlAction.ActionType.TEMPORARY_HOLD,
        )
        .first()
    )
    if not action:
        raise QualityCaseError("临时拦截记录不存在或不属于当前厂家。")
    if action.status != QualityControlAction.Status.ACTIVE:
        raise QualityCaseError("该临时拦截已经结束。")
    if action.quality_case.control_actions.filter(
        action_type=QualityControlAction.ActionType.RECALL,
        status=QualityControlAction.Status.ACTIVE,
    ).exists():
        raise QualityCaseError("召回进行中，禁止解除临时拦截。")
    action.status = QualityControlAction.Status.REVOKED
    action.ended_at = timezone.now()
    action.ended_by = user
    action.end_reason = _reason(reason)
    action.save(update_fields=("status", "ended_at", "ended_by", "end_reason"))
    _synchronize_case_control_status(action.quality_case)
    _event(
        event_type="TEMPORARY_HOLD_REVOKED",
        actor=user,
        identity=identity,
        quality_case=action.quality_case,
        alert=action.quality_case.source_alert,
        payload={"control_action_id": action.pk, "reason": action.end_reason},
    )
    return action


def _case_for_control(identity, case_id):
    quality_case = (
        BatchQualityCase.objects.select_for_update()
        .select_related("source_alert")
        .filter(pk=case_id, factory_organization=identity.organization)
        .first()
    )
    if not quality_case:
        raise QualityCaseError("质量案件不存在或不属于当前厂家。")
    if quality_case.status == BatchQualityCase.Status.CLOSED:
        raise QualityCaseError("已结案质量案件不能执行管控操作。")
    return quality_case


def _active_blocking_exists(quality_case):
    return quality_case.control_actions.filter(
        action_type__in=(
            QualityControlAction.ActionType.TEMPORARY_HOLD,
            QualityControlAction.ActionType.FORMAL_HOLD,
            QualityControlAction.ActionType.RECALL,
        ),
        status=QualityControlAction.Status.ACTIVE,
    ).exists()


def _frozen_scope(quality_case):
    return {
        "scope_type": quality_case.scope_type,
        "scope_key": quality_case.scope_key,
        "affected_device_source": "CASE_FROZEN_SET",
        "quality_case_id": quality_case.pk,
        "affected_device_count": quality_case.affected_devices.count(),
    }


@transaction.atomic
def place_formal_hold(*, user, case_id, reason):
    identity = _identity(user, CONTROL_MANAGER_ROLES)
    quality_case = _case_for_control(identity, case_id)
    if quality_case.control_actions.filter(
        action_type=QualityControlAction.ActionType.FORMAL_HOLD,
        status=QualityControlAction.Status.ACTIVE,
    ).exists():
        raise QualityCaseError("该案件已经存在生效中的正式拦截。")
    now = timezone.now()
    temporary_actions = list(quality_case.control_actions.select_for_update().filter(
        action_type=QualityControlAction.ActionType.TEMPORARY_HOLD,
        status=QualityControlAction.Status.ACTIVE,
    ))
    action = QualityControlAction.objects.create(
        quality_case=quality_case,
        action_type=QualityControlAction.ActionType.FORMAL_HOLD,
        operation_scope=_frozen_scope(quality_case),
        status=QualityControlAction.Status.ACTIVE,
        reason=_reason(reason),
        effective_at=now,
        acted_by=user,
    )
    for temporary in temporary_actions:
        temporary.status = QualityControlAction.Status.COMPLETED
        temporary.ended_at = now
        temporary.ended_by = user
        temporary.end_reason = f"升级为正式拦截（正式拦截动作 {action.pk}）"
        temporary.save(update_fields=("status", "ended_at", "ended_by", "end_reason"))
        _event(
            event_type="TEMPORARY_HOLD_UPGRADED",
            actor=user, identity=identity, quality_case=quality_case,
            alert=quality_case.source_alert,
            payload={"temporary_action_id": temporary.pk, "formal_action_id": action.pk},
        )
    _synchronize_case_control_status(quality_case)
    _event(
        event_type="FORMAL_HOLD_PLACED", actor=user, identity=identity,
        quality_case=quality_case, alert=quality_case.source_alert,
        payload={"control_action_id": action.pk, "reason": action.reason,
                 "affected_device_count": action.operation_scope["affected_device_count"]},
    )
    return action


@transaction.atomic
def release_formal_hold(*, user, action_id, reason):
    identity = _identity(user, CONTROL_MANAGER_ROLES)
    action = (
        QualityControlAction.objects.select_for_update()
        .select_related("quality_case__source_alert")
        .filter(
            pk=action_id,
            quality_case__factory_organization=identity.organization,
            action_type=QualityControlAction.ActionType.FORMAL_HOLD,
        ).first()
    )
    if not action or action.status != QualityControlAction.Status.ACTIVE:
        raise QualityCaseError("正式拦截不存在或已经结束。")
    if action.quality_case.control_actions.filter(
        action_type=QualityControlAction.ActionType.RECALL,
        status=QualityControlAction.Status.ACTIVE,
    ).exists():
        raise QualityCaseError("召回进行中，禁止解除正式拦截。")
    now = timezone.now()
    action.status = QualityControlAction.Status.REVOKED
    action.ended_at = now
    action.ended_by = user
    action.end_reason = _reason(reason)
    action.save(update_fields=("status", "ended_at", "ended_by", "end_reason"))
    release = QualityControlAction.objects.create(
        quality_case=action.quality_case,
        action_type=QualityControlAction.ActionType.RELEASE,
        operation_scope=action.operation_scope,
        status=QualityControlAction.Status.COMPLETED,
        reason=action.end_reason,
        effective_at=now,
        acted_by=user,
        ended_at=now,
        ended_by=user,
        end_reason=action.end_reason,
    )
    _synchronize_case_control_status(action.quality_case)
    _event(
        event_type="FORMAL_HOLD_RELEASED", actor=user, identity=identity,
        quality_case=action.quality_case, alert=action.quality_case.source_alert,
        payload={"control_action_id": action.pk, "release_action_id": release.pk,
                 "reason": action.end_reason},
    )
    return action


@transaction.atomic
def start_recall(*, user, case_id, reason):
    identity = _identity(user, CONTROL_MANAGER_ROLES)
    quality_case = _case_for_control(identity, case_id)
    if quality_case.control_actions.filter(
        action_type=QualityControlAction.ActionType.RECALL,
        status=QualityControlAction.Status.ACTIVE,
    ).exists():
        raise QualityCaseError("该案件已经存在执行中的召回行动。")
    action = QualityControlAction.objects.create(
        quality_case=quality_case,
        action_type=QualityControlAction.ActionType.RECALL,
        operation_scope=_frozen_scope(quality_case),
        status=QualityControlAction.Status.ACTIVE,
        reason=_reason(reason),
        effective_at=timezone.now(),
        acted_by=user,
    )
    _synchronize_case_control_status(quality_case)
    _event(
        event_type="RECALL_STARTED", actor=user, identity=identity,
        quality_case=quality_case, alert=quality_case.source_alert,
        payload={"control_action_id": action.pk, "reason": action.reason,
                 "affected_device_count": action.operation_scope["affected_device_count"]},
    )
    return action


@transaction.atomic
def complete_recall(*, user, action_id, reason):
    identity = _identity(user, CONTROL_MANAGER_ROLES)
    action = (
        QualityControlAction.objects.select_for_update()
        .select_related("quality_case__source_alert")
        .filter(
            pk=action_id,
            quality_case__factory_organization=identity.organization,
            action_type=QualityControlAction.ActionType.RECALL,
        ).first()
    )
    if not action or action.status != QualityControlAction.Status.ACTIVE:
        raise QualityCaseError("召回行动不存在或已经结束。")
    action.status = QualityControlAction.Status.COMPLETED
    action.ended_at = timezone.now()
    action.ended_by = user
    action.end_reason = _reason(reason)
    action.save(update_fields=("status", "ended_at", "ended_by", "end_reason"))
    completed_controls = []
    for control in action.quality_case.control_actions.select_for_update().filter(
        action_type__in=(
            QualityControlAction.ActionType.TEMPORARY_HOLD,
            QualityControlAction.ActionType.FORMAL_HOLD,
        ),
        status=QualityControlAction.Status.ACTIVE,
    ):
        control.status = QualityControlAction.Status.COMPLETED
        control.ended_at = action.ended_at
        control.ended_by = user
        control.end_reason = "召回批次完成，关联拦截同步结束"
        control.save(update_fields=("status", "ended_at", "ended_by", "end_reason"))
        completed_controls.append(control.pk)
    action.quality_case.status = BatchQualityCase.Status.REMEDIATING
    action.quality_case.save(update_fields=("status", "updated_at"))
    _event(
        event_type="RECALL_COMPLETED", actor=user, identity=identity,
        quality_case=action.quality_case, alert=action.quality_case.source_alert,
        payload={"control_action_id": action.pk,
                 "completed_control_action_ids": completed_controls,
                 "reason": action.end_reason},
    )
    return action


@transaction.atomic
def expire_temporary_holds(*, at=None):
    at = at or timezone.now()
    action_ids = list(
        QualityControlAction.objects.filter(
            action_type=QualityControlAction.ActionType.TEMPORARY_HOLD,
            status=QualityControlAction.Status.ACTIVE,
            expires_at__lte=at,
        ).values_list("pk", flat=True)
    )
    expired = 0
    for action_id in action_ids:
        action = (
            QualityControlAction.objects.select_for_update()
            .select_related("quality_case__source_alert")
            .get(pk=action_id)
        )
        if action.status != QualityControlAction.Status.ACTIVE or action.expires_at > at:
            continue
        action.status = QualityControlAction.Status.EXPIRED
        action.ended_at = at
        action.end_reason = "临时拦截到期自动解除"
        action.save(update_fields=("status", "ended_at", "end_reason"))
        _synchronize_case_control_status(action.quality_case)
        QualityTraceEvent.objects.create(
            quality_case=action.quality_case,
            alert=action.quality_case.source_alert,
            event_type="TEMPORARY_HOLD_EXPIRED",
            payload={"control_action_id": action.pk, "expired_at": at.isoformat()},
        )
        expired += 1
    return expired


def serialize_assessment(row):
    return {
        "id": row.pk,
        "conclusion": row.conclusion,
        "reason": row.reason,
        "evidence": row.evidence_snapshot,
        "assessed_by": row.assessed_by_id,
        "assessed_role": row.assessed_role_snapshot,
        "assessed_at": row.assessed_at.isoformat(),
    }


def serialize_control_action(row):
    return {
        "id": row.pk,
        "action_type": row.action_type,
        "status": row.status,
        "operation_scope": row.operation_scope,
        "reason": row.reason,
        "effective_at": row.effective_at.isoformat(),
        "expires_at": row.expires_at.isoformat() if row.expires_at else "",
        "acted_by": row.acted_by_id,
        "ended_at": row.ended_at.isoformat() if row.ended_at else "",
        "ended_by": row.ended_by_id,
        "end_reason": row.end_reason,
    }


def serialize_case(row, *, include_devices=False):
    data = {
        "id": row.pk,
        "case_no": row.case_no,
        "source_alert_id": row.source_alert_id,
        "scope_type": row.scope_type,
        "scope_key": row.scope_key,
        "scope": row.scope_snapshot,
        "reason": row.reason,
        "status": row.status,
        "opened_by": row.opened_by_id,
        "opened_at": row.opened_at.isoformat(),
        "affected_device_count": row.affected_devices.count(),
        "control_actions": [
            serialize_control_action(item)
            for item in row.control_actions.select_related("acted_by", "ended_by").all()
        ],
    }
    if include_devices:
        data["affected_devices"] = [
            {
                "device_id": item.device_id,
                "sn": item.sn_snapshot,
                "production_batch": item.production_batch_snapshot,
                "product_model": item.product_model_snapshot,
                "location": item.location_snapshot,
                "trace_evidence": item.trace_evidence_snapshot,
                "included_at": item.included_at.isoformat(),
            }
            for item in row.affected_devices.order_by("device_id")
        ]
    return data
