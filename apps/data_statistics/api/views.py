from django.http import JsonResponse

from account.services.auth_service import ApiAuthenticationError, authenticate_request
from data_statistics.models import DailyMetricSnapshot, MetricDefinition
from data_statistics.services import StatisticsAccessError, build_dashboard, get_statistics_identity
from data_statistics.services.material_service import build_factory_material_lot_catalog


def _error(message, code=1, status=200):
    return JsonResponse({"code": code, "msg": str(message), "data": {}}, status=status)


def _identity(request):
    try:
        user = authenticate_request(request)
        return get_statistics_identity(user), None
    except ApiAuthenticationError as exc:
        return None, _error(exc, code=401, status=401)
    except StatisticsAccessError as exc:
        return None, _error(exc)


def _get_only(request):
    return None if request.method == "GET" else _error("仅支持GET")


def dashboard(request):
    error = _get_only(request)
    if error:
        return error
    identity, error = _identity(request)
    if error:
        return error
    return JsonResponse({"code": 0, "msg": "", "data": build_dashboard(identity)})


def _domain(request, code):
    error = _get_only(request)
    if error:
        return error
    identity, error = _identity(request)
    if error:
        return error
    data = build_dashboard(identity, requested_domain=code)
    if not data["sections"]:
        return _error("当前岗位无权查看该统计专题。")
    return JsonResponse({"code": 0, "msg": "", "data": data["sections"][0]})


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
    except (PermissionError, ValueError) as exc:
        return _error(exc)
    return JsonResponse({"code": 0, "msg": "", "data": data})


def flow(request):
    return _domain(request, "FLOW")


def aftersales(request):
    return _domain(request, "AFTERSALES")


def quality(request):
    return _domain(request, "QUALITY")


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
    rows = DailyMetricSnapshot.objects.filter(
        organization=identity.organization, metric=definition, dimension_key="",
    ).order_by("snapshot_date")
    return JsonResponse({
        "code": 0, "msg": "", "data": {
            "metric": {"code": definition.code, "name": definition.name, "unit": definition.unit},
            "items": [{"date": row.snapshot_date.isoformat(), "value": float(row.value)} for row in rows],
        },
    })
