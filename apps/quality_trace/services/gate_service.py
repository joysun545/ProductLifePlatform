from functools import wraps

from django.db import transaction
from django.db.models import Case, IntegerField, Q, Value, When
from django.utils import timezone

from account.services.auth_service import get_active_identity
from lifecycle.models import Device
from quality_trace.models import (
    BatchQualityCase,
    QualityControlAction,
    QualityGateDecision,
)

from .case_service import expire_temporary_holds


class QualityGateError(Exception):
    pass


class QualityOperationBlocked(QualityGateError):
    def __init__(self, decision):
        self.decision = decision
        super().__init__(decision["message"])


BLOCKING_ACTION_TYPES = {
    QualityControlAction.ActionType.TEMPORARY_HOLD,
    QualityControlAction.ActionType.FORMAL_HOLD,
    QualityControlAction.ActionType.RECALL,
}

REMEDIATION_BLOCKING_STATUSES = {
    BatchQualityCase.Status.REMEDIATING,
    BatchQualityCase.Status.PENDING_RELEASE,
}


def _operation(value):
    result = str(value or "").strip().upper()
    valid = {item for item, _ in QualityGateDecision.Operation.choices}
    if result not in valid:
        raise QualityGateError("质量闸门操作类型无效。")
    return result


def _actor_snapshot(user):
    if not user:
        return None, ""
    identity = get_active_identity(user)
    return user, identity.role.code if identity else ""


def _active_action(device, at):
    return (
        QualityControlAction.objects.filter(
            quality_case__affected_devices__device=device,
            status=QualityControlAction.Status.ACTIVE,
            action_type__in=BLOCKING_ACTION_TYPES,
            effective_at__lte=at,
        )
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=at))
        .select_related("quality_case")
        .annotate(
            gate_priority=Case(
                When(action_type=QualityControlAction.ActionType.RECALL, then=Value(30)),
                When(action_type=QualityControlAction.ActionType.FORMAL_HOLD, then=Value(20)),
                When(action_type=QualityControlAction.ActionType.TEMPORARY_HOLD, then=Value(10)),
                default=Value(0),
                output_field=IntegerField(),
            )
        )
        .order_by("-gate_priority", "-effective_at", "-id")
        .first()
    )


def _remediation_case(device):
    """Return the newest case whose post-recall isolation is still in force.

    Recall completion ends the recall/hold actions themselves, but the frozen
    device set must remain isolated while engineering remediation and release
    approval are pending.  This is a case-stage gate, not another control
    action, so it deliberately does not create or reactivate an action row.
    """
    return (
        BatchQualityCase.objects.filter(
            affected_devices__device=device,
            status__in=REMEDIATION_BLOCKING_STATUSES,
        )
        .order_by("-updated_at", "-id")
        .first()
    )


def serialize_gate_decision(row):
    return {
        "id": row.pk,
        "factory_organization_id": row.factory_organization_id,
        "device_id": row.device_id,
        "operation": row.operation,
        "allowed": row.allowed,
        "decision_code": row.decision_code,
        "reason": row.reason,
        "quality_case_id": row.quality_case_id,
        "control_action_id": row.control_action_id,
        "checked_by": row.checked_by_id,
        "checked_role": row.checked_role_snapshot,
        "context": row.context_snapshot,
        "checked_at": row.checked_at.isoformat(),
    }


