from django.db import transaction
from django.utils import timezone

from account.models import UserOrganization
from lifecycle.models import (
    CustomerDeviceBinding, DeviceState, LifecycleEvent, OrganizationDevice,
)
from quality_trace.models import (
    QualityRecallCampaign, QualityRecallDevice,
    QualityRecallDispositionSupplierConfirmation,
    QualityRecallInventoryBatchConfirmation, QualityRecallPostDisposition,
    QualityRecallReusableInventory, QualityTraceEvent,
)
from workflow.models import WorkItem
from workflow.services.workflow_service import publish_workitem

from .case_service import _identity
from .recall_service import QualityRecallError


TASK_PRODUCTION = "QUALITY_RECALL_DISPOSITION_PRODUCTION"
TASK_SUPPLIER = "QUALITY_RECALL_DISPOSITION_SUPPLIER"
TASK_MATCHING = "QUALITY_RECALL_DISPOSITION_MATCHING"
TASK_CHIEF = "QUALITY_RECALL_DISPOSITION_CHIEF_CONFIRM"
NOTICE_DISASSEMBLY = "QUALITY_RECALL_DISASSEMBLY_NOTICE"


def _chief_campaign(user, campaign_id):
    identity = _identity(user, {"factory_chief_engineer"})
    campaign = QualityRecallCampaign.objects.select_for_update().filter(
        pk=campaign_id, factory_organization=identity.organization,
        status=QualityRecallCampaign.Status.ACTIVE,
    ).first()
    if not campaign:
        raise QualityRecallError("有效召回批次不存在或不属于当前厂家。")
    if not QualityRecallInventoryBatchConfirmation.objects.filter(campaign=campaign).exists():
        raise QualityRecallError("销售助理尚未确认本批召回设备全部入库记账。")
    return identity, campaign


def _supplier_owner_identity(user):
    """Resolve supplier execution identity without the factory-only case gate."""
    identity = (
        UserOrganization.objects.select_related(
            "organization", "organization__organization_type", "role",
        ).filter(
            user=user,
            user__is_active=True,
            is_active=True,
            organization__is_active=True,
            organization__organization_type__code="SUPPLIER",
            role__is_active=True,
            role__code="supplier_owner",
        ).first()
    )
    if not identity or not user.wx_openid:
        raise QualityRecallError("当前账号没有供应商负责人确认权限。")
    return identity


def _role_openids(organization, roles):
    return list(UserOrganization.objects.filter(
        organization=organization, role__code__in=set(roles),
        role__is_active=True, is_active=True, user__is_active=True,
    ).exclude(user__wx_openid__isnull=True).exclude(
        user__wx_openid="",
    ).values_list("user__wx_openid", flat=True))


def _device_link(item):
    return ("/pages/quality_trace/recall_detail/recall_detail"
            f"?campaign_id={item.campaign_id}&recall_device_id={item.recall_device_id}")


def _publish_action(item, *, task_type, suffix, title, recipients, actor, payload=None):
    if not recipients:
        raise QualityRecallError(f"“{title}”没有可用的接收账号。")
    return publish_workitem(
        domain="AFTERSALE", type=task_type, biz_id=str(item.campaign_id),
        biz_ref=f"quality_trace.QualityRecallPostDisposition:{item.pk}",
        uniq_key=f"quality-recall:disposition:{item.pk}:{suffix}",
        title=title,
        summary=f"{item.recall_device.sn_snapshot}｜{item.get_method_display()}",
        link=_device_link(item), recipients_openids=recipients,
        need_action=True, action_mode="ANYONE_DO",
        payload={"campaign_id": item.campaign_id,
                 "recall_device_id": item.recall_device_id,
                 "disposition_id": item.pk, "method": item.method,
                 **(payload or {})},
        actor_openid=actor.wx_openid or "", mark_workitem_done_immediately=False,
    )


