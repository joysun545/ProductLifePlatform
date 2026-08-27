from django.db import models, transaction
from django.utils import timezone

from account.models import UserOrganization
from lifecycle.services.lifecycle_service import LifecycleService
from manufacturing.models import ProductionBatch
from product.models import BOMItem
from sales.models import OrderPlan, OrderPlanItem, OrderPlanItemMaterialBatch
from sales.services.order_plan_reissue_service import ensure_reissue_qrcodes
from workflow.services.workflow_service import publish_workitem
from workflow.models import WorkItem


class OrderQRCodePrintError(Exception):
    pass


ELIGIBLE_STATUSES = {
    OrderPlan.Status.PRODUCTION_COMPLETED,
}


def _ensure_shipment_authorized(plan):
    if not plan.shipment_authorized_at or not plan.shipment_authorized_by_id:
        raise OrderQRCodePrintError(
            "创建订单的厂家销售经理尚未确认发货。"
        )


def _item_payload(item):
    return {
        "id": item.pk,
        "category_name": item.category.name,
        "product_model_code": item.product_model.code,
        "product_model_name": item.product_model.name,
        "quantity": item.quantity,
        "return_inventory_quantity": item.return_inventory_quantity,
        "production_quantity": item.production_quantity,
        "order_batch_code": item.order_batch_code,
        "generated": item.qrcode_generated_at is not None,
        "printed": item.qrcode_printed_at is not None,
    }


def get_print_center_data():
    plans = (
        OrderPlan.objects.select_related(
            "merchant_organization", "created_by", "brand",
            "shipment_authorized_by",
        )
        .filter(
            status__in=ELIGIBLE_STATUSES,
            shipment_authorized_at__isnull=False,
            shipment_authorized_by__isnull=False,
            items__qrcode_printed_at__isnull=True,
        )
        .distinct()
        .order_by("submitted_at", "id")
    )
    result = []
    for plan in plans:
        items = list(
            OrderPlanItem.objects.select_related("category", "product_model")
            .filter(plan=plan, qrcode_printed_at__isnull=True)
            .order_by("id")
        )
        if not items:
            continue
        result.append({
            "id": plan.pk,
            "plan_code": plan.plan_code,
            "merchant_name": plan.merchant_organization.name,
            "sales_manager_name": (
                plan.created_by.wx_nickname
                or plan.created_by.get_full_name()
                or plan.created_by.username
            ),
            "brand_name": plan.brand.name,
            "items": [_item_payload(item) for item in items],
        })
    return {"orders": result}


def _get_item(item_id, *, lock=False):
    qs = OrderPlanItem.objects.select_related(
        "plan", "plan__factory_organization", "category", "product_model",
        "bom_confirmation", "bom_confirmation__confirmed_bom",
    )
    if lock:
        qs = qs.select_for_update()
    item = qs.filter(pk=item_id).first()
    if not item:
        raise OrderQRCodePrintError("订单产品明细不存在。")
    if item.plan.status not in ELIGIBLE_STATUSES:
        raise OrderQRCodePrintError("该订单当前不在二维码打印阶段。")
    _ensure_shipment_authorized(item.plan)
    if item.qrcode_printed_at:
        raise OrderQRCodePrintError("该产品型号的二维码已经打印完成。")
    if item.production_quantity > 0 and not hasattr(item, "bom_confirmation"):
        raise OrderQRCodePrintError("该产品型号尚未确认生产BOM。")
    return item


def _validate_supplier_batch_coverage(item):
    """Protect new production without changing historical order behavior."""
    if item.production_quantity <= 0:
        return
    usages = OrderPlanItemMaterialBatch.objects.filter(order_plan_item=item)
    supplier_tasks = WorkItem.objects.filter(
        type="ORDER_PLAN_SUPPLIER_PREPARE",
        uniq_key__startswith=(
            f"sales:order-plan:{item.plan_id}:supplier:"
        ),
    )
    capture_required = usages.exists()
    capture_required = capture_required or any(
        bool((workitem.payload or {}).get("supplier_batch_capture_required"))
        for workitem in supplier_tasks
    )
    if not capture_required:
        return

    expected = set(
        BOMItem.objects.filter(
            bom_id=item.bom_confirmation.confirmed_bom_id,
            supplier_id__isnull=False,
        ).values_list("id", flat=True)
    )
    actual = set(usages.values_list("bom_item_id", flat=True))
    if expected != actual:
        raise OrderQRCodePrintError(
            "供应商生产批次尚未覆盖最终BOM全部供应物料，不能生成二维码。"
        )
    invalid = usages.exclude(
        supplier_material_batch__supplier_id=models.F("bom_item__supplier_id"),
        supplier_material_batch__material_id=models.F("bom_item__material_id"),
        supplier_material_batch__material_spec_id=models.F("bom_item__material_spec_id"),
    ).exists()
    if invalid:
        raise OrderQRCodePrintError("供应商生产批次与最终BOM物料不一致。")


