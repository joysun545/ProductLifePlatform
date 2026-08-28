from account.services.auth_service import get_active_identity


class QualityTraceAccessError(Exception):
    pass


FACTORY_ORGANIZATION_TYPE = "OWNER"
SUPPLIER_ORGANIZATION_TYPE = "SUPPLIER"
SUPPLIER_ALLOWED_ROLES = {"supplier_owner", "supplier_sales"}


def get_quality_trace_identity(user):
    """Resolve the only active identity and enforce the R13.0 data boundary."""

    identity = get_active_identity(user)
    if not identity:
        raise QualityTraceAccessError("当前账号没有有效的组织身份。")
    organization_type = identity.organization.organization_type.code
    if organization_type == FACTORY_ORGANIZATION_TYPE:
        return identity
    if (
        organization_type == SUPPLIER_ORGANIZATION_TYPE
        and identity.role.code in SUPPLIER_ALLOWED_ROLES
    ):
        return identity
    raise QualityTraceAccessError("当前岗位无权访问质量追溯数据。")

