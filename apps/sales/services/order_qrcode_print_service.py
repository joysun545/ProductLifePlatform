from django.db import transaction
from django.utils import timezone

from account.models import UserOrganization
from lifecycle.services.lifecycle_service import LifecycleService
from manufacturing.models import ProductionBatch
from sales.models import OrderPlan, OrderPlanItem
from workflow.services.workflow_service import publish_workitem


class OrderQRCodePrintError(Exception):
    pass


ELIGIBLE_STATUSES = {
    OrderPlan.Status.PRODUCTION_COMPLETED,
}


def _item_payload(item):
    return {
        "id": item.pk,
        "category_name": item.category.name,
        "product_model_code": item.product_model.code,
        "product_model_name": item.product_model.name,
        "quantity": item.quantity,
        "order_batch_code": item.order_batch_code,
        "generated": item.qrcode_generated_at is not None,
        "printed": item.qrcode_printed_at is not None,
    }


def get_print_center_data():
    plans = (
        OrderPlan.objects.select_related(
            "merchant_organization", "created_by", "brand"
        )
        .filter(status__in=ELIGIBLE_STATUSES, items__qrcode_printed_at__isnull=True)
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
    if item.qrcode_printed_at:
        raise OrderQRCodePrintError("该产品型号的二维码已经打印完成。")
    if not hasattr(item, "bom_confirmation"):
        raise OrderQRCodePrintError("该产品型号尚未确认生产BOM。")
    return item


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


def _publish_stock_outbound_task(plan):
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
                "order_batch_code": row.order_batch_code,
                "lifecycle_code": row.lifecycle_code,
            }
            for row in items
        ],
    }
    publish_workitem(
        domain="SALES",
        type="ORDER_PLAN_STOCK_OUTBOUND",
        biz_id=plan.plan_code,
        biz_ref=f"sales.OrderPlan:{plan.pk}",
        uniq_key=f"sales:order-plan:{plan.pk}:stock-outbound",
        title="订单二维码已打印，待一键出库",
        summary=(
            f"{plan.merchant_organization.name}｜"
            f"{plan.brand.name}｜共{plan.total_quantity}台"
        ),
        link=(
            "/pages/sales/order_plan_list/order_plan_list"
            f"?focus_plan_id={plan.pk}"
        ),
        recipients_openids=stock_openids,
        need_action=True,
        action_mode="ANYONE_DO",
        payload=payload,
        actor_openid="",
        mark_workitem_done_immediately=False,
    )


@transaction.atomic
def generate_item_qrcodes(*, item_id):
    item = _get_item(item_id, lock=True)
    batch, created = ProductionBatch.objects.select_for_update().get_or_create(
        batch_no=item.order_batch_code,
        defaults={
            "product_model": item.product_model,
            "bom": item.bom_confirmation.confirmed_bom,
            "manufacturer": item.plan.factory_organization,
            "production_date": timezone.localdate(),
            "quantity": item.quantity,
        },
    )
    if not created:
        expected = (
            item.product_model_id,
            item.bom_confirmation.confirmed_bom_id,
            item.plan.factory_organization_id,
            item.quantity,
        )
        actual = (
            batch.product_model_id,
            batch.bom_id,
            batch.manufacturer_id,
            batch.quantity,
        )
        if actual != expected:
            raise OrderQRCodePrintError("现有生产批次与订单产品明细不一致，请停止打印并检查数据。")

    devices = list(batch.devices.select_related("qrcode").order_by("id"))
    if len(devices) > item.quantity:
        raise OrderQRCodePrintError("该生产批次的设备数超过订单数量，请停止打印并检查数据。")
    while len(devices) < item.quantity:
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

    if item.qrcode_generated_at is None:
        item.qrcode_generated_at = timezone.now()
        item.save(update_fields=("qrcode_generated_at", "updated_at"))

    return {
        "item": _item_payload(item),
        "qrcodes": [
            {"device_sn": device.sn, "code": device.qrcode.code}
            for device in devices
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
    if not item.qrcode_generated_at:
        raise OrderQRCodePrintError("请先生成该产品型号的全部二维码。")
    batch = ProductionBatch.objects.filter(batch_no=item.order_batch_code).first()
    if not batch or batch.devices.count() != item.quantity:
        raise OrderQRCodePrintError("二维码数量尚未达到订单计划数量。")
    item.qrcode_printed_at = timezone.now()
    item.save(update_fields=("qrcode_printed_at", "updated_at"))
    remaining = plan.items.filter(qrcode_printed_at__isnull=True).count()
    if remaining == 0:
        _publish_stock_outbound_task(plan)
    return {
        "item_id": item.pk,
        "order_plan_id": plan.pk,
        "remaining_items": remaining,
        "order_printed": remaining == 0,
        "stock_task_created": remaining == 0,
    }
