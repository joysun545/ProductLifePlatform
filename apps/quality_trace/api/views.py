import json

from django.db.models import Q
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from account.services.auth_service import ApiAuthenticationError, authenticate_request
from lifecycle.models import Device
from quality_trace.models import (
    BatchQualityCase,
    QualityAlert,
    QualityAlertRule,
    QualityControlAction,
    QualityRecallCampaign,
    QualityMetricSnapshot,
    QualityGateDecision,
)
from quality_trace.services import (
    QualityImpactTraceError,
    QualityTraceAccessError,
    get_quality_trace_identity,
    trace_supplier_batch_impact,
    QualityRuleError,
    list_rules,
    publish_rule_version,
    evaluate_active_rules,
    QualityCaseError,
    assess_alert,
    open_case_from_alert,
    place_temporary_hold,
    revoke_temporary_hold,
    place_formal_hold,
    release_formal_hold,
    complete_recall,
    serialize_assessment,
    serialize_case,
    QualityGateError,
    check_device_operation,
    serialize_gate_decision,
)
from quality_trace.services.rule_service import serialize_rule
from quality_trace.services.recall_service import (
    QualityRecallError,
    ensure_recall_campaign,
)


def _authenticated_user(request):
    try:
        return authenticate_request(request), None
    except ApiAuthenticationError as exc:
        return None, JsonResponse(
            {"code": 401, "msg": str(exc), "data": {}}, status=401,
        )


def _method_error():
    return JsonResponse({"code": 1, "msg": "仅支持GET", "data": {}}, status=200)


def _request_data(request):
    if "application/json" in (request.content_type or "").lower() and request.body:
        try:
            data = json.loads(request.body.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
    return request.POST.dict()


def overview(request):
    if request.method != "GET":
        return _method_error()
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    try:
        identity = get_quality_trace_identity(user)
    except QualityTraceAccessError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)

    organization = identity.organization
    if organization.organization_type.code == "OWNER":
        rule_count = QualityAlertRule.objects.filter(factory_organization=organization).count()
        alert_count = QualityAlert.objects.filter(
            factory_organization=organization,
            status__in=(QualityAlert.Status.OPEN, QualityAlert.Status.ASSESSING),
        ).count()
        case_count = BatchQualityCase.objects.filter(
            factory_organization=organization,
        ).exclude(status=BatchQualityCase.Status.CLOSED).count()
    else:
        rule_count = 0
        alert_count = 0
        case_count = BatchQualityCase.objects.filter(
            supplier_material_batch__supplier=organization,
        ).exclude(status=BatchQualityCase.Status.CLOSED).count()

    return JsonResponse({
        "code": 0,
        "msg": "",
        "data": {
            "organization_id": organization.pk,
            "organization_name": organization.name,
            "organization_type": organization.organization_type.code,
            "role": identity.role.code,
            "rule_count": rule_count,
            "open_alert_count": alert_count,
            "open_case_count": case_count,
            "stage": "R13.3_CENTRAL_OPERATION_GATE",
        },
    })


def supplier_batch_impact(request, batch_id):
    if request.method != "GET":
        return _method_error()
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    try:
        data = trace_supplier_batch_impact(user=user, batch_id=batch_id)
    except (QualityTraceAccessError, QualityImpactTraceError) as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    return JsonResponse({"code": 0, "msg": "", "data": data}, status=200)


@csrf_exempt
def rules(request):
    if request.method not in {"GET", "POST"}:
        return JsonResponse(
            {"code": 1, "msg": "仅支持GET或POST", "data": {}}, status=200,
        )
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    try:
        if request.method == "GET":
            data = {"items": list_rules(user=user)}
            message = ""
        else:
            data = publish_rule_version(user=user, data=_request_data(request))
            message = "质量预警规则版本已发布"
    except (QualityTraceAccessError, QualityRuleError) as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    return JsonResponse({"code": 0, "msg": message, "data": data}, status=200)