def _publish_notice(item, *, suffix, title, recipients, actor, payload=None):
    if not recipients:
        return None
    return publish_workitem(
        domain="AFTERSALE", type=NOTICE_DISASSEMBLY,
        biz_id=str(item.campaign_id),
        biz_ref=f"quality_trace.QualityRecallPostDisposition:{item.pk}",
        uniq_key=f"quality-recall:disposition:{item.pk}:{suffix}",
        title=title, summary=f"{item.recall_device.sn_snapshot}｜拆机完成",
        link=_device_link(item), recipients_openids=recipients,
        need_action=False, action_mode="EACH_ACK", payload=payload or {},
        actor_openid=actor.wx_openid or "", mark_workitem_done_immediately=True,
    )


def _close_task(item, suffix, actor, reason):
    now = timezone.now()
    workitem = WorkItem.objects.select_for_update().filter(
        uniq_key=f"quality-recall:disposition:{item.pk}:{suffix}",
    ).first()
    if not workitem:
        return
    workitem.status = "DONE"
    workitem.done_by_openid = actor.wx_openid or ""
    workitem.done_at = now
    workitem.last_activity_at = now
    workitem.save(update_fields=("status", "done_by_openid", "done_at",
                                 "last_activity_at", "updated_at"))
    workitem.user_tasks.exclude(state="DONE").update(
        state="DONE", need_action=False, done_at=now,
        done_reason=reason, cursor_time=now,
    )


def _bom_suppliers(item):
    return list(item.recall_device.device.batch.bom.items.select_related("supplier")
                .exclude(supplier=None)
                .values_list("supplier_id", "supplier__name").distinct())


def _complete_disassembly(item, user, note):
    now = timezone.now()
    item.status = QualityRecallPostDisposition.Status.COMPLETED
    item.execution_stage = QualityRecallPostDisposition.ExecutionStage.COMPLETED
    item.stock_result = QualityRecallPostDisposition.StockResult.DISASSEMBLED
    item.completion_note = note
    item.completed_by = user
    item.completed_at = now
    item.save()
    row = item.recall_device
    row.status = QualityRecallDevice.Status.DISPOSED
    row.status_updated_at = now
    row.save(update_fields=("status", "status_updated_at"))
    CustomerDeviceBinding.objects.filter(
        device=row.device, status=CustomerDeviceBinding.Status.ACTIVE,
    ).update(status=CustomerDeviceBinding.Status.INACTIVE, updated_at=now)
    event = LifecycleEvent.objects.create(
        device=row.device, event_type="LIFECYCLE_END", operator=user,
        organization=item.campaign.factory_organization,
        from_organization=item.campaign.factory_organization,
        to_organization=item.campaign.factory_organization,
        data={"source": "QUALITY_RECALL", "campaign_id": item.campaign_id,
              "disposition_id": item.pk, "method": item.method},
    )
    DeviceState.objects.update_or_create(
        device=row.device,
        defaults={"current_organization": item.campaign.factory_organization,
                  "status": "SCRAPPED", "activation_status": "NOT_ACTIVATED",
                  "install_status": "NOT_INSTALLED", "last_event": event,
                  "last_event_time": now},
    )
    _publish_notice(
        item, suffix="disassembly-matching-notice",
        title="召回设备拆机完成，请知晓三包件线下退回",
        recipients=_role_openids(item.campaign.factory_organization, {"factory_matching"}),
        actor=user,
    )
    for supplier_id, supplier_name in _bom_suppliers(item):
        _publish_notice(
            item, suffix=f"disassembly-supplier-{supplier_id}",
            title="召回设备已拆机，请知晓三包件线下退回",
            recipients=_role_openids(supplier_id, {"supplier_owner", "supplier_sales"}),
            actor=user,
            payload={"supplier_id": supplier_id, "supplier_name": supplier_name},
        )


