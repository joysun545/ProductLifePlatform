import json
from pathlib import Path
from uuid import UUID, uuid4

from django.conf import settings
from django.core.cache import cache
from django.core.files.storage import FileSystemStorage
from django.http import HttpResponse, JsonResponse
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
    QualityRecallPostDisposition,
    QualityRecallTransportLeg,
)
from quality_trace.services.access_service import FACTORY_QUALITY_ROLES
from quality_trace.services.recall_access_service import (
    FACTORY_RECALL_VIEW_ROLES, MERCHANT_RECALL_ROLES, RecallViewer,
    recall_campaign_queryset, visible_rows,
)
from quality_trace.services.recall_display_service import (
    campaign_context, campaign_text, decorate_batch_flow, latest_transport, row_stage,
)
from quality_trace.services import (
    QualityCaseError,
    QualityRecallError,
    accept_factory_pickup_request,
    accept_merchant_handover_by_token,
    acknowledge_customer_recall,
    add_handover_evidence,
    calculate_recall_progress,
    confirm_recall_inventory_batch,
    complete_recall_campaign,
    complete_recall_disposition_production,
    complete_recall_device_disposition,
    confirm_recall_disposition_matching,
    confirm_recall_disposition_supplier,
    confirm_merchant_handover_receipt,
    create_next_recall_transport_leg,
    decide_unreachable_device,
    dispatch_recall_transport_leg,
    initialize_customer_handover,
    isolate_recalled_device,
    launch_recall_campaign,
    receive_recall_transport_leg,
    schedule_customer_pickup,
    select_customer_delivery_method,
    submit_customer_direct_shipment,
    submit_customer_pickup_shipment,
    start_recall_post_disposition,
)
from quality_trace.services.recall_service import ensure_recall_campaign
from quality_trace.services.recall_task_service import get_recall_task_scope
from quality_trace.services.recall_batch_service import execute_recall_batch, get_recall_batch_groups
from workflow.models import UserTask
from common.services.wechat_miniprogram_code import (
    WeChatMiniProgramCodeError,
    generate_unlimited_miniprogram_code,
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
    identity = _identity(user)
    from quality_trace.services.recall_access_service import FACTORY_RECALL_OBSERVER_ROLES
    if identity.role.code in FACTORY_RECALL_OBSERVER_ROLES:
        return _error("本岗位仅接收召回知晓通知，无凭证上传权限。")
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
    return RecallViewer(user).allows(row)


def _handover(row, user=None):
    handover = getattr(row, "customer_handover", None)
    if not handover:
        return None
    identity = _identity(user) if user else None
    role = identity.role.code if identity else ""
    organization_id = identity.organization_id if identity else None
    viewer = RecallViewer(user) if user else None
    active = row.campaign.status == "ACTIVE" and handover.status != "CANCELLED"
    customer_actor = bool(viewer and viewer.customer_actor(row))
    merchant_actor = bool(viewer and viewer.kind == "MERCHANT"
                          and role in MERCHANT_RECALL_ROLES
                          and organization_id == handover.merchant_organization_id)
    factory_pickup_actor = bool(
        identity
        and organization_id == handover.campaign.factory_organization_id
        and role in {"factory_chief_engineer", "factory_logistics"}
    )
    latest_leg = latest_transport(row)
    return {
        "id": handover.pk,
        "status": handover.status,
        "status_label": handover.get_status_display(),
        "delivery_method": handover.delivery_method,
        "delivery_method_label": handover.get_delivery_method_display() if handover.delivery_method else "",
        "method_selected_role": handover.method_selected_role,
        "method_selected_at": handover.method_selected_at.isoformat() if handover.method_selected_at else "",
        "method_locked": bool(handover.method_locked_at),
        "method_locked_at": handover.method_locked_at.isoformat() if handover.method_locked_at else "",
        "method_payload": handover.method_payload if customer_actor or factory_pickup_actor or merchant_actor else {},
        "customer_organization_id": handover.customer_organization_id,
        "merchant_organization_id": handover.merchant_organization_id,
        "customer_confirmed_at": handover.customer_confirmed_at.isoformat() if handover.customer_confirmed_at else "",
        "pickup_scheduled_at": handover.pickup_scheduled_at.isoformat() if handover.pickup_scheduled_at else "",
        "pickup_address": handover.pickup_address if customer_actor or factory_pickup_actor or merchant_actor else "",
        "merchant_received_at": handover.merchant_received_at.isoformat() if handover.merchant_received_at else "",
        "custody": handover.custody_snapshot,
        "merchant_transfer_token": (
            str(handover.merchant_transfer_token or "")
            if active and customer_actor and not handover.method_locked_at else ""
        ),
        "merchant_transfer_expires_at": (
            handover.merchant_transfer_expires_at.isoformat()
            if customer_actor and handover.merchant_transfer_expires_at else ""
        ),
        "available_actions": {
            "select_delivery_method": active and customer_actor and not handover.method_locked_at,
            "schedule_pickup": active and merchant_actor and handover.status == "ACKNOWLEDGED",
            "merchant_receive": active and merchant_actor and handover.status == "PICKUP_SCHEDULED",
            "submit_direct_shipment": bool(
                active and customer_actor
                and handover.delivery_method == QualityRecallCustomerHandover.DeliveryMethod.CUSTOMER_DIRECT_SHIP
                and not handover.method_locked_at
            ),
            "accept_factory_pickup": bool(
                active and factory_pickup_actor
                and handover.delivery_method == QualityRecallCustomerHandover.DeliveryMethod.FACTORY_PICKUP
                and handover.status == QualityRecallCustomerHandover.Status.PICKUP_REQUESTED
                and not handover.method_locked_at
            ),
            "submit_factory_pickup_shipment": bool(
                active and customer_actor
                and handover.status == QualityRecallCustomerHandover.Status.PICKUP_ACCEPTED
                and latest_leg
                and latest_leg.status == QualityRecallTransportLeg.Status.PREPARING
            ),
        },
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


def _device_source_state(row):
    """Return one normalized, immutable recall-origin description for every UI."""
    responsibility = row.responsibility_snapshot or {}
    mode = str(responsibility.get("mode") or "")
    groups = responsibility.get("groups") or []
    location = row.location_snapshot or {}

    definitions = {
        "ACTIVATED_USER": ("USER_ACTIVATED", "用户已激活", "customer_devices"),
        "MERCHANT_INVENTORY": ("MERCHANT_INVENTORY", "商家库存", "own_inventory"),
        "FACTORY_INVENTORY": ("FACTORY_INVENTORY", "厂家库存", "factory_inventory"),
        "IN_TRANSIT": ("IN_TRANSIT", "流转途中", "in_transit_devices"),
        "MANUAL_TRACE": ("MANUAL_TRACE", "待人工确认", "manual_trace"),
    }
    code, label, preferred_section = definitions.get(
        mode,
        (
            "USER_ACTIVATED" if location.get("activation_status") == "ACTIVATED" else "INVENTORY",
            "用户已激活" if location.get("activation_status") == "ACTIVATED" else "库存设备",
            "customer_devices" if location.get("activation_status") == "ACTIVATED" else "",
        ),
    )
    source_group = next(
        (item for item in groups if item.get("scope_section") == preferred_section),
        None,
    )
    if source_group is None and mode == "ACTIVATED_USER":
        source_group = next(
            (item for item in groups if item.get("organization_type") == "CUSTOMER"),
            None,
        )

    return {
        "code": code,
        "label": label,
        "organization_id": (
            source_group.get("organization_id") if source_group else None
        ),
        "organization_name": (
            source_group.get("organization_name") if source_group else ""
        ),
    }


def _device(row, user=None):
    isolation = getattr(row, "isolation_receipt", None)
    post_disposition = getattr(row, "post_disposition", None)
    transport_legs = list(row.transport_legs.all())
    latest_leg = latest_transport(row)
    viewer = RecallViewer(user) if user else None
    viewer_identity = viewer.identity if viewer else None
    stage = row_stage(row, viewer) if viewer else {"code": "", "label": "", "next_step": ""}
    current_stage, current_stage_label = stage["code"], stage["label"]
    factory_actor = bool(viewer and viewer.factory(row.campaign) and row.campaign.status == "ACTIVE")
    supplier_view = bool(viewer and viewer.kind == "SUPPLIER")
    responsibility = row.responsibility_snapshot or {}
    safe_responsibility = {
        "mode": responsibility.get("mode", ""),
        "groups": [{key: group.get(key) for key in (
            "key", "organization_id", "organization_name", "organization_type", "scope_section",
        )} for group in responsibility.get("groups", [])
            if not supplier_view and (not viewer or viewer.full_campaign(row.campaign)
                                     or str(group.get("organization_id")) == str(viewer.organization_id))],
    }
    return {
        "id": row.pk,
        "device_id": row.device_id,
        "campaign_id": row.campaign_id,
        "sn": row.sn_snapshot,
        "status": row.status,
        "product_model": row.product_model_snapshot,
        "production_batch": row.production_batch_snapshot,
        "responsibility": safe_responsibility,
        "source_state": ({"label": _device_source_state(row)["label"]} if supplier_view else _device_source_state(row)),
        "handover": None if supplier_view else _handover(row, user=user),
        "transport_legs": [] if supplier_view else [_leg(item) for item in transport_legs],
        "current_stage": current_stage,
        "current_stage_label": current_stage_label,
        "next_step": stage["next_step"],
        "isolation": ({
            "id": isolation.pk,
            "disposition": isolation.disposition,
            "location": isolation.isolation_location,
            "isolated_at": isolation.isolated_at.isoformat(),
        } if isolation else None),
        "post_disposition": ({
            "id": post_disposition.pk,
            "method": post_disposition.method,
            "method_label": post_disposition.get_method_display(),
            "status": post_disposition.status,
            "status_label": post_disposition.get_status_display(),
            "stock_result": post_disposition.stock_result,
            "stock_result_label": post_disposition.get_stock_result_display(),
            "replacement_parts": post_disposition.replacement_parts,
            "execution_stage": post_disposition.execution_stage,
            "execution_stage_label": post_disposition.get_execution_stage_display(),
            "supplier_confirmations": [{
                "id": confirmation.pk,
                "supplier_id": confirmation.supplier_id,
                "supplier_name": confirmation.supplier_name_snapshot,
                "confirmed": bool(confirmation.confirmed_at),
                "can_confirm": bool(
                    viewer_identity
                    and viewer_identity.role.code == "supplier_owner"
                    and viewer_identity.organization_id == confirmation.supplier_id
                    and not confirmation.confirmed_at
                    and row.campaign.status == "ACTIVE"
                    and post_disposition.execution_stage == "WAITING_SUPPLIER"
                ),
            } for confirmation in post_disposition.supplier_confirmations.all()
                if not supplier_view or confirmation.supplier_id == viewer.organization_id],
            "available_actions": ({
                "supplier_confirm": bool(
                    viewer_identity.role.code == "supplier_owner"
                    and row.campaign.status == "ACTIVE"
                    and post_disposition.execution_stage == "WAITING_SUPPLIER"
                    and any(
                        confirmation.supplier_id == viewer_identity.organization_id
                        and not confirmation.confirmed_at
                        for confirmation in post_disposition.supplier_confirmations.all()
                    )
                ),
                "matching_confirm": bool(
                    factory_actor and viewer_identity.role.code == "factory_matching"
                    and post_disposition.execution_stage == "WAITING_MATCHING"
                ),
                "production_complete": bool(
                    factory_actor and viewer_identity.role.code == "factory_production"
                    and post_disposition.execution_stage in {
                        "WAITING_PRODUCTION", "WAITING_PRODUCTION_COMPLETION",
                    }
                ),
                "chief_confirm": bool(
                    factory_actor and viewer_identity.role.code == "factory_chief_engineer"
                    and post_disposition.execution_stage == "WAITING_CHIEF_CONFIRMATION"
                ),
            } if viewer_identity else {}),
        } if post_disposition else None),
    }


def _campaign(row, include_devices=False, *, device_count=None, scope_label=""):
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
        "device_count": row.device_count if device_count is None else device_count,
        "scope_label": scope_label,
        "control_action_id": row.control_action_id,
        "is_published": row.status != QualityRecallCampaign.Status.DRAFT,
        "launched_at": row.launched_at.isoformat() if row.launched_at else "",
        "completed_at": row.completed_at.isoformat() if row.completed_at else "",
        "superseded_by_campaign_id": row.superseded_by_id,
        "superseded_at": row.superseded_at.isoformat() if row.superseded_at else "",
        "supersession_reason": row.supersession_reason,
    }
    accounted = row.isolation_receipts.count()
    confirmation = getattr(row, "inventory_batch_confirmation", None)
    data["inventory_posting"] = {
        "accounted_device_count": accounted,
        "campaign_device_count": row.device_count,
        "all_accounted": accounted == row.device_count,
        "batch_confirmed": bool(confirmation),
        "confirmed_at": confirmation.confirmed_at.isoformat() if confirmation else "",
    }
    dispositions = row.post_dispositions.all()
    disposition_count = dispositions.count()
    eligible_disposition_count = row.devices.exclude(
        status__in=(QualityRecallDevice.Status.UNREACHABLE,
                    QualityRecallDevice.Status.EXEMPTED),
    ).count()
    completed_disposition_count = dispositions.filter(
        status=QualityRecallPostDisposition.Status.COMPLETED,
    ).count()
    data["post_disposition"] = {
        "started": disposition_count > 0,
        "method": dispositions.first().method if disposition_count else "",
        "device_count": disposition_count,
        "eligible_device_count": eligible_disposition_count,
        "unassigned_device_count": eligible_disposition_count - disposition_count,
        "completed_device_count": completed_disposition_count,
        "all_completed": bool(
            disposition_count == eligible_disposition_count
            and completed_disposition_count == eligible_disposition_count
        ),
        "can_start": bool(
            confirmation and disposition_count < eligible_disposition_count
        ),
        "can_complete_campaign": bool(
            confirmation and disposition_count == eligible_disposition_count
            and completed_disposition_count == eligible_disposition_count
        ),
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


def _scoped_campaign(campaign, rows, viewer):
    context = campaign_context(campaign, rows, viewer)
    data = _campaign(campaign, device_count=len(rows), scope_label=context["scope_label"])
    data["viewer"] = {**viewer.payload, "full_campaign_access": viewer.full_campaign(campaign),
                      "observer_only": viewer.observer(campaign)}
    data.update(campaign_text(campaign, viewer))
    data["device_count"] = context["device_count"]
    data["show_device_count"] = not viewer.observer(campaign)
    data["view_context"] = context
    if not viewer.full_campaign(campaign):
        # Global factory statistics contain unrelated device IDs and controls.
        data["progress"] = None
        data["inventory_posting"] = None
        data["post_disposition"] = None
    return data


def _row(user, recall_device_id):
    row = QualityRecallDevice.objects.select_related(
        "campaign__factory_organization", "campaign__quality_case",
        "customer_handover", "isolation_receipt",
    ).prefetch_related(
        "customer_handover__evidence",
        "transport_legs__from_organization",
        "transport_legs__to_organization",
        "post_disposition",
        "post_disposition__supplier_confirmations",
    ).filter(pk=recall_device_id).first()
    if not row or not _can_view(row, user):
        raise QualityRecallError("召回设备不存在或当前账号无权查看。")
    return row


def _factory_campaign(user, campaign_id):
    identity = _identity(user)
    if identity.role.code not in {"factory_chief_engineer"}:
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
            row = ensure_recall_campaign(
                user=user,
                case_id=int(payload.get("case_id")),
                reason=payload.get("reason"),
            )
        except (QualityCaseError, QualityRecallError, TypeError, ValueError) as exc:
            return _error(exc)
        return JsonResponse({
            "code": 0,
            "msg": "召回已启动，召回批次已自动创建并绑定",
            "data": _campaign(row),
        }, status=200)
    if request.method != "GET":
        return _error("仅支持GET或POST")
    viewer = RecallViewer(user)
    rows = recall_campaign_queryset()
    if viewer.kind == "OWNER":
        if viewer.role not in FACTORY_RECALL_VIEW_ROLES:
            return _error("当前岗位无权访问厂家质量追溯数据。")
        rows = rows.filter(factory_organization_id=viewer.organization_id)
    else:
        rows = rows.exclude(status=QualityRecallCampaign.Status.SUPERSEDED)
    items = []
    # Apply exact identity/row authorization before the page limit. JSON text
    # searches of OpenIDs are neither scope checks nor portable candidate filters.
    for campaign in rows.iterator(chunk_size=100):
        visible = visible_rows(campaign, viewer)
        if visible or viewer.metadata_access(campaign):
            items.append(_scoped_campaign(campaign, visible, viewer))
        if len(items) >= 100:
            break

    return JsonResponse({"code": 0, "msg": "", "data": {"items": items}}, status=200)


def campaign_detail(request, campaign_id):
    if request.method != "GET":
        return _error("仅支持GET")
    user, error = _user(request)
    if error:
        return error
    campaign = recall_campaign_queryset().filter(pk=campaign_id).first()
    if not campaign:
        return _error("召回批次不存在。")
    viewer = RecallViewer(user)
    visible = visible_rows(campaign, viewer)
    if not viewer.metadata_access(campaign) and not visible:
        return _error("当前账号无权查看该召回批次。")
    data = _scoped_campaign(campaign, visible, viewer)
    data["devices"] = [_device(item, user=user) for item in visible]
    data["batch_flow"] = decorate_batch_flow(
        get_recall_batch_groups(user=user, campaign=campaign, rows=visible),
        data["view_context"], viewer,
    )
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
    return JsonResponse({"code": 0, "msg": "", "data": _device(row, user=user)}, status=200)


def _post(request, callback):
    if request.method != "POST":
        return _error("仅支持POST")
    user, error = _user(request)
    if error:
        return error
    try:
        from quality_trace.services.recall_access_service import FACTORY_RECALL_OBSERVER_ROLES
        if RecallViewer(user).role in FACTORY_RECALL_OBSERVER_ROLES:
            raise QualityRecallError("本岗位仅接收召回知晓通知，无召回操作权限。")
        return callback(user, _data(request))
    except (QualityCaseError, QualityRecallError, TypeError, ValueError) as exc:
        return _error(exc)


@csrf_exempt
def campaign_launch(request, campaign_id):
    return _post(request, lambda user, data: JsonResponse({
        "code": 0, "msg": "召回批次已发布",
        "data": _campaign(launch_recall_campaign(user=user, campaign_id=campaign_id)),
    }))


def campaign_task_scope(request, campaign_id):
    if request.method != "GET":
        return _error("仅支持GET")
    user, error = _user(request)
    if error:
        return error
    try:
        data = get_recall_task_scope(user=user, campaign_id=campaign_id)
    except QualityRecallError as exc:
        return _error(exc)
    return JsonResponse({"code": 0, "msg": "", "data": data}, status=200)


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
def campaign_disposition_start(request, campaign_id):
    return _post(request, lambda user, data: JsonResponse({
        "code": 0, "msg": "召回后处置已启动",
        "data": {"disposition_ids": [item.pk for item in start_recall_post_disposition(
            user=user, campaign_id=campaign_id, method=data.get("method"),
            recall_device_ids=data.get("recall_device_ids", []),
            note=data.get("note", ""),
        )]},
    }))


@csrf_exempt
def disposition_complete(request, disposition_id):
    return _post(request, lambda user, data: JsonResponse({
        "code": 0, "msg": "设备处置已完成",
        "data": {"disposition_id": complete_recall_device_disposition(
            user=user, disposition_id=disposition_id,
            completion_note=data.get("completion_note", ""),
            replacement_parts=data.get("replacement_parts", []),
        ).pk},
    }))


@csrf_exempt
def disposition_production_complete(request, disposition_id):
    return _post(request, lambda user, data: JsonResponse({
        "code": 0, "msg": "生产处置已完成",
        "data": {"disposition_id": complete_recall_disposition_production(
            user=user, disposition_id=disposition_id,
            completion_note=data.get("completion_note", ""),
        ).pk},
    }))


@csrf_exempt
def disposition_matching_confirm(request, disposition_id):
    return _post(request, lambda user, data: JsonResponse({
        "code": 0, "msg": "配套方案已确认",
        "data": {"disposition_id": confirm_recall_disposition_matching(
            user=user, disposition_id=disposition_id,
            replacement_parts=data.get("replacement_parts", []),
            note=data.get("note", ""),
        ).pk},
    }))


@csrf_exempt
def disposition_supplier_confirm(request, confirmation_id):
    return _post(request, lambda user, data: JsonResponse({
        "code": 0, "msg": "供应商负责人已确认",
        "data": {"confirmation_id": confirm_recall_disposition_supplier(
            user=user, confirmation_id=confirmation_id,
            note=data.get("note", ""),
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
def handover_delivery_method(request, handover_id):
    def callback(user, data):
        handover, changed = select_customer_delivery_method(
            user=user,
            handover_id=handover_id,
            delivery_method=data.get("delivery_method"),
            payload=data.get("payload"),
        )
        return JsonResponse({
            "code": 0,
            "msg": "召回交付方式已确认",
            "data": {
                "handover_id": handover.pk,
                "delivery_method": handover.delivery_method,
                "changed_before_execution": changed,
                "merchant_transfer_token": str(handover.merchant_transfer_token or ""),
            },
        })
    return _post(request, callback)


@csrf_exempt
def handover_direct_ship(request, handover_id):
    def callback(user, data):
        handover, leg = submit_customer_direct_shipment(
            user=user,
            handover_id=handover_id,
            logistics_provider=data.get("logistics_provider"),
            tracking_no=data.get("tracking_no"),
            evidence=data.get("evidence"),
            note=data.get("note"),
        )
        return JsonResponse({
            "code": 0,
            "msg": "直寄物流已提交，厂家到货岗位已收到任务",
            "data": {"handover_id": handover.pk, "transport_leg_id": leg.pk},
        })
    return _post(request, callback)


@csrf_exempt
def handover_pickup_accept(request, handover_id):
    def callback(user, data):
        handover, leg = accept_factory_pickup_request(
            user=user,
            handover_id=handover_id,
            pickup_scheduled_at=parse_datetime(str(data.get("pickup_scheduled_at") or "")),
            logistics_provider=data.get("logistics_provider"),
            note=data.get("note"),
        )
        return JsonResponse({
            "code": 0,
            "msg": "厂家已受理上门取件",
            "data": {"handover_id": handover.pk, "transport_leg_id": leg.pk},
        })
    return _post(request, callback)


@csrf_exempt
def handover_pickup_collected(request, handover_id):
    def callback(user, data):
        handover, leg = submit_customer_pickup_shipment(
            user=user,
            handover_id=handover_id,
            logistics_provider=data.get("logistics_provider"),
            tracking_no=data.get("tracking_no"),
            evidence=data.get("evidence"),
        )
        return JsonResponse({
            "code": 0,
            "msg": "已确认设备交付物流，厂家待收货岗位已收到任务",
            "data": {"handover_id": handover.pk, "transport_leg_id": leg.pk},
        })
    return _post(request, callback)


handover_pickup_dispatched = handover_pickup_collected


@csrf_exempt
def merchant_transfer_accept(request):
    if request.method == "GET":
        user, error = _user(request)
        if error:
            return error
        try:
            token = UUID(str(request.GET.get("token", "")))
        except ValueError:
            return _error("临时召回流转码无效。")
        handover = QualityRecallCustomerHandover.objects.select_related(
            "campaign", "recall_device", "merchant_organization",
        ).filter(merchant_transfer_token=token, campaign__status="ACTIVE",
                 delivery_method="MERCHANT_HANDOVER").exclude(status="CANCELLED").first()
        if not handover:
            return _error("临时召回流转码已失效，请让用户刷新交付方式。")
        viewer = RecallViewer(user)
        if (not viewer.valid or viewer.kind != "MERCHANT"
                or viewer.role not in MERCHANT_RECALL_ROLES
                or viewer.organization_id != handover.merchant_organization_id):
            return _error("只有设备来源商家的授权岗位可以查看并接管此设备。")
        received = handover.status == "MERCHANT_RECEIVED"
        if not received and (handover.method_locked_at or not handover.merchant_transfer_expires_at
                             or handover.merchant_transfer_expires_at <= timezone.now()):
            return _error("临时召回流转码已失效，请让用户刷新交付方式。")
        return JsonResponse({"code": 0, "msg": "", "data": {
            "recall_device_id": handover.recall_device_id,
            "campaign_id": handover.campaign_id, "recall_no": handover.campaign.recall_no,
            "sn": handover.recall_device.sn_snapshot,
            "product_model": handover.recall_device.product_model_snapshot,
            "merchant_name": handover.merchant_organization.name,
            "already_received": received, "can_accept": not received,
        }})
    def callback(user, data):
        handover = accept_merchant_handover_by_token(
            user=user,
            token=data.get("token"),
            receipt_note=data.get("receipt_note"),
        )
        return JsonResponse({
            "code": 0,
            "msg": "召回设备已由来源商家接管",
            "data": {
                "handover_id": handover.pk,
                "recall_device_id": handover.recall_device_id,
            },
        })
    return _post(request, callback)


def merchant_transfer_qrcode(request, token):
    handover = QualityRecallCustomerHandover.objects.filter(
        merchant_transfer_token=token,
        campaign__status=QualityRecallCampaign.Status.ACTIVE,
        delivery_method=QualityRecallCustomerHandover.DeliveryMethod.MERCHANT_HANDOVER,
        method_locked_at__isnull=True,
        merchant_transfer_expires_at__gt=timezone.now(),
    ).first()
    if not handover:
        return JsonResponse(
            {"code": 410, "msg": "临时召回流转码已失效", "data": {}},
            status=410,
        )
    cache_key = f"quality-recall:merchant-transfer:minicode:{token}"
    cached = cache.get(cache_key)
    if cached is None:
        try:
            cached = generate_unlimited_miniprogram_code(
                scene=token.hex,
                page="pages/quality_trace/recall_handover_accept/recall_handover_accept",
            )
        except WeChatMiniProgramCodeError as exc:
            return JsonResponse(
                {"code": 503, "msg": str(exc), "data": {}}, status=503,
            )
        remaining = max(
            1,
            int((handover.merchant_transfer_expires_at - timezone.now()).total_seconds()),
        )
        cache.set(cache_key, cached, timeout=min(remaining, 10 * 60))
    image_data, image_type = cached
    response = HttpResponse(image_data, content_type=image_type)
    response["Cache-Control"] = "no-store, max-age=0"
    return response


@csrf_exempt
def campaign_batch_operation(request, campaign_id):
    return _post(request, lambda user, data: JsonResponse({
        "code": 0, "msg": "召回批量操作已完成",
        "data": execute_recall_batch(
            user=user, campaign_id=campaign_id,
            action=data.get("action"), recall_device_ids=data.get("recall_device_ids"),
            client_request_id=data.get("client_request_id"), evidence=data.get("evidence"),
            logistics_provider=data.get("logistics_provider", ""),
            tracking_no=data.get("tracking_no", ""),
            isolation_location=data.get("isolation_location", ""),
            receipt_note=data.get("receipt_note", ""),
        ),
    }))


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
def campaign_inventory_confirm(request, campaign_id):
    return _post(request, lambda user, data: JsonResponse({
        "code": 0,
        "msg": "本批召回设备已全部确认入库记账",
        "data": {"confirmation_id": confirm_recall_inventory_batch(
            user=user, campaign_id=campaign_id, note=data.get("note", ""),
        ).pk},
    }))


@csrf_exempt
def unreachable_decide(request, recall_device_id):
    return _post(request, lambda user, data: JsonResponse({"code": 0, "msg": "无法联系审批已记录", "data": {"decision_id": decide_unreachable_device(
        user=user, recall_device_id=recall_device_id,
        decision=data.get("decision"), reason=data.get("reason"),
    ).pk}}))