def rule_detail(request, rule_id):
    if request.method != "GET":
        return _method_error()
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    try:
        identity = get_quality_trace_identity(user)
    except QualityTraceAccessError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    if identity.organization.organization_type.code != "OWNER":
        return JsonResponse({"code": 1, "msg": "只有厂家可以查看质量规则。", "data": {}}, status=200)
    rule = QualityAlertRule.objects.filter(
        pk=rule_id, factory_organization=identity.organization,
    ).prefetch_related("versions").first()
    if not rule:
        return JsonResponse({"code": 1, "msg": "质量规则不存在或不属于当前厂家。", "data": {}}, status=200)
    data = serialize_rule(rule)
    data["versions"] = [
        serialize_rule(rule, version)["version"]
        for version in rule.versions.order_by("-version")
    ]
    data["can_manage"] = identity.role.code in {"factory_admin", "factory_chief_engineer"}
    return JsonResponse({"code": 0, "msg": "", "data": data}, status=200)


@csrf_exempt
def evaluate_rules(request):
    if request.method != "POST":
        return JsonResponse({"code": 1, "msg": "仅支持POST", "data": {}}, status=200)
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    try:
        identity = get_quality_trace_identity(user)
        if (
            identity.organization.organization_type.code != "OWNER"
            or identity.role.code not in {"factory_admin", "factory_chief_engineer"}
        ):
            raise QualityTraceAccessError("当前岗位无权执行质量规则重算。")
        payload = _request_data(request)
        rule_id = payload.get("rule_id")
        results = evaluate_active_rules(
            source=QualityMetricSnapshot.Source.MANUAL,
            factory_id=identity.organization_id,
            rule_id=int(rule_id) if rule_id else None,
        )
    except (QualityTraceAccessError, ValueError) as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    return JsonResponse({
        "code": 0,
        "msg": "质量指标重算完成",
        "data": {"rules": results},
    }, status=200)


def metric_snapshots(request):
    if request.method != "GET":
        return _method_error()
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    try:
        identity = get_quality_trace_identity(user)
    except QualityTraceAccessError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    if identity.organization.organization_type.code != "OWNER":
        return JsonResponse({"code": 0, "msg": "", "data": {"items": []}}, status=200)
    rows = QualityMetricSnapshot.objects.filter(
        factory_organization=identity.organization,
    ).select_related("rule_version__rule")[:100]
    items = [{
        "id": row.pk,
        "rule_id": row.rule_version.rule_id,
        "rule_name": row.rule_version.rule.name,
        "rule_version": row.rule_version.version,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "target": row.target_snapshot,
        "source": row.calculation_source,
        "exposure_device_count": row.exposure_device_count,
        "fault_device_count": row.fault_device_count,
        "metric_value": str(row.metric_value),
        "threshold_met": row.threshold_met,
        "severity": row.severity,
        "calculated_at": row.calculated_at.isoformat(),
    } for row in rows]
    return JsonResponse({"code": 0, "msg": "", "data": {"items": items}}, status=200)


def alerts(request):
    if request.method != "GET":
        return _method_error()
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    try:
        identity = get_quality_trace_identity(user)
    except QualityTraceAccessError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    if identity.organization.organization_type.code != "OWNER":
        return JsonResponse({"code": 0, "msg": "", "data": {"items": []}}, status=200)
    rows = QualityAlert.objects.filter(
        factory_organization=identity.organization,
    ).select_related("rule_version__rule")[:100]
    items = [{
        "id": row.pk,
        "rule_id": row.rule_version.rule_id,
        "rule_name": row.rule_version.rule.name,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "target": row.target_snapshot,
        "severity": row.severity,
        "status": row.status,
        "exposure_device_count": row.exposure_device_count,
        "fault_device_count": row.fault_device_count,
        "metric_value": str(row.metric_value),
        "trigger_count": row.trigger_count,
        "last_triggered_at": row.last_triggered_at.isoformat(),
    } for row in rows]
    return JsonResponse({"code": 0, "msg": "", "data": {"items": items}}, status=200)


def _factory_alert(user, alert_id):
    identity = get_quality_trace_identity(user)
    if identity.organization.organization_type.code != "OWNER":
        raise QualityTraceAccessError("只有厂家可以查看质量预警详情。")
    alert = (
        QualityAlert.objects
        .select_related("rule_version__rule")
        .filter(pk=alert_id, factory_organization=identity.organization)
        .first()
    )
    if not alert:
        raise QualityTraceAccessError("质量预警不存在或不属于当前厂家。")
    return identity, alert