@transaction.atomic
def start_recall_post_disposition(*, user, campaign_id, method,
                                  recall_device_ids=None, note=""):
    identity, campaign = _chief_campaign(user, campaign_id)
    method = str(method or "").upper()
    valid = {value for value, _label in QualityRecallPostDisposition.Method.choices}
    if method not in valid:
        raise QualityRecallError("请选择拆机、不换件维修或换件维修。")
    requested_ids = {int(value) for value in (recall_device_ids or [])}
    if not requested_ids:
        raise QualityRecallError("请至少选择一台待处置设备。")
    rows = list(campaign.devices.select_for_update().select_related(
        "device__batch__bom", "device__product_model",
    ).exclude(status__in=(QualityRecallDevice.Status.UNREACHABLE,
                          QualityRecallDevice.Status.EXEMPTED))
      .filter(pk__in=requested_ids).order_by("pk"))
    if {row.pk for row in rows} != requested_ids:
        raise QualityRecallError("所选设备不存在或不属于当前召回批次。")
    if any(row.status != QualityRecallDevice.Status.RECEIVED for row in rows):
        raise QualityRecallError("只有已完成返厂入库记账的设备才能分配处置方式。")
    if campaign.post_dispositions.filter(recall_device_id__in=requested_ids).exists():
        raise QualityRecallError("所选设备中已有设备完成处置方式分配，不能重复覆盖。")
    production_openids = _role_openids(campaign.factory_organization, {"factory_production"})
    if not production_openids:
        raise QualityRecallError("厂家缺少有效生产经理账号。")
    if method == QualityRecallPostDisposition.Method.REPAIR_WITH_PARTS:
        if not _role_openids(campaign.factory_organization, {"factory_matching"}):
            raise QualityRecallError("厂家缺少有效配套经理账号。")
        for row in rows:
            suppliers = list(row.device.batch.bom.items.exclude(supplier=None)
                             .values_list("supplier_id", "supplier__name").distinct())
            if not suppliers:
                raise QualityRecallError(f"设备{row.sn_snapshot}的BOM没有可通知供应商。")
            for supplier_id, supplier_name in suppliers:
                if not _role_openids(supplier_id, {"supplier_owner"}):
                    raise QualityRecallError(f"供应商“{supplier_name}”缺少有效负责人账号。")

    now = timezone.now()
    created = []
    for row in rows:
        stage = (QualityRecallPostDisposition.ExecutionStage.WAITING_SUPPLIER
                 if method == QualityRecallPostDisposition.Method.REPAIR_WITH_PARTS
                 else QualityRecallPostDisposition.ExecutionStage.WAITING_PRODUCTION)
        item = QualityRecallPostDisposition.objects.create(
            campaign=campaign, recall_device=row, method=method,
            status=QualityRecallPostDisposition.Status.IN_PROGRESS,
            execution_stage=stage, decision_note=str(note or "").strip(),
            decided_by=user, decided_at=now,
        )
        created.append(item)
        if method != QualityRecallPostDisposition.Method.REPAIR_WITH_PARTS:
            _publish_action(
                item, task_type=TASK_PRODUCTION, suffix="production",
                title=("召回设备待拆机" if method == "DISASSEMBLE"
                       else "召回设备待不换件维修"),
                recipients=production_openids, actor=user,
            )
        else:
            for supplier_id, supplier_name in _bom_suppliers(item):
                confirmation = QualityRecallDispositionSupplierConfirmation.objects.create(
                    disposition=item, supplier_id=supplier_id,
                    supplier_name_snapshot=supplier_name,
                )
                _publish_action(
                    item, task_type=TASK_SUPPLIER,
                    suffix=f"supplier-{supplier_id}",
                    title="召回设备换件维修待供应商确认",
                    recipients=_role_openids(supplier_id, {"supplier_owner", "supplier_sales"}),
                    actor=user,
                    payload={"supplier_confirmation_id": confirmation.pk,
                             "supplier_id": supplier_id},
                )
            _publish_action(
                item, task_type=TASK_MATCHING, suffix="matching",
                title="召回换件维修待配套处理（等待供应商负责人确认）",
                recipients=_role_openids(
                    item.campaign.factory_organization, {"factory_matching"},
                ), actor=user, payload={"blocked_by_supplier": True},
            )
    QualityTraceEvent.objects.create(
        quality_case=campaign.quality_case, alert=campaign.quality_case.source_alert,
        event_type="RECALL_POST_DISPOSITION_STARTED", actor=user,
        actor_role_snapshot=identity.role.code,
        payload={"recall_campaign_id": campaign.pk, "method": method,
                 "recall_device_ids": sorted(requested_ids),
                 "disposition_ids": [row.pk for row in created]},
    )
    return created


