import json

from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from account.models import UserOrganization
from product.models import ProductCategory, ProductModel
from quality_trace.models import BatchQualityCase, QualityAlert
from quality_trace.services import (
    QualityRuleError, QualityTraceAccessError, get_quality_trace_identity,
    list_rules, publish_rule_version, simulate_rule,
)
from supply_chain.models import Material, MaterialFaultCause, MaterialSpec, SupplierMaterialBatch


def _development_rule_user(request):
    if request.user.is_authenticated:
        return request.user
    identity = UserOrganization.objects.filter(
        is_active=True,
        organization__organization_type__code="OWNER",
        role__code__in=("factory_chief_engineer", "factory_admin"),
    ).select_related("user").order_by("role__code", "pk").first()
    if not identity:
        raise QualityTraceAccessError("开发环境尚未配置厂家负责人或厂家总工程师账号。")
    return identity.user


def _payload(request):
    try:
        body = json.loads((request.body or b"{}").decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        body = {}
    return body if isinstance(body, dict) else {}


def _console_context(request):
    materials = Material.objects.filter(is_key_part=True).order_by("code")
    return {
        "materials": materials,
        "categories": ProductCategory.objects.all().order_by("name"),
        "models": ProductModel.objects.all().order_by("code"),
        "specs": MaterialSpec.objects.select_related("material").order_by("material__code", "code"),
        "supplier_batches": SupplierMaterialBatch.objects.select_related("supplier", "material", "material_spec").order_by("-id")[:500],
    }


@ensure_csrf_cookie
def dashboard(request):
    # Development phase: the QR-entry session gate is intentionally not yet
    # installed.  The production implementation will restore role-bound
    # temporary-session validation before this view is exposed.
    if not request.user.is_authenticated:
        return render(request, "quality_trace/dashboard.html", {
            "identity": None,
            "alerts": QualityAlert.objects.all()[:20],
            "cases": BatchQualityCase.objects.all()[:20],
            "development_mode": True,
            **_console_context(request),
        })
    try:
        identity = get_quality_trace_identity(request.user)
    except QualityTraceAccessError as exc:
        raise PermissionDenied(str(exc)) from exc

    organization = identity.organization
    if organization.organization_type.code == "OWNER":
        alerts = QualityAlert.objects.filter(factory_organization=organization)[:20]
        cases = BatchQualityCase.objects.filter(factory_organization=organization)[:20]
    else:
        alerts = QualityAlert.objects.none()
        cases = BatchQualityCase.objects.filter(
            supplier_material_batch__supplier=organization,
        )[:20]
    return render(request, "quality_trace/dashboard.html", {
        "identity": identity,
        "alerts": alerts,
        "cases": cases,
        **_console_context(request),
    })


@require_GET
def root_causes(request):
    material_id = request.GET.get("material_id")
    rows = MaterialFaultCause.objects.filter(material_id=material_id, is_active=True).order_by("grade", "code")
    return JsonResponse({"code": 0, "data": {"items": [
        {"id": item.pk, "code": item.code, "name": item.name, "grade": item.grade}
        for item in rows
    ]}})


@require_POST
def rule_simulation(request):
    try:
        data = simulate_rule(user=_development_rule_user(request), data=_payload(request))
    except (QualityTraceAccessError, QualityRuleError) as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    return JsonResponse({"code": 0, "msg": "模拟计算完成；未保存规则、未生成预警。", "data": data})


@require_POST
def rule_publish(request):
    try:
        data = publish_rule_version(user=_development_rule_user(request), data=_payload(request))
    except (QualityTraceAccessError, QualityRuleError) as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    return JsonResponse({"code": 0, "msg": "质量预警规则版本已发布。", "data": data})


@require_GET
def rule_list(request):
    try:
        items = list_rules(user=_development_rule_user(request))
    except (QualityTraceAccessError, QualityRuleError) as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)
    return JsonResponse({"code": 0, "data": {"items": items}})
