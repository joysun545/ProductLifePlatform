"""Batch recall custody operations; no GoodsTransfer or ordinary return inventory."""
import hashlib
import json
from collections import OrderedDict

from django.db import transaction
from django.utils import timezone

from account.services.auth_service import get_active_identity
from account.services.role_delegation_service import (
    has_open_action_task_copy,
    identity_has_role,
)
from quality_trace.models import (
    QualityRecallBatchOperation, QualityRecallCampaign, QualityRecallDevice,
    QualityRecallIsolationReceipt, QualityRecallTransportLeg, QualityTraceEvent,
)
from workflow.models import UserTask, WorkItem
from workflow.services.workflow_service import publish_workitem

from .recall_collaboration_service import (
    FACTORY_RECALL_COLLABORATION_ROLES, close_factory_recall_progress,
    close_merchant_return_task, organization_recipients,
)
from .recall_progress_service import calculate_recall_progress
from .recall_service import QualityRecallError
from .recall_task_service import complete_recall_scope_tasks_if_ready
from .recall_transport_service import (
    MERCHANT_ROLES, _current_custodian, _evidence, _snapshot, _upstream_target,
    create_next_recall_transport_leg, dispatch_recall_transport_leg,
    isolate_recalled_device, publish_batch_posting_confirmation,
    receive_recall_transport_leg,
)

TASK_TYPES = {
    "DISPATCH": "QUALITY_RECALL_BATCH_RETURN",
    "RECEIVE": "QUALITY_RECALL_BATCH_RECEIPT",
    "ISOLATE": "QUALITY_RECALL_BATCH_INVENTORY_POSTING",
}
ACTION_LABELS = {"DISPATCH": "一键确认退回", "RECEIVE": "一键确认接收", "ISOLATE": "一键隔离入账"}
MAX_BATCH_SIZE = 1000


def _identity(user):
    identity = get_active_identity(user)
    if not identity or not identity.organization.is_active or not user.wx_openid:
        raise QualityRecallError("当前账号没有有效的召回处理身份。")
    return identity


def _allowed(identity):
    kind = identity.organization.organization_type.code
    return (
        kind == "MERCHANT" and identity_has_role(identity, MERCHANT_ROLES)
    ) or (
        kind == "OWNER" and identity_has_role(identity, {"factory_logistics", "factory_sales_assistant"})
    )


def recall_batch_rows(campaign):
    return campaign.devices.select_related(
        "campaign__factory_organization", "customer_handover__merchant_organization",
        "isolation_receipt",
    ).prefetch_related(
        "transport_legs__from_organization__organization_type",
        "transport_legs__to_organization__organization_type",
    ).order_by("pk")


def _candidate(row, identity):
    if row.status in {"DISPOSED", "EXEMPTED", "UNREACHABLE"} or getattr(row, "isolation_receipt", None):
        return None
    legs = [leg for leg in row.transport_legs.all() if leg.status != "CANCELLED"]
    leg = legs[-1] if legs else None
    org_id = identity.organization_id
    if leg and leg.status == "IN_TRANSIT":
        allowed = identity_has_role(identity, {"factory_logistics"}) if leg.is_factory_destination else identity_has_role(identity, MERCHANT_ROLES)
        if leg.to_organization_id != org_id or not allowed:
            return None
        shipment = (leg.relation_snapshot or {}).get("dispatch_batch_operation_id")
        if not shipment:
            shipment = hashlib.sha256(
                f"{leg.from_organization_id}|{leg.logistics_provider}|{leg.tracking_no}".encode()
            ).hexdigest()[:16]
        return {
            "key": f"receive:{leg.from_organization_id}:{shipment}",
            "action": "RECEIVE", "title": f"接收{leg.from_organization.name}退回的设备",
            "from_organization": _snapshot(leg.from_organization),
            "to_organization": _snapshot(leg.to_organization),
            "logistics_provider": leg.logistics_provider, "tracking_no": leg.tracking_no,
            "leg_id": leg.pk,
        }
    if leg and leg.status == "RECEIVED" and leg.is_factory_destination:
        if org_id != row.campaign.factory_organization_id or not identity_has_role(identity, {"factory_sales_assistant"}):
            return None
        return {
            "key": f"isolate:{org_id}", "action": "ISOLATE", "title": "厂家已收货，待隔离入账",
            "from_organization": None, "to_organization": _snapshot(leg.to_organization),
            "leg_id": leg.pk,
        }
    if not identity_has_role(identity, MERCHANT_ROLES):
        return None
    if leg and leg.status == "PREPARING":
        if leg.from_organization_id != org_id:
            return None
        target = leg.to_organization
        expected = _upstream_target(identity.organization, row.campaign.factory_organization_id)
        if expected.source_organization_id != target.pk:
            raise QualityRecallError("待发出设备的上级关系已变化，请核对原程次。")
    else:
        current = _current_custodian(row)
        if current.pk != org_id:
            return None
        target = _upstream_target(current, row.campaign.factory_organization_id).source_organization
    return {
        "key": f"dispatch:{target.pk}", "action": "DISPATCH",
        "title": f"退回至{target.name}",
        "from_organization": _snapshot(identity.organization),
        "to_organization": _snapshot(target),
        "leg_id": leg.pk if leg and leg.status == "PREPARING" else None,
    }