def alert_detail(request, alert_id):
    if request.method != "GET":
        return _method_error()
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    try:
        _, alert = _factory_alert(user, alert_id)
    except QualityTraceAccessError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    metric = (
        QualityMetricSnapshot.objects.filter(
            rule_version=alert.rule_version,
            target_type=alert.target_type,
            target_id=alert.target_id,
            window_ended_at__lte=alert.last_triggered_at,
        ).order_by("-window_ended_at", "-id").first()
    )
    evidence_rows = metric.device_evidence.order_by("device_id") if metric else []
    data = {
        "id": alert.pk,
        "rule_id": alert.rule_version.rule_id,
        "rule_name": alert.rule_version.rule.name,
        "rule_version": alert.rule_version.version,
        "target_type": alert.target_type,
        "target_id": alert.target_id,
        "target": alert.target_snapshot,
        "severity": alert.severity,
        "status": alert.status,
        "exposure_device_count": alert.exposure_device_count,
        "fault_device_count": alert.fault_device_count,
        "metric_value": str(alert.metric_value),
        "metric_evidence": alert.metric_snapshot,
        "trigger_count": alert.trigger_count,
        "first_triggered_at": alert.first_triggered_at.isoformat(),
        "last_triggered_at": alert.last_triggered_at.isoformat(),
        "assessments": [
            serialize_assessment(row)
            for row in alert.assessments.select_related("assessed_by").all()
        ],
        "quality_case": (
            serialize_case(alert.quality_cases.first())
            if alert.quality_cases.exists() else None
        ),
        "device_evidence": [
            {
                "device_id": row.device_id,
                "is_fault": row.is_fault,
                "fault_occurrence_count": row.fault_occurrence_count,
                "evidence": row.evidence_snapshot,
            }
            for row in evidence_rows[:500]
        ],
        "device_evidence_complete_count": evidence_rows.count() if metric else 0,
        "available_actions": {
            "assess": alert.status not in {QualityAlert.Status.DISMISSED, QualityAlert.Status.CLOSED},
            "open_case": (
                alert.status not in {QualityAlert.Status.DISMISSED, QualityAlert.Status.CLOSED}
                and alert.assessments.exists()
                and not alert.quality_cases.exists()
            ),
        },
    }
    return JsonResponse({"code": 0, "msg": "", "data": data}, status=200)


@csrf_exempt
def alert_assess(request, alert_id):
    if request.method != "POST":
        return JsonResponse({"code": 1, "msg": "仅支持POST", "data": {}}, status=200)
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    payload = _request_data(request)
    try:
        row = assess_alert(
            user=user,
            alert_id=alert_id,
            conclusion=payload.get("conclusion"),
            reason=payload.get("reason"),
            evidence=payload.get("evidence"),
        )
    except QualityCaseError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    return JsonResponse({
        "code": 0,
        "msg": "人工研判已记录",
        "data": serialize_assessment(row),
    }, status=200)


@csrf_exempt
def alert_open_case(request, alert_id):
    if request.method != "POST":
        return JsonResponse({"code": 1, "msg": "仅支持POST", "data": {}}, status=200)
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    try:
        row = open_case_from_alert(
            user=user,
            alert_id=alert_id,
            reason=_request_data(request).get("reason"),
        )
    except QualityCaseError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    return JsonResponse({
        "code": 0,
        "msg": "质量案件已建立，受影响设备清单已冻结",
        "data": serialize_case(row, include_devices=True),
    }, status=200)


def quality_cases(request):
    if request.method != "GET":
        return _method_error()
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    try:
        identity = get_quality_trace_identity(user)
    except QualityTraceAccessError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    if identity.organization.organization_type.code == "OWNER":
        rows = BatchQualityCase.objects.filter(factory_organization=identity.organization)
    else:
        rows = BatchQualityCase.objects.filter(
            supplier_material_batch__supplier=identity.organization,
        )
    return JsonResponse({
        "code": 0,
        "msg": "",
        "data": {"items": [serialize_case(row) for row in rows[:100]]},
    }, status=200)


