from django.utils import timezone

from .aftersales_service import build_aftersales_statistics
from .dashboard_config import DASHBOARD_NAMES, modules_for_role
from .flow_service import build_flow_statistics
from .material_service import build_material_statistics
from .quality_service import build_quality_statistics


BUILDERS = {
    "FLOW": build_flow_statistics,
    "MATERIAL": build_material_statistics,
    "AFTERSALES": build_aftersales_statistics,
    "QUALITY": build_quality_statistics,
}


def build_dashboard(identity, requested_domain=None):
    modules = modules_for_role(identity.role.code)
    from lifecycle.services.aftersales_access import capabilities
    if capabilities(identity.user)["can_view_repair_basis"] and "AFTERSALES" not in modules:
        modules = modules + ("AFTERSALES",)
    if requested_domain:
        requested_domain = requested_domain.upper()
        modules = (requested_domain,) if requested_domain in modules else ()
    sections = [BUILDERS[module](identity) for module in modules if module in BUILDERS]
    return {
        "dashboard_code": identity.role.code.upper(),
        "dashboard_name": DASHBOARD_NAMES.get(identity.role.code, "岗位数据驾驶舱"),
        "generated_at": timezone.now().isoformat(),
        "identity": {
            "organization_id": identity.organization.pk,
            "organization_name": identity.organization.name,
            "organization_type": identity.organization_type,
            "role_code": identity.role.code,
            "role_name": identity.role.name,
        },
        "modules": list(modules),
        "sections": sections,
        "read_only": True,
    }
