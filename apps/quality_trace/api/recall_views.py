import json
from datetime import date
from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.http import JsonResponse
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt

from account.services.auth_service import (
    ApiAuthenticationError,
    authenticate_request,
    get_active_identity,
)
from quality_trace.models import (
    QualityRecallCampaign,
    QualityRecallCustomerHandover,
    QualityRecallDevice,
    QualityRecallIsolationReceipt,
    QualityRecallTransportLeg,
)
from quality_trace.services.access_service import FACTORY_QUALITY_ROLES
from quality_trace.services import (
    QualityCaseError,
    QualityRecallError,
    acknowledge_customer_recall,
    add_handover_evidence,
    calculate_recall_progress,
    complete_recall_campaign,
    confirm_merchant_handover_receipt,
    create_next_recall_transport_leg,
    create_recall_campaign,
    decide_unreachable_device,
    dispatch_recall_transport_leg,
    initialize_customer_handover,
    isolate_recalled_device,
    launch_recall_campaign,
    publish_recall_tasks,
    receive_recall_transport_leg,
    schedule_customer_pickup,
)


def _user(request):
    try:
        return authenticate_request(request), None
    except ApiAuthenticationError as exc:
        return None, JsonResponse({"code": 401, "msg": str(exc), "data": {}}, status=401)


def _data(request):
    if request.body:
        try:
            value = json.loads(request.body.decode("utf-8"))
            return value if isinstance(value, dict) else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    return request.POST.dict()


def _error(exc):
    return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)


@csrf_exempt
def evidence_upload(request):
    if request.method != "POST":
        return _error("仅支持POST")
    user, error = _user(request)
    if error:
        return error
    _identity(user)
    uploaded = request.FILES.get("file")
    if not uploaded:
        return _error("请选择召回凭证文件。")
    if uploaded.size > 220 * 1024 * 1024:
        return _error("召回凭证不能超过220MB。")
    content_type = str(uploaded.content_type or "").lower()
    allowed = (
        content_type.startswith("image/")
        or content_type.startswith("video/")
        or content_type == "application/pdf"
    )
    if not allowed:
        return _error("仅支持图片、视频或PDF凭证。")
    suffix = Path(uploaded.name or "evidence").suffix.lower()[:12]
    key = f"recall_evidence/{timezone.now():%Y/%m}/{uuid4().hex}{suffix}"
    root = getattr(
        settings, "QUALITY_RECALL_PRIVATE_ROOT",
        settings.BASE_DIR / "private_quality_recall",
    )
    storage = FileSystemStorage(location=root, base_url=None)
    saved_key = storage.save(key, uploaded)
    return JsonResponse({
        "code": 0,
        "msg": "凭证上传成功",
        "data": {
            "storage_key": saved_key,
            "original_name": uploaded.name,
            "content_type": content_type,
            "file_size": uploaded.size,
        },
    }, status=200)


def _identity(user):
    identity = get_active_identity(user)
    if not identity:
        raise QualityRecallError("当前账号没有有效组织身份。")
    return identity


def _can_view(row, user):
    identity = _identity(user)
    if (
        identity.organization.organization_type.code == "OWNER"
        and row.campaign.factory_organization_id == identity.organization_id
    ):
        return True
    openid = str(user.wx_openid or "")
    if any(
        recipient.get("openid") == openid
        for group in (row.responsibility_snapshot or {}).get("groups", [])
        for recipient in group.get("recipients", [])
    ):
        return True
    handover = getattr(row, "customer_handover", None)
    if handover and identity.organization_id in {
        handover.customer_organization_id, handover.merchant_organization_id,
    }:
        return True
    return row.transport_legs.filter(
        from_organization_id=identity.organization_id,
    ).exists() or row.transport_legs.filter(
        to_organization_id=identity.organization_id,
    ).exists()