def _factory_stock_openids(plan):
    return list(
        UserOrganization.objects.filter(
            organization=plan.factory_organization,
            role__code="factory_stock",
            role__is_active=True,
            is_active=True,
            user__is_active=True,
            user__wx_openid__isnull=False,
        )
        .exclude(user__wx_openid="")
        .values_list("user__wx_openid", flat=True)
    )


def _publish_stock_outbound_task(plan, *, qrcodes_ready):
    _ensure_shipment_authorized(plan)
    stock_openids = _factory_stock_openids(plan)
    if not stock_openids:
        raise OrderQRCodePrintError("厂家尚未配置有效的库管账号，不能完成整单打印。")

    items = list(
        plan.items.select_related("category", "product_model").order_by("id")
    )
    payload = {
        "order_plan_id": plan.pk,
        "plan_code": plan.plan_code,
        "items": [
            {
                "id": row.pk,
                "category_name": row.category.name,
                "product_model_code": row.product_model.code,
                "product_model_name": row.product_model.name,
                "quantity": row.quantity,
                "return_inventory_quantity": row.return_inventory_quantity,
                "production_quantity": row.production_quantity,
                "order_batch_code": row.order_batch_code,
                "lifecycle_code": row.lifecycle_code,
            }
            for row in items
        ],
    }
    actor_openid = (
        plan.shipment_authorized_by.wx_openid
        if plan.shipment_authorized_by_id
        else ""
    )
    workitem = publish_workitem(
        domain="SALES",
        type="ORDER_PLAN_STOCK_OUTBOUND",
        biz_id=plan.plan_code,
        biz_ref=f"sales.OrderPlan:{plan.pk}",
        uniq_key=f"sales:order-plan:{plan.pk}:stock-outbound",
        title=(
            "订单二维码已打印，待一键出库"
            if qrcodes_ready
            else "全量调用退货库存，待打印新二维码并发货"
        ),
        summary=(
            f"{plan.merchant_organization.name}｜"
            f"{plan.brand.name}｜共{plan.total_quantity}台｜"
            + ("二维码已完成" if qrcodes_ready else "请先在网页打印中心完成二维码")
        ),
        link=(
            "/pages/sales/order_plan_list/order_plan_list"
            f"?focus_plan_id={plan.pk}"
        ),
        recipients_openids=stock_openids,
        need_action=True,
        action_mode="ANYONE_DO",
        payload={**payload, "qrcodes_ready": qrcodes_ready},
        actor_openid=actor_openid,
        mark_workitem_done_immediately=False,
    )
    # publish_workitem 会复用同一 uniq_key。旧订单部署迁移时可能暂时
    # 关闭过未授权的出库任务，因此重新打开时显式清理旧完成痕迹，
    # 并把本轮真正的发货确认人记录为任务发起人。
    workitem.status = "OPEN"
    workitem.actor_openid = actor_openid
    workitem.done_by_openid = ""
    workitem.done_at = None
    workitem.save(
        update_fields=(
            "status",
            "actor_openid",
            "done_by_openid",
            "done_at",
            "updated_at",
        )
    )
    return workitem


def publish_stock_outbound_preparation_task(*, plan):
    """全退货库存订单在创建时直接通知库管打印新二维码并发货。

    任务使用与最终出库相同的 uniq_key；全部二维码打印后，
    只更新这一条任务为“待发货”，不再制造第二条任务。
    """

    _ensure_shipment_authorized(plan)
    if plan.status not in ELIGIBLE_STATUSES:
        return None
    return _publish_stock_outbound_task(plan, qrcodes_ready=False)


def publish_stock_outbound_task_if_ready(plan):
    """销售已授权且全部二维码已打印时，发布或重新打开库管出库任务。"""
    _ensure_shipment_authorized(plan)
    if plan.status not in ELIGIBLE_STATUSES:
        return False
    if plan.items.filter(qrcode_printed_at__isnull=True).exists():
        return False
    _publish_stock_outbound_task(plan, qrcodes_ready=True)
    return True