@transaction.atomic
def confirm_recall_disposition_supplier(*, user, confirmation_id, note=""):
    identity = _supplier_owner_identity(user)
    confirmation = (QualityRecallDispositionSupplierConfirmation.objects
        .select_for_update().select_related(
            "disposition__campaign__factory_organization", "disposition__recall_device",
        ).filter(pk=confirmation_id, supplier=identity.organization).first())
    if not confirmation:
        raise QualityRecallError("供应商确认任务不存在或不属于当前账号。")
    if confirmation.confirmed_at:
        return confirmation
    confirmation.confirmed_by = user
    confirmation.confirmed_at = timezone.now()
    confirmation.note = str(note or "").strip()[:1000]
    confirmation.save(update_fields=("confirmed_by", "confirmed_at", "note"))
    item = confirmation.disposition
    _close_task(item, f"supplier-{identity.organization_id}", user, "SUPPLIER_CONFIRMED")
    if not item.supplier_confirmations.filter(confirmed_at=None).exists():
        item.execution_stage = QualityRecallPostDisposition.ExecutionStage.WAITING_MATCHING
        item.save(update_fields=("execution_stage",))
        _publish_action(
            item, task_type=TASK_MATCHING, suffix="matching",
            title="召回设备换件维修待配套确认",
            recipients=_role_openids(item.campaign.factory_organization, {"factory_matching"}),
            actor=user,
        )
    return confirmation


@transaction.atomic
def confirm_recall_disposition_matching(*, user, disposition_id,
                                        replacement_parts=None, note=""):
    identity = _identity(user, {"factory_matching"})
    item = QualityRecallPostDisposition.objects.select_for_update().select_related(
        "campaign__factory_organization", "recall_device",
    ).filter(pk=disposition_id,
             campaign__factory_organization=identity.organization).first()
    if not item or item.method != QualityRecallPostDisposition.Method.REPAIR_WITH_PARTS:
        raise QualityRecallError("换件维修配套任务不存在。")
    if item.execution_stage != QualityRecallPostDisposition.ExecutionStage.WAITING_MATCHING:
        raise QualityRecallError("尚未完成全部供应商负责人确认，不能进行配套确认。")
    parts = replacement_parts if isinstance(replacement_parts, list) else []
    if not parts:
        raise QualityRecallError("换件维修必须登记更换物料。")
    item.replacement_parts = parts
    item.matching_completed_by = user
    item.matching_completed_at = timezone.now()
    item.execution_stage = QualityRecallPostDisposition.ExecutionStage.WAITING_PRODUCTION_COMPLETION
    item.execution_payload = {**(item.execution_payload or {}),
                              "matching_note": str(note or "").strip()}
    item.save()
    _close_task(item, "matching", user, "MATCHING_CONFIRMED")
    _publish_action(
        item, task_type=TASK_PRODUCTION, suffix="production",
        title="召回设备待完成换件维修",
        recipients=_role_openids(item.campaign.factory_organization, {"factory_production"}),
        actor=user,
    )
    return item