def _manager_has_candidate_task(identity, row, candidate):
    if candidate["action"] == "RECEIVE":
        keys = (f"quality-recall-transport:{candidate['leg_id']}:receive",)
        return has_open_action_task_copy(identity, keys)
    if candidate["action"] == "ISOLATE":
        keys = (f"quality-recall-transport:{candidate['leg_id']}:inventory-posting",)
        return has_open_action_task_copy(identity, keys)

    organization_id = identity.organization_id
    handover = getattr(row, "customer_handover", None)
    keys = [f"quality-recall:{row.campaign_id}:org:{organization_id}"]
    if handover and handover.merchant_organization_id == organization_id:
        keys.append(
            f"quality-recall-merchant-return:{row.pk}:org:{organization_id}"
        )
    workitem_ids = (row.responsibility_snapshot or {}).get("workitem_ids", [])
    return has_open_action_task_copy(
        identity, keys, workitem_ids=workitem_ids,
    )


def get_recall_batch_groups(*, user, campaign, rows=None):
    identity = get_active_identity(user)
    if not identity or not identity.organization.is_active or campaign.status != "ACTIVE" or not _allowed(identity):
        return {"enabled": False, "groups": [], "blocked_devices": []}
    groups, blocked = OrderedDict(), []
    for row in rows if rows is not None else recall_batch_rows(campaign):
        try:
            candidate = _candidate(row, identity)
        except QualityRecallError as exc:
            own_scope = any(
                group.get("organization_id") == identity.organization_id
                and group.get("scope_section") == "own_inventory"
                for group in (row.responsibility_snapshot or {}).get("groups", [])
            )
            if own_scope:
                blocked.append({"recall_device_id": row.pk, "sn": row.sn_snapshot, "reason": str(exc)})
            continue
        if not candidate:
            continue
        if not _manager_has_candidate_task(identity, row, candidate):
            continue
        group = groups.setdefault(candidate["key"], {
            **{key: value for key, value in candidate.items() if key != "leg_id"},
            "action_label": ACTION_LABELS[candidate["action"]], "devices": [],
        })
        group["devices"].append({
            "recall_device_id": row.pk, "device_id": row.device_id,
            "sn": row.sn_snapshot, "leg_id": candidate["leg_id"],
        })
    for group in groups.values():
        group["device_count"] = len(group["devices"])
    return {"enabled": True, "groups": list(groups.values()), "blocked_devices": blocked}


def _text(value, label, maximum, required=False):
    value = str(value or "").strip()
    if required and not value:
        raise QualityRecallError(f"请填写{label}。")
    if len(value) > maximum:
        raise QualityRecallError(f"{label}不能超过{maximum}个字符。")
    return value


