from sales.services.supplier_batch_trace_service import (
    SupplierBatchTraceError,
    get_supplier_batch_trace,
)
from supply_chain.models import SupplierMaterialBatch

from .access_service import get_quality_trace_identity


class QualityImpactTraceError(Exception):
    pass


def trace_supplier_batch_impact(*, user, batch_id):
    """Call the existing supplier-batch trace and apply viewer data boundaries.

    The source service remains authoritative for order/device linkage. This App
    does not duplicate or mutate sales, manufacturing or lifecycle facts.
    """

    identity = get_quality_trace_identity(user)
    batch = SupplierMaterialBatch.objects.filter(pk=batch_id).first()
    if not batch:
        raise QualityImpactTraceError("供应商物料批次不存在。")

    organization_type = identity.organization.organization_type.code
    if organization_type == "SUPPLIER" and batch.supplier_id != identity.organization_id:
        raise QualityImpactTraceError("无权查看其他供应商的物料批次。")

    try:
        data = get_supplier_batch_trace(
            batch_id=batch.pk,
            factory_id=identity.organization_id if organization_type == "OWNER" else None,
        )
    except SupplierBatchTraceError as exc:
        raise QualityImpactTraceError(str(exc)) from exc

    data["viewer_scope"] = {
        "organization_id": identity.organization_id,
        "organization_name": identity.organization.name,
        "organization_type": organization_type,
        "role": identity.role.code,
    }
    return data

