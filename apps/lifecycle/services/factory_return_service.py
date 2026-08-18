from collections import defaultdict
from pathlib import Path
import uuid

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from account.models import UserOrganization
from account.services.auth_service import get_active_identity
from lifecycle.models import (
    DeviceState,
    FactoryReturnDocument,
    FactoryReturnInventoryItem,
    FactoryReturnProcess,
    GoodsTransfer,
    GoodsTransferAccountCase,
    GoodsTransferItem,
    LifecycleEvent,
    OrganizationDevice,
    QRCode,
)
from workflow.models import WorkItem
from workflow.services.workflow_service import publish_workitem


SALES_NOTICE_SUBMITTED = "FACTORY_RETURN_SALES_NOTICE_SUBMITTED"
PICKUP_WORK_TYPE = "FACTORY_RETURN_PICKUP"
SALES_NOTICE_PICKED_UP = "FACTORY_RETURN_SALES_NOTICE_PICKED_UP"
POSTING_WORK_TYPE = "FACTORY_RETURN_POSTING"
STOCK_NOTICE_WORK_TYPE = "FACTORY_RETURN_STOCK_NOTICE"
DISASSEMBLY_PRODUCTION_WORK_TYPE = "FACTORY_RETURN_DISASSEMBLY_PRODUCTION"
DISASSEMBLY_MATCHING_WORK_TYPE = "FACTORY_RETURN_DISASSEMBLY_MATCHING"
REPAIR_PRODUCTION_WORK_TYPE = "FACTORY_RETURN_REPAIR_PRODUCTION"
REPAIR_MATCHING_WORK_TYPE = "FACTORY_RETURN_REPAIR_MATCHING"
RETURN_DOCUMENT_WORK_TYPE = "FACTORY_RETURN_DOCUMENT_UPLOAD"
RETURN_LOGISTICS_NOTICE_TYPE = "FACTORY_RETURN_LOGISTICS_DOCUMENT_NOTICE"
RETURN_GOODS_LIST_NOTICE_TYPE = "FACTORY_RETURN_GOODS_LIST_NOTICE"

MERCHANT_RETURN_DOCUMENT_ROLES = {
    "merchant_owner",
    "merchant_sales",
    "merchant_stock",
}
MAX_RETURN_DOCUMENT_SIZE = 10 * 1024 * 1024

DOCUMENT_KIND_CONFIG = {
    FactoryReturnDocument.DocumentKind.LOGISTICS: {
        "label": "物流单据",
        "factory_role": "factory_logistics",
        "notice_type": RETURN_LOGISTICS_NOTICE_TYPE,
        "notice_title": "商家已上传退货物流单据",
        "file_prefix": "logistics",
    },
    FactoryReturnDocument.DocumentKind.GOODS_LIST: {
        "label": "货品清单",
        "factory_role": "factory_sales_assistant",
        "notice_type": RETURN_GOODS_LIST_NOTICE_TYPE,
        "notice_title": "商家已上传退货货品清单",
        "file_prefix": "goods-list",
    },
}

FACTORY_RETURN_VIEW_ROLES = {
    "factory_sales",
    "factory_logistics",
    "factory_sales_assistant",
    "factory_stock",
    "factory_matching",
    "factory_production",
}
POSTING_STOCK_STATUSES = {
    FactoryReturnInventoryItem.StockStatus.PENDING_DISASSEMBLY,
    FactoryReturnInventoryItem.StockStatus.PENDING_REISSUE,
    FactoryReturnInventoryItem.StockStatus.PENDING_REPAIR,
}

INTERNAL_BRANCH_CONFIG = {
    "REPAIR": {
        "production_type": REPAIR_PRODUCTION_WORK_TYPE,
        "production_suffix": "repair-production",
        "matching_type": REPAIR_MATCHING_WORK_TYPE,
        "matching_suffix": "repair-matching",
        "matching_title": "退货修复物料待配套处理",
    },
    "DISASSEMBLY": {
        "production_type": DISASSEMBLY_PRODUCTION_WORK_TYPE,
        "production_suffix": "disassembly-production",
        "matching_type": DISASSEMBLY_MATCHING_WORK_TYPE,
        "matching_suffix": "disassembly-matching",
        "matching_title": "退货拆机三包物料待处理",
    },
}


class FactoryReturnProcessError(Exception):
    pass


def _display_name(user):
    return user.wx_nickname or user.get_full_name() or user.username


def _factory_identity(user, allowed_roles=None):
    identity = get_active_identity(user)
    if (
        not identity
        or not identity.is_active
        or identity.organization.organization_type.code != "OWNER"
        or (allowed_roles and identity.role.code not in allowed_roles)
    ):
        raise FactoryReturnProcessError("当前账号没有厂家退货处理权限。")
    return identity


def _role_openids(organization, role_codes):
    return tuple(
        UserOrganization.objects.filter(
            organization=organization,
            is_active=True,
            role__code__in=role_codes,
            role__is_active=True,
            user__is_active=True,
        )
        .exclude(user__wx_openid__isnull=True)
        .exclude(user__wx_openid="")
        .values_list("user__wx_openid", flat=True)
        .distinct()
    )


def _process_link(transfer):
    return (
        "/pages/stock/factory_return_process/factory_return_process"
        f"?transfer_id={transfer.pk}"
    )


def _detail_link(transfer):
    return (
        "/pages/stock/goods_transfer_detail/goods_transfer_detail"
        f"?transfer_id={transfer.pk}"
    )


def _task_payload(transfer, **extra):
    payload = {
        "goods_transfer_id": transfer.pk,
        "flow_no": transfer.flow_no,
        "from_organization_id": transfer.from_organization_id,
        "from_organization_name": transfer.from_organization.name,
        "to_organization_id": transfer.to_organization_id,
        "to_organization_name": transfer.to_organization.name,
        "device_count": transfer.items.count(),
    }
    payload.update(extra)
    return payload


def _validate_factory_return(transfer, organization_id=None):
    if (
        transfer.flow_type != GoodsTransfer.FlowType.RETURN
        or transfer.to_organization.organization_type.code != "OWNER"
    ):
        raise FactoryReturnProcessError("该商品流转不是商家退回厂家。")
    if organization_id and transfer.to_organization_id != organization_id:
        raise FactoryReturnProcessError("该退货批次不属于当前厂家。")


def _publish_notice(transfer, work_type, title, summary, actor, suffix):
    recipients = _role_openids(transfer.to_organization, {"factory_sales"})
    if not recipients:
        return None
    return publish_workitem(
        domain="LOGISTICS",
        type=work_type,
        biz_id=transfer.flow_no,
        biz_ref=f"lifecycle.GoodsTransfer:{transfer.pk}",
        uniq_key=f"lifecycle:factory-return:{transfer.pk}:sales:{suffix}",
        title=title,
        summary=summary,
        link=_process_link(transfer),
        recipients_openids=recipients,
        need_action=False,
        action_mode="EACH_ACK",
        payload=_task_payload(transfer, stage=suffix),
        actor_openid=actor.wx_openid or "",
        mark_workitem_done_immediately=True,
    )


def _publish_action(
    transfer,
    *,
    work_type,
    role_codes,
    key_suffix,
    title,
    summary,
    actor,
    payload=None,
):
    recipients = _role_openids(transfer.to_organization, role_codes)
    if not recipients:
        role_text = "、".join(sorted(role_codes))
        raise FactoryReturnProcessError(
            f"厂家缺少可接收任务的有效岗位账号：{role_text}。"
        )
    return publish_workitem(
        domain="LOGISTICS",
        type=work_type,
        biz_id=transfer.flow_no,
        biz_ref=f"lifecycle.GoodsTransfer:{transfer.pk}",
        uniq_key=f"lifecycle:factory-return:{transfer.pk}:{key_suffix}",
        title=title,
        summary=summary,
        link=_process_link(transfer),
        recipients_openids=recipients,
        need_action=True,
        action_mode="ANYONE_DO",
        payload=_task_payload(transfer, **(payload or {})),
        actor_openid=actor.wx_openid or "",
        mark_workitem_done_immediately=False,
    )


def _return_document_key(transfer):
    return f"lifecycle:factory-return:{transfer.pk}:documents"


def _publish_return_document_action(transfer, actor):
    recipient_openid = (transfer.initiated_by.wx_openid or "").strip()
    if not recipient_openid:
        raise FactoryReturnProcessError("本次退货发起账号缺少有效微信OpenID。")
    return publish_workitem(
        domain="LOGISTICS",
        type=RETURN_DOCUMENT_WORK_TYPE,
        biz_id=transfer.flow_no,
        biz_ref=f"lifecycle.GoodsTransfer:{transfer.pk}",
        uniq_key=_return_document_key(transfer),
        title="退厂退货待补传物流单据",
        summary=(
            f"退至{transfer.to_organization.name}｜"
            f"共{transfer.items.count()}台"
        ),
        link=_detail_link(transfer),
        recipients_openids=(recipient_openid,),
        need_action=True,
        action_mode="ANYONE_DO",
        payload=_task_payload(
            transfer,
            stage="return-logistics-document",
            document_operation_owner_user_id=transfer.initiated_by_id,
            logistics_document_blocks_pickup=False,
        ),
        actor_openid=actor.wx_openid or "",
        mark_workitem_done_immediately=False,
    )