def _handover(row):
    handover = getattr(row, "customer_handover", None)
    if not handover:
        return None
    return {
        "id": handover.pk,
        "status": handover.status,
        "customer_organization_id": handover.customer_organization_id,
        "merchant_organization_id": handover.merchant_organization_id,
        "customer_confirmed_at": handover.customer_confirmed_at.isoformat() if handover.customer_confirmed_at else "",
        "pickup_scheduled_at": handover.pickup_scheduled_at.isoformat() if handover.pickup_scheduled_at else "",
        "pickup_address": handover.pickup_address,
        "merchant_received_at": handover.merchant_received_at.isoformat() if handover.merchant_received_at else "",
        "custody": handover.custody_snapshot,
        "evidence": [{
            "id": item.pk, "kind": item.kind, "original_name": item.original_name,
            "content_type": item.content_type, "file_size": item.file_size,
            "uploaded_at": item.uploaded_at.isoformat(),
        } for item in handover.evidence.all()],
    }


def _leg(item):
    return {
        "id": item.pk,
        "sequence_no": item.sequence_no,
        "status": item.status,
        "from_organization": {"id": item.from_organization_id, "name": item.from_organization.name},
        "to_organization": {"id": item.to_organization_id, "name": item.to_organization.name},
        "is_factory_destination": item.is_factory_destination,
        "logistics_provider": item.logistics_provider,
        "tracking_no": item.tracking_no,
        "dispatched_at": item.dispatched_at.isoformat() if item.dispatched_at else "",
        "received_at": item.received_at.isoformat() if item.received_at else "",
        "custody": item.custody_snapshot,
    }


def _device(row):
    isolation = getattr(row, "isolation_receipt", None)
    return {
        "id": row.pk,
        "device_id": row.device_id,
        "sn": row.sn_snapshot,
        "status": row.status,
        "product_model": row.product_model_snapshot,
        "production_batch": row.production_batch_snapshot,
        "responsibility": row.responsibility_snapshot,
        "handover": _handover(row),
        "transport_legs": [_leg(item) for item in row.transport_legs.all()],
        "isolation": ({
            "id": isolation.pk,
            "disposition": isolation.disposition,
            "location": isolation.isolation_location,
            "isolated_at": isolation.isolated_at.isoformat(),
        } if isolation else None),
    }


def _campaign(row, include_devices=False):
    data = {
        "id": row.pk,
        "recall_no": row.recall_no,
        "quality_case_id": row.quality_case_id,
        "title": row.title,
        "reason": row.reason,
        "risk_level": row.risk_level,
        "remedy_type": row.remedy_type,
        "deadline": row.deadline.isoformat(),
        "status": row.status,
        "device_count": row.device_count,
        "launched_at": row.launched_at.isoformat() if row.launched_at else "",
        "completed_at": row.completed_at.isoformat() if row.completed_at else "",
    }
    latest = row.progress_snapshots.order_by("-calculated_at", "-id").first()
    data["progress"] = ({
        "id": latest.pk, "counts": latest.counts,
        "completion_eligible_count": latest.completion_eligible_count,
        "incomplete_device_ids": latest.incomplete_device_ids,
        "overdue_device_ids": latest.overdue_device_ids,
        "is_overdue": latest.is_overdue,
        "calculated_at": latest.calculated_at.isoformat(),
    } if latest else None)
    if include_devices:
        data["devices"] = [_device(item) for item in row.devices.all()]
    return data


def _row(user, recall_device_id):
    row = QualityRecallDevice.objects.select_related(
        "campaign__factory_organization", "campaign__quality_case",
        "customer_handover", "isolation_receipt",
    ).prefetch_related(
        "customer_handover__evidence",
        "transport_legs__from_organization",
        "transport_legs__to_organization",
    ).filter(pk=recall_device_id).first()
    if not row or not _can_view(row, user):
        raise QualityRecallError("召回设备不存在或当前账号无权查看。")
    return row


def _factory_campaign(user, campaign_id):
    identity = _identity(user)
    if identity.role.code not in FACTORY_QUALITY_ROLES:
        raise QualityRecallError("当前岗位无权操作厂家召回批次。")
    row = QualityRecallCampaign.objects.filter(
        pk=campaign_id,
        factory_organization=identity.organization,
        factory_organization__organization_type__code="OWNER",
    ).first()
    if not row:
        raise QualityRecallError("召回批次不存在或当前账号无权操作。")
    return row


