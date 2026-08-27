from collections import Counter

from lifecycle.models import LifecycleEvent
from manufacturing.models import ProductionBatch
from sales.models import OrderPlanItemMaterialBatch
from supply_chain.models import SupplierMaterialBatch


class SupplierBatchTraceError(Exception):
    pass


def get_supplier_batch_trace(*, batch_id):
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
    for usage in usages:
        item = usage.order_plan_item
        production_batch = (
            ProductionBatch.objects.filter(
                batch_no=item.order_batch_code,
            ).first()
        )
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
            "affected_new_production_quantity": item.production_quantity,
            "excluded_return_inventory_quantity": item.return_inventory_quantity,
            "production_batch_id": (
                production_batch.pk if production_batch else None
            ),
            "production_batch_no": (
                production_batch.batch_no if production_batch else ""
            ),
            "generated_device_count": len(devices),
            "device_trace_ready": bool(
                production_batch
                and len(devices) == item.production_quantity
            ),
            "devices": device_rows,
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