def _ensure_return_document_action(transfer, actor):
    existing = WorkItem.objects.filter(
        type=RETURN_DOCUMENT_WORK_TYPE,
        uniq_key=_return_document_key(transfer),
    ).first()
    if existing:
        return existing
    return _publish_return_document_action(transfer, actor)


def _refresh_existing_workitem(
    workitem,
    *,
    title=None,
    summary=None,
    payload_updates=None,
):
    """Refresh one existing inbox row without creating or reopening a task."""
    if not workitem:
        return None
    now = timezone.now()
    payload = dict(workitem.payload or {})
    payload.update(payload_updates or {})
    if title is not None:
        workitem.title = title
    if summary is not None:
        workitem.summary = summary
    workitem.payload = payload
    workitem.last_activity_at = now
    workitem.save(
        update_fields=(
            "title",
            "summary",
            "payload",
            "last_activity_at",
            "updated_at",
        )
    )
    workitem.user_tasks.exclude(state="DONE").update(
        state="NEW",
        read_at=None,
        display_title_snapshot=workitem.title,
        display_subtitle_snapshot=workitem.summary,
        cursor_time=now,
        updated_at=now,
    )
    workitem.user_tasks.filter(state="DONE").update(
        display_title_snapshot=workitem.title,
        display_subtitle_snapshot=workitem.summary,
        cursor_time=now,
        updated_at=now,
    )
    return workitem


def _refresh_document_consumers(transfer, document_kind):
    """Attach new documents to the original three factory-role tasks."""
    summary = (
        f"{transfer.from_organization.name}退回｜"
        f"共{transfer.items.count()}台"
    )
    stage = f"{document_kind.lower()}-uploaded"
    sales_notice = WorkItem.objects.select_for_update().filter(
        uniq_key=f"lifecycle:factory-return:{transfer.pk}:sales:submitted",
    ).first()
    _refresh_existing_workitem(
        sales_notice,
        title="商家退厂退货资料已更新",
        summary=summary,
        payload_updates={stage: True, "latest_document_kind": document_kind},
    )
    if document_kind == FactoryReturnDocument.DocumentKind.LOGISTICS:
        pickup = WorkItem.objects.select_for_update().filter(
            type=PICKUP_WORK_TYPE,
            uniq_key=f"lifecycle:factory-return:{transfer.pk}:pickup",
        ).first()
        _refresh_existing_workitem(
            pickup,
            title=(
                "商家退货待取回（物流单据已上传）"
                if pickup and pickup.status == "OPEN"
                else "退货已取回（物流单据已补充）"
            ),
            summary=summary,
            payload_updates={
                "logistics_document_uploaded": True,
                "latest_document_kind": document_kind,
            },
        )


def _complete_action(transfer, work_type, key_suffix, user, own_title):
    workitem = WorkItem.objects.select_for_update().filter(
        type=work_type,
        uniq_key=f"lifecycle:factory-return:{transfer.pk}:{key_suffix}",
    ).first()
    if not workitem or workitem.status != "OPEN":
        raise FactoryReturnProcessError("对应操作任务不存在或已经完成。")
    task = (
        workitem.user_tasks.select_for_update()
        .filter(to_openid=user.wx_openid, need_action=True)
        .exclude(state="DONE")
        .first()
    )
    if not task:
        raise FactoryReturnProcessError("当前账号不是该操作任务的处理人。")
    now = timezone.now()
    actor_name = _display_name(user)
    workitem.status = "DONE"
    workitem.done_by_openid = user.wx_openid or ""
    workitem.done_at = now
    workitem.last_activity_at = now
    workitem.save(
        update_fields=(
            "status",
            "done_by_openid",
            "done_at",
            "last_activity_at",
            "updated_at",
        )
    )
    workitem.user_tasks.filter(to_openid=user.wx_openid).update(
        state="DONE",
        need_action=False,
        done_at=now,
        done_reason="SELF_ACTION",
        display_title_snapshot=own_title,
        display_subtitle_snapshot="由你完成",
        cursor_time=now,
        updated_at=now,
    )
    workitem.user_tasks.exclude(to_openid=user.wx_openid).update(
        state="DONE",
        need_action=False,
        done_at=now,
        done_reason="OTHER_ACTION",
        display_title_snapshot=f"已由{actor_name}完成",
        display_subtitle_snapshot="同岗位无需重复操作",
        cursor_time=now,
        updated_at=now,
    )


def initialize_factory_return_process(
    *,
    transfer,
    items,
    actor,
    goods_list_document,
    goods_list_client_request_id,
):
    """最终提交退货时一次建立单据、账务引用和三个厂家岗位任务。"""
    _validate_factory_return(transfer)
    process, _created = FactoryReturnProcess.objects.get_or_create(
        transfer=transfer,
        defaults={"phase": FactoryReturnProcess.Phase.WAITING_PICKUP},
    )
    _save_return_document(
        process=process,
        user=actor,
        document_kind=FactoryReturnDocument.DocumentKind.GOODS_LIST,
        client_request_id=goods_list_client_request_id,
        document=goods_list_document,
        remark=transfer.remark,
    )
    for item in items:
        batch = item.device.batch
        FactoryReturnInventoryItem.objects.get_or_create(
            transfer_item=item,
            defaults={
                "process": process,
                "source_product_model": item.device.product_model,
                "source_production_batch": batch,
                "source_bom": batch.bom,
            },
        )
    from lifecycle.services.goods_transfer_account_service import (
        initialize_goods_transfer_accounts,
    )

    cases, _account_created = initialize_goods_transfer_accounts(
        transfer=transfer,
        actor=actor,
    )
    return_case = next(
        (
            case
            for case in cases
            if case.case_type == GoodsTransferAccountCase.CaseType.RETURN_CREDIT
        ),
        None,
    )
    if not return_case:
        raise FactoryReturnProcessError("无法建立本次退厂退货冲抵记录。")
    if process.account_case_id != return_case.pk:
        process.account_case = return_case
        process.save(update_fields=("account_case", "updated_at"))

    _ensure_return_document_action(transfer, actor)
    summary = (
        f"{transfer.from_organization.name}退回｜"
        f"共{len(items)}台"
    )
    _publish_notice(
        transfer,
        SALES_NOTICE_SUBMITTED,
        "商家已发起退厂退货",
        summary,
        actor,
        "submitted",
    )
    _publish_action(
        transfer,
        work_type=PICKUP_WORK_TYPE,
        role_codes={"factory_logistics"},
        key_suffix="pickup",
        title="商家退货待取回",
        summary=summary,
        actor=actor,
    )
    _publish_action(
        transfer,
        work_type=POSTING_WORK_TYPE,
        role_codes={"factory_sales_assistant"},
        key_suffix="posting",
        title="退货待物流取回，随后下账处理",
        summary=summary,
        actor=actor,
        payload={
            "blocked_by_pickup": True,
            "credit_amount_recorded": False,
            "inventory_posted": False,
            "required_substeps": ["CREDIT_AMOUNT", "INVENTORY_POSTING"],
        },
    )
    return process


def _return_document_identity(user):
    identity = get_active_identity(user)
    if (
        not identity
        or not identity.is_active
        or identity.organization.organization_type.code != "MERCHANT"
        or identity.role.code not in MERCHANT_RETURN_DOCUMENT_ROLES
    ):
        raise FactoryReturnProcessError("当前岗位不能上传退厂退货单据。")
    return identity


def _return_document_access(identity, transfer, document_kind):
    config = DOCUMENT_KIND_CONFIG[document_kind]
    merchant_access = bool(
        identity.organization_id == transfer.from_organization_id
        and identity.organization.organization_type.code == "MERCHANT"
        and identity.role.code in MERCHANT_RETURN_DOCUMENT_ROLES
    )
    factory_access = bool(
        identity.organization_id == transfer.to_organization_id
        and identity.organization.organization_type.code == "OWNER"
        and identity.role.code in {config["factory_role"], "factory_sales"}
    )
    return merchant_access or factory_access


def _validate_return_document(document, document_kind):
    label = DOCUMENT_KIND_CONFIG[document_kind]["label"]
    if not document:
        raise FactoryReturnProcessError(f"请上传退货{label}。")
    if document.size <= 0 or document.size > MAX_RETURN_DOCUMENT_SIZE:
        raise FactoryReturnProcessError(f"退货{label}不能超过10MB。")
    header = document.read(12)
    document.seek(0)
    original_name = Path(document.name).name[:255]
    if header.startswith(b"%PDF-"):
        return (
            ".pdf",
            original_name,
            "application/pdf",
            FactoryReturnDocument.FileType.PDF,
        )
    if header.startswith(b"\xff\xd8\xff"):
        suffix, content_type = ".jpg", "image/jpeg"
    elif header.startswith(b"\x89PNG\r\n\x1a\n"):
        suffix, content_type = ".png", "image/png"
    elif header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        suffix, content_type = ".webp", "image/webp"
    else:
        raise FactoryReturnProcessError(
            f"退货{label}只支持JPG、PNG、WEBP或PDF格式。"
        )
    return (
        suffix,
        original_name,
        content_type,
        FactoryReturnDocument.FileType.IMAGE,
    )