def quality_case_detail(request, case_id):
    if request.method != "GET":
        return _method_error()
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    try:
        identity = get_quality_trace_identity(user)
    except QualityTraceAccessError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    filters = {"pk": case_id}
    if identity.organization.organization_type.code == "OWNER":
        filters["factory_organization"] = identity.organization
    else:
        filters["supplier_material_batch__supplier"] = identity.organization
    row = BatchQualityCase.objects.filter(**filters).first()
    if not row:
        return JsonResponse({"code": 1, "msg": "质量案件不存在或无权查看。", "data": {}}, status=200)
    data = serialize_case(row, include_devices=True)
    active_actions = list(row.control_actions.filter(status=QualityControlAction.Status.ACTIVE))
    active_temporary = next((item for item in active_actions if item.action_type == QualityControlAction.ActionType.TEMPORARY_HOLD), None)
    active_formal = next((item for item in active_actions if item.action_type == QualityControlAction.ActionType.FORMAL_HOLD), None)
    active_recall = next((item for item in active_actions if item.action_type == QualityControlAction.ActionType.RECALL), None)
    current_campaign = (
        QualityRecallCampaign.objects.select_related("control_action")
        .filter(Q(quality_case=row) | Q(case_links__quality_case=row))
        .filter(status__in=(
            QualityRecallCampaign.Status.DRAFT,
            QualityRecallCampaign.Status.ACTIVE,
        ))
        .distinct()
        .order_by("-id")
        .first()
    )
    bound_action_ids = set(
        row.recall_campaigns.exclude(control_action_id=None)
        .values_list("control_action_id", flat=True)
    )
    orphan_recall = bool(active_recall and active_recall.pk not in bound_action_ids)
    can_control = identity.organization.organization_type.code == "OWNER"
    data["recall_campaign"] = ({
        "id": current_campaign.pk,
        "recall_no": current_campaign.recall_no,
        "title": current_campaign.title,
        "status": current_campaign.status,
        "device_count": current_campaign.device_count,
        "deadline": current_campaign.deadline.isoformat(),
        "control_action_id": current_campaign.control_action_id,
    } if current_campaign else None)
    data["recall_binding"] = {
        "has_live_campaign": bool(current_campaign),
        "has_orphan_active_action": orphan_recall,
        "is_consistent": not active_recall or bool(
            current_campaign and current_campaign.control_action_id == active_recall.pk
        ),
    }
    data["available_actions"] = {
        "temporary_hold": can_control and row.status == BatchQualityCase.Status.OPEN and not active_actions,
        "formal_hold": can_control and row.status in {BatchQualityCase.Status.OPEN, BatchQualityCase.Status.HELD} and not active_formal and not active_recall,
        "start_recall": can_control and row.status in {
            BatchQualityCase.Status.OPEN,
            BatchQualityCase.Status.HELD,
        } and not current_campaign and not active_recall,
        "create_recall_campaign": False,
        "open_recall_detail": can_control and row.status in {
            BatchQualityCase.Status.OPEN,
            BatchQualityCase.Status.HELD,
            BatchQualityCase.Status.RECALLING,
        } and bool(current_campaign or orphan_recall),
        "continue_recall_campaign_id": current_campaign.pk if current_campaign else None,
        "revoke_temporary_action_id": active_temporary.pk if can_control and active_temporary and not active_recall else None,
        "release_formal_action_id": active_formal.pk if can_control and active_formal and not active_recall else None,
        "complete_recall_action_id": None,
    }
    data["events"] = [{
        "id": event.pk,
        "event_type": event.event_type,
        "actor_role": event.actor_role_snapshot,
        "payload": event.payload,
        "created_at": event.created_at.isoformat(),
    } for event in row.events.order_by("-created_at", "-id")[:100]]
    return JsonResponse({"code": 0, "msg": "", "data": data}, status=200)


@csrf_exempt
def temporary_hold(request, case_id):
    if request.method != "POST":
        return JsonResponse({"code": 1, "msg": "仅支持POST", "data": {}}, status=200)
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    payload = _request_data(request)
    try:
        row = place_temporary_hold(
            user=user,
            case_id=case_id,
            reason=payload.get("reason"),
            duration_hours=payload.get("duration_hours"),
        )
    except QualityCaseError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    return JsonResponse({
        "code": 0,
        "msg": "临时拦截已生效，到期后将自动解除",
        "data": serialize_case(row.quality_case, include_devices=False),
    }, status=200)


