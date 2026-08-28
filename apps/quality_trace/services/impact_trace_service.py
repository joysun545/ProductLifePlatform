from sales.services.supplier_batch_trace_service import (
    SupplierBatchTraceError,
    get_supplier_batch_trace,
)
from supply_chain.models import SupplierMaterialBatch

from .access_service import get_quality_trace_identity


class QualityImpactTraceError(Exception):
    pass


def _filtered_totals(data, orders):
    result = dict(data)
    result["orders"] = orders
    result["order_usage_count"] = len(orders)
    result["affected_new_device_quantity"] = sum(
        row["affected_new_production_quantity"] for row in orders
    )
    result["excluded_return_inventory_quantity"] = sum(
        row["excluded_return_inventory_quantity"] for row in orders
    )
    result["generated_device_count"] = sum(
        row["generated_device_count"] for row in orders
    )
    return result


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
        data = get_supplier_batch_trace(batch_id=batch.pk)
    except SupplierBatchTraceError as exc:
        raise QualityImpactTraceError(str(exc)) from exc

    if organization_type == "OWNER":
        factory_id = identity.organization_id
        plan_ids = {
            usage.order_plan_item.plan_id
            for usage in batch.order_plan_usages.select_related(
                "order_plan_item__plan"
            ).filter(
                order_plan_item__plan__factory_organization_id=factory_id,
            )
        }
        orders = [row for row in data["orders"] if row["plan_id"] in plan_ids]
        data = _filtered_totals(data, orders)

    data["viewer_scope"] = {
        "organization_id": identity.organization_id,
        "organization_name": identity.organization.name,
        "organization_type": organization_type,
        "role": identity.role.code,
    }
    return data

