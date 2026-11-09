from django.http import JsonResponse

from account.services.auth_service import ApiAuthenticationError, authenticate_request
from data_statistics.models import DailyMetricSnapshot, MetricDefinition
from data_statistics.services import StatisticsAccessError, build_dashboard, get_statistics_identity
from data_statistics.services.material_service import (
    MATERIAL_METRIC_DEFINITIONS,
    build_factory_material_lot_catalog,
)


def _error(message, code=1, status=200):
    return JsonResponse({"code": code, "msg": str(message), "data": {}}, status=status)


def _access_denied(message):
    return JsonResponse({
        "code": 1, "msg": str(message), "data": {},
        "error_code": "STATISTICS_ACCESS_DENIED",
    })


def _identity(request):
    try:
        user = authenticate_request(request)
        return get_statistics_identity(user), None
    except ApiAuthenticationError as exc:
        return None, _error(exc, code=401, status=401)
    except StatisticsAccessError as exc:
        return None, _access_denied(exc)


def _get_only(request):
    return None if request.method == "GET" else _error("仅支持GET")


def dashboard(request):
    error = _get_only(request)
    if error:
        return error
    identity, error = _identity(request)
    if error:
        return error
    return JsonResponse({"code": 0, "msg": "", "data": build_dashboard(
        identity, requested_finance_root=request.GET.get("root_organization_id"),
    )})


def _domain(request, code):
    error = _get_only(request)
    if error:
        return error
    identity, error = _identity(request)
    if error:
        return error
    data = build_dashboard(identity, requested_domain=code,
                           requested_finance_root=request.GET.get("root_organization_id"))
    if not data["sections"]:
        return _access_denied("当前岗位无权查看该统计专题。")
    section = data["sections"][0]
    if section.get("access_denied"):
        return _access_denied(section["metric_note"])
    if code == "MATERIAL":
        section["can_view_warehouse_lots"] = identity.can_view_warehouse_lots
    return JsonResponse({"code": 0, "msg": "", "data": section})


def material(request):
    return _domain(request, "MATERIAL")


def material_warehouse_lots(request):
    error = _get_only(request)
    if error:
        return error
    identity, error = _identity(request)
    if error:
        return error
    try:
        data = build_factory_material_lot_catalog(
            identity, query=request.GET.get("q", ""), cursor=request.GET.get("cursor"),
        )
    except PermissionError as exc:
        return _access_denied(exc)
    except ValueError as exc:
        return _error(exc)
    return JsonResponse({"code": 0, "msg": "", "data": data})


def flow(request):
    return _domain(request, "FLOW")


def aftersales(request):
    return _domain(request, "AFTERSALES")


def quality(request):
    return _domain(request, "QUALITY")


def finance(request):
    return _domain(request, "FINANCE")


def metric_definitions(request):
    error = _get_only(request)
    if error:
        return error
    identity, error = _identity(request)
    if error:
        return error
    modules = set(build_dashboard(identity)["modules"])
    rows = MetricDefinition.objects.filter(enabled=True, domain__in=modules).order_by("domain", "code")
    items = list(rows.values(
        "code", "name", "domain", "unit", "description",
        "numerator_definition", "denominator_definition", "source_models",
    ))
    for item in items:
        item.update(MATERIAL_METRIC_DEFINITIONS.get(item["code"], {}))
    return JsonResponse({"code": 0, "msg": "", "data": {"items": items}})


def metric_trends(request, metric_code):
    error = _get_only(request)
    if error:
        return error
    identity, error = _identity(request)
    if error:
        return error
    definition = MetricDefinition.objects.filter(code=metric_code, enabled=True).first()
    if not definition or definition.domain not in build_dashboard(identity)["modules"]:
        return _error("指标不存在或当前岗位无权查看。")
    # Organization-only snapshots cannot represent an employee/driver's current scope.
    if definition.domain == "AFTERSALES" and not (
        identity.role.is_owner_role
        or identity.role.code in {"factory_admin", "factory_owner", "factory_aftersales"}
    ):
        return _error("该售后专题按当前授权范围实时统计，暂不提供未区分岗位范围的组织历史趋势。")
    rows = DailyMetricSnapshot.objects.filter(
        organization=identity.organization, metric=definition, dimension_key="",
    ).order_by("snapshot_date")
    return JsonResponse({
        "code": 0, "msg": "", "data": {
            "metric": {"code": definition.code, "name": definition.name, "unit": definition.unit},
            "items": [{"date": row.snapshot_date.isoformat(), "value": float(row.value)} for row in rows],
        },
    })


def aftersales_capabilities(request):
    error = _get_only(request)
    if error:
        return error
    identity, error = _identity(request)
    if error:
        return error
    from lifecycle.services.aftersales_access import capabilities
    return JsonResponse({"code": 0, "msg": "", "data": capabilities(identity.user)})


def repair_basis(request):
    error = _get_only(request)
    if error:
        return error
    identity, error = _identity(request)
    if error:
        return error
    from lifecycle.services.aftersales_access import capabilities
    from lifecycle.services.repair_basis_service import catalog
    if not capabilities(identity.user)["can_view_repair_basis"]:
        return _access_denied("当前岗位无权查看维修结算依据。")
    try:
        result = catalog(identity.user, request.GET.get("cursor"))
    except ValueError as exc:
        return _error(exc)
    return JsonResponse({"code": 0, "msg": "", "data": result})