def _save_return_document(
    *,
    process,
    user,
    document_kind,
    client_request_id,
    document,
    remark="",
):
    """Store one idempotent private document inside the surrounding transaction."""
    label = DOCUMENT_KIND_CONFIG[document_kind]["label"]
    request_id = str(client_request_id or "").strip()
    if not request_id or len(request_id) > 64:
        raise FactoryReturnProcessError(f"{label}请求标识无效，请重新进入页面。")
    existing = process.documents.filter(
        document_kind=document_kind,
        client_request_id=request_id,
    ).select_related("uploaded_by").first()
    if existing:
        return existing, False

    suffix, original_name, content_type, file_type = _validate_return_document(
        document,
        document_kind,
    )
    sequence_no = (
        process.documents.filter(
            document_kind=document_kind,
        ).aggregate(value=Max("sequence_no"))["value"]
        or 0
    ) + 1
    row = FactoryReturnDocument(
        process=process,
        document_kind=document_kind,
        sequence_no=sequence_no,
        client_request_id=request_id,
        original_name=original_name,
        file_type=file_type,
        content_type=content_type,
        file_size=document.size,
        remark=str(remark or "").strip()[:255],
        uploaded_by=user,
    )
    file_prefix = DOCUMENT_KIND_CONFIG[document_kind]["file_prefix"]
    row.document_file.save(
        f"{file_prefix}-{sequence_no}-{uuid.uuid4().hex}{suffix}",
        document,
        save=False,
    )
    row.save()
    return row, True


def _serialize_return_document(document):
    transfer_id = document.process.transfer_id
    return {
        "id": document.pk,
        "document_kind": document.document_kind,
        "document_kind_name": document.get_document_kind_display(),
        "sequence_no": document.sequence_no,
        "original_name": document.original_name,
        "file_type": document.file_type,
        "content_type": document.content_type,
        "file_size": document.file_size,
        "remark": document.remark,
        "uploaded_by_name": _display_name(document.uploaded_by),
        "uploaded_at": document.uploaded_at.isoformat(),
        "preview_path": (
            f"/lifecycle/goods-transfers/{transfer_id}/factory-return/"
            f"{document.document_kind.lower().replace('_', '-')}-documents/"
            f"{document.pk}/file/"
        ),
    }


def serialize_factory_return_documents(process, document_kind):
    return [
        _serialize_return_document(document)
        for document in process.documents.select_related(
            "uploaded_by",
        ).filter(
            document_kind=document_kind,
        ).order_by("sequence_no", "id")
    ]


def factory_return_document_actions(identity, transfer):
    is_factory_return = bool(
        transfer.flow_type == GoodsTransfer.FlowType.RETURN
        and transfer.to_organization.organization_type.code == "OWNER"
    )
    after_submit = transfer.status in {
        GoodsTransfer.Status.PENDING_FACTORY_PICKUP,
        GoodsTransfer.Status.PENDING_FACTORY_POSTING,
        GoodsTransfer.Status.COMPLETED,
    }
    can_view_logistics = bool(
        identity
        and is_factory_return
        and after_submit
        and _return_document_access(
            identity,
            transfer,
            FactoryReturnDocument.DocumentKind.LOGISTICS,
        )
    )
    can_view_goods_list = bool(
        identity
        and is_factory_return
        and after_submit
        and _return_document_access(
            identity,
            transfer,
            FactoryReturnDocument.DocumentKind.GOODS_LIST,
        )
    )
    is_operation_owner = bool(
        identity
        and identity.user_id == transfer.initiated_by_id
        and identity.organization_id == transfer.from_organization_id
        and identity.role.code in MERCHANT_RETURN_DOCUMENT_ROLES
    )
    workitem = WorkItem.objects.filter(
        type=RETURN_DOCUMENT_WORK_TYPE,
        uniq_key=_return_document_key(transfer),
    ).first()
    process = FactoryReturnProcess.objects.filter(transfer=transfer).first()
    uploaded_kinds = set()
    if process:
        uploaded_kinds = set(
            process.documents.values_list("document_kind", flat=True).distinct()
        )
    return {
        "can_view_logistics": can_view_logistics,
        "can_view_goods_list": can_view_goods_list,
        "can_upload_logistics": bool(can_view_logistics and is_operation_owner),
        "can_upload_goods_list": bool(can_view_goods_list and is_operation_owner),
        "operation_owner_user_id": transfer.initiated_by_id,
        "is_operation_owner": is_operation_owner,
        "logistics_uploaded": (
            FactoryReturnDocument.DocumentKind.LOGISTICS in uploaded_kinds
        ),
        "goods_list_uploaded": (
            FactoryReturnDocument.DocumentKind.GOODS_LIST in uploaded_kinds
        ),
        "task_status": workitem.status if workitem else "NOT_CREATED",
        "task_completed": bool(workitem and workitem.status == "DONE"),
    }


@transaction.atomic
def upload_factory_return_document(
    *,
    user,
    transfer_id,
    document_kind,
    client_request_id,
    document,
    remark="",
):
    identity = _return_document_identity(user)
    document_kind = str(document_kind or "").strip().upper()
    if document_kind not in DOCUMENT_KIND_CONFIG:
        raise FactoryReturnProcessError("退厂退货单据类型无效。")
    label = DOCUMENT_KIND_CONFIG[document_kind]["label"]
    transfer = (
        GoodsTransfer.objects.select_for_update()
        .select_related(
            "from_organization__organization_type",
            "to_organization__organization_type",
            "initiated_by",
        )
        .filter(pk=transfer_id)
        .first()
    )
    if not transfer:
        raise FactoryReturnProcessError("退货商品流转单不存在。")
    _validate_factory_return(transfer)
    if identity.organization_id != transfer.from_organization_id:
        raise FactoryReturnProcessError(f"该退货{label}不属于当前商家。")
    if transfer.initiated_by_id != user.pk:
        raise FactoryReturnProcessError(
            "退货单据只由本次扫码商品流转的发起账号上传，无需同组织协同。"
        )
    if transfer.status not in {
        GoodsTransfer.Status.PENDING_FACTORY_PICKUP,
        GoodsTransfer.Status.PENDING_FACTORY_POSTING,
        GoodsTransfer.Status.COMPLETED,
    }:
        raise FactoryReturnProcessError("请先完成退货逐台扫码并提交。")
    process = _locked_process(transfer)
    row, created = _save_return_document(
        process=process,
        user=user,
        document_kind=document_kind,
        client_request_id=client_request_id,
        document=document,
        remark=remark,
    )
    if not created:
        return {
            "logistics_documents": serialize_factory_return_documents(
                process,
                FactoryReturnDocument.DocumentKind.LOGISTICS,
            ),
            "goods_list_documents": serialize_factory_return_documents(
                process,
                FactoryReturnDocument.DocumentKind.GOODS_LIST,
            ),
            "actions": factory_return_document_actions(identity, transfer),
        }, False

    _refresh_document_consumers(transfer, document_kind)
    workitem = _ensure_return_document_action(transfer, user)
    if (
        document_kind == FactoryReturnDocument.DocumentKind.LOGISTICS
        and workitem
        and workitem.status == "OPEN"
    ):
        _complete_action(
            transfer,
            RETURN_DOCUMENT_WORK_TYPE,
            "documents",
            user,
            "退货物流单据已上传",
        )
    return {
        "logistics_documents": serialize_factory_return_documents(
            process,
            FactoryReturnDocument.DocumentKind.LOGISTICS,
        ),
        "goods_list_documents": serialize_factory_return_documents(
            process,
            FactoryReturnDocument.DocumentKind.GOODS_LIST,
        ),
        "actions": factory_return_document_actions(identity, transfer),
    }, True


def get_factory_return_document_file(
    *,
    user,
    transfer_id,
    document_id,
    document_kind,
):
    document_kind = str(document_kind or "").strip().upper()
    if document_kind not in DOCUMENT_KIND_CONFIG:
        raise FactoryReturnProcessError("退厂退货单据类型无效。")
    label = DOCUMENT_KIND_CONFIG[document_kind]["label"]
    identity = get_active_identity(user)
    if not identity or not identity.is_active:
        raise FactoryReturnProcessError(f"当前账号没有查看退货{label}权限。")
    document = (
        FactoryReturnDocument.objects.select_related(
            "process__transfer__from_organization__organization_type",
            "process__transfer__to_organization__organization_type",
        )
        .filter(
            pk=document_id,
            process__transfer_id=transfer_id,
            document_kind=document_kind,
        )
        .first()
    )
    if not document:
        raise FactoryReturnProcessError(f"退货{label}不存在。")
    transfer = document.process.transfer
    if not _return_document_access(identity, transfer, document_kind):
        raise FactoryReturnProcessError(f"当前账号没有查看退货{label}权限。")
    return document.document_file, document.original_name, document.content_type


