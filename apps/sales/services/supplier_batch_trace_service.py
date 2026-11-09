from collections import Counter

from django.db.models import F, OuterRef, Subquery
from django.db.models.functions import Trim, Upper

from lifecycle.models import LifecycleEvent
from manufacturing.models import ProductionBatch
from sales.models import OrderPlanItemMaterialBatch, OrderPlanMaterialLotAllocation
from supply_chain.models import SupplierMaterialBatch


class SupplierBatchTraceError(Exception):
    pass


def consumed_material_lots(factory_id=None):
    """Actual V2 usage attached to a production subplan and a receipt lot."""
    plan = "requirement__production_subplan_item__order_plan_item__plan"
    rows = OrderPlanMaterialLotAllocation.objects.filter(
        consumed_milli__gt=0,
        material_lot__stock__factory_id=F(plan + "__factory_organization_id"),
        material_lot__stock__supplier_id=F("requirement__supplier_id"),
        material_lot__stock__material_spec_id=F("requirement__material_spec_id"),
    ).order_by()
    if factory_id is not None:
        rows = rows.filter(**{plan + "__factory_organization_id": factory_id})
    return rows


def with_supplier_batch_id(rows):
    """Resolve a recorded supplier label within its supplier and material scope."""
    batches = SupplierMaterialBatch.objects.filter(
        supplier_id=OuterRef("requirement__supplier_id"),
        material_id=OuterRef("requirement__material_id"),
        material_spec_id=OuterRef("requirement__material_spec_id"),
        normalized_batch_code=Upper(Trim(OuterRef("material_lot__supplier_batch_code"))),
    ).order_by()
    return rows.annotate(supplier_batch_id=Subquery(batches.values("pk")[:1]))


def get_supplier_batch_trace(*, batch_id, factory_id=None):
    """Trace one supplier batch only through newly produced devices.

    Return-inventory devices belong to their original production batches and
    are reported separately; they never inherit the current order item's new
    supplier batches.
    """
    batch = (
        SupplierMaterialBatch.objects.select_related(
            "supplier", "material", "material_spec",
        ).filter(pk=batch_id).first()
    )
    if not batch:
        raise SupplierBatchTraceError("供应商物料批次不存在。")

    orders = []
    usages = (
        OrderPlanItemMaterialBatch.objects.filter(
            supplier_material_batch=batch,
        )
        .select_related(
            "order_plan_item__plan",
            "order_plan_item__product_model",
            "bom_item",
        )
        .order_by("order_plan_item_id", "bom_item_id")
    )
    if factory_id is not None:
        usages = usages.filter(order_plan_item__plan__factory_organization_id=factory_id)

    scopes = []
    for usage in usages:
        item = usage.order_plan_item
        production_batch = ProductionBatch.objects.filter(
            batch_no=item.order_batch_code,
            manufacturer_id=item.plan.factory_organization_id,
        ).first()
        scopes.append((item, production_batch, item.production_quantity, None))

    allocations = with_supplier_batch_id(consumed_material_lots(factory_id)).filter(
        supplier_batch_id=batch.pk,
    ).select_related(
        "requirement__production_subplan_item__production_batch",
        "requirement__production_subplan_item__order_plan_item__plan",
        "requirement__production_subplan_item__order_plan_item__product_model",
        "material_lot",
    )
    lots_by_subplan_item = {}
    for allocation in allocations:
        subplan_item = allocation.requirement.production_subplan_item
        entry = lots_by_subplan_item.setdefault(subplan_item.pk, (subplan_item, set()))
        entry[1].add(allocation.material_lot.batch_code)
    for subplan_item, lot_codes in lots_by_subplan_item.values():
        item = subplan_item.order_plan_item
        production_batch = subplan_item.production_batch
        if production_batch and production_batch.manufacturer_id != item.plan.factory_organization_id:
            continue
        scopes.append((item, production_batch, subplan_item.quantity, sorted(lot_codes)))

    seen_batches = set()
    seen_order_items = set()
    # Prefer the actual subplan when legacy and V2 both point at the same batch.
    scopes.sort(key=lambda scope: scope[3] is None)
    for item, production_batch, production_quantity, lot_codes in scopes:
        if production_batch:
            if production_batch.pk in seen_batches:
                continue
            seen_batches.add(production_batch.pk)
        excluded_return_quantity = item.return_inventory_quantity if item.pk not in seen_order_items else 0
        seen_order_items.add(item.pk)
        devices = list(
            production_batch.devices.select_related("qrcode").order_by("id")
        ) if production_batch else []
        device_rows = []
        for device in devices:
            event_counts = Counter(
                LifecycleEvent.objects.filter(device=device).values_list(
                    "event_type", flat=True,
                )
            )
            device_rows.append({
                "device_id": device.pk,
                "sn": device.sn,
                "qrcode": (
                    device.qrcode.code if hasattr(device, "qrcode") else ""
                ),
                "lifecycle_event_counts": dict(event_counts),
            })
        orders.append({
            "plan_id": item.plan_id,
            "plan_code": item.plan.plan_code,
            "order_plan_item_id": item.pk,
            "order_batch_code": item.order_batch_code,
            "product_model_id": item.product_model_id,
            "product_model_name": item.product_model.name,
            "order_total_quantity": item.quantity,
            "affected_new_production_quantity": production_quantity,
            "excluded_return_inventory_quantity": excluded_return_quantity,
            "production_batch_id": (
                production_batch.pk if production_batch else None
            ),
            "production_batch_no": (
                production_batch.batch_no if production_batch else ""
            ),
            "generated_device_count": len(devices),
            "device_trace_ready": bool(
                production_batch
                and len(devices) == production_quantity
            ),
            "devices": device_rows,
            "factory_receipt_batch_codes": lot_codes or [],
            "trace_granularity": "PRODUCTION_BATCH",
        })

    return {
        "supplier_batch": {
            "id": batch.pk,
            "supplier_id": batch.supplier_id,
            "supplier_name": batch.supplier.name,
            "material_id": batch.material_id,
            "material_name": batch.material.name,
            "material_spec_id": batch.material_spec_id,
            "material_spec_name": batch.material_spec.name,
            "batch_code": batch.batch_code,
            "production_date": (
                batch.production_date.isoformat() if batch.production_date else ""
            ),
        },
        "order_usage_count": len(orders),
        "affected_new_device_quantity": sum(
            row["affected_new_production_quantity"] for row in orders
        ),
        "excluded_return_inventory_quantity": sum(
            row["excluded_return_inventory_quantity"] for row in orders
        ),
        "generated_device_count": sum(
            row["generated_device_count"] for row in orders
        ),
        "orders": orders,
    }