def _request_data(action, device_ids, evidence, provider, tracking, location, note):
    if action not in ACTION_LABELS:
        raise QualityRecallError("批量召回操作类型无效。")
    if not isinstance(device_ids, list) or not device_ids or len(device_ids) > MAX_BATCH_SIZE:
        raise QualityRecallError(f"请选择1至{MAX_BATCH_SIZE}台设备。")
    if any(type(value) is not int or value <= 0 for value in device_ids):
        raise QualityRecallError("设备清单格式无效。")
    if len(set(device_ids)) != len(device_ids):
        raise QualityRecallError("设备清单中存在重复设备。")
    if not isinstance(evidence, dict):
        raise QualityRecallError("请上传本次操作的凭证。")
    if len(json.dumps(evidence, ensure_ascii=False)) > 16384:
        raise QualityRecallError("凭证信息过长，请重新上传。")
    return {
        "action": action, "recall_device_ids": sorted(device_ids),
        "evidence": _evidence(evidence, "本次批量操作"),
        "logistics_provider": _text(provider, "物流承运方", 128, action == "DISPATCH"),
        "tracking_no": _text(tracking, "物流单号", 128, action == "DISPATCH"),
        "isolation_location": _text(location, "隔离库位", 255, action == "ISOLATE"),
        "receipt_note": _text(note, "交接说明", 2000),
    }


def _pending_devices(item):
    payload = item.payload or {}
    legs = QualityRecallTransportLeg.objects.filter(pk__in=payload.get("leg_ids", []))
    if item.type == TASK_TYPES["RECEIVE"]:
        return list(legs.filter(status="IN_TRANSIT").values_list("recall_device_id", flat=True))
    if item.type == TASK_TYPES["ISOLATE"]:
        return list(legs.filter(isolation_receipt__isnull=True).values_list("recall_device_id", flat=True))
    pending = []
    for leg in legs:
        if not QualityRecallTransportLeg.objects.filter(
            recall_device_id=leg.recall_device_id, sequence_no__gt=leg.sequence_no,
            from_organization_id=leg.to_organization_id,
        ).exclude(status="CANCELLED").exists():
            pending.append(leg.recall_device_id)
    return pending


def _close_processed_batch_tasks(operation, user):
    now = timezone.now()
    items = WorkItem.objects.select_for_update().filter(
        biz_id=str(operation.campaign_id), type=TASK_TYPES[operation.action], status="OPEN",
        payload__recipient_organization_id=operation.organization_id,
    )
    for item in items:
        remaining = _pending_devices(item)
        if remaining:
            payload = {**item.payload, "pending_device_ids": remaining, "pending_count": len(remaining)}
            item.payload = payload
            item.summary = f"本次交接仍有{len(remaining)}台待处理，请在召回详情中继续办理。"
            item.save(update_fields=("payload", "summary", "updated_at"))
            item.user_tasks.exclude(state="DONE").update(display_subtitle_snapshot=item.summary)
            continue
        item.status, item.done_by_openid, item.done_at = "DONE", user.wx_openid or "", now
        item.last_activity_at = now
        item.save(update_fields=("status", "done_by_openid", "done_at", "last_activity_at", "updated_at"))
        UserTask.objects.filter(workitem=item).exclude(state="DONE").update(
            state="DONE", done_at=now, done_reason="RECALL_BATCH_ACTION_COMPLETED",
            cursor_time=now, updated_at=now,
        )


def _publish_action(operation, *, organization, roles, action, legs, user):
    recipients = organization_recipients(organization, roles)
    if not recipients:
        raise QualityRecallError(f"{organization.name}没有可接收本阶段任务的有效岗位账号。")
    count = len(legs)
    title = {
        "DISPATCH": f"已接管{count}台召回设备，请批量继续退回",
        "RECEIVE": f"有{count}台召回设备待接收",
        "ISOLATE": f"厂家已收到{count}台召回设备，待批量隔离入账",
    }[action]
    stage = {
        "DISPATCH": "UPSTREAM_MERCHANT_RECEIVED",
        "RECEIVE": "FACTORY_PENDING_RECEIPT" if organization.pk == operation.campaign.factory_organization_id else "MERCHANT_PENDING_RECEIPT",
        "ISOLATE": "FACTORY_RECEIVED_PENDING_POSTING",
    }[action]
    return publish_workitem(
        domain="SYSTEM", type=TASK_TYPES[action], biz_id=str(operation.campaign_id),
        biz_ref=f"QualityRecallBatchOperation:{operation.pk}",
        uniq_key=f"quality-recall-batch:{operation.pk}:{action.lower()}",
        title=title, summary="在召回详情页核对本次设备清单并一键办理。",
        link=f"/pages/quality_trace/recall_detail/recall_detail?campaign_id={operation.campaign_id}",
        recipients_openids=recipients, need_action=True, action_mode="ANYONE_DO",
        payload={
            "recall_campaign_id": operation.campaign_id, "batch_operation_id": operation.pk,
            "recipient_organization_id": organization.pk, "stage": stage,
            "recall_device_ids": [leg.recall_device_id for leg in legs],
            "leg_ids": [leg.pk for leg in legs], "device_count": count, "pending_count": count,
        },
        actor_openid=user.wx_openid or "", mark_workitem_done_immediately=False,
    )