def _locked_process(transfer, expected_phase=None):
    process = (
        FactoryReturnProcess.objects.select_for_update()
        .select_related("account_case")
        .filter(transfer=transfer)
        .first()
    )
    if not process:
        raise FactoryReturnProcessError("该退货批次尚未建立厂家处理流程。")
    if expected_phase and process.phase != expected_phase:
        raise FactoryReturnProcessError("该退货批次当前不在可操作阶段。")
    return process


def _sync_assistant_action(transfer, actor):
    """Keep both assistant substeps on the single task created at submission."""
    process = _locked_process(transfer)
    account_case = process.account_case
    pickup_done = bool(process.picked_at)
    amount_done = bool(
        account_case
        and account_case.status == GoodsTransferAccountCase.Status.CONFIRMED
        and account_case.confirmed_amount is not None
    )
    posting_done = process.phase == FactoryReturnProcess.Phase.POSTED
    workitem = WorkItem.objects.select_for_update().filter(
        type=POSTING_WORK_TYPE,
        uniq_key=f"lifecycle:factory-return:{transfer.pk}:posting",
    ).first()
    if not workitem:
        raise FactoryReturnProcessError("厂家销售助理任务不存在。")

    summary = (
        f"{transfer.from_organization.name}退回｜"
        f"共{transfer.items.count()}台"
    )
    if not pickup_done:
        title = "退货待物流取回，随后下账处理"
    elif not amount_done and not posting_done:
        title = "退货待填写冲抵金额并分类入库"
    elif amount_done and not posting_done:
        title = "退货待分类入库"
    elif posting_done and not amount_done:
        title = "退货待填写冲抵金额"
    else:
        title = "退货下账与分类入库已完成"

    _refresh_existing_workitem(
        workitem,
        title=title,
        summary=summary,
        payload_updates={
            "blocked_by_pickup": not pickup_done,
            "credit_amount_recorded": amount_done,
            "inventory_posted": posting_done,
            "required_substeps": ["CREDIT_AMOUNT", "INVENTORY_POSTING"],
        },
    )
    if amount_done and posting_done and workitem.status == "OPEN":
        _complete_action(
            transfer,
            POSTING_WORK_TYPE,
            "posting",
            actor,
            "退货下账与分类入库已完成",
        )
    return workitem


@transaction.atomic
def record_factory_return_credit_amount(*, user, transfer_id, amount):
    """厂家销售助理直接登记冲抵金额，不经过商家提交或双方确认。"""
    identity = _factory_identity(user, {"factory_sales_assistant"})
    transfer = (
        GoodsTransfer.objects.select_for_update()
        .select_related(
            "from_organization__organization_type",
            "to_organization__organization_type",
            "initiated_by",
        )
        .filter(pk=transfer_id)
        .first()
    )
    if not transfer:
        raise FactoryReturnProcessError("退货商品流转单不存在。")
    _validate_factory_return(transfer, identity.organization_id)
    process = _locked_process(transfer)
    if not process.picked_at:
        raise FactoryReturnProcessError("厂家物流尚未确认取回，不能填写冲抵金额。")
    account_case = process.account_case
    if not account_case:
        raise FactoryReturnProcessError("本次退货尚未建立冲抵记录。")
    account_case = GoodsTransferAccountCase.objects.select_for_update().get(
        pk=account_case.pk
    )
    if (
        account_case.status == GoodsTransferAccountCase.Status.CONFIRMED
        and account_case.confirmed_amount is not None
    ):
        return _serialize_process(process, identity), False

    now = timezone.now()
    account_case.proposed_amount = amount
    account_case.confirmed_amount = amount
    account_case.status = GoodsTransferAccountCase.Status.CONFIRMED
    account_case.submitted_by = user
    account_case.submitted_at = now
    account_case.confirmed_by = user
    account_case.confirmed_at = now
    account_case.rejected_by = None
    account_case.rejected_at = None
    account_case.rejection_reason = ""
    account_case.save(
        update_fields=(
            "proposed_amount",
            "confirmed_amount",
            "status",
            "submitted_by",
            "submitted_at",
            "confirmed_by",
            "confirmed_at",
            "rejected_by",
            "rejected_at",
            "rejection_reason",
            "updated_at",
        )
    )
    process.account_case = account_case
    _sync_assistant_action(transfer, user)
    process.refresh_from_db()
    return _serialize_process(process, identity), True


@transaction.atomic
def confirm_factory_return_pickup(*, user, transfer_id):
    identity = _factory_identity(user, {"factory_logistics"})
    transfer = (
        GoodsTransfer.objects.select_for_update()
        .select_related(
            "from_organization__organization_type",
            "to_organization__organization_type",
            "initiated_by",
        )
        .filter(pk=transfer_id)
        .first()
    )
    if not transfer:
        raise FactoryReturnProcessError("退货商品流转单不存在。")
    _validate_factory_return(transfer, identity.organization_id)
    if transfer.status in {
        GoodsTransfer.Status.PENDING_FACTORY_POSTING,
        GoodsTransfer.Status.COMPLETED,
    }:
        return _serialize_process(_locked_process(transfer), identity), False
    if transfer.status != GoodsTransfer.Status.PENDING_FACTORY_PICKUP:
        raise FactoryReturnProcessError("当前退货批次不在待取回状态。")
    process = _locked_process(
        transfer,
        FactoryReturnProcess.Phase.WAITING_PICKUP,
    )
    items = list(
        GoodsTransferItem.objects.select_for_update()
        .select_related("device__qrcode")
        .filter(transfer=transfer)
        .order_by("id")
    )
    device_ids = [item.device_id for item in items]
    ownerships = {
        row.device_id: row
        for row in OrganizationDevice.objects.select_for_update().filter(
            device_id__in=device_ids
        )
    }
    states = {
        row.device_id: row
        for row in DeviceState.objects.select_for_update().filter(
            device_id__in=device_ids
        )
    }
    for item in items:
        ownership = ownerships.get(item.device_id)
        state = states.get(item.device_id)
        if (
            not ownership
            or ownership.organization_id != transfer.from_organization_id
            or ownership.status != "RETURNED"
            or not state
            or state.current_organization_id != transfer.from_organization_id
            or state.status != "SHIPPED"
        ):
            raise FactoryReturnProcessError(
                f"设备{item.device.sn}状态已变化，不能确认取回。"
            )
    if not _role_openids(
        transfer.to_organization,
        {"factory_sales_assistant"},
    ):
        raise FactoryReturnProcessError("厂家缺少有效的销售助理账号。")
    now = timezone.now()
    for item in items:
        qrcode = QRCode.objects.select_for_update().filter(
            device=item.device
        ).first()
        if qrcode and qrcode.status != "DISABLED":
            qrcode.status = "DISABLED"
            qrcode.disabled_at = now
            qrcode.save(update_fields=("status", "disabled_at", "updated_at"))
        event = LifecycleEvent.objects.create(
            device=item.device,
            event_type="FACTORY_RETURN_PICKUP",
            operator=user,
            organization=transfer.to_organization,
            from_organization=transfer.from_organization,
            to_organization=transfer.to_organization,
            data={
                "goods_transfer_id": transfer.pk,
                "goods_transfer_item_id": item.pk,
                "flow_no": transfer.flow_no,
                "old_qr_code": qrcode.code if qrcode else "",
                "old_qr_disabled": bool(qrcode),
                "lifecycle_closed": False,
            },
        )
        DeviceState.objects.filter(device=item.device).update(
            current_organization=transfer.to_organization,
            status="RETURNED_TO_FACTORY",
            last_event=event,
            last_event_time=now,
            updated_at=now,
        )
        OrganizationDevice.objects.filter(device=item.device).update(
            organization=transfer.to_organization,
            status="RETURNED_TO_FACTORY",
            received_at=now,
            last_event=event,
            updated_at=now,
        )
        item.status = GoodsTransferItem.Status.PENDING_FACTORY_POSTING
        item.receipt_event = event
        item.save(update_fields=("status", "receipt_event", "updated_at"))

    _complete_action(transfer, PICKUP_WORK_TYPE, "pickup", user, "退货已取回")
    transfer.status = GoodsTransfer.Status.PENDING_FACTORY_POSTING
    transfer.received_by = user
    transfer.received_at = now
    transfer.factory_lifecycle_terminated = False
    transfer.save(
        update_fields=(
            "status",
            "received_by",
            "received_at",
            "factory_lifecycle_terminated",
            "updated_at",
        )
    )
    process.phase = FactoryReturnProcess.Phase.WAITING_POSTING
    process.picked_by = user
    process.picked_at = now
    process.save(update_fields=("phase", "picked_by", "picked_at", "updated_at"))

    summary = (
        f"{transfer.from_organization.name}退回｜"
        f"共{len(items)}台｜待下账分类"
    )
    sales_notice = WorkItem.objects.select_for_update().filter(
        uniq_key=f"lifecycle:factory-return:{transfer.pk}:sales:submitted",
    ).first()
    _refresh_existing_workitem(
        sales_notice,
        title="厂家物流已取回商家退货",
        summary=summary,
        payload_updates={"stage": "picked-up", "pickup_confirmed": True},
    )
    _sync_assistant_action(transfer, user)
    return _serialize_process(process, identity), True


