from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from lifecycle.models import (
    FactoryReturnInventoryItem,
    FactoryReturnProcess,
    GoodsTransfer,
    GoodsTransferItem,
)
from quality_trace.models import (
    QualityRecallCampaign,
    QualityRecallDevice,
    QualityRecallDeviceEvent,
    QualityRecallReturnLeg,
    QualityTraceEvent,
)
from workflow.models import UserTask, WorkItem

from .recall_service import QualityRecallError
from .recall_task_service import complete_recall_scope_tasks_if_ready


def _authorized_recipient(row, user):
    openid = str(user.wx_openid or "")
    groups = (row.responsibility_snapshot or {}).get("groups") or []
    return any(
        recipient.get("openid") == openid
        for group in groups
        for recipient in group.get("recipients") or []
    )


def _organization_snapshot(organization):
    return {
        "id": organization.pk,
        "name": organization.name,
        "type": organization.organization_type.code,
    }


def _status_from_transfer(transfer):
    if transfer.status == GoodsTransfer.Status.SCANNING:
        return QualityRecallReturnLeg.Status.PREPARING
    if transfer.status in {
        GoodsTransfer.Status.PENDING_RECEIPT,
        GoodsTransfer.Status.PENDING_FACTORY_PICKUP,
    }:
        return QualityRecallReturnLeg.Status.IN_TRANSIT
    if transfer.status == GoodsTransfer.Status.PENDING_FACTORY_POSTING:
        return QualityRecallReturnLeg.Status.FACTORY_PICKED_UP
    if transfer.status == GoodsTransfer.Status.CANCELLED:
        return QualityRecallReturnLeg.Status.CANCELLED
    if transfer.status == GoodsTransfer.Status.COMPLETED:
        if transfer.to_organization.organization_type.code == "OWNER":
            return QualityRecallReturnLeg.Status.FACTORY_ISOLATED
        return QualityRecallReturnLeg.Status.UPSTREAM_RECEIVED
    return QualityRecallReturnLeg.Status.PREPARING


def _complete_current_recall_tasks(row, user):
    now = timezone.now()
    workitem_ids = (row.responsibility_snapshot or {}).get("workitem_ids") or []
    workitems = WorkItem.objects.select_for_update().filter(
        pk__in=workitem_ids,
        status="OPEN",
        biz_ref__endswith=f":Device:{row.device_id}",
    )
    for workitem in workitems:
        workitem.status = "DONE"
        workitem.done_by_openid = user.wx_openid or ""
        workitem.done_at = now
        workitem.last_activity_at = now
        workitem.save(update_fields=(
            "status", "done_by_openid", "done_at", "last_activity_at", "updated_at",
        ))
        UserTask.objects.filter(workitem=workitem).exclude(state="DONE").update(
            state="DONE",
            done_at=now,
            done_reason="RECALL_RETURN_STARTED",
            cursor_time=now,
            updated_at=now,
        )


@transaction.atomic
def bind_recall_return_transfer(*, user, recall_device_id, transfer_id):
    row = (
        QualityRecallDevice.objects.select_for_update()
        .select_related("campaign__quality_case", "device")
        .filter(pk=recall_device_id, campaign__status=QualityRecallCampaign.Status.ACTIVE)
        .first()
    )
    if not row:
        raise QualityRecallError("召回设备不存在或召回批次未发布。")
    if not _authorized_recipient(row, user):
        raise QualityRecallError("当前账号不是该设备召回任务的处理人。")
    transfer = (
        GoodsTransfer.objects.select_for_update()
        .select_related(
            "from_organization__organization_type",
            "to_organization__organization_type",
        )
        .filter(pk=transfer_id, initiated_by=user, flow_type=GoodsTransfer.FlowType.RETURN)
        .first()
    )
    if not transfer:
        raise QualityRecallError("退货流转单不存在、不是退货类型或不属于当前账号。")
    item = GoodsTransferItem.objects.select_for_update().filter(
        transfer=transfer, device_id=row.device_id,
    ).first()
    if not item:
        raise QualityRecallError("退货流转单中没有该召回设备。")
    existing = QualityRecallReturnLeg.objects.filter(goods_transfer=transfer).first()
    if existing:
        if existing.recall_device_id != row.pk:
            raise QualityRecallError("该退货流转单已经绑定其他召回设备。")
        return existing, False
    sequence = (
        row.return_legs.aggregate(value=Max("sequence_no"))["value"] or 0
    ) + 1
    now = timezone.now()
    leg = QualityRecallReturnLeg.objects.create(
        campaign=row.campaign,
        recall_device=row,
        sequence_no=sequence,
        goods_transfer=transfer,
        goods_transfer_item=item,
        from_organization_snapshot=_organization_snapshot(transfer.from_organization),
        to_organization_snapshot=_organization_snapshot(transfer.to_organization),
        status=_status_from_transfer(transfer),
        bound_by=user,
        status_updated_at=now,
    )
    old_status = row.status
    row.status = QualityRecallDevice.Status.RETURN_IN_PROGRESS
    row.status_updated_at = now
    row.save(update_fields=("status", "status_updated_at"))
    _complete_current_recall_tasks(row, user)
    complete_recall_scope_tasks_if_ready(
        recall_device=row,
        user=user,
        audience_type="MERCHANT",
    )
    QualityRecallDeviceEvent.objects.create(
        campaign=row.campaign,
        recall_device=row,
        from_status=old_status,
        to_status=QualityRecallDevice.Status.RETURN_IN_PROGRESS,
        reason="召回设备已绑定现有退货流转单",
        actor=user,
        actor_role_snapshot=(user.organization_identity.role.code if user.organization_identity else ""),
        payload={
            "return_leg_id": leg.pk,
            "sequence_no": leg.sequence_no,
            "goods_transfer_id": transfer.pk,
            "flow_no": transfer.flow_no,
        },
    )
    return leg, True