@csrf_exempt
def campaigns(request):
    user, error = _user(request)
    if error:
        return error
    identity = _identity(user)
    if request.method == "POST":
        payload = _data(request)
        try:
            row = create_recall_campaign(
                user=user,
                case_id=int(payload.get("case_id")),
                title=payload.get("title"), reason=payload.get("reason"),
                risk_level=payload.get("risk_level"),
                remedy_type=payload.get("remedy_type"),
                deadline=date.fromisoformat(str(payload.get("deadline"))),
                device_ids=payload.get("device_ids"),
            )
        except (QualityCaseError, QualityRecallError, TypeError, ValueError) as exc:
            return _error(exc)
        return JsonResponse({"code": 0, "msg": "召回批次已创建", "data": _campaign(row)}, status=200)
    if request.method != "GET":
        return _error("仅支持GET或POST")
    rows = QualityRecallCampaign.objects.select_related("quality_case").prefetch_related(
        "progress_snapshots",
    )
    if identity.organization.organization_type.code == "OWNER":
        if identity.role.code not in FACTORY_QUALITY_ROLES:
            return _error("当前岗位无权访问厂家质量追溯数据。")
        rows = rows.filter(factory_organization=identity.organization)
    else:
        rows = rows.filter(
            Q(devices__responsibility_snapshot__icontains=str(user.wx_openid or ""))
            | Q(devices__customer_handover__customer_organization=identity.organization)
            | Q(devices__customer_handover__merchant_organization=identity.organization)
            | Q(devices__transport_legs__from_organization=identity.organization)
            | Q(devices__transport_legs__to_organization=identity.organization)
        ).distinct()
    return JsonResponse({"code": 0, "msg": "", "data": {"items": [_campaign(row) for row in rows[:100]]}}, status=200)


def campaign_detail(request, campaign_id):
    if request.method != "GET":
        return _error("仅支持GET")
    user, error = _user(request)
    if error:
        return error
    campaign = QualityRecallCampaign.objects.select_related(
        "quality_case", "factory_organization",
    ).prefetch_related(
        "progress_snapshots",
        "devices__customer_handover__evidence",
        "devices__transport_legs__from_organization",
        "devices__transport_legs__to_organization",
        "devices__isolation_receipt",
    ).filter(pk=campaign_id).first()
    if not campaign:
        return _error("召回批次不存在。")
    visible = [item for item in campaign.devices.all() if _can_view(item, user)]
    identity = _identity(user)
    factory_view = (
        identity.organization.organization_type.code == "OWNER"
        and campaign.factory_organization_id == identity.organization_id
        and identity.role.code in FACTORY_QUALITY_ROLES
    )
    if not factory_view and not visible:
        return _error("当前账号无权查看该召回批次。")
    data = _campaign(campaign)
    data["devices"] = [_device(item) for item in (campaign.devices.all() if factory_view else visible)]
    return JsonResponse({"code": 0, "msg": "", "data": data}, status=200)


def device_detail(request, recall_device_id):
    if request.method != "GET":
        return _error("仅支持GET")
    user, error = _user(request)
    if error:
        return error
    try:
        row = _row(user, recall_device_id)
    except QualityRecallError as exc:
        return _error(exc)
    return JsonResponse({"code": 0, "msg": "", "data": _device(row)}, status=200)


def _post(request, callback):
    if request.method != "POST":
        return _error("仅支持POST")
    user, error = _user(request)
    if error:
        return error
    try:
        return callback(user, _data(request))
    except (QualityCaseError, QualityRecallError, TypeError, ValueError) as exc:
        return _error(exc)


@csrf_exempt
def campaign_launch(request, campaign_id):
    return _post(request, lambda user, data: JsonResponse({
        "code": 0, "msg": "召回批次已发布",
        "data": _campaign(launch_recall_campaign(user=user, campaign_id=campaign_id)),
    }))


@csrf_exempt
def campaign_publish_tasks(request, campaign_id):
    return _post(request, lambda user, data: JsonResponse({
        "code": 0, "msg": "召回任务已生成",
        "data": {"items": publish_recall_tasks(user=user, campaign_id=campaign_id)},
    }))