def _publish_stock_notice(transfer, actor, counts):
    recipients = _role_openids(transfer.to_organization, {"factory_stock"})
    if not recipients:
        raise FactoryReturnProcessError("厂家缺少有效的库管账号。")
    summary = (
        f"待拆机{counts.get('PENDING_DISASSEMBLY', 0)}台｜"
        f"待流转{counts.get('PENDING_REISSUE', 0)}台｜"
        f"待修复{counts.get('PENDING_REPAIR', 0)}台"
    )
    return publish_workitem(
        domain="LOGISTICS",
        type=STOCK_NOTICE_WORK_TYPE,
        biz_id=transfer.flow_no,
        biz_ref=f"lifecycle.GoodsTransfer:{transfer.pk}",
        uniq_key=f"lifecycle:factory-return:{transfer.pk}:stock-notice",
        title="商家退货已分类入库",
        summary=summary,
        link=_process_link(transfer),
        recipients_openids=recipients,
        need_action=False,
        action_mode="EACH_ACK",
        payload=_task_payload(transfer, classification_counts=dict(counts)),
        actor_openid=actor.wx_openid or "",
        mark_workitem_done_immediately=True,
    )


def _publish_followup_tasks(transfer, actor, grouped):
    task_definitions = {
        FactoryReturnInventoryItem.StockStatus.PENDING_DISASSEMBLY: (
            (
                DISASSEMBLY_PRODUCTION_WORK_TYPE,
                {"factory_production"},
                "disassembly-production",
                "退货设备待制定拆机处理",
            ),
            (
                DISASSEMBLY_MATCHING_WORK_TYPE,
                {"factory_matching"},
                "disassembly-matching",
                "退货设备待制定拆机物料处理",
            ),
        ),
        FactoryReturnInventoryItem.StockStatus.PENDING_REPAIR: (
            (
                REPAIR_PRODUCTION_WORK_TYPE,
                {"factory_production"},
                "repair-production",
                "退货设备待制定修复方案",
            ),
            (
                REPAIR_MATCHING_WORK_TYPE,
                {"factory_matching"},
                "repair-matching",
                "退货设备待评估修复物料",
            ),
        ),
    }
    for stock_status, definitions in task_definitions.items():
        inventory_items = grouped.get(stock_status, [])
        if not inventory_items:
            continue
        payload = {
            "stock_status": stock_status,
            "factory_return_inventory_item_ids": [row.pk for row in inventory_items],
            "goods_transfer_item_ids": [row.transfer_item_id for row in inventory_items],
        }
        summary = f"{transfer.from_organization.name}退回｜共{len(inventory_items)}台"
        for work_type, role_codes, suffix, title in definitions:
            workitem = _publish_action(
                transfer,
                work_type=work_type,
                role_codes=role_codes,
                key_suffix=suffix,
                title=title,
                summary=summary,
                actor=actor,
                payload=payload,
            )
            if role_codes == {"factory_matching"}:
                waiting_title = "等待生产经理提交处置结果"
                _refresh_existing_workitem(
                    workitem,
                    title=waiting_title,
                    payload_updates={
                        "blocked_by_production": True,
                        "matching_disposition_ready": False,
                    },
                )
                workitem.user_tasks.exclude(state="DONE").update(
                    need_action=False,
                    display_title_snapshot=waiting_title,
                )


@transaction.atomic
def post_factory_return_inventory(
    *,
    user,
    transfer_id,
    items,
    posting_remark="",
):
    identity = _factory_identity(user, {"factory_sales_assistant"})
    transfer = (
        GoodsTransfer.objects.select_for_update()
        .select_related(
            "from_organization__organization_type",
            "to_organization__organization_type",
            "initiated_by",
        )
        .filter(pk=transfer_id)
        .first()
    )
    if not transfer:
        raise FactoryReturnProcessError("退货商品流转单不存在。")
    _validate_factory_return(transfer, identity.organization_id)
    process = _locked_process(transfer)
    if process.phase == FactoryReturnProcess.Phase.POSTED:
        return _serialize_process(process, identity), False
    if (
        transfer.status != GoodsTransfer.Status.PENDING_FACTORY_POSTING
        or process.phase != FactoryReturnProcess.Phase.WAITING_POSTING
    ):
        raise FactoryReturnProcessError("厂家物流尚未确认取回，不能分类入库。")

    inventory_items = list(
        FactoryReturnInventoryItem.objects.select_for_update()
        .select_related("transfer_item__device")
        .filter(process=process)
        .order_by("transfer_item_id")
    )
    expected_ids = {row.transfer_item_id for row in inventory_items}
    submitted = list(items or [])
    submitted_ids = [int(row.get("item_id") or 0) for row in submitted]
    if len(submitted_ids) != len(set(submitted_ids)):
        raise FactoryReturnProcessError("同一设备不能重复分类。")
    if set(submitted_ids) != expected_ids:
        raise FactoryReturnProcessError("必须一次性完成本批全部设备的分类入库。")
    submitted_by_id = {int(row["item_id"]): row for row in submitted}
    for row in submitted:
        stock_status = str(row.get("stock_status") or "").upper()
        if stock_status not in POSTING_STOCK_STATUSES:
            raise FactoryReturnProcessError("请选择有效的退货入库分类。")

    # 先验证所有必需岗位，避免分类后才发现任务无法分发。
    if not _role_openids(transfer.to_organization, {"factory_stock"}):
        raise FactoryReturnProcessError("厂家缺少有效的库管账号。")
    needs_followup = any(
        str(row.get("stock_status") or "").upper()
        in {
            FactoryReturnInventoryItem.StockStatus.PENDING_DISASSEMBLY,
            FactoryReturnInventoryItem.StockStatus.PENDING_REPAIR,
        }
        for row in submitted
    )
    if needs_followup:
        if not _role_openids(transfer.to_organization, {"factory_production"}):
            raise FactoryReturnProcessError("厂家缺少有效的生产经理账号。")
        if not _role_openids(transfer.to_organization, {"factory_matching"}):
            raise FactoryReturnProcessError("厂家缺少有效的配套经理账号。")

    now = timezone.now()
    grouped = defaultdict(list)
    transfer_items_by_id = {
        row.pk: row
        for row in GoodsTransferItem.objects.select_for_update()
        .select_related("device")
        .filter(transfer=transfer)
    }
    for inventory_item in inventory_items:
        submitted_row = submitted_by_id[inventory_item.transfer_item_id]
        stock_status = str(submitted_row["stock_status"]).upper()
        inventory_item.stock_status = stock_status
        if stock_status in {
            FactoryReturnInventoryItem.StockStatus.PENDING_DISASSEMBLY,
            FactoryReturnInventoryItem.StockStatus.PENDING_REPAIR,
        }:
            inventory_item.production_status = (
                FactoryReturnInventoryItem.ProductionStatus.PENDING
            )
            inventory_item.matching_status = (
                FactoryReturnInventoryItem.MatchingStatus.BLOCKED
            )
        else:
            inventory_item.production_status = (
                FactoryReturnInventoryItem.ProductionStatus.NOT_REQUIRED
            )
            inventory_item.matching_status = (
                FactoryReturnInventoryItem.MatchingStatus.NOT_REQUIRED
            )
        inventory_item.classified_by = user
        inventory_item.classified_at = now
        inventory_item.remark = str(submitted_row.get("remark") or "").strip()[:255]
        inventory_item.save(
            update_fields=(
                "stock_status",
                "classified_by",
                "classified_at",
                "remark",
                "production_status",
                "matching_status",
                "updated_at",
            )
        )
        grouped[stock_status].append(inventory_item)
        transfer_item = transfer_items_by_id[inventory_item.transfer_item_id]
        event = LifecycleEvent.objects.create(
            device=transfer_item.device,
            event_type="FACTORY_RETURN_INTAKE",
            operator=user,
            organization=transfer.to_organization,
            from_organization=transfer.from_organization,
            to_organization=transfer.to_organization,
            data={
                "goods_transfer_id": transfer.pk,
                "goods_transfer_item_id": transfer_item.pk,
                "factory_return_process_id": process.pk,
                "factory_return_inventory_item_id": inventory_item.pk,
                "stock_status": stock_status,
                "source_product_model_id": inventory_item.source_product_model_id,
                "source_production_batch_id": inventory_item.source_production_batch_id,
                "source_bom_id": inventory_item.source_bom_id,
                "lifecycle_closed": False,
            },
        )
        DeviceState.objects.filter(device=transfer_item.device).update(
            status="RETURNED_TO_FACTORY",
            last_event=event,
            last_event_time=now,
            updated_at=now,
        )
        OrganizationDevice.objects.filter(device=transfer_item.device).update(
            status="RETURNED_TO_FACTORY",
            last_event=event,
            updated_at=now,
        )
        transfer_item.status = GoodsTransferItem.Status.COMPLETED
        transfer_item.completed_at = now
        transfer_item.save(update_fields=("status", "completed_at", "updated_at"))

    transfer.status = GoodsTransfer.Status.COMPLETED
    transfer.completed_at = now
    transfer.factory_lifecycle_terminated = False
    transfer.save(
        update_fields=(
            "status",
            "completed_at",
            "factory_lifecycle_terminated",
            "updated_at",
        )
    )
    process.phase = FactoryReturnProcess.Phase.POSTED
    process.posted_by = user
    process.posted_at = now
    process.posting_remark = str(posting_remark or "").strip()[:255]
    process.save(
        update_fields=(
            "phase",
            "posted_by",
            "posted_at",
            "posting_remark",
            "updated_at",
        )
    )
    counts = {key: len(value) for key, value in grouped.items()}
    _publish_stock_notice(transfer, user, counts)
    _publish_followup_tasks(transfer, user, grouped)
    _sync_assistant_action(transfer, user)
    return _serialize_process(process, identity), True