def _publish_followups(operation, legs, user):
    from .recall_notice_service import publish_sales_batch_notice
    publish_sales_batch_notice(operation=operation, legs=legs, actor=user)
    factory = operation.campaign.factory_organization
    if operation.action == "DISPATCH":
        target = legs[0].to_organization
        roles = {"factory_logistics"} if target.pk == factory.pk else MERCHANT_ROLES
        _publish_action(operation, organization=target, roles=roles, action="RECEIVE", legs=legs, user=user)
        stage, message = "RETURN_IN_TRANSIT", f"{operation.organization.name}已批量退回{len(legs)}台，发往{target.name}。"
        awareness_roles = FACTORY_RECALL_COLLABORATION_ROLES - ({"factory_logistics"} if target.pk == factory.pk else set())
    elif operation.action == "RECEIVE":
        if operation.organization_id == factory.pk:
            _publish_action(operation, organization=factory, roles={"factory_sales_assistant"}, action="ISOLATE", legs=legs, user=user)
            stage, message = "FACTORY_RECEIVED_PENDING_POSTING", f"厂家物流已批量确认收到{len(legs)}台，待销售助理隔离入账。"
        else:
            _publish_action(operation, organization=operation.organization, roles=MERCHANT_ROLES, action="DISPATCH", legs=legs, user=user)
            stage, message = "UPSTREAM_MERCHANT_RECEIVED", f"{operation.organization.name}已批量接收{len(legs)}台，待继续退回。"
        awareness_roles = {"factory_chief_engineer"}
    else:
        publish_batch_posting_confirmation(operation.campaign, user=user)
        stage, message = "FACTORY_INVENTORY_POSTED", f"销售助理已完成{len(legs)}台召回设备的隔离入账。"
        awareness_roles = {"factory_chief_engineer"}
    recipients = organization_recipients(factory, awareness_roles)
    if recipients:
        publish_workitem(
            domain="SYSTEM", type="QUALITY_RECALL_BATCH_PROGRESS", biz_id=str(operation.campaign_id),
            biz_ref=f"QualityRecallBatchOperation:{operation.pk}",
            uniq_key=f"quality-recall-batch:{operation.pk}:progress",
            title=f"召回批量流转进度：{len(legs)}台", summary=message,
            link=f"/pages/quality_trace/recall_detail/recall_detail?campaign_id={operation.campaign_id}",
            recipients_openids=recipients, need_action=False, action_mode="EACH_ACK",
            payload={"recall_campaign_id": operation.campaign_id, "batch_operation_id": operation.pk,
                     "stage": stage, "device_count": len(legs)},
            actor_openid=user.wx_openid or "", mark_workitem_done_immediately=False,
        )


