from django.db.models import Count, Q, Sum
from django.utils import timezone

from lifecycle.models import DeviceMaterialFaultRecord
from sales.models import OrderPlanItemMaterialBatch
from supply_chain.models import SupplierMaterialBatch
from supply_chain.models import FactoryMaterialLot
from supply_chain.services.material_inventory_service import quantity_text


def _usage_scope(identity):
    qs = OrderPlanItemMaterialBatch.objects.select_related(
        "supplier_material_batch__supplier", "bom_item__material",
    )
    if identity.organization_type == "OWNER":
        return qs.filter(order_plan_item__plan__factory_organization=identity.organization)
    if identity.organization_type == "SUPPLIER":
        return qs.filter(supplier_material_batch__supplier=identity.organization)
    return qs.none()


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
    batch_ids = usages.values("supplier_material_batch_id")
    batches = SupplierMaterialBatch.objects.filter(id__in=batch_ids)
    # 同一订单明细会为多个 BOM 物料行各生成一条 usage，必须先按订单
    # 明细去重，否则会把设备计划数量乘以 BOM 行数。
    covered_quantity = sum(
        row["order_plan_item__quantity"]
        for row in usages.values(
            "order_plan_item_id", "order_plan_item__quantity",
        ).distinct()
    )
    fault_devices = faults.values("device_id").distinct().count()
    supplier_rows = list(
        usages.values("supplier_material_batch__supplier_id", "supplier_material_batch__supplier__name")
        .annotate(batch_count=Count("supplier_material_batch_id", distinct=True), planned_device_quantity=Sum("order_plan_item__quantity"))
        .order_by("-planned_device_quantity")[:10]
    )
    material_rows = list(
        usages.values("bom_item__material_id", "bom_item__material__code", "bom_item__material__name")
        .annotate(batch_count=Count("supplier_material_batch_id", distinct=True), planned_device_quantity=Sum("order_plan_item__quantity"))
        .order_by("-planned_device_quantity")[:10]
    )
    fault_rows = list(
        faults.exclude(material_id=None).values("material_id", "material__code", "material__name")
        .annotate(device_count=Count("device_id", distinct=True), occurrence_count=Count("id"))
        .order_by("-device_count", "material_id")[:10]
    )
    return {
        "domain": "MATERIAL",
        "metrics": {
            "material_batch_count": batches.count(),
            "material_usage_count": usages.count(),
            "material_covered_device_count": covered_quantity,
            "material_fault_device_count": fault_devices,
        },
        "metric_note": "配套设备数当前按订单明细计划数量统计；待建立逐台订单—设备绑定事实后升级为真实去重设备数。",
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
    if identity.role.code != "factory_material_stock" or identity.organization_type != "OWNER":
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