@csrf_exempt
def campaign_progress(request, campaign_id):
    def callback(user, data):
        _factory_campaign(user, campaign_id)
        snapshot = calculate_recall_progress(campaign_id=campaign_id, source="API")
        return JsonResponse({"code": 0, "msg": "进度已更新", "data": {"snapshot_id": snapshot.pk}})
    return _post(request, callback)


@csrf_exempt
def campaign_complete(request, campaign_id):
    return _post(request, lambda user, data: JsonResponse({
        "code": 0, "msg": "召回执行已完成并转入正式隔离",
        "data": {"closeout_id": complete_recall_campaign(
            user=user, campaign_id=campaign_id, reason=data.get("reason"),
        ).pk},
    }))


@csrf_exempt
def handover_initialize(request, recall_device_id):
    def callback(user, data):
        row, created = initialize_customer_handover(user=user, recall_device_id=recall_device_id)
        return JsonResponse({"code": 0, "msg": "交接单已建立", "data": {"handover_id": row.pk, "created": created}})
    return _post(request, callback)


@csrf_exempt
def handover_ack(request, handover_id):
    return _post(request, lambda user, data: JsonResponse({"code": 0, "msg": "已确认召回通知", "data": {"handover_id": acknowledge_customer_recall(user=user, handover_id=handover_id, note=data.get("note")).pk}}))


@csrf_exempt
def handover_schedule(request, handover_id):
    return _post(request, lambda user, data: JsonResponse({"code": 0, "msg": "已预约取回", "data": {"handover_id": schedule_customer_pickup(
        user=user, handover_id=handover_id,
        pickup_scheduled_at=parse_datetime(str(data.get("pickup_scheduled_at") or "")),
        pickup_address=data.get("pickup_address"), note=data.get("note"),
    ).pk}}))


@csrf_exempt
def handover_evidence(request, handover_id):
    return _post(request, lambda user, data: JsonResponse({"code": 0, "msg": "凭证已登记", "data": {"evidence_id": add_handover_evidence(
        user=user, handover_id=handover_id, kind=data.get("kind"),
        client_request_id=data.get("client_request_id"), evidence=data.get("evidence"),
    ).pk}}))


@csrf_exempt
def handover_receive(request, handover_id):
    return _post(request, lambda user, data: JsonResponse({"code": 0, "msg": "商家已验收接管", "data": {"handover_id": confirm_merchant_handover_receipt(
        user=user, handover_id=handover_id, receipt_note=data.get("receipt_note"),
    ).pk}}))


@csrf_exempt
def transport_create(request, recall_device_id):
    return _post(request, lambda user, data: JsonResponse({"code": 0, "msg": "下一程召回退回已建立", "data": {"leg_id": create_next_recall_transport_leg(user=user, recall_device_id=recall_device_id).pk}}))


@csrf_exempt
def transport_dispatch(request, leg_id):
    return _post(request, lambda user, data: JsonResponse({"code": 0, "msg": "召回设备已发出", "data": {"leg_id": dispatch_recall_transport_leg(
        user=user, leg_id=leg_id, logistics_provider=data.get("logistics_provider"),
        tracking_no=data.get("tracking_no"), evidence=data.get("evidence"),
    ).pk}}))


@csrf_exempt
def transport_receive(request, leg_id):
    return _post(request, lambda user, data: JsonResponse({"code": 0, "msg": "本程召回设备已验收", "data": {"leg_id": receive_recall_transport_leg(
        user=user, leg_id=leg_id, evidence=data.get("evidence"),
        receipt_note=data.get("receipt_note"),
    ).pk}}))


@csrf_exempt
def transport_isolate(request, leg_id):
    return _post(request, lambda user, data: JsonResponse({"code": 0, "msg": "厂家隔离已完成", "data": {"isolation_receipt_id": isolate_recalled_device(
        user=user, leg_id=leg_id, disposition=data.get("disposition"),
        isolation_location=data.get("isolation_location"), evidence=data.get("evidence"),
    ).pk}}))


@csrf_exempt
def unreachable_decide(request, recall_device_id):
    return _post(request, lambda user, data: JsonResponse({"code": 0, "msg": "无法联系审批已记录", "data": {"decision_id": decide_unreachable_device(
        user=user, recall_device_id=recall_device_id,
        decision=data.get("decision"), reason=data.get("reason"),
    ).pk}}))