@transaction.atomic
def execute_recall_batch(
    *, user, campaign_id, action, recall_device_ids, client_request_id,
    evidence, logistics_provider="", tracking_no="", isolation_location="", receipt_note="",
):
    identity = _identity(user)
    if not _allowed(identity):
        raise QualityRecallError("当前岗位无权办理召回批量流转。")
    request_id = _text(client_request_id, "提交标识", 64, True)
    request = _request_data(
        action, recall_device_ids, evidence, logistics_provider, tracking_no,
        isolation_location, receipt_note,
    )
    fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    campaign = QualityRecallCampaign.objects.select_for_update().select_related(
        "factory_organization__organization_type",
    ).filter(pk=campaign_id).first()
    if not campaign:
        raise QualityRecallError("召回批次不存在。")
    previous = QualityRecallBatchOperation.objects.filter(
        campaign=campaign, organization=identity.organization, client_request_id=request_id,
    ).first()
    if previous:
        if previous.request_fingerprint != fingerprint:
            raise QualityRecallError("同一提交标识不能用于不同的设备或交接信息。")
        return {**previous.result_payload, "idempotent_replay": True}
    if campaign.status != "ACTIVE":
        raise QualityRecallError("该召回批次当前不能办理流转。")
    rows = list(recall_batch_rows(campaign).select_for_update().filter(pk__in=request["recall_device_ids"]))
    if len(rows) != len(request["recall_device_ids"]):
        raise QualityRecallError("所选设备不完整或不属于本次召回批次。")
    candidates = []
    for row in rows:
        candidate = _candidate(row, identity)
        if (
            not candidate
            or candidate["action"] != action
            or not _manager_has_candidate_task(identity, row, candidate)
        ):
            raise QualityRecallError(f"设备{row.sn_snapshot}当前不属于您的本阶段待办，请刷新清单。")
        candidates.append(candidate)
    if len({candidate["key"] for candidate in candidates}) != 1:
        raise QualityRecallError("请选择同一流转范围内的设备；不同到货批次请分别确认。")
    operation = QualityRecallBatchOperation.objects.create(
        campaign=campaign, organization=identity.organization, action=action,
        client_request_id=request_id, request_fingerprint=fingerprint,
        request_payload=request,
        recall_device_ids=request["recall_device_ids"], actor=user,
        actor_role_snapshot=identity.role.code,
    )
    legs, receipt_ids = [], []
    for row, candidate in zip(rows, candidates):
        if action == "DISPATCH":
            if candidate["leg_id"]:
                leg_id = candidate["leg_id"]
            else:
                leg_id = create_next_recall_transport_leg(
                    user=user, recall_device_id=row.pk, notify=False,
                ).pk
            leg = dispatch_recall_transport_leg(
                user=user, leg_id=leg_id, evidence=request["evidence"],
                logistics_provider=request["logistics_provider"],
                tracking_no=request["tracking_no"], notify=False,
            )
            leg.relation_snapshot = {**leg.relation_snapshot, "dispatch_batch_operation_id": operation.pk}
            leg.save(update_fields=("relation_snapshot", "updated_at"))
            handover = getattr(row, "customer_handover", None)
            if handover:
                close_merchant_return_task(handover, identity.organization, user=user)
        elif action == "RECEIVE":
            leg = receive_recall_transport_leg(
                user=user, leg_id=candidate["leg_id"], evidence=request["evidence"],
                receipt_note=request["receipt_note"], notify=False,
            )
        else:
            receipt = isolate_recalled_device(
                user=user, leg_id=candidate["leg_id"], evidence=request["evidence"],
                isolation_location=request["isolation_location"],
                disposition=QualityRecallIsolationReceipt.Disposition.PENDING_INSPECTION,
                notify=False,
            )
            receipt_ids.append(receipt.pk)
            leg = receipt.transport_leg
            handover = getattr(row, "customer_handover", None)
            if handover:
                close_factory_recall_progress(handover, user=user)
        legs.append(leg)
    if action == "DISPATCH":
        workitem_ids = {pk for row in rows for pk in (row.responsibility_snapshot or {}).get("workitem_ids", [])}
        complete_recall_scope_tasks_if_ready(
            recall_device=rows[0], user=user, audience_type="MERCHANT", workitem_ids=workitem_ids,
        )
    _close_processed_batch_tasks(operation, user)
    _publish_followups(operation, legs, user)
    progress = calculate_recall_progress(campaign_id=campaign.pk, source="BATCH")
    now = timezone.now()
    result = {
        "operation_id": operation.pk, "campaign_id": campaign.pk, "action": action,
        "processed_count": len(rows), "recall_device_ids": request["recall_device_ids"],
        "leg_ids": [leg.pk for leg in legs], "isolation_receipt_ids": receipt_ids,
        "completed_at": now.isoformat(), "progress_snapshot_id": progress.pk,
        "idempotent_replay": False,
    }
    operation.result_payload, operation.completed_at = result, now
    operation.save(update_fields=("result_payload", "completed_at", "updated_at"))
    QualityTraceEvent.objects.create(
        quality_case=campaign.quality_case, alert=campaign.quality_case.source_alert,
        event_type="RECALL_BATCH_" + action, actor=user, actor_role_snapshot=identity.role.code,
        payload={**result, "organization_id": identity.organization_id,
                 "client_request_id": request_id},
    )
    return result
