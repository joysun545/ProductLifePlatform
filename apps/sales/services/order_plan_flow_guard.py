"""V2 order-flow display helpers."""

from sales.models import OrderPlan, OrderPlanShipment


def material_flow_status_name(plan, material_plan=None):
    """Return the V2 parent-order status shown to every participant."""

    if plan.status == OrderPlan.Status.CANCELLED:
        return "已取消"
    if plan.accounted_at:
        return "订单已入账"
    if plan.status == OrderPlan.Status.RECEIVED:
        return "商家已入库，待销售助理入账"
    if plan.status == OrderPlan.Status.SHIPPED:
        return "已分批发货，待商家入库"

    material_status = getattr(material_plan, "status", "")
    if (
        material_status == "PRODUCTION_COMPLETED"
        and getattr(material_plan, "pk", None)
        and OrderPlanShipment.objects.filter(
            order_plan=plan,
            status__in=(
                OrderPlanShipment.Status.SHIPPED,
                OrderPlanShipment.Status.RECEIVED,
            ),
        ).exists()
    ):
        # The parent compatibility status becomes SHIPPED only after every
        # source quantity has left the factory.  During a genuine partial
        # shipment, show the live V2 fact instead of incorrectly suggesting
        # that no batch has reached fulfillment yet.
        return "分批履约中"
    if getattr(plan, "pk", None):
        batches = OrderPlanShipment.objects.filter(order_plan=plan)
        if batches.filter(status=OrderPlanShipment.Status.OUTBOUNDED).exists():
            return "已有批次出库，待成品库管发货"
        if batches.filter(status=OrderPlanShipment.Status.AUTHORIZED, qrcode_printed_at__isnull=False).exists():
            return "已有批次贴包，待物流确认出库"
        if batches.filter(status=OrderPlanShipment.Status.AUTHORIZED, qrcode_printed_at__isnull=True).exists():
            return "已有批次获准发货，待打印贴包"
    if (
        material_status == "WAITING_SUPPLIER_DISPATCH"
        and getattr(material_plan, "pk", None)
    ):
        # The database parent remains in its compatibility state until the
        # material keeper explicitly releases production.  Derive the
        # intermediate human label from real delivery rows so a dispatched
        # task never continues to say “待供应配送推送”, and a confirmed task
        # never looks as though it still needs supplier action.
        delivery_statuses = {
            delivery.status for delivery in material_plan.supply_deliveries.all()
        }
        if "QUEUED" in delivery_statuses:
            return "待供应配送推送"
        if "DISPATCHED" in delivery_statuses:
            return "供应商配送确认中"
        if delivery_statuses:
            return "待物料入库"

    material_labels = {
        "WAITING_WAREHOUSE_REVIEW": "待物料库管核对",
        "WAITING_SUPPLIER_DISPATCH": "待供应配送推送",
        "WAITING_RECEIPT": "待物料入库",
        "READY_FOR_PRODUCTION": "物料齐套待生产",
        "PRODUCTION_IN_PROGRESS": "分批生产中",
        "PRODUCTION_COMPLETED": "生产已完成，待分批打码发货",
        "CANCELLED": "已取消",
    }
    return material_labels.get(material_status) or "物料库存订单处理中"