def check_device_operation(
    *, device_id, operation, user=None, context=None, record_allowed=False, at=None,
):
    operation = _operation(operation)
    at = at or timezone.now()
    expire_temporary_holds(at=at)
    device = (
        Device.objects.select_related("batch__manufacturer", "product_model")
        .filter(pk=device_id)
        .first()
    )
    if not device:
        raise QualityGateError("设备不存在。")
    action = _active_action(device, at)
    remediation_case = None if action else _remediation_case(device)
    blocking_case = action.quality_case if action else remediation_case
    actor, role = _actor_snapshot(user)
    context_snapshot = dict(context) if isinstance(context, dict) else {}
    context_snapshot.update({
        "device_sn": device.sn,
        "product_model_id": device.product_model_id,
        "product_model_name": device.product_model.name,
        "production_batch_id": device.batch_id,
        "production_batch_no": device.batch.batch_no,
        "checked_at": at.isoformat(),
    })
    if blocking_case:
        if action:
            message = (
                f"设备{device.sn}受质量管控限制，当前不能执行"
                f"{dict(QualityGateDecision.Operation.choices)[operation]}。"
            )
            gate_basis = "ACTIVE_CONTROL_ACTION"
        else:
            message = (
                f"设备{device.sn}处于召回完成后的质量整改隔离阶段，当前不能执行"
                f"{dict(QualityGateDecision.Operation.choices)[operation]}。"
            )
            gate_basis = "REMEDIATION_ISOLATION"
        context_snapshot["quality_gate_basis"] = gate_basis
        context_snapshot["quality_case_status"] = blocking_case.status
        row = QualityGateDecision.objects.create(
            factory_organization=device.batch.manufacturer,
            device=device,
            operation=operation,
            allowed=False,
            decision_code="QUALITY_CONTROL_BLOCKED",
            reason=message,
            quality_case=blocking_case,
            control_action=action,
            checked_by=actor,
            checked_role_snapshot=role,
            context_snapshot=context_snapshot,
        )
        return {
            "allowed": False,
            "code": row.decision_code,
            "message": message,
            "decision_id": row.pk,
            "device_id": device.pk,
            "device_sn": device.sn,
            "operation": operation,
            "quality_case_id": blocking_case.pk,
            "case_no": blocking_case.case_no,
            "control_action_id": action.pk if action else None,
            "control_action_type": (
                action.action_type if action else "REMEDIATION_ISOLATION"
            ),
            "expires_at": (
                action.expires_at.isoformat() if action and action.expires_at else ""
            ),
            "audit": serialize_gate_decision(row),
        }
    row = None
    if record_allowed:
        row = QualityGateDecision.objects.create(
            factory_organization=device.batch.manufacturer,
            device=device,
            operation=operation,
            allowed=True,
            decision_code="ALLOWED",
            checked_by=actor,
            checked_role_snapshot=role,
            context_snapshot=context_snapshot,
        )
    return {
        "allowed": True,
        "code": "ALLOWED",
        "message": "",
        "decision_id": row.pk if row else None,
        "device_id": device.pk,
        "device_sn": device.sn,
        "operation": operation,
        "quality_case_id": None,
        "case_no": "",
        "control_action_id": None,
        "control_action_type": "",
        "expires_at": "",
        "audit": serialize_gate_decision(row) if row else None,
    }


def enforce_device_operation(**kwargs):
    decision = check_device_operation(**kwargs)
    if not decision["allowed"]:
        raise QualityOperationBlocked(decision)
    return decision


def persist_blocked_decision_after_rollback(decision):
    audit = decision.get("audit") or {}
    original_id = audit.get("id")
    if original_id and QualityGateDecision.objects.filter(pk=original_id).exists():
        return QualityGateDecision.objects.get(pk=original_id)
    return QualityGateDecision.objects.create(
        factory_organization_id=audit["factory_organization_id"],
        device_id=audit["device_id"],
        operation=audit["operation"],
        allowed=False,
        decision_code=audit["decision_code"],
        reason=audit.get("reason") or "",
        quality_case_id=audit.get("quality_case_id"),
        control_action_id=audit.get("control_action_id"),
        checked_by_id=audit.get("checked_by"),
        checked_role_snapshot=audit.get("checked_role") or "",
        context_snapshot=audit.get("context") or {},
    )


def quality_gate_atomic(error_class):
    """Rollback business writes, then preserve the rejected gate audit."""
    def decorator(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            try:
                with transaction.atomic():
                    return func(*args, **kwargs)
            except QualityOperationBlocked as exc:
                row = persist_blocked_decision_after_rollback(exc.decision)
                message = (
                    f"{exc}（质量案件：{exc.decision['case_no']}；"
                    f"门禁审计：{row.pk}）"
                )
                raise error_class(message) from exc
        return wrapped
    return decorator