@transaction.atomic
def complete_recall_disposition_production(*, user, disposition_id,
                                           completion_note=""):
    identity = _identity(user, {"factory_production"})
    item = QualityRecallPostDisposition.objects.select_for_update().select_related(
        "campaign__factory_organization", "campaign__quality_case__source_alert",
        "recall_device__device__batch__bom",
    ).filter(pk=disposition_id,
             campaign__factory_organization=identity.organization).first()
    if not item:
        raise QualityRecallError("召回生产处置任务不存在。")
    valid_stage = (QualityRecallPostDisposition.ExecutionStage.WAITING_PRODUCTION_COMPLETION
                   if item.method == QualityRecallPostDisposition.Method.REPAIR_WITH_PARTS
                   else QualityRecallPostDisposition.ExecutionStage.WAITING_PRODUCTION)
    if item.execution_stage != valid_stage:
        raise QualityRecallError("当前前置环节尚未完成，不能确认生产处置。")
    note = str(completion_note or "").strip()
    if not note:
        raise QualityRecallError("请填写生产处置完成说明。")
    item.production_completed_by = user
    item.production_completed_at = timezone.now()
    item.execution_payload = {**(item.execution_payload or {}), "production_note": note}
    _close_task(item, "production", user, "PRODUCTION_COMPLETED")
    if item.method == QualityRecallPostDisposition.Method.DISASSEMBLE:
        _complete_disassembly(item, user, note)
    else:
        item.execution_stage = QualityRecallPostDisposition.ExecutionStage.WAITING_CHIEF_CONFIRMATION
        item.save()
        _publish_action(
            item, task_type=TASK_CHIEF, suffix="chief",
            title="召回维修完成，待总工程师确认处置",
            recipients=_role_openids(item.campaign.factory_organization,
                                     {"factory_chief_engineer"}),
            actor=user,
        )
    return item


@transaction.atomic
def complete_recall_device_disposition(*, user, disposition_id,
                                       completion_note="", replacement_parts=None):
    identity = _identity(user, {"factory_chief_engineer"})
    item = QualityRecallPostDisposition.objects.select_for_update().select_related(
        "campaign__factory_organization", "campaign__quality_case__source_alert",
        "recall_device__device__batch__bom", "recall_device__device__product_model",
    ).filter(pk=disposition_id,
             campaign__factory_organization=identity.organization).first()
    if not item:
        raise QualityRecallError("召回后处置记录不存在。")
    if item.status == QualityRecallPostDisposition.Status.COMPLETED:
        return item
    if item.method == QualityRecallPostDisposition.Method.DISASSEMBLE:
        raise QualityRecallError("拆机由生产经理完成后自动收口，无需总工程师重复确认。")
    if item.execution_stage != QualityRecallPostDisposition.ExecutionStage.WAITING_CHIEF_CONFIRMATION:
        raise QualityRecallError("生产维修尚未完成，不能确认设备处置完成。")
    note = str(completion_note or "").strip()
    if not note:
        raise QualityRecallError("请填写处置完成说明。")
    now = timezone.now()
    item.status = QualityRecallPostDisposition.Status.COMPLETED
    item.execution_stage = QualityRecallPostDisposition.ExecutionStage.COMPLETED
    item.stock_result = QualityRecallPostDisposition.StockResult.READY_FOR_OUTBOUND
    item.completion_note = note
    item.completed_by = user
    item.completed_at = now
    item.save()
    row = item.recall_device
    row.status = QualityRecallDevice.Status.DISPOSED
    row.status_updated_at = now
    row.save(update_fields=("status", "status_updated_at"))
    device = row.device
    QualityRecallReusableInventory.objects.get_or_create(
        disposition=item,
        defaults={"factory_organization": item.campaign.factory_organization,
                  "device": device, "source_product_model": device.product_model,
                  "source_production_batch": device.batch,
                  "source_bom": device.batch.bom,
                  "stock_status": QualityRecallReusableInventory.Status.PENDING_REISSUE,
                  "available_at": now},
    )
    CustomerDeviceBinding.objects.filter(
        device=device, status=CustomerDeviceBinding.Status.ACTIVE,
    ).update(status=CustomerDeviceBinding.Status.INACTIVE, updated_at=now)
    event = LifecycleEvent.objects.create(
        device=device, event_type="REPAIR", operator=user,
        organization=item.campaign.factory_organization,
        from_organization=item.campaign.factory_organization,
        to_organization=item.campaign.factory_organization,
        data={"source": "QUALITY_RECALL", "campaign_id": item.campaign_id,
              "disposition_id": item.pk, "method": item.method,
              "stock_status": "PENDING_REISSUE"},
    )
    OrganizationDevice.objects.update_or_create(
        device=device,
        defaults={"organization": item.campaign.factory_organization,
                  "status": "IN_STOCK", "received_at": now,
                  "delivered_at": None, "last_event": event},
    )
    DeviceState.objects.update_or_create(
        device=device,
        defaults={"current_organization": item.campaign.factory_organization,
                  "status": "IN_STOCK", "activation_status": "NOT_ACTIVATED",
                  "install_status": "NOT_INSTALLED", "last_event": event,
                  "last_event_time": now},
    )
    _close_task(item, "chief", user, "CHIEF_CONFIRMED")
    QualityTraceEvent.objects.create(
        quality_case=item.campaign.quality_case,
        alert=item.campaign.quality_case.source_alert,
        event_type="RECALL_DEVICE_POST_DISPOSITION_COMPLETED",
        actor=user, actor_role_snapshot=identity.role.code,
        payload={"recall_campaign_id": item.campaign_id,
                 "recall_device_id": row.pk, "method": item.method,
                 "stock_result": item.stock_result,
                 "replacement_parts": item.replacement_parts},
    )
    return item