def _finish_internal_workitem(workitem, actor, title):
    if not workitem or workitem.status != "OPEN":
        return
    now = timezone.now()
    actor_openid = actor.wx_openid or ""
    actor_name = _display_name(actor)
    workitem.status = "DONE"
    workitem.title = title
    workitem.done_by_openid = actor_openid
    workitem.done_at = now
    workitem.last_activity_at = now
    workitem.save(
        update_fields=(
            "status",
            "title",
            "done_by_openid",
            "done_at",
            "last_activity_at",
            "updated_at",
        )
    )
    workitem.user_tasks.filter(to_openid=actor_openid).update(
        state="DONE",
        need_action=False,
        done_at=now,
        done_reason="SELF_ACTION",
        display_title_snapshot=title,
        display_subtitle_snapshot="由你完成",
        cursor_time=now,
        updated_at=now,
    )
    workitem.user_tasks.exclude(to_openid=actor_openid).update(
        state="DONE",
        need_action=False,
        done_at=now,
        done_reason="OTHER_ACTION",
        display_title_snapshot=title,
        display_subtitle_snapshot=f"由{actor_name}完成或系统自动结束",
        cursor_time=now,
        updated_at=now,
    )


def _branch_items(workitem):
    payload = dict(workitem.payload or {}) if workitem else {}
    item_ids = [
        int(value)
        for value in payload.get("factory_return_inventory_item_ids", [])
        if value
    ]
    return list(
        FactoryReturnInventoryItem.objects.select_for_update()
        .filter(pk__in=item_ids)
        .order_by("pk")
    )


def _sync_internal_branch_tasks(transfer, branch, actor):
    config = INTERNAL_BRANCH_CONFIG[branch]
    production = WorkItem.objects.select_for_update().filter(
        type=config["production_type"],
        uniq_key=(
            f"lifecycle:factory-return:{transfer.pk}:"
            f"{config['production_suffix']}"
        ),
    ).first()
    matching = WorkItem.objects.select_for_update().filter(
        type=config["matching_type"],
        uniq_key=(
            f"lifecycle:factory-return:{transfer.pk}:"
            f"{config['matching_suffix']}"
        ),
    ).first()
    items = _branch_items(production or matching)
    if not items:
        return

    production_pending = sum(
        row.production_status
        != FactoryReturnInventoryItem.ProductionStatus.COMPLETED
        for row in items
    )
    if production:
        if production_pending == 0:
            _finish_internal_workitem(
                production,
                actor,
                "退货设备生产处置已完成",
            )
        elif production.status == "OPEN":
            _refresh_existing_workitem(
                production,
                title=f"退货设备生产处置剩余{production_pending}台",
                payload_updates={"remaining_count": production_pending},
            )

    if not matching or matching.status != "OPEN":
        return
    matching_pending = sum(
        row.matching_status
        == FactoryReturnInventoryItem.MatchingStatus.PENDING
        for row in items
    )
    matching_blocked = sum(
        row.matching_status
        == FactoryReturnInventoryItem.MatchingStatus.BLOCKED
        for row in items
    )
    matching_finished = all(
        row.matching_status
        in {
            FactoryReturnInventoryItem.MatchingStatus.NOT_REQUIRED,
            FactoryReturnInventoryItem.MatchingStatus.COMPLETED,
        }
        for row in items
    )
    if matching_finished:
        _finish_internal_workitem(
            matching,
            actor,
            "退货设备配套处置已完成",
        )
        return
    if matching_pending:
        now = timezone.now()
        _refresh_existing_workitem(
            matching,
            title=config["matching_title"],
            payload_updates={
                "blocked_by_production": False,
                "matching_disposition_ready": True,
                "pending_count": matching_pending,
                "blocked_count": matching_blocked,
            },
        )
        matching.user_tasks.exclude(state="DONE").update(
            need_action=True,
            state="NEW",
            read_at=None,
            done_at=None,
            done_reason="",
            cursor_time=now,
            updated_at=now,
        )
        return
    _refresh_existing_workitem(
        matching,
        title="等待生产经理提交处置结果",
        payload_updates={
            "blocked_by_production": True,
            "matching_disposition_ready": False,
            "pending_count": 0,
            "blocked_count": matching_blocked,
        },
    )
    matching.user_tasks.exclude(state="DONE").update(need_action=False)


def _internal_item_event(item, user, event_type, data, *, close_lifecycle=False):
    transfer = item.process.transfer
    device = item.transfer_item.device
    event = LifecycleEvent.objects.create(
        device=device,
        event_type=event_type,
        operator=user,
        organization=transfer.to_organization,
        from_organization=transfer.from_organization,
        to_organization=transfer.to_organization,
        data={
            "goods_transfer_id": transfer.pk,
            "factory_return_process_id": item.process_id,
            "factory_return_inventory_item_id": item.pk,
            **data,
        },
    )
    final_event = event
    if close_lifecycle:
        final_event = LifecycleEvent.objects.create(
            device=device,
            event_type="LIFECYCLE_CLOSE",
            operator=user,
            organization=transfer.to_organization,
            from_organization=transfer.from_organization,
            to_organization=transfer.to_organization,
            data={
                "goods_transfer_id": transfer.pk,
                "factory_return_inventory_item_id": item.pk,
                "disassembly_event_id": event.pk,
                "reason": "FACTORY_RETURN_DISASSEMBLY",
                "lifecycle_closed": True,
            },
        )
    now = timezone.now()
    state_status = (
        "SCRAPPED"
        if close_lifecycle
        or item.stock_status
        == FactoryReturnInventoryItem.StockStatus.LIFECYCLE_ENDED
        else "RETURNED_TO_FACTORY"
    )
    DeviceState.objects.filter(device=device).update(
        status=state_status,
        last_event=final_event,
        last_event_time=now,
        updated_at=now,
    )
    OrganizationDevice.objects.filter(device=device).update(
        status="RETURNED_TO_FACTORY",
        last_event=final_event,
        updated_at=now,
    )
    return final_event


def _locked_internal_inventory_item(transfer, item_id):
    item = (
        FactoryReturnInventoryItem.objects.select_for_update()
        .select_related(
            "process__transfer__from_organization",
            "process__transfer__to_organization",
            "transfer_item__device",
        )
        .filter(pk=item_id, process__transfer=transfer)
        .first()
    )
    if not item:
        raise FactoryReturnProcessError("退货库存设备不存在。")
    if item.process.phase != FactoryReturnProcess.Phase.POSTED:
        raise FactoryReturnProcessError("该退货批次尚未完成分类入库。")
    return item