@transaction.atomic
def generate_item_qrcodes(*, item_id):
    item = _get_item(item_id, lock=True)
    devices = []
    if item.production_quantity > 0:
        _validate_supplier_batch_coverage(item)
        batch, created = ProductionBatch.objects.select_for_update().get_or_create(
            batch_no=item.order_batch_code,
            defaults={
                "product_model": item.product_model,
                "bom": item.bom_confirmation.confirmed_bom,
                "manufacturer": item.plan.factory_organization,
                "production_date": timezone.localdate(),
                "quantity": item.production_quantity,
            },
        )
        if not created:
            expected = (
                item.product_model_id,
                item.bom_confirmation.confirmed_bom_id,
                item.plan.factory_organization_id,
                item.production_quantity,
            )
            actual = (
                batch.product_model_id,
                batch.bom_id,
                batch.manufacturer_id,
                batch.quantity,
            )
            if actual != expected:
                raise OrderQRCodePrintError(
                    "现有生产批次与订单新生产数量不一致，请停止打印并检查数据。"
                )

        devices = list(batch.devices.select_related("qrcode").order_by("id"))
        if len(devices) > item.production_quantity:
            raise OrderQRCodePrintError("该生产批次的设备数超过新生产数量。")
        while len(devices) < item.production_quantity:
            device = LifecycleService.create_device(
                product_model=item.product_model,
                operator=item.plan.created_by,
                organization=item.plan.factory_organization,
                batch=batch,
            )
            devices.append(device)

        devices = list(batch.devices.select_related("qrcode").order_by("id"))
        if any(not hasattr(device, "qrcode") for device in devices):
            raise OrderQRCodePrintError("生产批次中存在未绑定二维码的设备。")

    reissue_qrcodes = ensure_reissue_qrcodes(
        order_plan_item=item,
        actor=item.plan.created_by,
    )
    if len(devices) + len(reissue_qrcodes) != item.quantity:
        raise OrderQRCodePrintError("新生产设备与退货库存设备合计数量不等于订单需求。")

    if item.qrcode_generated_at is None:
        item.qrcode_generated_at = timezone.now()
        item.save(update_fields=("qrcode_generated_at", "updated_at"))

    return {
        "item": _item_payload(item),
        "qrcodes": [
            {"device_sn": device.sn, "code": device.qrcode.code}
            for device in devices
        ] + [
            {
                "device_sn": qrcode.device.sn,
                "code": qrcode.code,
                "source": "RETURN_INVENTORY",
            }
            for qrcode in reissue_qrcodes
        ],
    }


@transaction.atomic
def confirm_item_printed(*, item_id):
    candidate = _get_item(item_id)
    plan = OrderPlan.objects.select_for_update().select_related(
        "merchant_organization", "brand", "factory_organization"
    ).get(pk=candidate.plan_id)

    # 固定按“订单 → 明细”的顺序加锁，避免多个型号同时确认时重复创建任务。
    locked_items = list(
        plan.items.select_for_update()
        .select_related(
            "category", "product_model", "bom_confirmation",
            "bom_confirmation__confirmed_bom",
        )
        .order_by("id")
    )
    item = next((row for row in locked_items if row.pk == item_id), None)
    if not item:
        raise OrderQRCodePrintError("订单产品明细不存在。")
    if item.qrcode_printed_at:
        raise OrderQRCodePrintError("该产品型号的二维码已经打印完成。")
    if plan.status not in ELIGIBLE_STATUSES:
        raise OrderQRCodePrintError("该订单当前不在二维码打印阶段。")
    _ensure_shipment_authorized(plan)
    if not item.qrcode_generated_at:
        raise OrderQRCodePrintError("请先生成该产品型号的全部二维码。")
    production_count = 0
    if item.production_quantity > 0:
        batch = ProductionBatch.objects.filter(batch_no=item.order_batch_code).first()
        if not batch:
            raise OrderQRCodePrintError("新生产设备二维码尚未生成。")
        production_count = batch.devices.count()
    reissue_count = item.return_inventory_allocations.filter(
        replacement_qr__isnull=False
    ).count()
    if (
        production_count != item.production_quantity
        or reissue_count != item.return_inventory_quantity
        or production_count + reissue_count != item.quantity
    ):
        raise OrderQRCodePrintError("二维码数量尚未达到订单总需求数量。")
    item.qrcode_printed_at = timezone.now()
    item.save(update_fields=("qrcode_printed_at", "updated_at"))
    remaining = plan.items.filter(qrcode_printed_at__isnull=True).count()
    stock_task_created = False
    if remaining == 0:
        stock_task_created = publish_stock_outbound_task_if_ready(plan)
    return {
        "item_id": item.pk,
        "order_plan_id": plan.pk,
        "remaining_items": remaining,
        "order_printed": remaining == 0,
        "stock_task_created": stock_task_created,
    }
