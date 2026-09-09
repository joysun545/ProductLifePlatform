from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import render

from quality_trace.models import BatchQualityCase, QualityAlert
from quality_trace.services import QualityTraceAccessError, get_quality_trace_identity


@login_required
def dashboard(request):
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
    })