@transaction.atomic
def process_factory_return_production_item(
    *,
    user,
    transfer_id,
    item_id,
    action,
    needs_replacement_material=None,
    production_remark="",
    material_requirement="",
    disassembly_result="",
    has_warranty_material=None,
):
    identity = _factory_identity(user, {"factory_production"})
    transfer = (
        GoodsTransfer.objects.select_for_update()
        .select_related(
            "from_organization__organization_type",
            "to_organization__organization_type",
        )
        .filter(pk=transfer_id)
        .first()
    )
    if not transfer:
        raise FactoryReturnProcessError("退货商品流转单不存在。")
    _validate_factory_return(transfer, identity.organization_id)
    item = _locked_internal_inventory_item(transfer, item_id)
    action = str(action or "").upper()
    now = timezone.now()

    if action == "REPAIR_DIAGNOSIS":
        if (
            item.stock_status
            != FactoryReturnInventoryItem.StockStatus.PENDING_REPAIR
            or item.production_status
            != FactoryReturnInventoryItem.ProductionStatus.PENDING
        ):
            raise FactoryReturnProcessError("该设备当前不能重复提交修复诊断。")
        item.needs_replacement_material = bool(needs_replacement_material)
        item.production_remark = str(production_remark or "").strip()[:1000]
        item.material_requirement = str(material_requirement or "").strip()[:1000]
        item.stock_status = FactoryReturnInventoryItem.StockStatus.REPAIRING
        if item.needs_replacement_material:
            if not item.material_requirement:
                raise FactoryReturnProcessError("需要换料时必须填写物料需求。")
            item.production_status = (
                FactoryReturnInventoryItem.ProductionStatus.WAITING_MATCHING
            )
            item.matching_status = (
                FactoryReturnInventoryItem.MatchingStatus.PENDING
            )
        else:
            item.production_status = (
                FactoryReturnInventoryItem.ProductionStatus.READY_TO_COMPLETE
            )
            item.matching_status = (
                FactoryReturnInventoryItem.MatchingStatus.NOT_REQUIRED
            )
        event_type = "FACTORY_RETURN_REPAIR_DIAGNOSIS"
        event_data = {
            "needs_replacement_material": item.needs_replacement_material,
            "production_remark": item.production_remark,
            "material_requirement": item.material_requirement,
        }
    elif action == "REPAIR_COMPLETE":
        if (
            item.stock_status != FactoryReturnInventoryItem.StockStatus.REPAIRING
            or item.production_status
            != FactoryReturnInventoryItem.ProductionStatus.READY_TO_COMPLETE
        ):
            raise FactoryReturnProcessError("该设备尚未具备完成修复条件。")
        if (
            item.needs_replacement_material
            and item.matching_status
            != FactoryReturnInventoryItem.MatchingStatus.COMPLETED
        ):
            raise FactoryReturnProcessError("请等待配套经理完成物料处置。")
        completion_remark = str(production_remark or "").strip()[:1000]
        if completion_remark:
            item.production_remark = completion_remark
        item.stock_status = FactoryReturnInventoryItem.StockStatus.PENDING_REISSUE
        item.production_status = (
            FactoryReturnInventoryItem.ProductionStatus.COMPLETED
        )
        event_type = "FACTORY_RETURN_REPAIR_COMPLETE"
        event_data = {
            "production_remark": item.production_remark,
            "needs_replacement_material": item.needs_replacement_material,
            "material_plan": item.material_plan,
            "ready_for_reissue": True,
        }
    elif action == "DISASSEMBLY_COMPLETE":
        if (
            item.stock_status
            != FactoryReturnInventoryItem.StockStatus.PENDING_DISASSEMBLY
            or item.production_status
            != FactoryReturnInventoryItem.ProductionStatus.PENDING
        ):
            raise FactoryReturnProcessError("该设备当前不能重复完成拆机。")
        result = str(disassembly_result or "").strip()[:1000]
        if not result:
            raise FactoryReturnProcessError("请填写拆机结果和拆出物料清单。")
        item.disassembly_result = result
        item.has_warranty_material = bool(has_warranty_material)
        item.production_remark = str(production_remark or "").strip()[:1000]
        item.stock_status = FactoryReturnInventoryItem.StockStatus.LIFECYCLE_ENDED
        item.production_status = (
            FactoryReturnInventoryItem.ProductionStatus.COMPLETED
        )
        item.matching_status = (
            FactoryReturnInventoryItem.MatchingStatus.PENDING
            if item.has_warranty_material
            else FactoryReturnInventoryItem.MatchingStatus.NOT_REQUIRED
        )
        event_type = "FACTORY_RETURN_DISASSEMBLY_COMPLETE"
        event_data = {
            "disassembly_result": item.disassembly_result,
            "has_warranty_material": item.has_warranty_material,
            "production_remark": item.production_remark,
            "lifecycle_closed": True,
        }
    else:
        raise FactoryReturnProcessError("生产处置动作无效。")

    item.production_processed_by = user
    item.production_processed_at = now
    item.save()
    _internal_item_event(
        item,
        user,
        event_type,
        event_data,
        close_lifecycle=action == "DISASSEMBLY_COMPLETE",
    )
    branch = "DISASSEMBLY" if action == "DISASSEMBLY_COMPLETE" else "REPAIR"
    _sync_internal_branch_tasks(transfer, branch, user)
    return _serialize_process(item.process, identity), True


@transaction.atomic
def process_factory_return_matching_item(
    *,
    user,
    transfer_id,
    item_id,
    action,
    material_plan="",
    warranty_disposition="",
    matching_remark="",
):
    identity = _factory_identity(user, {"factory_matching"})
    transfer = (
        GoodsTransfer.objects.select_for_update()
        .select_related(
            "from_organization__organization_type",
            "to_organization__organization_type",
        )
        .filter(pk=transfer_id)
        .first()
    )
    if not transfer:
        raise FactoryReturnProcessError("退货商品流转单不存在。")
    _validate_factory_return(transfer, identity.organization_id)
    item = _locked_internal_inventory_item(transfer, item_id)
    if item.matching_status != FactoryReturnInventoryItem.MatchingStatus.PENDING:
        raise FactoryReturnProcessError("当前仍在等待生产结果或配套处置已经完成。")
    action = str(action or "").upper()
    if action == "REPAIR_MATERIAL_PLAN":
        plan = str(material_plan or "").strip()[:1000]
        if not plan:
            raise FactoryReturnProcessError("请填写修复物料方案。")
        if (
            item.stock_status != FactoryReturnInventoryItem.StockStatus.REPAIRING
            or not item.needs_replacement_material
        ):
            raise FactoryReturnProcessError("该设备没有待处理的修复物料需求。")
        item.material_plan = plan
        item.production_status = (
            FactoryReturnInventoryItem.ProductionStatus.READY_TO_COMPLETE
        )
        branch = "REPAIR"
    elif action == "DISASSEMBLY_MATERIAL_DISPOSITION":
        disposition = str(warranty_disposition or "").strip()[:1000]
        if not disposition:
            raise FactoryReturnProcessError("请填写拆机三包物料处置意见。")
        if (
            item.stock_status
            != FactoryReturnInventoryItem.StockStatus.LIFECYCLE_ENDED
            or not item.has_warranty_material
        ):
            raise FactoryReturnProcessError("该设备没有待处理的三包拆机物料。")
        item.warranty_disposition = disposition
        branch = "DISASSEMBLY"
    else:
        raise FactoryReturnProcessError("配套处置动作无效。")

    now = timezone.now()
    item.matching_status = FactoryReturnInventoryItem.MatchingStatus.COMPLETED
    item.matching_remark = str(matching_remark or "").strip()[:1000]
    item.matching_processed_by = user
    item.matching_processed_at = now
    item.save()
    _internal_item_event(
        item,
        user,
        "FACTORY_RETURN_MATCHING_COMPLETE",
        {
            "action": action,
            "material_plan": item.material_plan,
            "warranty_disposition": item.warranty_disposition,
            "matching_remark": item.matching_remark,
        },
    )
    _sync_internal_branch_tasks(transfer, branch, user)
    return _serialize_process(item.process, identity), True


def _serialize_inventory_item(item, can_post, role_code=""):
    transfer_item = item.transfer_item
    device = transfer_item.device
    qrcode = getattr(device, "qrcode", None)
    return {
        "id": item.pk,
        "goods_transfer_item_id": transfer_item.pk,
        "stock_status": item.stock_status,
        "stock_status_name": item.get_stock_status_display(),
        "production_status": item.production_status,
        "production_status_name": item.get_production_status_display(),
        "matching_status": item.matching_status,
        "matching_status_name": item.get_matching_status_display(),
        "remark": item.remark,
        "needs_replacement_material": item.needs_replacement_material,
        "production_remark": item.production_remark,
        "material_requirement": item.material_requirement,
        "material_plan": item.material_plan,
        "disassembly_result": item.disassembly_result,
        "has_warranty_material": item.has_warranty_material,
        "warranty_disposition": item.warranty_disposition,
        "matching_remark": item.matching_remark,
        "production_processed_by_name": (
            _display_name(item.production_processed_by)
            if item.production_processed_by_id
            else ""
        ),
        "production_processed_at": (
            item.production_processed_at.isoformat()
            if item.production_processed_at
            else ""
        ),
        "matching_processed_by_name": (
            _display_name(item.matching_processed_by)
            if item.matching_processed_by_id
            else ""
        ),
        "matching_processed_at": (
            item.matching_processed_at.isoformat()
            if item.matching_processed_at
            else ""
        ),
        "classified_by_name": (
            _display_name(item.classified_by) if item.classified_by_id else ""
        ),
        "classified_at": item.classified_at.isoformat() if item.classified_at else "",
        "device": {
            "id": device.pk,
            "sn": device.sn,
            "product_model_id": item.source_product_model_id,
            "product_model_name": item.source_product_model.name,
            "old_qr_code": qrcode.code if qrcode else "",
            "old_qr_status": qrcode.status if qrcode else "",
            "old_qr_status_name": qrcode.get_status_display() if qrcode else "",
        },
        "source_batch": {
            "id": item.source_production_batch_id,
            "batch_no": item.source_production_batch.batch_no,
        },
        "source_bom": {
            "id": item.source_bom_id,
            "version": item.source_bom.version,
            "version_no": item.source_bom.version_no,
        },
        "actions": {
            "can_classify": bool(can_post),
            "can_submit_repair_diagnosis": bool(
                role_code == "factory_production"
                and item.stock_status
                == FactoryReturnInventoryItem.StockStatus.PENDING_REPAIR
                and item.production_status
                == FactoryReturnInventoryItem.ProductionStatus.PENDING
            ),
            "can_complete_repair": bool(
                role_code == "factory_production"
                and item.stock_status
                == FactoryReturnInventoryItem.StockStatus.REPAIRING
                and item.production_status
                == FactoryReturnInventoryItem.ProductionStatus.READY_TO_COMPLETE
            ),
            "can_complete_disassembly": bool(
                role_code == "factory_production"
                and item.stock_status
                == FactoryReturnInventoryItem.StockStatus.PENDING_DISASSEMBLY
                and item.production_status
                == FactoryReturnInventoryItem.ProductionStatus.PENDING
            ),
            "can_submit_repair_material_plan": bool(
                role_code == "factory_matching"
                and item.stock_status
                == FactoryReturnInventoryItem.StockStatus.REPAIRING
                and item.matching_status
                == FactoryReturnInventoryItem.MatchingStatus.PENDING
                and item.needs_replacement_material
            ),
            "can_submit_disassembly_disposition": bool(
                role_code == "factory_matching"
                and item.stock_status
                == FactoryReturnInventoryItem.StockStatus.LIFECYCLE_ENDED
                and item.matching_status
                == FactoryReturnInventoryItem.MatchingStatus.PENDING
                and item.has_warranty_material
            ),
        },
    }


