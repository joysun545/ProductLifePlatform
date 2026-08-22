from collections import OrderedDict

from django.db import transaction
from django.utils import timezone

from lifecycle.models import (
    FactoryReturnInventoryItem,
    LifecycleEvent,
    QRCode,
)
from lifecycle.services.qrcode_service import QRCodeService
from sales.models import (
    OrderPlan,
    OrderPlanItem,
    OrderPlanItemBOMConfirmation,
    OrderPlanReturnInventoryAllocation,
)


class OrderPlanReissueError(Exception):
    """退货待流转库存抵扣或重新流转失败。"""


def _available_queryset(*, factory_organization, product_model):
    return (
        FactoryReturnInventoryItem.objects.select_related(
            "process__transfer",
            "transfer_item__device",
            "source_bom",
            "source_production_batch",
        )
        .filter(
            process__transfer__to_organization=factory_organization,
            source_product_model=product_model,
            stock_status=FactoryReturnInventoryItem.StockStatus.PENDING_REISSUE,
        )
        .order_by("classified_at", "id")
    )


def _bom_groups(rows):
    groups = OrderedDict()
    for row in rows:
        bom = row.source_bom
        group = groups.setdefault(
            bom.pk,
            {
                "bom_id": bom.pk,
                "version": bom.version,
                "count": 0,
            },
        )
        group["count"] += 1
    return list(groups.values())


def recent_merchant_bom(*, merchant_organization, product_model):
    confirmation = (
        OrderPlanItemBOMConfirmation.objects.select_related(
            "confirmed_bom",
            "order_plan_item__plan",
        )
        .filter(
            order_plan_item__plan__merchant_organization=merchant_organization,
            order_plan_item__product_model=product_model,
        )
        .exclude(order_plan_item__plan__status=OrderPlan.Status.CANCELLED)
        .order_by("-order_plan_item__plan__submitted_at", "-id")
        .first()
    )
    if not confirmation:
        return None
    return {
        "bom_id": confirmation.confirmed_bom_id,
        "version": confirmation.confirmed_bom.version,
        "order_plan_id": confirmation.order_plan_item.plan_id,
        "plan_code": confirmation.order_plan_item.plan.plan_code,
        "confirmed_at": confirmation.confirmed_at.isoformat(),
    }


def get_return_inventory_options(
    *, factory_organization, merchant_organization, product_model
):
    rows = list(
        _available_queryset(
            factory_organization=factory_organization,
            product_model=product_model,
        )
    )
    return {
        "product_model_id": product_model.pk,
        "product_model_name": product_model.name,
        "available_count": len(rows),
        "return_inventory_boms": _bom_groups(rows),
        "recent_merchant_bom": recent_merchant_bom(
            merchant_organization=merchant_organization,
            product_model=product_model,
        ),
    }


def _source_order_snapshots(inventory_item):
    source_item = (
        OrderPlanItem.objects.filter(
            order_batch_code=inventory_item.source_production_batch.batch_no
        )
        .only("order_batch_code", "lifecycle_code")
        .first()
    )
    return (
        source_item.order_batch_code if source_item else inventory_item.source_production_batch.batch_no,
        source_item.lifecycle_code if source_item else "",
    )


def reserve_return_inventory(*, order_plan_item, requested_quantity, actor):
    """锁定并逐台预占；调用方必须在订单创建事务内执行。"""

    requested_quantity = max(0, int(requested_quantity or 0))
    if requested_quantity == 0:
        return []
    candidates = list(
        _available_queryset(
            factory_organization=order_plan_item.plan.factory_organization,
            product_model=order_plan_item.product_model,
        )
        .select_for_update()[:requested_quantity]
    )
    allocations = []
    for inventory_item in candidates:
        device = inventory_item.transfer_item.device
        qrcode = QRCode.objects.filter(device=device).first()
        source_order_batch, source_lifecycle = _source_order_snapshots(inventory_item)
        allocations.append(
            OrderPlanReturnInventoryAllocation.objects.create(
                order_plan_item=order_plan_item,
                inventory_item=inventory_item,
                source_device=device,
                source_bom=inventory_item.source_bom,
                source_order_batch_snapshot=source_order_batch,
                source_lifecycle_snapshot=source_lifecycle,
                source_qr_code_snapshot=qrcode.code if qrcode else "",
                original_qr=qrcode,
                reserved_by=actor,
            )
        )
        inventory_item.stock_status = (
            FactoryReturnInventoryItem.StockStatus.RESERVED_FOR_ORDER
        )
        inventory_item.save(update_fields=("stock_status", "updated_at"))
    return allocations