@csrf_exempt
def revoke_hold(request, action_id):
    if request.method != "POST":
        return JsonResponse({"code": 1, "msg": "仅支持POST", "data": {}}, status=200)
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    try:
        row = revoke_temporary_hold(
            user=user,
            action_id=action_id,
            reason=_request_data(request).get("reason"),
        )
    except QualityCaseError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    return JsonResponse({
        "code": 0,
        "msg": "临时拦截已人工解除",
        "data": serialize_case(row.quality_case),
    }, status=200)


@csrf_exempt
def formal_hold(request, case_id):
    if request.method != "POST":
        return JsonResponse({"code": 1, "msg": "仅支持POST", "data": {}}, status=200)
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    try:
        row = place_formal_hold(
            user=user, case_id=case_id,
            reason=_request_data(request).get("reason"),
        )
    except QualityCaseError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    return JsonResponse({
        "code": 0, "msg": "正式拦截已生效",
        "data": serialize_case(row.quality_case),
    }, status=200)


@csrf_exempt
def formal_hold_release(request, action_id):
    if request.method != "POST":
        return JsonResponse({"code": 1, "msg": "仅支持POST", "data": {}}, status=200)
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    try:
        row = release_formal_hold(
            user=user, action_id=action_id,
            reason=_request_data(request).get("reason"),
        )
    except QualityCaseError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    return JsonResponse({
        "code": 0, "msg": "正式拦截已解除",
        "data": serialize_case(row.quality_case),
    }, status=200)


@csrf_exempt
def recall_start(request, case_id):
    if request.method != "POST":
        return JsonResponse({"code": 1, "msg": "仅支持POST", "data": {}}, status=200)
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    try:
        campaign = ensure_recall_campaign(
            user=user,
            case_id=case_id,
            reason=_request_data(request).get("reason"),
        )
    except (QualityCaseError, QualityRecallError) as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    return JsonResponse({
        "code": 0,
        "msg": "召回已启动，召回批次与质量案件已绑定",
        "data": {
            "id": campaign.pk,
            "recall_no": campaign.recall_no,
            "quality_case_id": campaign.quality_case_id,
            "status": campaign.status,
            "control_action_id": campaign.control_action_id,
            "device_count": campaign.device_count,
        },
    }, status=200)


@csrf_exempt
def recall_complete(request, action_id):
    if request.method != "POST":
        return JsonResponse({"code": 1, "msg": "仅支持POST", "data": {}}, status=200)
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    try:
        row = complete_recall(
            user=user, action_id=action_id,
            reason=_request_data(request).get("reason"),
        )
    except QualityCaseError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    return JsonResponse({
        "code": 0, "msg": "召回行动已完成，案件进入处置阶段",
        "data": serialize_case(row.quality_case),
    }, status=200)


@csrf_exempt
def gate_check(request):
    if request.method != "POST":
        return JsonResponse({"code": 1, "msg": "仅支持POST", "data": {}}, status=200)
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    payload = _request_data(request)
    try:
        identity = get_quality_trace_identity(user)
        if identity.organization.organization_type.code != "OWNER":
            raise QualityTraceAccessError("只有厂家可以通过API检查设备质量闸门。")
        device_id = int(payload.get("device_id"))
        if not Device.objects.filter(
            pk=device_id,
            batch__manufacturer=identity.organization,
        ).exists():
            raise QualityTraceAccessError("该设备不属于当前厂家。")
        decision = check_device_operation(
            device_id=device_id,
            operation=payload.get("operation"),
            user=user,
            context=payload.get("context"),
            record_allowed=bool(payload.get("record_allowed", False)),
        )
    except (QualityTraceAccessError, QualityGateError, TypeError, ValueError) as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    return JsonResponse({
        "code": 0 if decision["allowed"] else 2,
        "msg": decision["message"],
        "data": decision,
    }, status=200)


def gate_decisions(request):
    if request.method != "GET":
        return _method_error()
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    try:
        identity = get_quality_trace_identity(user)
    except QualityTraceAccessError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    if identity.organization.organization_type.code != "OWNER":
        return JsonResponse({"code": 0, "msg": "", "data": {"items": []}}, status=200)
    rows = QualityGateDecision.objects.filter(
        factory_organization=identity.organization,
    ).select_related("device", "quality_case", "control_action")[:200]
    return JsonResponse({
        "code": 0,
        "msg": "",
        "data": {"items": [serialize_gate_decision(row) for row in rows]},
    }, status=200)