@transaction.atomic
def synchronize_recall_return_leg(*, leg_id):
    leg = (
        QualityRecallReturnLeg.objects.select_for_update()
        .select_related(
            "recall_device__campaign__quality_case",
            "goods_transfer__to_organization__organization_type",
        )
        .get(pk=leg_id)
    )
    transfer = leg.goods_transfer
    target_status = _status_from_transfer(transfer)
    isolation = {}
    if transfer.to_organization.organization_type.code == "OWNER":
        process = FactoryReturnProcess.objects.filter(transfer=transfer).first()
        inventory = FactoryReturnInventoryItem.objects.filter(
            transfer_item=leg.goods_transfer_item,
        ).first()
        if process:
            isolation["factory_return_process_id"] = process.pk
            isolation["factory_return_phase"] = process.phase
            if process.phase == FactoryReturnProcess.Phase.WAITING_POSTING:
                target_status = QualityRecallReturnLeg.Status.FACTORY_PICKED_UP
        if inventory:
            isolation.update({
                "factory_return_inventory_item_id": inventory.pk,
                "stock_status": inventory.stock_status,
                "classified_at": (
                    inventory.classified_at.isoformat() if inventory.classified_at else ""
                ),
            })
            if (
                process
                and process.phase == FactoryReturnProcess.Phase.POSTED
                and inventory.stock_status
                != FactoryReturnInventoryItem.StockStatus.WAITING_POSTING
            ):
                target_status = QualityRecallReturnLeg.Status.FACTORY_ISOLATED
    changed = target_status != leg.status or isolation != leg.isolation_snapshot
    if changed:
        leg.status = target_status
        leg.isolation_snapshot = isolation
        leg.status_updated_at = timezone.now()
        leg.save(update_fields=("status", "isolation_snapshot", "status_updated_at"))
    row = leg.recall_device
    if target_status == QualityRecallReturnLeg.Status.FACTORY_ISOLATED:
        old_status = row.status
        row.status = QualityRecallDevice.Status.RECEIVED
        row.status_updated_at = timezone.now()
        row.save(update_fields=("status", "status_updated_at"))
        if old_status != row.status:
            QualityRecallDeviceEvent.objects.create(
                campaign=row.campaign,
                recall_device=row,
                from_status=old_status,
                to_status=QualityRecallDevice.Status.RECEIVED,
                reason="厂家已完成召回设备分类隔离入库",
                payload={"return_leg_id": leg.pk, **isolation},
            )
    QualityTraceEvent.objects.create(
        quality_case=row.campaign.quality_case,
        alert=row.campaign.quality_case.source_alert,
        event_type="RECALL_RETURN_LEG_SYNCHRONIZED",
        payload={
            "recall_campaign_id": row.campaign_id,
            "recall_device_id": row.pk,
            "return_leg_id": leg.pk,
            "goods_transfer_id": transfer.pk,
            "return_leg_status": target_status,
            "isolation": isolation,
        },
    )
    return leg