def serialize_item_reissue(order_plan_item):
    allocations = list(
        order_plan_item.return_inventory_allocations.select_related(
            "source_bom", "source_device"
        ).all()
    )
    return {
        "return_inventory_quantity": order_plan_item.return_inventory_quantity,
        "production_quantity": order_plan_item.production_quantity,
        "requires_matching": order_plan_item.production_quantity > 0,
        "return_inventory_boms": _bom_groups(allocations) if allocations else [],
        "allocated_devices": [
            {
                "allocation_id": allocation.pk,
                "device_id": allocation.source_device_id,
                "device_sn": allocation.source_device.sn,
                "source_bom_id": allocation.source_bom_id,
                "source_bom_version": allocation.source_bom.version,
                "status": allocation.status,
            }
            for allocation in allocations
        ],
    }


@transaction.atomic
def mark_return_inventory_ready_to_ship(*, plan):
    now = timezone.now()
    allocations = OrderPlanReturnInventoryAllocation.objects.select_for_update().filter(
        order_plan_item__plan=plan,
        status=OrderPlanReturnInventoryAllocation.Status.RESERVED,
    )
    inventory_ids = list(allocations.values_list("inventory_item_id", flat=True))
    allocations.update(
        status=OrderPlanReturnInventoryAllocation.Status.READY_TO_SHIP,
        ready_to_ship_at=now,
        updated_at=now,
    )
    if inventory_ids:
        FactoryReturnInventoryItem.objects.filter(pk__in=inventory_ids).update(
            stock_status=FactoryReturnInventoryItem.StockStatus.READY_TO_SHIP,
            updated_at=now,
        )


def ensure_reissue_qrcodes(*, order_plan_item, actor):
    """为沿用实体设备生成新二维码，并保留旧二维码和旧生命周期快照。"""

    allocations = list(
        OrderPlanReturnInventoryAllocation.objects.select_for_update()
        .select_related("source_device", "original_qr", "replacement_qr")
        .filter(order_plan_item=order_plan_item)
        .order_by("id")
    )
    result = []
    for allocation in allocations:
        if allocation.replacement_qr_id:
            result.append(allocation.replacement_qr)
            continue

        device = allocation.source_device
        old_qr = QRCode.objects.select_for_update().filter(device=device).first()
        if old_qr:
            if not allocation.original_qr_id:
                allocation.original_qr = old_qr
            if not allocation.source_qr_code_snapshot:
                allocation.source_qr_code_snapshot = old_qr.code
            old_qr.status = "REPLACED"
            old_qr.replaced_at = timezone.now()
            old_qr.device = None
            old_qr.save(
                update_fields=("status", "replaced_at", "device", "updated_at")
            )

        new_qr = QRCodeService.create_qrcode(device=device, created_by=actor)
        allocation.replacement_qr = new_qr
        allocation.save(
            update_fields=(
                "original_qr",
                "source_qr_code_snapshot",
                "replacement_qr",
                "updated_at",
            )
        )
        LifecycleEvent.objects.create(
            device=device,
            event_type="FACTORY_OUT",
            operator=actor,
            organization=order_plan_item.plan.factory_organization,
            from_organization=order_plan_item.plan.factory_organization,
            to_organization=order_plan_item.plan.factory_organization,
            data={
                "subtype": "RETURN_STOCK_REISSUE_QR",
                "order_plan_id": order_plan_item.plan_id,
                "order_plan_item_id": order_plan_item.pk,
                "allocation_id": allocation.pk,
                "source_order_batch": allocation.source_order_batch_snapshot,
                "source_lifecycle_code": allocation.source_lifecycle_snapshot,
                "old_qr_code": allocation.source_qr_code_snapshot,
                "new_order_batch": order_plan_item.order_batch_code,
                "new_lifecycle_code": order_plan_item.lifecycle_code,
                "new_qr_code": new_qr.code,
            },
        )
        result.append(new_qr)
    return result


@transaction.atomic
def mark_return_inventory_reissued(*, plan):
    now = timezone.now()
    allocations = OrderPlanReturnInventoryAllocation.objects.select_for_update().filter(
        order_plan_item__plan=plan,
        status=OrderPlanReturnInventoryAllocation.Status.READY_TO_SHIP,
    )
    inventory_ids = list(allocations.values_list("inventory_item_id", flat=True))
    allocations.update(
        status=OrderPlanReturnInventoryAllocation.Status.REISSUED,
        reissued_at=now,
        updated_at=now,
    )
    if inventory_ids:
        FactoryReturnInventoryItem.objects.filter(pk__in=inventory_ids).update(
            stock_status=FactoryReturnInventoryItem.StockStatus.REISSUED,
            updated_at=now,
        )
