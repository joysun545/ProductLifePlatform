from django.db.models import Count, F, Q
from django.utils import timezone

from lifecycle.models import DeviceMaterialFaultRecord
from sales.services.supplier_batch_trace_service import consumed_material_lots
from supply_chain.models import FactoryMaterialLot
from supply_chain.services.material_inventory_service import quantity_text


MATERIAL_METRIC_DEFINITIONS = {
    "material_batch_count": {
        "description": "存在实际配套消耗的去重厂家入库批次。",
        "source_models": ["supply_chain.FactoryMaterialLot", "sales.OrderPlanMaterialLotAllocation"],
    },
    "material_covered_device_count": {
        "description": "通过实际配套消耗关联至生产批次的已生成设备，按设备去重。",
        "source_models": [
            "sales.OrderPlanMaterialLotAllocation", "sales.OrderPlanProductionSubplanItem",
            "manufacturing.ProductionBatch", "lifecycle.Device",
        ],
    },
}


def _usage_scope(identity):
    if identity.organization_type == "OWNER":
        return consumed_material_lots(factory_id=identity.organization.pk)
    if identity.organization_type == "SUPPLIER":
        root = identity.organization.get_factory_root()
        if root:
            return consumed_material_lots(factory_id=root.pk).filter(
                requirement__supplier=identity.organization,
            )
    return consumed_material_lots().none()


def _fault_scope(identity):
    qs = DeviceMaterialFaultRecord.objects.all()
    if identity.organization_type == "OWNER":
        return qs.filter(source_production_batch__manufacturer=identity.organization)
    if identity.organization_type == "SUPPLIER":
        return qs.filter(supplier=identity.organization)
    return qs.none()


def build_material_statistics(identity):
    usages = _usage_scope(identity)
    faults = _fault_scope(identity)
    subplan_item = "requirement__production_subplan_item"
    batch = subplan_item + "__production_batch"
    factory = subplan_item + "__order_plan_item__plan__factory_organization_id"
    batch_count = Count("material_lot_id", distinct=True)
    # Consumption can precede device creation. Count each real device once,
    # even if it is covered by several requirements or receipt lots.
    device_count = Count(
        batch + "__devices",
        filter=Q(**{batch + "__manufacturer_id": F(factory)}),
        distinct=True,
    )
    metrics = usages.aggregate(
        material_batch_count=batch_count,
        material_usage_count=Count("pk", distinct=True),
        material_covered_device_count=device_count,
    )
    metrics["material_fault_device_count"] = faults.values("device_id").distinct().count()
    supplier_rows = list(
        usages.values(supplier_id=F("requirement__supplier_id"), supplier_name=F("requirement__supplier__name"))
        .annotate(batch_count=batch_count, device_count=device_count)
        .order_by("-device_count", "supplier_id")[:10]
    )
    material_rows = list(
        usages.values(
            material_id=F("requirement__material_id"),
            material_code=F("requirement__material__code"),
            material_name=F("requirement__material__name"),
        )
        .annotate(batch_count=batch_count, device_count=device_count)
        .order_by("-device_count", "material_id")[:10]
    )
    fault_rows = list(
        faults.exclude(material_id=None).values("material_id", "material__code", "material__name")
        .annotate(device_count=Count("device_id", distinct=True), occurrence_count=Count("id"))
        .order_by("-device_count", "material_id")[:10]
    )
    return {
        "domain": "MATERIAL",
        "metrics": metrics,
        "metric_note": "物料批次按实际消耗的厂家入库批次去重。配套记录按物料需求与入库批次之间的实际消耗记录统计。配套设备按关联生产批次已生成的设备去重。",
        "charts": {
            "supplier_distribution": supplier_rows,
            "material_distribution": material_rows,
            "material_fault_distribution": fault_rows,
        },
    }


def build_factory_material_lot_catalog(identity, *, query="", cursor=None):
    """Historical factory lots belong to the warehouse keeper's statistics view.

    The material warehouse page is for operations and targeted lookup. This
    read-only catalog is the only routine place to browse all historical lots.
    """
    if not identity.can_view_warehouse_lots:
        raise PermissionError("当前岗位无权查看厂家物料批次清单。")
    rows = FactoryMaterialLot.objects.select_related(
        "stock__supplier", "stock__material_spec__material",
    ).filter(stock__factory=identity.organization)
    query = str(query).strip()[:100]
    if query:
        rows = rows.filter(
            Q(batch_code__icontains=query)
            | Q(supplier_batch_code__icontains=query)
            | Q(stock__supplier__name__icontains=query)
            | Q(stock__material_spec__material__name__icontains=query)
            | Q(stock__material_spec__code__icontains=query)
            | Q(stock__material_spec__name__icontains=query)
        )
    if cursor:
        if not str(cursor).isdigit() or int(cursor) <= 0:
            raise ValueError("分页位置无效")
        rows = rows.filter(pk__lt=int(cursor))
    entries = list(rows.order_by("-pk")[:31])
    items = []
    for lot in entries[:30]:
        available = lot.on_hand_milli - lot.reserved_milli
        items.append({
            "id": lot.pk, "stock_id": lot.stock_id, "batch_code": lot.batch_code,
            "supplier_batch_code": lot.supplier_batch_code,
            "supplier_name": lot.stock.supplier.name,
            "material_name": lot.stock.material_spec.material.name,
            "spec_name": lot.stock.material_spec.name, "spec_code": lot.stock.material_spec.code,
            "unit": lot.stock.unit, "received": quantity_text(lot.received_milli),
            "on_hand": quantity_text(lot.on_hand_milli), "reserved": quantity_text(lot.reserved_milli),
            "available": quantity_text(available),
            "received_at": timezone.localtime(lot.received_at).strftime("%Y-%m-%d %H:%M"),
            "status": "有结余" if lot.on_hand_milli else "已用完或已退完",
        })
    return {"items": items, "next_cursor": entries[29].pk if len(entries) > 30 else None}