@transaction.atomic
def sync_recall_disposition_execution_tasks(*, campaign_id=None):
    """Idempotently backfill execution stages/tasks for pre-R13.9.5 records."""
    rows = QualityRecallPostDisposition.objects.select_for_update().select_related(
        "campaign__factory_organization", "recall_device__device__batch__bom",
        "decided_by",
    ).filter(campaign__status=QualityRecallCampaign.Status.ACTIVE)
    if campaign_id:
        rows = rows.filter(campaign_id=campaign_id)
    results = []
    for item in rows:
        if item.status == QualityRecallPostDisposition.Status.COMPLETED:
            if item.execution_stage != QualityRecallPostDisposition.ExecutionStage.COMPLETED:
                item.execution_stage = QualityRecallPostDisposition.ExecutionStage.COMPLETED
                item.save(update_fields=("execution_stage",))
            continue
        actor = item.decided_by
        if item.method == QualityRecallPostDisposition.Method.REPAIR_WITH_PARTS:
            item.execution_stage = QualityRecallPostDisposition.ExecutionStage.WAITING_SUPPLIER
            item.save(update_fields=("execution_stage",))
            for supplier_id, supplier_name in _bom_suppliers(item):
                confirmation, _created = (
                    QualityRecallDispositionSupplierConfirmation.objects.get_or_create(
                        disposition=item, supplier_id=supplier_id,
                        defaults={"supplier_name_snapshot": supplier_name},
                    )
                )
                if not confirmation.confirmed_at:
                    _publish_action(
                        item, task_type=TASK_SUPPLIER,
                        suffix=f"supplier-{supplier_id}",
                        title="召回设备换件维修待供应商确认",
                        recipients=_role_openids(
                            supplier_id, {"supplier_owner", "supplier_sales"},
                        ), actor=actor,
                        payload={"supplier_confirmation_id": confirmation.pk,
                                 "supplier_id": supplier_id},
                    )
            _publish_action(
                item, task_type=TASK_MATCHING, suffix="matching",
                title="召回换件维修待配套处理（等待供应商负责人确认）",
                recipients=_role_openids(
                    item.campaign.factory_organization, {"factory_matching"},
                ), actor=actor, payload={"blocked_by_supplier": True},
            )
        else:
            item.execution_stage = QualityRecallPostDisposition.ExecutionStage.WAITING_PRODUCTION
            item.save(update_fields=("execution_stage",))
            _publish_action(
                item, task_type=TASK_PRODUCTION, suffix="production",
                title=("召回设备待拆机" if item.method == "DISASSEMBLE"
                       else "召回设备待不换件维修"),
                recipients=_role_openids(
                    item.campaign.factory_organization, {"factory_production"},
                ), actor=actor,
            )
        results.append({"disposition_id": item.pk,
                        "execution_stage": item.execution_stage})
    return results