def _serialize_process(process, identity):
    transfer = process.transfer
    document_actions = factory_return_document_actions(
        identity,
        transfer,
    )
    inventory_items = list(
        process.inventory_items.select_related(
            "classified_by",
            "production_processed_by",
            "matching_processed_by",
            "source_product_model",
            "source_production_batch",
            "source_bom",
            "transfer_item__device__qrcode",
        ).order_by("transfer_item_id")
    )
    can_pickup = bool(
        identity.role.code == "factory_logistics"
        and process.phase == FactoryReturnProcess.Phase.WAITING_PICKUP
    )
    can_post = bool(
        identity.role.code == "factory_sales_assistant"
        and process.phase == FactoryReturnProcess.Phase.WAITING_POSTING
    )
    account_case = process.account_case
    credit_amount_recorded = bool(
        account_case
        and account_case.status == GoodsTransferAccountCase.Status.CONFIRMED
        and account_case.confirmed_amount is not None
    )
    can_record_credit_amount = bool(
        identity.role.code == "factory_sales_assistant"
        and process.picked_at
        and account_case
        and not credit_amount_recorded
    )
    assistant_workitem = WorkItem.objects.filter(
        type=POSTING_WORK_TYPE,
        uniq_key=f"lifecycle:factory-return:{transfer.pk}:posting",
    ).first()
    can_view_account_case = identity.role.code in {
        "factory_sales",
        "factory_sales_assistant",
    }
    inventory_summary = defaultdict(int)
    for inventory_item in inventory_items:
        inventory_summary[inventory_item.stock_status] += 1
    return {
        "transfer": {
            "id": transfer.pk,
            "flow_no": transfer.flow_no,
            "status": transfer.status,
            "status_name": transfer.get_status_display(),
            "from_organization_name": transfer.from_organization.name,
            "to_organization_name": transfer.to_organization.name,
            "submitted_at": transfer.submitted_at.isoformat() if transfer.submitted_at else "",
            "received_at": transfer.received_at.isoformat() if transfer.received_at else "",
            "completed_at": transfer.completed_at.isoformat() if transfer.completed_at else "",
            "return_remark": transfer.remark,
        },
        "process": {
            "id": process.pk,
            "phase": process.phase,
            "phase_name": process.get_phase_display(),
            "picked_by_name": _display_name(process.picked_by) if process.picked_by_id else "",
            "picked_at": process.picked_at.isoformat() if process.picked_at else "",
            "posted_by_name": _display_name(process.posted_by) if process.posted_by_id else "",
            "posted_at": process.posted_at.isoformat() if process.posted_at else "",
            "posting_remark": process.posting_remark,
        },
        "viewer": {
            "role": identity.role.code,
            "role_name": identity.role.name,
        },
        "account_case": (
            {
                "id": account_case.pk,
                "status": account_case.status,
                "status_name": account_case.get_status_display(),
                "proposed_amount": (
                    f"{account_case.proposed_amount:.2f}"
                    if account_case.proposed_amount is not None
                    else None
                ),
                "confirmed_amount": (
                    f"{account_case.confirmed_amount:.2f}"
                    if account_case.confirmed_amount is not None
                    else None
                ),
                "amount_recorded": credit_amount_recorded,
                "amount_recorded_by_name": (
                    _display_name(account_case.confirmed_by)
                    if account_case.confirmed_by_id
                    else ""
                ),
                "amount_recorded_at": (
                    account_case.confirmed_at.isoformat()
                    if account_case.confirmed_at
                    else ""
                ),
                "detail_link": (
                    "/pages/stock/goods_transfer_accounts/goods_transfer_accounts"
                    f"?transfer_id={transfer.pk}"
                ),
            }
            if account_case and can_view_account_case
            else None
        ),
        "classification_options": [
            {
                "value": FactoryReturnInventoryItem.StockStatus.PENDING_DISASSEMBLY,
                "label": "入库待拆机",
                "description": "后续由生产经理和配套经理分别处理。",
            },
            {
                "value": FactoryReturnInventoryItem.StockStatus.PENDING_REISSUE,
                "label": "入库待流转",
                "description": "进入厂家待流转库存，后续可被新订单占用。",
            },
            {
                "value": FactoryReturnInventoryItem.StockStatus.PENDING_REPAIR,
                "label": "入库待修复",
                "description": "后续由生产经理和配套经理分别处理。",
            },
        ],
        "total_count": len(inventory_items),
        "inventory_summary": dict(inventory_summary),
        "logistics_documents": (
            serialize_factory_return_documents(
                process,
                FactoryReturnDocument.DocumentKind.LOGISTICS,
            )
            if document_actions["can_view_logistics"]
            else []
        ),
        "goods_list_documents": (
            serialize_factory_return_documents(
                process,
                FactoryReturnDocument.DocumentKind.GOODS_LIST,
            )
            if document_actions["can_view_goods_list"]
            else []
        ),
        "document_task": document_actions,
        "items": [
            _serialize_inventory_item(item, can_post, identity.role.code)
            for item in inventory_items
        ],
        "actions": {
            "can_confirm_pickup": can_pickup,
            "can_post_inventory": can_post,
            "can_record_credit_amount": can_record_credit_amount,
            "assistant_action_locked": bool(
                identity.role.code == "factory_sales_assistant"
                and not process.picked_at
            ),
            "assistant_task_status": (
                assistant_workitem.status if assistant_workitem else "NOT_CREATED"
            ),
            "can_view_accounts": bool(account_case and can_view_account_case),
            "can_view_logistics_documents": document_actions[
                "can_view_logistics"
            ],
            "can_view_goods_list_documents": document_actions[
                "can_view_goods_list"
            ],
        },
        "rules": {
            "credit_amount_blocks_posting": False,
            "inventory_posting_blocks_credit_amount": False,
            "return_documents_block_pickup": False,
            "lifecycle_closed": False,
            "old_qr_disabled_after_pickup": bool(process.picked_at),
        },
    }


def get_factory_return_process(*, user, transfer_id):
    identity = _factory_identity(user, FACTORY_RETURN_VIEW_ROLES)
    process = (
        FactoryReturnProcess.objects.select_related(
            "transfer__from_organization__organization_type",
            "transfer__to_organization__organization_type",
            "picked_by",
            "posted_by",
            "account_case",
        )
        .filter(transfer_id=transfer_id)
        .first()
    )
    if not process:
        raise FactoryReturnProcessError("厂家退货处理流程不存在。")
    _validate_factory_return(process.transfer, identity.organization_id)
    return _serialize_process(process, identity)


def get_factory_return_inventory(*, user):
    identity = _factory_identity(
        user,
        {
            "factory_sales",
            "factory_sales_assistant",
            "factory_stock",
            "factory_production",
            "factory_matching",
        },
    )
    items = list(
        FactoryReturnInventoryItem.objects.select_related(
            "process__transfer__from_organization",
            "process__transfer__to_organization",
            "classified_by",
            "production_processed_by",
            "matching_processed_by",
            "source_product_model",
            "source_production_batch",
            "source_bom",
            "transfer_item__device__qrcode",
        )
        .filter(
            process__transfer__to_organization_id=identity.organization_id,
            process__phase=FactoryReturnProcess.Phase.POSTED,
        )
        .order_by("stock_status", "-classified_at", "-pk")
    )
    counts = defaultdict(int)
    for item in items:
        counts[item.stock_status] += 1
    return {
        "viewer": {
            "role": identity.role.code,
            "role_name": identity.role.name,
            "organization_id": identity.organization_id,
            "organization_name": identity.organization.name,
        },
        "status_options": [
            {"value": value, "label": label}
            for value, label in FactoryReturnInventoryItem.StockStatus.choices
            if value != FactoryReturnInventoryItem.StockStatus.WAITING_POSTING
        ],
        "counts": dict(counts),
        "total_count": len(items),
        "items": [
            {
                **_serialize_inventory_item(item, False, identity.role.code),
                "flow_no": item.process.transfer.flow_no,
                "from_organization_name": (
                    item.process.transfer.from_organization.name
                ),
                "process_link": _process_link(item.process.transfer),
            }
            for item in items
        ],
    }
