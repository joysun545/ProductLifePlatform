from collections import defaultdict
import calendar
from decimal import Decimal
from pathlib import Path
import uuid

from django.db import transaction
from django.db.models import Max, OuterRef, Q, Subquery, Sum
from django.utils import timezone

from account.models import UserOrganization
from account.services.auth_service import get_active_identity
from account.services.role_delegation_service import (
    SENIOR_MANAGER_ROLE_CODES,
    delegated_role_codes,
    effective_role_codes,
    identity_has_role,
    vacant_role_manager_openids,
)
from lifecycle.models import (
    DeviceMaterialFaultRecord,
    DeviceMaterialWarrantyEvaluation,
    DeviceState,
    FactoryReturnDocument,
    FactoryReturnInventoryItem,
    FactoryReturnMaterialReturnRecord,
    FactoryReturnProcess,
    FactoryReturnRepairMaterialRequirement,
    FactoryReturnWarrantyDispatch,
    FactoryReturnWarrantyDispatchItem,
    GoodsTransfer,
    GoodsTransferAccountCase,
    GoodsTransferItem,
    LifecycleEvent,
    OrganizationDevice,
    QRCode,
)
from lifecycle.services.goods_transfer_service import goods_transfer_root_payload
from sales.models import (
    OrderPlanItem,
    OrderPlanProductionSubplanItem,
    OrderPlanShipmentDevice,
    OrderPlanSupplyDeliveryRequirement,
)
from sales.services.supplier_batch_trace_service import (
    consumed_material_lots,
    with_supplier_batch_id,
)
from supply_chain.models import MaterialSpecSupplier
from supply_chain.services.supplier_responsibility_service import (
    SupplierResponsibilityError,
    get_supplier_identity,
    get_supplier_task_recipients,
)
from workflow.models import UserTask, WorkItem
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
SUPPLIER_WARRANTY_WORK_TYPE = "FACTORY_RETURN_SUPPLIER_WARRANTY"
SUPPLIER_REPAIR_MATERIAL_WORK_TYPE = "FACTORY_RETURN_SUPPLIER_REPAIR_MATERIAL"

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

# The domain detail renders a role-specific workflow strip.  These are planned
# nodes, not replacement inbox rows: an unpublished node is visible but cannot
# be read or operated, while a published node points to its real UserTask.
FACTORY_RETURN_ROLE_TASK_NODES = {
    "factory_sales": (
        {
            "type": SALES_NOTICE_SUBMITTED,
            "title": "退货提交与取回进度",
            "is_action": False,
        },
    ),
    "factory_logistics": (
        {
            "type": PICKUP_WORK_TYPE,
            "title": "厂家物流取回退货",
            "is_action": True,
        },
    ),
    "factory_sales_assistant": (
        {
            "type": POSTING_WORK_TYPE,
            "title": "退货下账与分类入库",
            "is_action": True,
        },
    ),
    "factory_stock": (
        {
            "type": STOCK_NOTICE_WORK_TYPE,
            "title": "退货分类入库知晓",
            "is_action": False,
        },
    ),
    "factory_production": (
        {
            "type": DISASSEMBLY_PRODUCTION_WORK_TYPE,
            "title": "退货设备拆机处置",
            "is_action": True,
        },
        {
            "type": REPAIR_PRODUCTION_WORK_TYPE,
            "title": "退货设备修复处置",
            "is_action": True,
        },
    ),
    "factory_matching": (
        {
            "type": DISASSEMBLY_MATCHING_WORK_TYPE,
            "title": "拆机三包物料配套处置",
            "is_action": True,
        },
        {
            "type": REPAIR_MATCHING_WORK_TYPE,
            "title": "修复换料配套处置",
            "is_action": True,
        },
    ),
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
        or (allowed_roles and not identity_has_role(identity, allowed_roles))
    ):
        raise FactoryReturnProcessError("当前账号没有厂家退货处理权限。")
    return identity


def _senior_manager_has_task(identity, transfer, work_types, *, require_action=False):
    """Whether this manager received this exact organization-scoped task."""

    openid = (identity.user.wx_openid or "").strip()
    if not openid:
        return False
    tasks = UserTask.objects.filter(
        to_openid=openid,
        workitem__biz_ref=f"lifecycle.GoodsTransfer:{transfer.pk}",
        workitem__type__in=tuple(work_types),
    )
    if require_action:
        tasks = tasks.filter(
            need_action=True,
            state__in=("NEW", "READ"),
            workitem__status="OPEN",
        )
    return tasks.exists()


def _role_task_access(
    identity,
    transfer,
    role_code,
    work_types,
    *,
    require_action=False,
):
    if not identity_has_role(identity, role_code):
        return False
    if identity.role.code not in SENIOR_MANAGER_ROLE_CODES:
        return identity.role.code == role_code
    return _senior_manager_has_task(
        identity,
        transfer,
        work_types,
        require_action=require_action,
    )


def _require_role_task_access(identity, transfer, role_code, work_types):
    if not _role_task_access(
        identity,
        transfer,
        role_code,
        work_types,
        require_action=True,
    ):
        raise FactoryReturnProcessError("当前高级经理没有收到这项退货协同任务。")


def _manager_task_transfer_ids(identity):
    """Factory-return records visible through the manager's own task copies."""

    if identity.role.code not in SENIOR_MANAGER_ROLE_CODES:
        return None
    role_codes = effective_role_codes(identity)
    task_types = tuple(
        node["type"]
        for role_code, definitions in FACTORY_RETURN_ROLE_TASK_NODES.items()
        if role_code in role_codes
        for node in definitions
    )
    openid = (identity.user.wx_openid or "").strip()
    if not openid or not task_types:
        return set()
    refs = UserTask.objects.filter(
        to_openid=openid,
        workitem__type__in=task_types,
        workitem__biz_ref__startswith="lifecycle.GoodsTransfer:",
    ).values_list("workitem__biz_ref", flat=True).distinct()
    transfer_ids = set()
    for ref in refs:
        try:
            transfer_ids.add(int(str(ref).rsplit(":", 1)[1]))
        except (IndexError, TypeError, ValueError):
            continue
    return transfer_ids


def _manager_can_view_return(identity, transfer):
    if identity.role.code not in SENIOR_MANAGER_ROLE_CODES:
        return True
    return transfer.pk in _manager_task_transfer_ids(identity)


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


def _action_openids(organization, role_codes):
    return (*_role_openids(organization, role_codes),
            *vacant_role_manager_openids(organization, role_codes))


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
    recipients = _action_openids(transfer.to_organization, role_codes)
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
        or not identity_has_role(identity, MERCHANT_RETURN_DOCUMENT_ROLES)
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
    factory_role_task = {
        "factory_logistics": PICKUP_WORK_TYPE,
        "factory_sales_assistant": POSTING_WORK_TYPE,
    }.get(config["factory_role"], "")
    factory_access_by_role = bool(
        factory_role_task
        and identity.organization_id == transfer.to_organization_id
        and identity.organization.organization_type.code == "OWNER"
        and _role_task_access(
            identity,
            transfer,
            config["factory_role"],
            (factory_role_task,),
        )
    )
    factory_sales_access = bool(
        identity.organization_id == transfer.to_organization_id
        and identity.organization.organization_type.code == "OWNER"
        and _role_task_access(
            identity,
            transfer,
            "factory_sales",
            (SALES_NOTICE_SUBMITTED,),
        )
    )
    factory_access = bool(
        factory_access_by_role or factory_sales_access
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
    _require_role_task_access(
        identity,
        transfer,
        "factory_sales_assistant",
        (POSTING_WORK_TYPE,),
    )
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
    _require_role_task_access(
        identity,
        transfer,
        "factory_logistics",
        (PICKUP_WORK_TYPE,),
    )
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
    if not _action_openids(
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
    recipients = _action_openids(transfer.to_organization, {"factory_stock"})
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
    _require_role_task_access(
        identity,
        transfer,
        "factory_sales_assistant",
        (POSTING_WORK_TYPE,),
    )
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
    if not _action_openids(transfer.to_organization, {"factory_stock"}):
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
        if not _action_openids(transfer.to_organization, {"factory_production"}):
            raise FactoryReturnProcessError("厂家缺少有效的生产经理账号。")
        if not _action_openids(transfer.to_organization, {"factory_matching"}):
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


_SOURCE_LOOKUP_REQUIRED = object()


def _source_production_subplan_item(inventory_item):
    """Use the recorded production batch link for the current production flow."""

    batch = inventory_item.source_production_batch
    items = list(
        OrderPlanProductionSubplanItem.objects.select_related(
            "order_plan_item__plan",
            "order_plan_item__sales_bom_selection",
            "production_subplan__material_plan",
        ).filter(
            Q(production_batch_id=batch.pk) | Q(production_batch_code=batch.batch_no)
        ).order_by("id")[:2]
    )
    if not items:
        return None
    item = items[0]
    line = item.order_plan_item
    selection = getattr(line, "sales_bom_selection", None)
    if (
        len(items) != 1
        or item.production_batch_id != batch.pk
        or item.production_batch_code != batch.batch_no
        or item.production_subplan.material_plan.order_plan_id != line.plan_id
        or line.plan.factory_organization_id != batch.manufacturer_id
        or line.product_model_id != batch.product_model_id
        or not selection
        or selection.frozen_bom_id != batch.bom_id
    ):
        raise FactoryReturnProcessError("原生产批次与订单明细或冻结BOM不一致，请先核对生产记录。")
    return item


def _source_order_item(inventory_item, *, production_item=_SOURCE_LOOKUP_REQUIRED):
    """Resolve the order line that manufactured the device."""

    if production_item is _SOURCE_LOOKUP_REQUIRED:
        production_item = _source_production_subplan_item(inventory_item)
    if production_item:
        return production_item.order_plan_item

    batch = inventory_item.source_production_batch
    return (
        OrderPlanItem.objects.select_related(
            "plan",
            "bom_confirmation__confirmed_bom",
        )
        .filter(
            order_batch_code=batch.batch_no,
            plan__factory_organization_id=batch.manufacturer_id,
            product_model_id=batch.product_model_id,
        )
        .first()
    )


def _source_order_trace(inventory_item, order_item, production_item):
    device = inventory_item.transfer_item.device
    batch = inventory_item.source_production_batch
    if (
        device.batch_id != batch.pk
        or device.product_model_id != inventory_item.source_product_model_id
        or batch.product_model_id != inventory_item.source_product_model_id
        or batch.bom_id != inventory_item.source_bom_id
    ):
        raise FactoryReturnProcessError("退货设备与记录的原生产批次、型号或BOM不一致，请先核对。")
    if order_item and batch.manufacturer_id != inventory_item.process.transfer.to_organization_id:
        raise FactoryReturnProcessError("设备原生产订单不属于本次收货厂家的链路。")
    # A production batch can be split across several physical shipments. The
    # exact device allocation identifies its shipment, never the batch alone.
    shipment_scope = Q(return_inventory_allocation__isnull=True)
    if production_item:
        shipment_scope |= Q(order_plan_id=order_item.plan_id)
    allocations = list(
        OrderPlanShipmentDevice.objects.select_related("shipment", "shipment_item")
        .filter(shipment_scope, device_id=device.pk)
        .order_by("id")[:2]
    )
    shipped_allocation = None
    if allocations:
        allocation = allocations[0]
        shipment = allocation.shipment
        shipment_item = allocation.shipment_item
        if (
            len(allocations) != 1
            or not production_item
            or allocation.order_plan_id != order_item.plan_id
            or shipment.order_plan_id != order_item.plan_id
            or shipment.production_subplan_id != production_item.production_subplan_id
            or shipment_item.shipment_id != allocation.shipment_id
            or shipment_item.order_plan_item_id != order_item.pk
            or shipment_item.production_subplan_item_id != production_item.pk
            or allocation.return_inventory_allocation_id is not None
        ):
            raise FactoryReturnProcessError("设备原发货分配与生产订单明细不一致，请先核对发货记录。")
        if shipment.shipped_at:
            shipped_allocation = allocation
    if not order_item:
        return None
    return {
        "order_plan_id": order_item.plan_id,
        "order_plan_code": order_item.plan.plan_code,
        "order_plan_item_id": order_item.pk,
        "order_batch_code": order_item.order_batch_code,
        "production_subplan_item_id": production_item.pk if production_item else None,
        "shipment_id": shipped_allocation.shipment_id if shipped_allocation else None,
        "shipment_item_id": shipped_allocation.shipment_item_id if shipped_allocation else None,
        "shipment_sequence_no": shipped_allocation.shipment.sequence_no if shipped_allocation else None,
    }


def _original_supplier_task_recipients(*, inventory_item, supplier, material_ids):
    """Resolve supplier recipients without rewriting the original sales chain.

    Supplier owners are resolved from the supplier's current active identities so
    the notification always reaches the current owner slot. Responsible sales
    managers come from the original delivery task for the actual production
    requirement, or from the original preparation task on older orders.

    Legacy devices that cannot be linked to an order line keep the previous
    current-responsibility fallback; the returned source marker makes that
    limitation explicit in the new task payload.
    """

    material_ids = {
        int(material_id)
        for material_id in material_ids or ()
        if material_id
    }
    current = get_supplier_task_recipients(
        supplier=supplier,
        material_ids=material_ids,
    )
    subplan_item = _source_production_subplan_item(inventory_item)
    order_item = (
        subplan_item.order_plan_item if subplan_item else _source_order_item(inventory_item)
    )
    if not order_item:
        assigned_material_ids = {
            int(material_id)
            for scope in current["recipient_scopes"].values()
            if scope.get("role") == "supplier_sales"
            for material_id in scope.get("material_ids", [])
        }
        current.update(
            {
                "responsibility_source": "CURRENT_FALLBACK",
                "responsibility_source_name": "历史订单无法定位，按当前责任范围兼容",
                "unassigned_sales_material_ids": sorted(
                    material_ids - assigned_material_ids
                ),
            }
        )
        return current

    owner_openids = list(current["owner_openids"])
    owner_scopes = {
        openid: scope
        for openid, scope in current["recipient_scopes"].items()
        if scope.get("role") == "supplier_owner"
    }
    source_delivery_ids = []
    if subplan_item:
        scoped_sources, source_delivery_ids = _production_delivery_responsibility_scopes(
            subplan_item, supplier, material_ids,
        )
        if source_delivery_ids:
            responsibility_source = "ORDER_DELIVERY_TASK_SNAPSHOT"
            responsibility_source_name = "原订单实际配送任务责任快照"
        else:
            responsibility_source = "NO_ORIGINAL_DELIVERY_TASK"
            responsibility_source_name = "原生产物料未经过订单配送推送，本次仅通知供应商负责人"
    else:
        source_payload = {}
        for workitem in WorkItem.objects.filter(
            type="ORDER_PLAN_SUPPLIER_PREPARE",
            biz_ref=f"sales.OrderPlan:{order_item.plan_id}",
        ).order_by("id"):
            payload = workitem.payload if isinstance(workitem.payload, dict) else {}
            try:
                payload_supplier_id = int(payload.get("supplier_id") or 0)
            except (TypeError, ValueError):
                continue
            if payload_supplier_id == supplier.pk:
                source_payload = payload
                break
        initial_scopes = source_payload.get("initial_recipient_scopes")
        if isinstance(initial_scopes, dict):
            source_scopes = initial_scopes
            responsibility_source = "ORDER_CONFIRMATION_INITIAL_SNAPSHOT"
            responsibility_source_name = "原订单首次确认配套不可变责任快照"
        elif source_payload:
            source_scopes = source_payload.get("recipient_scopes") or {}
            responsibility_source = "ORDER_CONFIRMATION_TASK_SNAPSHOT_LEGACY"
            responsibility_source_name = "原订单历史任务责任快照（兼容旧数据）"
        else:
            source_scopes = {}
            responsibility_source = "ORDER_CONFIRMATION_TASK_MISSING"
            responsibility_source_name = "原订单责任快照缺失，本次仅通知供应商负责人"
        scoped_sources = [(scope, material_ids) for scope in source_scopes.values()]

    identity_materials = defaultdict(set)
    for scope, source_material_ids in scoped_sources:
        if not isinstance(scope, dict) or scope.get("role") != "supplier_sales":
            continue
        try:
            identity_id = int(scope.get("identity_id") or 0)
        except (TypeError, ValueError):
            continue
        if identity_id <= 0:
            continue
        scoped_material_ids = set()
        for value in scope.get("material_ids", []) or []:
            try:
                scoped_material_ids.add(int(value))
            except (TypeError, ValueError):
                continue
        identity_materials[identity_id].update(source_material_ids & scoped_material_ids)

    sales_identities = {
        identity.pk: identity
        for identity in UserOrganization.objects.select_related(
            "user",
            "role",
        ).filter(
            pk__in=identity_materials,
            organization=supplier,
            role__code="supplier_sales",
            role__is_active=True,
            is_active=True,
            user__is_active=True,
            user__wx_openid__isnull=False,
        ).exclude(user__wx_openid="")
    }
    sales_openids = []
    sales_scopes = {}
    assigned_material_ids = set()
    for identity_id, scoped_material_ids in identity_materials.items():
        identity = sales_identities.get(identity_id)
        if not identity or not scoped_material_ids:
            continue
        openid = identity.user.wx_openid
        sales_openids.append(openid)
        assigned_material_ids.update(scoped_material_ids)
        sales_scopes[openid] = {
            "role": "supplier_sales",
            "identity_id": identity.pk,
            "name": (
                identity.user.wx_nickname
                or identity.user.get_full_name()
                or identity.user.username
            ),
            "material_ids": sorted(scoped_material_ids),
        }

    recipient_scopes = dict(owner_scopes)
    recipient_scopes.update(sales_scopes)
    sales_openids = list(dict.fromkeys(sales_openids))
    return {
        "openids": list(dict.fromkeys(owner_openids + sales_openids)),
        "owner_openids": owner_openids,
        "sales_openids": sales_openids,
        "sales_identity_ids": sorted(sales_identities),
        "recipient_scopes": recipient_scopes,
        "responsibility_source": responsibility_source,
        "responsibility_source_name": responsibility_source_name,
        "source_delivery_ids": source_delivery_ids,
        "unassigned_sales_material_ids": sorted(
            material_ids - assigned_material_ids
        ),
    }


def _production_delivery_responsibility_scopes(subplan_item, supplier, material_ids):
    """Read immutable scopes for the selected materials' recorded delivery lines."""
    plan = subplan_item.order_plan_item.plan
    production_subplan = subplan_item.production_subplan
    allocations = OrderPlanSupplyDeliveryRequirement.objects.select_related(
        "requirement", "delivery_line__delivery",
    ).filter(
        requirement__production_subplan_item=subplan_item,
        requirement__supplier=supplier,
        requirement__material_id__in=material_ids,
    )
    deliveries = defaultdict(dict)
    for allocation in allocations:
        line = allocation.delivery_line
        delivery = line.delivery
        requirement = allocation.requirement
        if (
            delivery.material_plan_id != production_subplan.material_plan_id
            or delivery.supplier_id != supplier.pk
            or line.material_id != requirement.material_id
            or line.material_spec_id != requirement.material_spec_id
        ):
            raise FactoryReturnProcessError("原生产物料的配送明细关系不一致。")
        if (
            not delivery.dispatched_at
            or not delivery.supplier_confirmed_at
            or not production_subplan.production_completed_at
            or not (
                delivery.dispatched_at <= delivery.supplier_confirmed_at
                <= production_subplan.production_completed_at
            )
        ):
            raise FactoryReturnProcessError("原生产物料的配送确认记录缺失或时间顺序不一致。")
        deliveries[delivery.pk][line.pk] = (line.material_id, line.material_spec_id)

    scoped_sources = []
    for delivery_id, lines in deliveries.items():
        workitem = WorkItem.objects.filter(
            type="ORDER_PLAN_SUPPLIER_DELIVERY",
            biz_ref=f"sales.OrderPlan:{plan.pk}",
            uniq_key=f"sales:order-plan:{plan.pk}:material-delivery:{delivery_id}",
        ).first()
        payload = (
            workitem.payload if workitem and isinstance(workitem.payload, dict) else {}
        )
        scopes = payload.get("recipient_scopes")
        snapshot_rows = payload.get("materials")
        snapshot_lines = {
            row.get("delivery_line_id"): (row.get("material_id"), row.get("material_spec_id"))
            for row in (snapshot_rows if isinstance(snapshot_rows, list) else [])
            if isinstance(row, dict)
        }
        if (
            payload.get("order_plan_id") != plan.pk
            or payload.get("factory_id") != plan.factory_organization_id
            or payload.get("supplier_id") != supplier.pk
            or payload.get("delivery_id") != delivery_id
            or payload.get("material_plan_id") != production_subplan.material_plan_id
            or any(snapshot_lines.get(line_id) != row for line_id, row in lines.items())
            or not isinstance(scopes, dict)
        ):
            raise FactoryReturnProcessError("原订单配送任务或责任快照缺失，请核对原配送记录。")
        delivery_material_ids = {material_id for material_id, _spec_id in lines.values()}
        sales_task_openids = set(
            workitem.user_tasks.filter(to_role_snapshot="supplier_sales")
            .values_list("to_openid", flat=True)
        )
        scope_openids = {
            openid for openid, scope in scopes.items()
            if isinstance(scope, dict) and scope.get("role") == "supplier_sales"
        }
        if sales_task_openids != scope_openids:
            raise FactoryReturnProcessError("原订单配送任务接收人和责任快照不一致。")
        scoped_sources.extend(
            (scopes[openid], delivery_material_ids) for openid in sorted(sales_task_openids)
        )
    return scoped_sources, sorted(deliveries)


def _matching_bom_item(bom, source_bom_item):
    if not bom:
        return None
    return (
        bom.items.select_related(
            "material",
            "material_spec",
            "supplier",
        )
        .filter(
            material_id=source_bom_item.material_id,
            material_spec_id=source_bom_item.material_spec_id,
        )
        .order_by("id")
        .first()
    )


def _production_material_lots(requirement, *, factory_id):
    return [
        {
            "factory_lot_id": row.material_lot_id,
            "factory_batch_code": row.material_lot.batch_code,
            "supplier_material_batch_id": row.supplier_batch_id,
            "supplier_batch_code": row.material_lot.supplier_batch_code,
            "consumed_milli": row.consumed_milli,
        }
        for row in with_supplier_batch_id(
            consumed_material_lots(factory_id).filter(requirement=requirement)
        ).select_related("material_lot").order_by("material_lot_id")
    ]


def _resolve_material_trace(
    inventory_item,
    source_bom_item,
    order_item=_SOURCE_LOOKUP_REQUIRED,
    *,
    production_item=_SOURCE_LOOKUP_REQUIRED,
):
    """Trace the selected row against the device's own production material facts."""

    if production_item is _SOURCE_LOOKUP_REQUIRED:
        production_item = _source_production_subplan_item(inventory_item)
    if production_item:
        batch = inventory_item.source_production_batch
        requirement = production_item.material_requirements.select_related("supplier").filter(
            bom_item=source_bom_item,
            bom_item__bom_id=batch.bom_id,
            sales_bom_selection__order_plan_item_id=production_item.order_plan_item_id,
            sales_bom_selection__frozen_bom_id=batch.bom_id,
            material_id=source_bom_item.material_id,
            material_spec_id=source_bom_item.material_spec_id,
        ).first()
        if not requirement:
            raise FactoryReturnProcessError("所选物料缺少原生产批次的冻结物料明细，请先核对生产记录。")
        material_lots = _production_material_lots(requirement, factory_id=batch.manufacturer_id)
        if sum(row["consumed_milli"] for row in material_lots) != requirement.required_milli:
            raise FactoryReturnProcessError("所选物料的原生产批次消耗记录不完整，请先核对物料批次。")
        return {
            "supplier": requirement.supplier,
            "trace_source": DeviceMaterialFaultRecord.TraceSource.PRODUCTION_BATCH,
            "trace_source_name": "原生产批次物料消耗",
            "actual_binding_reference": "",
            "material_batch": material_lots[0]["factory_batch_code"] if len(material_lots) == 1 else "",
            "material_lots": material_lots,
        }

    if order_item is _SOURCE_LOOKUP_REQUIRED:
        order_item = _source_order_item(inventory_item, production_item=None)
    confirmation = getattr(order_item, "bom_confirmation", None) if order_item else None
    confirmed_bom_item = _matching_bom_item(
        confirmation.confirmed_bom if confirmation else None,
        source_bom_item,
    )
    if confirmed_bom_item and confirmed_bom_item.supplier_id:
        supplier = confirmed_bom_item.supplier
        trace_source = DeviceMaterialFaultRecord.TraceSource.PRODUCTION_BATCH
    elif source_bom_item.supplier_id:
        supplier = source_bom_item.supplier
        trace_source = DeviceMaterialFaultRecord.TraceSource.BOM_INFERRED
    else:
        supplier = None
        trace_source = DeviceMaterialFaultRecord.TraceSource.UNRESOLVED
    return {
        "supplier": supplier,
        "trace_source": trace_source,
        "trace_source_name": trace_source.label,
        "actual_binding_reference": "",
        "material_batch": "",
        "material_lots": [],
    }


def _material_warranty_snapshot(
    *,
    material_spec_id,
    supplier_id,
    production_date,
    evaluated_at,
):
    """Return a deterministic, read-only warranty snapshot for one material.

    The first operational version uses the original production-batch date as
    the warranty start.  Every result records the exact rule source so a later
    material-batch binding can be added without rewriting historical facts.
    """

    evaluated_date = (
        timezone.localtime(evaluated_at).date()
        if hasattr(evaluated_at, "tzinfo")
        else evaluated_at
    )
    relation = None
    result = DeviceMaterialWarrantyEvaluation.Result.UNKNOWN
    warranty_months = None
    warranty_end_date = None
    if not material_spec_id or not supplier_id:
        rule_source = DeviceMaterialWarrantyEvaluation.RuleSource.MISSING_TRACE
    else:
        relation = MaterialSpecSupplier.objects.filter(
            material_spec_id=material_spec_id,
            supplier_id=supplier_id,
        ).first()
        if not production_date:
            rule_source = (
                DeviceMaterialWarrantyEvaluation.RuleSource.MISSING_PRODUCTION_DATE
            )
        elif not relation:
            rule_source = (
                DeviceMaterialWarrantyEvaluation.RuleSource.MISSING_EXACT_RULE
            )
        else:
            rule_source = (
                DeviceMaterialWarrantyEvaluation.RuleSource.EXACT_SUPPLIER_SPEC
            )
            warranty_months = relation.warranty_months
            warranty_end_date = _add_calendar_months(
                production_date,
                warranty_months,
            )
            result = (
                DeviceMaterialWarrantyEvaluation.Result.IN_WARRANTY
                if evaluated_date <= warranty_end_date
                else DeviceMaterialWarrantyEvaluation.Result.OUT_OF_WARRANTY
            )
    return {
        "result": result,
        "result_name": dict(DeviceMaterialWarrantyEvaluation.Result.choices)[result],
        "rule_source": rule_source,
        "rule_source_name": dict(
            DeviceMaterialWarrantyEvaluation.RuleSource.choices
        )[rule_source],
        "relation": relation,
        "warranty_months": warranty_months,
        "warranty_start_date": production_date,
        "warranty_end_date": warranty_end_date,
        "evaluated_date": evaluated_date,
    }


def _serialize_material_warranty(snapshot):
    return {
        "result": snapshot["result"],
        "result_name": snapshot["result_name"],
        "rule_source": snapshot["rule_source"],
        "rule_source_name": snapshot["rule_source_name"],
        "warranty_months": snapshot["warranty_months"],
        "warranty_start_date": (
            snapshot["warranty_start_date"].isoformat()
            if snapshot["warranty_start_date"]
            else ""
        ),
        "warranty_end_date": (
            snapshot["warranty_end_date"].isoformat()
            if snapshot["warranty_end_date"]
            else ""
        ),
        "evaluated_date": snapshot["evaluated_date"].isoformat(),
        "computed_automatically": True,
    }


def _bom_material_options(inventory_item, *, order_item, production_item):
    existing = {
        row.source_bom_item_id: row
        for row in inventory_item.material_fault_records.select_related(
            "supplier",
        ).filter(source_bom_item__isnull=False)
    }
    repair_existing = {
        row.source_bom_item_id: row
        for row in inventory_item.repair_material_requirements.select_related(
            "supplier",
        ).all()
    }
    returned_existing = {
        (row.source_bom_item_id, row.return_kind): row
        for row in inventory_item.material_return_records.select_related(
            "supplier",
            "material_spec_supplier",
        ).all()
    }
    has_return_records = bool(returned_existing)
    options = []
    bom_items = inventory_item.source_bom.items.select_related(
        "material",
        "material_spec",
        "supplier",
    ).order_by("-material__is_key_part", "material__code", "id")
    for bom_item in bom_items:
        trace = _resolve_material_trace(
            inventory_item, bom_item, order_item, production_item=production_item,
        )
        fact = existing.get(bom_item.pk)
        repair_requirement = repair_existing.get(bom_item.pk)
        return_record = returned_existing.get(
            (
                bom_item.pk,
                FactoryReturnMaterialReturnRecord.ReturnKind.DISASSEMBLY,
            )
        )
        supplier = (
            return_record.supplier
            if return_record
            else (fact.supplier if fact else trace["supplier"])
        )
        trace_source = (
            return_record.trace_source
            if return_record
            else (fact.trace_source if fact else trace["trace_source"])
        )
        trace_name = (
            trace["trace_source_name"]
            if trace_source == trace["trace_source"]
            else dict(DeviceMaterialFaultRecord.TraceSource.choices).get(trace_source, "")
        )
        if return_record:
            warranty = {
                "result": return_record.warranty_result,
                "result_name": return_record.get_warranty_result_display(),
                "rule_source": return_record.warranty_rule_source,
                "rule_source_name": return_record.get_warranty_rule_source_display(),
                "warranty_months": return_record.warranty_months,
                "warranty_start_date": return_record.warranty_start_date,
                "warranty_end_date": return_record.warranty_end_date,
                "evaluated_date": return_record.warranty_evaluated_date,
            }
        else:
            warranty = _material_warranty_snapshot(
                material_spec_id=bom_item.material_spec_id,
                supplier_id=supplier.pk if supplier else None,
                production_date=inventory_item.source_production_batch.production_date,
                evaluated_at=timezone.now(),
            )
        options.append(
            {
                "bom_item_id": bom_item.pk,
                "material_id": bom_item.material_id,
                "material_code": bom_item.material.code,
                "material_name": bom_item.material.name,
                "is_key_part": bom_item.material.is_key_part,
                "material_spec_id": bom_item.material_spec_id,
                "material_spec_code": (
                    bom_item.material_spec.code if bom_item.material_spec_id else ""
                ),
                "material_spec_name": (
                    bom_item.material_spec.name if bom_item.material_spec_id else "未指定规格"
                ),
                "quantity": bom_item.quantity,
                "supplier_id": supplier.pk if supplier else None,
                "supplier_name": supplier.name if supplier else "供应商待确认",
                "trace_source": trace_source,
                "trace_source_name": trace_name,
                "supplier_inferred": trace_source
                == DeviceMaterialFaultRecord.TraceSource.BOM_INFERRED,
                "material_batch": (
                    return_record.material_batch_snapshot
                    if return_record
                    else (
                        fact.material_batch_snapshot
                        if fact
                        else trace["material_batch"]
                    )
                ),
                "material_lots": trace["material_lots"],
                # 关键件默认作为实物退回，生产经理可反选；故障标记独立。
                "returned_selected": bool(return_record) or (
                    not has_return_records
                    and bool(bom_item.material.is_key_part)
                ),
                "selected": bool(fact),
                "fault_selected": bool(fact),
                "original_note": (
                    return_record.original_note
                    if return_record
                    else (fact.original_note if fact else "")
                ),
                "is_warranty_candidate": (
                    fact.is_warranty_candidate if fact else False
                ),
                "warranty_evaluation": _serialize_material_warranty(warranty),
                "repair_selected": bool(repair_requirement),
                "repair_note": (
                    repair_requirement.original_note
                    if repair_requirement
                    else ""
                ),
            }
        )
    return options


def _create_repair_material_requirements(
    *,
    inventory_item,
    selections,
    actor,
    requested_at,
):
    if inventory_item.repair_material_requirements.exists():
        raise FactoryReturnProcessError("该设备已经保存结构化换料需求，不能重复覆盖。")
    selected_ids = [int(row.get("bom_item_id") or 0) for row in selections]
    if not selected_ids or any(value <= 0 for value in selected_ids):
        raise FactoryReturnProcessError("需要换料时，请至少勾选一项原BOM物料。")
    if len(selected_ids) != len(set(selected_ids)):
        raise FactoryReturnProcessError("同一BOM物料不能重复选择。")
    bom_items = {
        row.pk: row
        for row in inventory_item.source_bom.items.select_related(
            "material",
            "material_spec",
            "supplier",
        ).filter(pk__in=selected_ids)
    }
    if set(bom_items) != set(selected_ids):
        raise FactoryReturnProcessError("所选换料需求不属于该设备原BOM。")

    transfer = inventory_item.process.transfer
    device = inventory_item.transfer_item.device
    production_item = _source_production_subplan_item(inventory_item)
    order_item = _source_order_item(inventory_item, production_item=production_item)
    records = []
    for selection in selections:
        bom_item = bom_items[int(selection["bom_item_id"])]
        trace = _resolve_material_trace(
            inventory_item, bom_item, order_item, production_item=production_item,
        )
        spec = bom_item.material_spec
        supplier = trace["supplier"]
        records.append(
            FactoryReturnRepairMaterialRequirement.objects.create(
                inventory_item=inventory_item,
                device=device,
                source_order_plan=order_item.plan if order_item else None,
                source_order_plan_item=order_item,
                source_production_batch=inventory_item.source_production_batch,
                source_bom=inventory_item.source_bom,
                source_bom_item=bom_item,
                material=bom_item.material,
                material_spec=spec,
                supplier=supplier,
                trace_source=trace["trace_source"],
                schema_version=1,
                requested_quantity=bom_item.quantity,
                original_note=str(selection.get("note") or "").strip()[:500],
                requested_by=actor,
                requested_at=requested_at,
                device_sn_snapshot=device.sn,
                return_flow_no_snapshot=transfer.flow_no,
                production_batch_no_snapshot=(
                    inventory_item.source_production_batch.batch_no
                ),
                bom_version_snapshot=inventory_item.source_bom.version,
                material_code_snapshot=bom_item.material.code,
                material_name_snapshot=bom_item.material.name,
                material_spec_code_snapshot=spec.code if spec else "",
                material_spec_name_snapshot=spec.name if spec else "",
                supplier_name_snapshot=supplier.name if supplier else "",
            )
        )
    return records


def _serialize_repair_material_requirement(record, *, production_item=_SOURCE_LOOKUP_REQUIRED):
    returned_record = getattr(record, "returned_material_record", None)
    if (
        not record.source_order_plan_item_id
        or record.trace_source != DeviceMaterialFaultRecord.TraceSource.PRODUCTION_BATCH
    ):
        production_item = None
    elif production_item is _SOURCE_LOOKUP_REQUIRED:
        production_item = _source_production_subplan_item(record.inventory_item)
    material_trace = (
        _resolve_material_trace(
            record.inventory_item, record.source_bom_item, production_item=production_item,
        )
        if production_item and production_item.order_plan_item_id == record.source_order_plan_item_id
        else {}
    )
    return {
        "id": record.pk,
        "schema_version": record.schema_version,
        "material_id": record.material_id,
        "material_code": record.material_code_snapshot,
        "material_name": record.material_name_snapshot,
        "material_spec_id": record.material_spec_id,
        "material_spec_code": record.material_spec_code_snapshot,
        "material_spec_name": record.material_spec_name_snapshot,
        "requested_quantity": str(record.requested_quantity),
        "supplier_id": record.supplier_id,
        "supplier_name": record.supplier_name_snapshot or "供应商待确认",
        "trace_source": record.trace_source,
        "trace_source_name": material_trace.get("trace_source_name") or record.get_trace_source_display(),
        "source_order_plan_id": record.source_order_plan_id,
        "source_order_plan_item_id": record.source_order_plan_item_id,
        "source_production_batch_id": record.source_production_batch_id,
        "source_bom_id": record.source_bom_id,
        "source_bom_item_id": record.source_bom_item_id,
        "material_batch": material_trace.get("material_batch", ""),
        "material_lots": material_trace.get("material_lots", []),
        "supplier_inferred": (
            record.trace_source
            == DeviceMaterialFaultRecord.TraceSource.BOM_INFERRED
        ),
        "supplier_pending_confirmation": not bool(record.supplier_id),
        "original_note": record.original_note,
        "requested_by_name": _display_name(record.requested_by),
        "requested_at": record.requested_at.isoformat(),
        "notified": bool(record.notified_at),
        "notified_at": record.notified_at.isoformat() if record.notified_at else "",
        "actual_return_recorded": bool(returned_record),
        "actual_return_quantity": (
            str(returned_record.returned_quantity) if returned_record else "0.00"
        ),
        "actual_return_record": (
            _serialize_material_return_record(returned_record)
            if returned_record
            else None
        ),
        "new_material_quantity_rule": (
            "新换入物料由后续订单计划重新计数，本清单只统计换下退回旧件。"
        ),
    }


def _create_material_return_record(
    *,
    inventory_item,
    bom_item,
    return_kind,
    is_fault_material,
    note,
    actor,
    recorded_at,
    data_origin=FactoryReturnMaterialReturnRecord.DataOrigin.OPERATION,
):
    transfer = inventory_item.process.transfer
    device = inventory_item.transfer_item.device
    order_item = _source_order_item(inventory_item)
    trace = _resolve_material_trace(inventory_item, bom_item, order_item)
    spec = bom_item.material_spec
    supplier = trace["supplier"]
    warranty = _material_warranty_snapshot(
        material_spec_id=bom_item.material_spec_id,
        supplier_id=supplier.pk if supplier else None,
        production_date=inventory_item.source_production_batch.production_date,
        evaluated_at=recorded_at,
    )
    return FactoryReturnMaterialReturnRecord.objects.create(
        inventory_item=inventory_item,
        device=device,
        return_transfer=transfer,
        source_order_plan=order_item.plan if order_item else None,
        source_order_plan_item=order_item,
        source_production_batch=inventory_item.source_production_batch,
        source_bom=inventory_item.source_bom,
        source_bom_item=bom_item,
        material=bom_item.material,
        material_spec=spec,
        supplier=supplier,
        material_spec_supplier=warranty["relation"],
        return_kind=return_kind,
        returned_quantity=Decimal(str(bom_item.quantity)),
        is_fault_material=bool(is_fault_material),
        trace_source=trace["trace_source"],
        data_origin=data_origin,
        schema_version=2,
        actual_binding_reference=trace["actual_binding_reference"],
        material_batch_snapshot=trace["material_batch"],
        original_note=str(note or "").strip()[:500],
        returned_at=recorded_at,
        recorded_by=actor,
        recorded_organization=transfer.to_organization,
        recorded_role_snapshot="factory_production",
        warranty_result=warranty["result"],
        warranty_rule_source=warranty["rule_source"],
        warranty_months=warranty["warranty_months"],
        warranty_start_date=warranty["warranty_start_date"],
        warranty_end_date=warranty["warranty_end_date"],
        warranty_evaluated_date=warranty["evaluated_date"],
        device_sn_snapshot=device.sn,
        return_flow_no_snapshot=transfer.flow_no,
        production_batch_no_snapshot=inventory_item.source_production_batch.batch_no,
        bom_version_snapshot=inventory_item.source_bom.version,
        material_code_snapshot=bom_item.material.code,
        material_name_snapshot=bom_item.material.name,
        material_spec_code_snapshot=spec.code if spec else "",
        material_spec_name_snapshot=spec.name if spec else "",
        supplier_name_snapshot=supplier.name if supplier else "",
    )


def _create_fault_record_from_return(*, return_record, actor, recorded_at):
    inventory_item = return_record.inventory_item
    order_item = return_record.source_order_plan_item
    record = DeviceMaterialFaultRecord.objects.create(
        inventory_item=inventory_item,
        return_record=return_record,
        device=return_record.device,
        return_transfer=return_record.return_transfer,
        source_order_plan=return_record.source_order_plan,
        source_order_plan_item=order_item,
        source_production_batch=return_record.source_production_batch,
        source_bom=return_record.source_bom,
        source_bom_item=return_record.source_bom_item,
        material=return_record.material,
        material_spec=return_record.material_spec,
        supplier=return_record.supplier,
        trace_source=return_record.trace_source,
        schema_version=2,
        actual_binding_reference=return_record.actual_binding_reference,
        material_batch_snapshot=return_record.material_batch_snapshot,
        is_warranty_candidate=(
            return_record.warranty_result
            == DeviceMaterialWarrantyEvaluation.Result.IN_WARRANTY
        ),
        original_note=return_record.original_note,
        dismantled_at=recorded_at,
        recorded_by=actor,
        recorded_organization=return_record.recorded_organization,
        recorded_role_snapshot=return_record.recorded_role_snapshot,
        device_sn_snapshot=return_record.device_sn_snapshot,
        lifecycle_code_snapshot=order_item.lifecycle_code if order_item else "",
        return_flow_no_snapshot=return_record.return_flow_no_snapshot,
        order_plan_code_snapshot=(
            order_item.plan.plan_code if order_item else ""
        ),
        order_batch_code_snapshot=(
            order_item.order_batch_code if order_item else ""
        ),
        production_batch_no_snapshot=return_record.production_batch_no_snapshot,
        bom_version_snapshot=return_record.bom_version_snapshot,
        material_code_snapshot=return_record.material_code_snapshot,
        material_name_snapshot=return_record.material_name_snapshot,
        material_spec_code_snapshot=return_record.material_spec_code_snapshot,
        material_spec_name_snapshot=return_record.material_spec_name_snapshot,
        supplier_name_snapshot=return_record.supplier_name_snapshot,
        recorded_at=recorded_at,
    )
    DeviceMaterialWarrantyEvaluation.objects.create(
        fault_record=record,
        version=1,
        result=return_record.warranty_result,
        rule_source=return_record.warranty_rule_source,
        material_spec_supplier=return_record.material_spec_supplier,
        warranty_months=return_record.warranty_months,
        warranty_start_date=return_record.warranty_start_date,
        warranty_end_date=return_record.warranty_end_date,
        evaluated_date=return_record.warranty_evaluated_date,
        supplier_name_snapshot=return_record.supplier_name_snapshot,
        evaluated_by=actor,
    )
    return record


def _create_disassembly_material_records(
    *,
    inventory_item,
    selections,
    actor,
    recorded_at,
):
    if inventory_item.material_return_records.filter(
        return_kind=FactoryReturnMaterialReturnRecord.ReturnKind.DISASSEMBLY,
    ).exists():
        raise FactoryReturnProcessError("该设备已经保存拆机退回物料，不能覆盖原始事实。")
    selected_ids = [int(row.get("bom_item_id") or 0) for row in selections]
    if not selected_ids or any(value <= 0 for value in selected_ids):
        raise FactoryReturnProcessError("请至少勾选一项实际拆下退回物料。")
    if len(selected_ids) != len(set(selected_ids)):
        raise FactoryReturnProcessError("同一BOM物料不能重复选择。")
    if not any(bool(row.get("is_fault_material")) for row in selections):
        raise FactoryReturnProcessError("请至少标记一项导致本次拆机的故障物料。")
    bom_items = {
        row.pk: row
        for row in inventory_item.source_bom.items.select_related(
            "material",
            "material_spec",
            "supplier",
        ).filter(pk__in=selected_ids)
    }
    if set(bom_items) != set(selected_ids):
        raise FactoryReturnProcessError("所选退回物料不属于该设备实际BOM。")

    return_records = []
    fault_records = []
    for selection in selections:
        return_record = _create_material_return_record(
            inventory_item=inventory_item,
            bom_item=bom_items[int(selection["bom_item_id"])],
            return_kind=FactoryReturnMaterialReturnRecord.ReturnKind.DISASSEMBLY,
            is_fault_material=bool(selection.get("is_fault_material")),
            note=selection.get("note"),
            actor=actor,
            recorded_at=recorded_at,
        )
        return_records.append(return_record)
        if return_record.is_fault_material:
            fault_records.append(
                _create_fault_record_from_return(
                    return_record=return_record,
                    actor=actor,
                    recorded_at=recorded_at,
                )
            )
    return return_records, fault_records


def _create_repair_return_records(*, inventory_item, actor, recorded_at):
    """Record old parts actually removed when repair is completed.

    Requested replacement quantities remain planning data.  Only these return
    facts participate in returned-material and future settlement statistics.
    """

    records = []
    for requirement in inventory_item.repair_material_requirements.select_related(
        "source_bom_item__material",
        "source_bom_item__material_spec",
        "source_bom_item__supplier",
    ).order_by("id"):
        if requirement.returned_material_record_id:
            records.append(requirement.returned_material_record)
            continue
        return_record = _create_material_return_record(
            inventory_item=inventory_item,
            bom_item=requirement.source_bom_item,
            return_kind=FactoryReturnMaterialReturnRecord.ReturnKind.REPAIR_REPLACED,
            is_fault_material=False,
            note=requirement.original_note,
            actor=actor,
            recorded_at=recorded_at,
        )
        FactoryReturnRepairMaterialRequirement.objects.filter(
            pk=requirement.pk,
        ).update(returned_material_record=return_record)
        requirement.returned_material_record = return_record
        records.append(return_record)
    return records


def _serialize_fault_record(record):
    evaluation = getattr(record, "_current_warranty_evaluation", None)
    data = {
        "id": record.pk,
        "schema_version": record.schema_version,
        "trace_source": record.trace_source,
        "trace_source_name": record.get_trace_source_display(),
        "supplier_inferred": record.trace_source
        == DeviceMaterialFaultRecord.TraceSource.BOM_INFERRED,
        "supplier_pending_confirmation": record.trace_source
        == DeviceMaterialFaultRecord.TraceSource.UNRESOLVED,
        "material_id": record.material_id,
        "material_code": record.material_code_snapshot,
        "material_name": record.material_name_snapshot or "历史拆机自由文本",
        "material_spec_id": record.material_spec_id,
        "material_spec_code": record.material_spec_code_snapshot,
        "material_spec_name": record.material_spec_name_snapshot,
        "material_batch": record.material_batch_snapshot,
        "supplier_id": record.supplier_id,
        "supplier_name": record.supplier_name_snapshot or "供应商待确认",
        "is_warranty_candidate": record.is_warranty_candidate,
        "original_note": record.original_note,
        "recorded_by_name": (
            _display_name(record.recorded_by) if record.recorded_by_id else "历史数据"
        ),
        "recorded_role": record.recorded_role_snapshot,
        "recorded_at": record.recorded_at.isoformat(),
        "dismantled_at": record.dismantled_at.isoformat(),
    }
    if evaluation:
        data["warranty_evaluation"] = _serialize_warranty_evaluation(evaluation)
    return data


def _serialize_material_return_record(record):
    warranty = {
        "result": record.warranty_result,
        "result_name": record.get_warranty_result_display(),
        "rule_source": record.warranty_rule_source,
        "rule_source_name": record.get_warranty_rule_source_display(),
        "warranty_months": record.warranty_months,
        "warranty_start_date": record.warranty_start_date,
        "warranty_end_date": record.warranty_end_date,
        "evaluated_date": record.warranty_evaluated_date,
    }
    return {
        "id": record.pk,
        "schema_version": record.schema_version,
        "return_kind": record.return_kind,
        "return_kind_name": record.get_return_kind_display(),
        "returned_quantity": str(record.returned_quantity),
        "is_fault_material": record.is_fault_material,
        "material_id": record.material_id,
        "material_code": record.material_code_snapshot,
        "material_name": record.material_name_snapshot,
        "material_spec_id": record.material_spec_id,
        "material_spec_code": record.material_spec_code_snapshot,
        "material_spec_name": record.material_spec_name_snapshot,
        "material_batch": record.material_batch_snapshot,
        "supplier_id": record.supplier_id,
        "supplier_name": record.supplier_name_snapshot or "供应商待确认",
        "trace_source": record.trace_source,
        "trace_source_name": record.get_trace_source_display(),
        "supplier_inferred": (
            record.trace_source
            == DeviceMaterialFaultRecord.TraceSource.BOM_INFERRED
        ),
        "supplier_pending_confirmation": not bool(record.supplier_id),
        "original_note": record.original_note,
        "returned_at": record.returned_at.isoformat(),
        "recorded_by_name": (
            _display_name(record.recorded_by) if record.recorded_by_id else "历史数据"
        ),
        "data_origin": record.data_origin,
        "data_origin_name": record.get_data_origin_display(),
        "settlement_status": record.settlement_status,
        "settlement_status_name": record.get_settlement_status_display(),
        "warranty_evaluation": _serialize_material_warranty(warranty),
    }


def _serialize_warranty_evaluation(evaluation):
    return {
        "id": evaluation.pk,
        "version": evaluation.version,
        "result": evaluation.result,
        "result_name": evaluation.get_result_display(),
        "rule_source": evaluation.rule_source,
        "rule_source_name": evaluation.get_rule_source_display(),
        "warranty_months": evaluation.warranty_months,
        "warranty_start_date": (
            evaluation.warranty_start_date.isoformat()
            if evaluation.warranty_start_date
            else ""
        ),
        "warranty_end_date": (
            evaluation.warranty_end_date.isoformat()
            if evaluation.warranty_end_date
            else ""
        ),
        "evaluated_date": evaluation.evaluated_date.isoformat(),
    }


def _add_calendar_months(value, months):
    month_index = value.month - 1 + int(months)
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _evaluate_fault_warranty(*, record, actor, evaluated_at):
    """固化自动质保结果；新数据直接复用对应实际退回物料快照。"""

    existing = record.warranty_evaluations.order_by("-version", "-id").first()
    if existing:
        return existing

    return_record = getattr(record, "return_record", None)
    if return_record:
        return DeviceMaterialWarrantyEvaluation.objects.create(
            fault_record=record,
            version=1,
            result=return_record.warranty_result,
            rule_source=return_record.warranty_rule_source,
            material_spec_supplier=return_record.material_spec_supplier,
            warranty_months=return_record.warranty_months,
            warranty_start_date=return_record.warranty_start_date,
            warranty_end_date=return_record.warranty_end_date,
            evaluated_date=return_record.warranty_evaluated_date,
            supplier_name_snapshot=return_record.supplier_name_snapshot,
            evaluated_by=actor,
        )

    snapshot = _material_warranty_snapshot(
        material_spec_id=record.material_spec_id,
        supplier_id=record.supplier_id,
        production_date=record.source_production_batch.production_date,
        evaluated_at=evaluated_at,
    )

    return DeviceMaterialWarrantyEvaluation.objects.create(
        fault_record=record,
        version=1,
        result=snapshot["result"],
        rule_source=snapshot["rule_source"],
        material_spec_supplier=snapshot["relation"],
        warranty_months=snapshot["warranty_months"],
        warranty_start_date=snapshot["warranty_start_date"],
        warranty_end_date=snapshot["warranty_end_date"],
        evaluated_date=snapshot["evaluated_date"],
        supplier_name_snapshot=record.supplier_name_snapshot,
        evaluated_by=actor,
    )


def _supplier_warranty_link(dispatch):
    return (
        "/pages/supply_chain/warranty_list/warranty_list"
        f"?dispatch_id={dispatch.pk}"
    )


def _publish_supplier_warranty_dispatches(*, inventory_item, actor, confirmed_at):
    returned_records = list(
        inventory_item.material_return_records.select_related(
            "material",
            "supplier",
            "material_spec_supplier",
        ).filter(
            return_kind=FactoryReturnMaterialReturnRecord.ReturnKind.DISASSEMBLY,
        )
    )
    if not returned_records:
        raise FactoryReturnProcessError("该设备没有结构化拆机退回物料。")
    unresolved = [
        row.material_name_snapshot for row in returned_records if not row.supplier_id
    ]
    if unresolved:
        raise FactoryReturnProcessError(
            "以下实际退回物料的供应商待确认，暂不能通知：" + "、".join(unresolved)
        )

    grouped = defaultdict(list)
    for row in returned_records:
        grouped[row.supplier_id].append(row)
    dispatches = []
    for supplier_id, material_return_records in grouped.items():
        supplier = material_return_records[0].supplier
        material_ids = {
            row.material_id for row in material_return_records if row.material_id
        }
        recipients = _original_supplier_task_recipients(
            inventory_item=inventory_item,
            supplier=supplier,
            material_ids=material_ids,
        )
        if not recipients["owner_openids"]:
            raise FactoryReturnProcessError(
                f"供应商“{supplier.name}”缺少有效负责人账号。"
            )
        dispatch = FactoryReturnWarrantyDispatch.objects.create(
            inventory_item=inventory_item,
            supplier=supplier,
            supplier_name_snapshot=supplier.name,
            confirmed_by=actor,
            confirmed_role_snapshot="factory_matching",
            confirmed_at=confirmed_at,
        )
        fault_records = list(
            inventory_item.material_fault_records.filter(
                return_record_id__in=[row.pk for row in material_return_records],
            ).order_by("id")
        )
        FactoryReturnWarrantyDispatchItem.objects.bulk_create(
            [
                FactoryReturnWarrantyDispatchItem(
                    dispatch=dispatch,
                    fault_record=record,
                )
                for record in fault_records
            ]
        )
        transfer = inventory_item.process.transfer
        payload_materials = [
            {
                "return_record_id": record.pk,
                "material_id": record.material_id,
                "material_name": record.material_name_snapshot,
                "material_spec_name": record.material_spec_name_snapshot,
                "material_batch": record.material_batch_snapshot,
                "returned_quantity": str(record.returned_quantity),
                "is_fault_material": record.is_fault_material,
                "warranty_result": record.warranty_result,
                "warranty_result_name": record.get_warranty_result_display(),
                "warranty_start_date": (
                    record.warranty_start_date.isoformat()
                    if record.warranty_start_date else ""
                ),
                "warranty_end_date": (
                    record.warranty_end_date.isoformat()
                    if record.warranty_end_date else ""
                ),
                "trace_source": record.trace_source,
                "trace_source_name": record.get_trace_source_display(),
                "production_note": record.original_note,
            }
            for record in material_return_records
        ]
        publish_workitem(
            domain="AFTERSALE",
            type=SUPPLIER_WARRANTY_WORK_TYPE,
            biz_id=str(dispatch.pk),
            biz_ref=f"lifecycle.FactoryReturnWarrantyDispatch:{dispatch.pk}",
            uniq_key=f"lifecycle:factory-return-warranty:{dispatch.pk}",
            title="厂家已推送拆机退回物料及自动质保结果",
            summary=(
                f"{inventory_item.transfer_item.device.sn}｜"
                f"{len(material_return_records)}项实际退回｜{transfer.flow_no}"
            ),
            link=_supplier_warranty_link(dispatch),
            recipients_openids=recipients["openids"],
            need_action=False,
            action_mode="EACH_ACK",
            payload={
                "dispatch_id": dispatch.pk,
                "inventory_item_id": inventory_item.pk,
                "supplier_id": supplier.pk,
                "supplier_name": supplier.name,
                "factory_organization_id": transfer.to_organization_id,
                "factory_organization_name": transfer.to_organization.name,
                "return_flow_no": transfer.flow_no,
                "device_sn": inventory_item.transfer_item.device.sn,
                "materials": payload_materials,
                "recipient_scopes": recipients["recipient_scopes"],
                "responsibility_source": recipients[
                    "responsibility_source"
                ],
                "responsibility_source_name": recipients[
                    "responsibility_source_name"
                ],
                "source_delivery_ids": recipients.get("source_delivery_ids", []),
                "unassigned_sales_material_ids": recipients[
                    "unassigned_sales_material_ids"
                ],
                "notice_mode": "EACH_ACK",
                "actual_return_count": len(material_return_records),
                "fault_occurrence_count": len(fault_records),
                "schema_version": 4,
            },
            actor_openid=actor.wx_openid or "",
            mark_workitem_done_immediately=True,
        )
        dispatches.append(dispatch)
    return dispatches


def _publish_supplier_repair_material_requests(*, inventory_item, actor, confirmed_at):
    requirements = list(
        inventory_item.repair_material_requirements.select_related(
            "material",
            "material_spec",
            "supplier",
        ).order_by("id")
    )
    if not requirements:
        raise FactoryReturnProcessError("该设备没有结构化修复换料需求。")
    unresolved = [
        row.material_name_snapshot for row in requirements if not row.supplier_id
    ]
    if unresolved:
        raise FactoryReturnProcessError(
            "以下换料需求的原供应商待确认，暂不能推送：" + "、".join(unresolved)
        )

    transfer = inventory_item.process.transfer
    grouped = defaultdict(list)
    for row in requirements:
        grouped[row.supplier_id].append(row)
    notified_supplier_count = 0
    for supplier_id, rows in grouped.items():
        supplier = rows[0].supplier
        material_ids = {row.material_id for row in rows}
        recipients = _original_supplier_task_recipients(
            inventory_item=inventory_item,
            supplier=supplier,
            material_ids=material_ids,
        )
        if not recipients["owner_openids"]:
            raise FactoryReturnProcessError(
                f"供应商“{supplier.name}”缺少有效负责人账号。"
            )
        payload_materials = [
            {
                "requirement_id": row.pk,
                "material_id": row.material_id,
                "material_name": row.material_name_snapshot,
                "material_spec_name": row.material_spec_name_snapshot,
                "requested_quantity": str(row.requested_quantity),
                "trace_source": row.trace_source,
                "trace_source_name": row.get_trace_source_display(),
                "production_note": row.original_note,
            }
            for row in rows
        ]
        publish_workitem(
            domain="SUPPLY_CHAIN",
            type=SUPPLIER_REPAIR_MATERIAL_WORK_TYPE,
            biz_id=f"{inventory_item.pk}:{supplier.pk}",
            biz_ref=(
                "lifecycle.FactoryReturnRepairMaterialRequirement:"
                f"{inventory_item.pk}:{supplier.pk}"
            ),
            uniq_key=(
                "lifecycle:factory-return-repair-material:"
                f"{inventory_item.pk}:{supplier.pk}"
            ),
            title="厂家退货修复需要原配套物料",
            summary=(
                f"{inventory_item.transfer_item.device.sn}｜"
                f"{len(rows)}项物料｜{transfer.flow_no}"
            ),
            link=(
                "/pages/supply_chain/warranty_list/warranty_list"
                f"?tab=repair&repair_item_id={inventory_item.pk}"
            ),
            recipients_openids=recipients["openids"],
            need_action=False,
            action_mode="EACH_ACK",
            payload={
                "inventory_item_id": inventory_item.pk,
                "supplier_id": supplier.pk,
                "supplier_name": supplier.name,
                "factory_organization_id": transfer.to_organization_id,
                "factory_organization_name": transfer.to_organization.name,
                "return_flow_no": transfer.flow_no,
                "device_sn": inventory_item.transfer_item.device.sn,
                "materials": payload_materials,
                "recipient_scopes": recipients["recipient_scopes"],
                "responsibility_source": recipients[
                    "responsibility_source"
                ],
                "responsibility_source_name": recipients[
                    "responsibility_source_name"
                ],
                "source_delivery_ids": recipients.get("source_delivery_ids", []),
                "unassigned_sales_material_ids": recipients[
                    "unassigned_sales_material_ids"
                ],
                "notice_mode": "EACH_ACK",
                "schema_version": 2,
            },
            actor_openid=actor.wx_openid or "",
            mark_workitem_done_immediately=True,
        )
        FactoryReturnRepairMaterialRequirement.objects.filter(
            pk__in=[row.pk for row in rows],
        ).update(
            notified_by=actor,
            notified_at=confirmed_at,
        )
        notified_supplier_count += 1
    return notified_supplier_count


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
    repair_materials=None,
    disassembly_result="",
    has_warranty_material=None,
    fault_materials=None,
    returned_materials=None,
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
    if action in {"REPAIR_DIAGNOSIS", "REPAIR_COMPLETE"}:
        task_type = REPAIR_PRODUCTION_WORK_TYPE
    elif action == "DISASSEMBLY_COMPLETE":
        task_type = DISASSEMBLY_PRODUCTION_WORK_TYPE
    else:
        task_type = ""
    if task_type:
        _require_role_task_access(
            identity,
            transfer,
            "factory_production",
            (task_type,),
        )
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
        item.stock_status = FactoryReturnInventoryItem.StockStatus.REPAIRING
        if item.needs_replacement_material:
            requirements = _create_repair_material_requirements(
                inventory_item=item,
                selections=list(repair_materials or []),
                actor=user,
                requested_at=now,
            )
            item.material_requirement = (
                "结构化换料需求："
                + "、".join(
                    f"{row.material_name_snapshot}"
                    f"({row.material_spec_name_snapshot or '未指定规格'})"
                    for row in requirements
                )
            )[:1000]
            item.production_status = (
                FactoryReturnInventoryItem.ProductionStatus.WAITING_MATCHING
            )
            item.matching_status = (
                FactoryReturnInventoryItem.MatchingStatus.PENDING
            )
        else:
            item.material_requirement = ""
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
            "repair_material_requirement_ids": (
                [row.pk for row in requirements]
                if item.needs_replacement_material
                else []
            ),
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
        repair_return_records = (
            _create_repair_return_records(
                inventory_item=item,
                actor=user,
                recorded_at=now,
            )
            if item.needs_replacement_material
            else []
        )
        item.stock_status = FactoryReturnInventoryItem.StockStatus.PENDING_REISSUE
        item.production_status = (
            FactoryReturnInventoryItem.ProductionStatus.COMPLETED
        )
        event_type = "FACTORY_RETURN_REPAIR_COMPLETE"
        event_data = {
            "production_remark": item.production_remark,
            "needs_replacement_material": item.needs_replacement_material,
            "material_plan": item.material_plan,
            "actual_repair_return_record_ids": [
                row.pk for row in repair_return_records
            ],
            "actual_repair_return_quantity": str(
                sum(
                    (row.returned_quantity for row in repair_return_records),
                    Decimal("0"),
                )
            ),
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
        selections = list(returned_materials or [])
        if not selections and fault_materials:
            # Compatibility with the previously released client: rows in the
            # old field were already explicitly selected as fault materials.
            selections = [
                {
                    "bom_item_id": row.get("bom_item_id"),
                    "note": row.get("note", ""),
                    "is_fault_material": True,
                }
                for row in fault_materials
            ]
        return_records, fault_records = _create_disassembly_material_records(
            inventory_item=item,
            selections=selections,
            actor=user,
            recorded_at=now,
        )
        material_names = [row.material_name_snapshot for row in return_records]
        fault_material_names = [
            row.material_name_snapshot for row in fault_records
        ]
        legacy_summary = str(disassembly_result or "").strip()[:1000]
        item.disassembly_result = legacy_summary or (
            "实际退回物料："
            + "、".join(material_names)
            + "；故障物料："
            + "、".join(fault_material_names)
        )[:1000]
        item.has_warranty_material = any(
            row.warranty_result
            == DeviceMaterialWarrantyEvaluation.Result.IN_WARRANTY
            for row in return_records
        )
        item.production_remark = str(production_remark or "").strip()[:1000]
        item.stock_status = FactoryReturnInventoryItem.StockStatus.LIFECYCLE_ENDED
        item.production_status = (
            FactoryReturnInventoryItem.ProductionStatus.COMPLETED
        )
        item.matching_status = FactoryReturnInventoryItem.MatchingStatus.PENDING
        event_type = "FACTORY_RETURN_DISASSEMBLY_COMPLETE"
        event_data = {
            "disassembly_result": item.disassembly_result,
            "has_warranty_material": item.has_warranty_material,
            "production_remark": item.production_remark,
            "material_return_record_ids": [row.pk for row in return_records],
            "returned_material_count": len(return_records),
            "returned_material_quantity": str(
                sum(
                    (row.returned_quantity for row in return_records),
                    Decimal("0"),
                )
            ),
            "fault_record_ids": [row.pk for row in fault_records],
            "fault_occurrence_count": len(fault_records),
            "in_warranty_return_count": sum(
                row.warranty_result
                == DeviceMaterialWarrantyEvaluation.Result.IN_WARRANTY
                for row in return_records
            ),
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
    action = str(action or "").upper()
    if action == "REPAIR_MATERIAL_PLAN":
        task_type = REPAIR_MATCHING_WORK_TYPE
    elif action == "DISASSEMBLY_MATERIAL_DISPOSITION":
        task_type = DISASSEMBLY_MATCHING_WORK_TYPE
    else:
        task_type = ""
    if task_type:
        _require_role_task_access(
            identity,
            transfer,
            "factory_matching",
            (task_type,),
        )
    if item.matching_status != FactoryReturnInventoryItem.MatchingStatus.PENDING:
        raise FactoryReturnProcessError("当前仍在等待生产结果或配套处置已经完成。")
    if action == "REPAIR_MATERIAL_PLAN":
        plan = str(material_plan or "").strip()[:1000]
        if (
            item.stock_status != FactoryReturnInventoryItem.StockStatus.REPAIRING
            or not item.needs_replacement_material
        ):
            raise FactoryReturnProcessError("该设备没有待处理的修复物料需求。")
        notified_supplier_count = _publish_supplier_repair_material_requests(
            inventory_item=item,
            actor=user,
            confirmed_at=timezone.now(),
        )
        item.material_plan = (
            plan
            or f"已核对原BOM换料需求，并向{notified_supplier_count}家原供应商推送"
        )[:1000]
        item.production_status = (
            FactoryReturnInventoryItem.ProductionStatus.READY_TO_COMPLETE
        )
        branch = "REPAIR"
    elif action == "DISASSEMBLY_MATERIAL_DISPOSITION":
        if (
            item.stock_status
            != FactoryReturnInventoryItem.StockStatus.LIFECYCLE_ENDED
            or not item.material_return_records.filter(
                return_kind=(
                    FactoryReturnMaterialReturnRecord.ReturnKind.DISASSEMBLY
                )
            ).exists()
        ):
            raise FactoryReturnProcessError("该设备没有待核对的拆机实际退回物料。")
        now = timezone.now()
        dispatches = _publish_supplier_warranty_dispatches(
            inventory_item=item,
            actor=user,
            confirmed_at=now,
        )
        item.warranty_disposition = (
            f"已确认{item.material_return_records.filter(return_kind='DISASSEMBLY').count()}项"
            f"实际退回物料及自动质保结果，并向{len(dispatches)}家供应商生成协同任务"
        )[:1000]
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
            "warranty_dispatch_ids": (
                [row.pk for row in dispatches]
                if action == "DISASSEMBLY_MATERIAL_DISPOSITION"
                else []
            ),
            "repair_supplier_count": (
                notified_supplier_count
                if action == "REPAIR_MATERIAL_PLAN"
                else 0
            ),
        },
    )
    _sync_internal_branch_tasks(transfer, branch, user)
    return _serialize_process(item.process, identity), True


def _serialize_inventory_item(item, can_post, role_codes=None):
    if isinstance(role_codes, str):
        role_codes = {role_codes}
    else:
        role_codes = set(role_codes or ())
    transfer_item = item.transfer_item
    device = transfer_item.device
    qrcode = getattr(device, "qrcode", None)
    production_item = _source_production_subplan_item(item)
    order_item = _source_order_item(item, production_item=production_item)
    fault_records = list(
        item.material_fault_records.select_related(
            "material",
            "material_spec",
            "supplier",
            "recorded_by",
        ).order_by("id")
    )
    warranty_candidates = [
        row for row in fault_records if row.is_warranty_candidate
    ]
    unresolved_warranty_count = sum(
        not row.supplier_id for row in warranty_candidates
    )
    material_return_records = list(
        item.material_return_records.select_related(
            "material",
            "material_spec",
            "supplier",
            "material_spec_supplier",
            "recorded_by",
        ).order_by("return_kind", "id")
    )
    disassembly_return_records = [
        row
        for row in material_return_records
        if row.return_kind
        == FactoryReturnMaterialReturnRecord.ReturnKind.DISASSEMBLY
    ]
    unresolved_return_count = sum(
        not row.supplier_id for row in disassembly_return_records
    )
    repair_requirements = list(
        item.repair_material_requirements.select_related(
            "material",
            "material_spec",
            "supplier",
            "requested_by",
            "returned_material_record__material",
            "returned_material_record__material_spec",
            "returned_material_record__supplier",
            "returned_material_record__recorded_by",
        ).order_by("id")
    )
    unresolved_repair_count = sum(
        not row.supplier_id for row in repair_requirements
    )
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
        "source_order": _source_order_trace(item, order_item, production_item),
        "bom_material_options": _bom_material_options(
            item, order_item=order_item, production_item=production_item,
        ),
        "repair_material_requirements": [
            _serialize_repair_material_requirement(record, production_item=production_item)
            for record in repair_requirements
        ],
        "repair_material_summary": {
            "total": len(repair_requirements),
            "supplier_pending_confirmation": unresolved_repair_count,
            "notified": sum(bool(row.notified_at) for row in repair_requirements),
        },
        "material_return_records": [
            _serialize_material_return_record(record)
            for record in material_return_records
        ],
        "material_return_summary": {
            "actual_line_count": len(material_return_records),
            "actual_quantity": str(
                sum(
                    (row.returned_quantity for row in material_return_records),
                    Decimal("0"),
                )
            ),
            "disassembly_line_count": len(disassembly_return_records),
            "fault_occurrence_count": sum(
                bool(row.is_fault_material) for row in material_return_records
            ),
            "in_warranty_count": sum(
                row.warranty_result
                == DeviceMaterialWarrantyEvaluation.Result.IN_WARRANTY
                for row in material_return_records
            ),
            "supplier_pending_confirmation": unresolved_return_count,
        },
        "fault_material_records": [
            _serialize_fault_record(record) for record in fault_records
        ],
        "fault_material_summary": {
            "total": len(fault_records),
            "warranty_candidates": len(warranty_candidates),
            "supplier_pending_confirmation": unresolved_warranty_count,
        },
        "warranty_dispatches": [
            {
                "id": dispatch.pk,
                "supplier_id": dispatch.supplier_id,
                "supplier_name": dispatch.supplier_name_snapshot,
                "status": dispatch.status,
                "status_name": dispatch.get_status_display(),
                "material_count": dispatch.dispatch_items.count(),
            }
            for dispatch in item.warranty_dispatches.select_related(
                "supplier",
            ).order_by("supplier_name_snapshot", "id")
        ],
        "actions": {
            "can_classify": bool(can_post),
            "can_submit_repair_diagnosis": bool(
                "factory_production" in role_codes
                and item.stock_status
                == FactoryReturnInventoryItem.StockStatus.PENDING_REPAIR
                and item.production_status
                == FactoryReturnInventoryItem.ProductionStatus.PENDING
            ),
            "can_complete_repair": bool(
                "factory_production" in role_codes
                and item.stock_status
                == FactoryReturnInventoryItem.StockStatus.REPAIRING
                and item.production_status
                == FactoryReturnInventoryItem.ProductionStatus.READY_TO_COMPLETE
            ),
            "can_complete_disassembly": bool(
                "factory_production" in role_codes
                and item.stock_status
                == FactoryReturnInventoryItem.StockStatus.PENDING_DISASSEMBLY
                and item.production_status
                == FactoryReturnInventoryItem.ProductionStatus.PENDING
            ),
            "can_submit_repair_material_plan": bool(
                "factory_matching" in role_codes
                and item.stock_status
                == FactoryReturnInventoryItem.StockStatus.REPAIRING
                and item.matching_status
                == FactoryReturnInventoryItem.MatchingStatus.PENDING
                and item.needs_replacement_material
            ),
            "can_notify_repair_suppliers": bool(
                "factory_matching" in role_codes
                and item.stock_status
                == FactoryReturnInventoryItem.StockStatus.REPAIRING
                and item.matching_status
                == FactoryReturnInventoryItem.MatchingStatus.PENDING
                and item.needs_replacement_material
                and repair_requirements
                and unresolved_repair_count == 0
            ),
            "can_submit_disassembly_disposition": bool(
                "factory_matching" in role_codes
                and item.stock_status
                == FactoryReturnInventoryItem.StockStatus.LIFECYCLE_ENDED
                and item.matching_status
                == FactoryReturnInventoryItem.MatchingStatus.PENDING
                and disassembly_return_records
            ),
            "can_notify_warranty_suppliers": bool(
                "factory_matching" in role_codes
                and item.stock_status
                == FactoryReturnInventoryItem.StockStatus.LIFECYCLE_ENDED
                and item.matching_status
                == FactoryReturnInventoryItem.MatchingStatus.PENDING
                and disassembly_return_records
                and unresolved_return_count == 0
            ),
        },
    }


def _serialize_role_workflow_tasks(transfer, identity):
    """Return planned and published nodes for the current factory role.

    A node becomes readable only when a real UserTask addressed to the current
    OpenID exists.  Keeping unpublished nodes in the response lets the client
    explain the whole role workflow without fabricating tasks or permissions.
    """

    role_codes = effective_role_codes(identity)
    node_definitions = tuple(
        node
        for role_code, definitions in FACTORY_RETURN_ROLE_TASK_NODES.items()
        if role_code in role_codes
        for node in definitions
    )
    if not node_definitions:
        return []

    task_types = [node["type"] for node in node_definitions]
    workitems = list(
        WorkItem.objects.filter(
            biz_ref=f"lifecycle.GoodsTransfer:{transfer.pk}",
            type__in=task_types,
        )
        .prefetch_related("user_tasks")
        .order_by("-id")
    )
    workitem_by_type = {}
    for workitem in workitems:
        workitem_by_type.setdefault(workitem.type, workitem)

    openid = identity.user.wx_openid or ""
    nodes = []
    for sequence, definition in enumerate(node_definitions, start=1):
        workitem = workitem_by_type.get(definition["type"])
        user_task = None
        if workitem and openid:
            user_task = next(
                (
                    row
                    for row in workitem.user_tasks.all()
                    if row.to_openid == openid
                ),
                None,
            )

        published = bool(user_task)
        read_state = "UNREAD" if published and user_task.state == "NEW" else "READ"
        read_state_name = "未读" if read_state == "UNREAD" else "已读"
        if not published:
            read_state = "NOT_PUBLISHED"
            read_state_name = "尚未推送"

        action_state = ""
        action_state_name = ""
        if definition["is_action"]:
            if not published:
                action_state = "NOT_PUBLISHED"
                action_state_name = "尚未推送"
            elif (
                user_task.state == "DONE"
                or workitem.status == "DONE"
            ):
                action_state = "COMPLETED"
                action_state_name = "已完成"
            elif user_task.need_action:
                action_state = "PENDING"
                action_state_name = "待处理"
            elif workitem.status == "CANCELLED":
                action_state = "CANCELLED"
                action_state_name = "已取消"
            else:
                action_state = "WAITING"
                action_state_name = "等待前置步骤"

        nodes.append(
            {
                "sequence": sequence,
                "type": definition["type"],
                "title": definition["title"],
                "event_title": (
                    user_task.display_title_snapshot
                    or workitem.title
                    or definition["title"]
                    if user_task
                    else ""
                ),
                "summary": (
                    user_task.display_subtitle_snapshot
                    or workitem.summary
                    or ""
                    if user_task
                    else "业务条件满足后由系统推送"
                ),
                "is_action": definition["is_action"],
                "published": published,
                "task_id": user_task.pk if user_task else None,
                "read_state": read_state,
                "read_state_name": read_state_name,
                "action_state": action_state,
                "action_state_name": action_state_name,
                "can_mark_read": bool(
                    published and user_task.state == "NEW"
                ),
                "cursor_time": (
                    user_task.cursor_time.isoformat()
                    if user_task
                    else ""
                ),
            }
        )
    return nodes


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
        _role_task_access(
            identity,
            transfer,
            "factory_logistics",
            (PICKUP_WORK_TYPE,),
            require_action=True,
        )
        and process.phase == FactoryReturnProcess.Phase.WAITING_PICKUP
    )
    can_post = bool(
        _role_task_access(
            identity,
            transfer,
            "factory_sales_assistant",
            (POSTING_WORK_TYPE,),
            require_action=True,
        )
        and process.phase == FactoryReturnProcess.Phase.WAITING_POSTING
    )
    action_roles = set()
    if _role_task_access(
        identity,
        transfer,
        "factory_production",
        (DISASSEMBLY_PRODUCTION_WORK_TYPE, REPAIR_PRODUCTION_WORK_TYPE),
        require_action=True,
    ):
        action_roles.add("factory_production")
    if _role_task_access(
        identity,
        transfer,
        "factory_matching",
        (DISASSEMBLY_MATCHING_WORK_TYPE, REPAIR_MATCHING_WORK_TYPE),
        require_action=True,
    ):
        action_roles.add("factory_matching")
    account_case = process.account_case
    credit_amount_recorded = bool(
        account_case
        and account_case.status == GoodsTransferAccountCase.Status.CONFIRMED
        and account_case.confirmed_amount is not None
    )
    can_record_credit_amount = bool(
        _role_task_access(
            identity,
            transfer,
            "factory_sales_assistant",
            (POSTING_WORK_TYPE,),
            require_action=True,
        )
        and process.picked_at
        and account_case
        and not credit_amount_recorded
    )
    assistant_workitem = WorkItem.objects.filter(
        type=POSTING_WORK_TYPE,
        uniq_key=f"lifecycle:factory-return:{transfer.pk}:posting",
    ).first()
    can_view_account_case = bool(
        _role_task_access(
            identity,
            transfer,
            "factory_sales",
            (SALES_NOTICE_SUBMITTED,),
        )
        or _role_task_access(
            identity,
            transfer,
            "factory_sales_assistant",
            (POSTING_WORK_TYPE,),
        )
    )
    inventory_summary = defaultdict(int)
    for inventory_item in inventory_items:
        inventory_summary[inventory_item.stock_status] += 1
    workflow_tasks = _serialize_role_workflow_tasks(transfer, identity)
    return {
        "root_organization": goods_transfer_root_payload(transfer),
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
        "workflow_tasks": workflow_tasks,
        "workflow_task_summary": {
            "total": len(workflow_tasks),
            "published": sum(row["published"] for row in workflow_tasks),
            "unread": sum(
                row["read_state"] == "UNREAD" for row in workflow_tasks
            ),
            "pending_action": sum(
                row["action_state"] == "PENDING"
                for row in workflow_tasks
            ),
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
            _serialize_inventory_item(item, can_post, action_roles)
            for item in inventory_items
        ],
        "actions": {
            "can_confirm_pickup": can_pickup,
            "can_post_inventory": can_post,
            "can_record_credit_amount": can_record_credit_amount,
            "assistant_action_locked": bool(
                identity_has_role(identity, "factory_sales_assistant")
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
    if not _manager_can_view_return(identity, process.transfer):
        raise FactoryReturnProcessError("该退货流程没有发给当前高级经理的协同任务。")
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
    inventory_queryset = (
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
        ).filter(
            process__transfer__to_organization_id=identity.organization_id,
            process__phase=FactoryReturnProcess.Phase.POSTED,
        )
    )
    if identity.role.code in SENIOR_MANAGER_ROLE_CODES:
        inventory_queryset = inventory_queryset.filter(
            process__transfer_id__in=_manager_task_transfer_ids(identity),
        )
    items = list(inventory_queryset.order_by("stock_status", "-classified_at", "-pk"))
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


def _supplier_warranty_dispatch_access(user, dispatch, *, require_open_task=False):
    identity = get_active_identity(user)
    if (
        not identity
        or not identity.is_active
        or identity.organization_id != dispatch.supplier_id
        or identity.organization.organization_type.code != "SUPPLIER"
        or identity.role.code not in {"supplier_owner", "supplier_sales"}
    ):
        raise FactoryReturnProcessError("当前账号不能查看该供应商三包任务。")
    workitem = WorkItem.objects.filter(
        type=SUPPLIER_WARRANTY_WORK_TYPE,
        uniq_key=f"lifecycle:factory-return-warranty:{dispatch.pk}",
    ).first()
    if not workitem:
        raise FactoryReturnProcessError("供应商三包协同任务不存在。")
    task = workitem.user_tasks.filter(to_openid=user.wx_openid).first()
    if not task:
        raise FactoryReturnProcessError("当前账号不在该三包任务的责任范围内。")
    if require_open_task and (
        workitem.status != "OPEN" or task.state == "DONE" or not task.need_action
    ):
        raise FactoryReturnProcessError("该三包任务已经由协同账号接收。")
    return identity, workitem, task


def _serialize_supplier_warranty_dispatch(dispatch, identity, workitem):
    payload = workitem.payload if isinstance(workitem.payload, dict) else {}
    scope = (payload.get("recipient_scopes") or {}).get(
        identity.user.wx_openid,
        {},
    )
    scoped_material_ids = {
        int(value) for value in scope.get("material_ids", []) if value
    }
    fault_records = list(
        dispatch.fault_records.select_related(
            "material",
            "material_spec",
            "supplier",
            "recorded_by",
        ).order_by("id")
    )
    return_records = list(
        dispatch.inventory_item.material_return_records.select_related(
            "material",
            "material_spec",
            "supplier",
            "material_spec_supplier",
            "recorded_by",
        ).filter(
            return_kind=(
                FactoryReturnMaterialReturnRecord.ReturnKind.DISASSEMBLY
            ),
            supplier_id=dispatch.supplier_id,
        ).order_by("id")
    )
    if identity.role.code == "supplier_sales":
        fault_records = [
            row for row in fault_records if row.material_id in scoped_material_ids
        ]
        return_records = [
            row
            for row in return_records
            if row.material_id in scoped_material_ids
        ]
    evaluations = {}
    for evaluation in DeviceMaterialWarrantyEvaluation.objects.filter(
        fault_record_id__in=[record.pk for record in fault_records],
    ).order_by("fault_record_id", "-version", "-id"):
        evaluations.setdefault(evaluation.fault_record_id, evaluation)
    for record in fault_records:
        evaluation = evaluations.get(record.pk)
        if evaluation:
            record._current_warranty_evaluation = evaluation
    return {
        "id": dispatch.pk,
        "status": dispatch.status,
        "status_name": dispatch.get_status_display(),
        "supplier": {
            "id": dispatch.supplier_id,
            "name": dispatch.supplier_name_snapshot,
        },
        "factory": {
            "id": dispatch.inventory_item.process.transfer.to_organization_id,
            "name": dispatch.inventory_item.process.transfer.to_organization.name,
        },
        "return_flow_no": (
            dispatch.inventory_item.process.transfer.flow_no
        ),
        "device_sn": dispatch.inventory_item.transfer_item.device.sn,
        "product_model_name": (
            dispatch.inventory_item.source_product_model.name
        ),
        "confirmed_by_name": _display_name(dispatch.confirmed_by),
        "confirmed_at": dispatch.confirmed_at.isoformat(),
        "received_by_name": (
            _display_name(dispatch.received_by) if dispatch.received_by_id else ""
        ),
        "received_at": (
            dispatch.received_at.isoformat() if dispatch.received_at else ""
        ),
        # materials 保留为兼容字段；新页面必须分别使用下面两个事实集合。
        "materials": [_serialize_fault_record(row) for row in fault_records],
        "returned_materials": [
            _serialize_material_return_record(row) for row in return_records
        ],
        "fault_materials": [
            _serialize_fault_record(row) for row in fault_records
        ],
        "summary": {
            "returned_material_lines": len(return_records),
            "returned_quantity": str(
                sum(
                    (row.returned_quantity for row in return_records),
                    Decimal("0"),
                )
            ),
            "fault_occurrences": len(fault_records),
            "in_warranty_lines": sum(
                row.warranty_result
                == DeviceMaterialWarrantyEvaluation.Result.IN_WARRANTY
                for row in return_records
            ),
        },
        "viewer": {
            "role": identity.role.code,
            "role_name": identity.role.name,
        },
        "actions": {
            "can_receive": False,
        },
        "rules": {
            "owner_and_sales_ack_independently": True,
            "fact_records_are_immutable": True,
            "returned_materials_and_fault_occurrences_are_separate": True,
            "warranty_is_computed_automatically": True,
            "quality_conclusion_not_in_this_version": True,
        },
    }


def _supplier_sales_historical_material_scopes(*, identity):
    """Resolve visibility from immutable task snapshots, never current routing.

    A responsibility manager can change after an order was matched. Historical
    supplier pages therefore use the recipient scope captured when the task was
    published. This prevents a newly assigned salesperson from inheriting old
    cases, and prevents an original recipient from losing the cases already
    delivered to them.
    """

    return_pairs = set()
    repair_pairs = set()
    warranty_dispatch_scopes = []
    openid = identity.user.wx_openid or ""
    workitems = WorkItem.objects.filter(
        type__in=(SUPPLIER_WARRANTY_WORK_TYPE, SUPPLIER_REPAIR_MATERIAL_WORK_TYPE),
    ).order_by("id")
    for workitem in workitems:
        payload = workitem.payload if isinstance(workitem.payload, dict) else {}
        if int(payload.get("supplier_id") or 0) != identity.organization_id:
            continue
        recipient_scope = (payload.get("recipient_scopes") or {}).get(openid)
        if not isinstance(recipient_scope, dict):
            continue
        material_ids = {
            int(value)
            for value in recipient_scope.get("material_ids", [])
            if value
        }
        if not material_ids:
            continue
        if workitem.type == SUPPLIER_WARRANTY_WORK_TYPE:
            inventory_item_id = int(payload.get("inventory_item_id") or 0)
            dispatch_id = int(payload.get("dispatch_id") or 0)
            if inventory_item_id:
                return_pairs.update(
                    (inventory_item_id, material_id)
                    for material_id in material_ids
                )
            elif dispatch_id:
                warranty_dispatch_scopes.append((dispatch_id, material_ids))
        else:
            inventory_item_id = int(payload.get("inventory_item_id") or 0)
            if inventory_item_id:
                repair_pairs.update(
                    (inventory_item_id, material_id)
                    for material_id in material_ids
                )

    # Older published warranty tasks did not include inventory_item_id. Resolve
    # their dispatch FK without mutating the original task payload.
    if warranty_dispatch_scopes:
        inventory_by_dispatch = dict(
            FactoryReturnWarrantyDispatch.objects.filter(
                pk__in=[row[0] for row in warranty_dispatch_scopes],
            ).values_list("id", "inventory_item_id")
        )
        for dispatch_id, material_ids in warranty_dispatch_scopes:
            inventory_item_id = inventory_by_dispatch.get(dispatch_id)
            if inventory_item_id:
                return_pairs.update(
                    (inventory_item_id, material_id)
                    for material_id in material_ids
                )
    return return_pairs, repair_pairs


def get_supplier_warranty_lists(*, user):
    """Return three supplier lists without mixing their accounting meanings.

    ``return_materials`` is the physical/settlement fact, ``fault_materials``
    is the quality occurrence fact, and ``repair_material_requests`` keeps the
    requested new-part quantity separate from the old part actually returned.
    Warranty is a read-only snapshot on every physical-return row.
    """

    try:
        identity = get_supplier_identity(user)
    except SupplierResponsibilityError as exc:
        raise FactoryReturnProcessError(str(exc)) from exc

    return_records = list(
        FactoryReturnMaterialReturnRecord.objects.select_related(
            "material",
            "material_spec",
            "supplier",
            "material_spec_supplier",
            "recorded_by",
            "source_production_batch",
            "source_bom",
            "inventory_item__source_product_model",
            "inventory_item__process__transfer__from_organization",
            "inventory_item__process__transfer__to_organization",
            "inventory_item__transfer_item__device",
        )
        .filter(
            supplier_id=identity.organization_id,
            return_kind=(
                FactoryReturnMaterialReturnRecord.ReturnKind.DISASSEMBLY
            ),
        )
        .order_by("-returned_at", "-id")
    )
    if identity.role.code == "supplier_sales":
        return_scope_pairs, repair_scope_pairs = (
            _supplier_sales_historical_material_scopes(identity=identity)
        )
        return_records = [
            row
            for row in return_records
            if (row.inventory_item_id, row.material_id) in return_scope_pairs
        ]
    else:
        return_scope_pairs = None
        repair_scope_pairs = None

    fault_records = list(
        DeviceMaterialFaultRecord.objects.select_related(
            "material",
            "material_spec",
            "supplier",
            "recorded_by",
            "source_production_batch",
            "source_bom",
            "inventory_item__source_product_model",
            "inventory_item__process__transfer__from_organization",
            "inventory_item__process__transfer__to_organization",
            "inventory_item__transfer_item__device",
            "warranty_dispatch_item__dispatch",
        )
        .filter(
            supplier_id=identity.organization_id,
            return_record_id__in=[row.pk for row in return_records],
        )
        .order_by("-dismantled_at", "-id")
    )
    latest_evaluations = {}
    for evaluation in DeviceMaterialWarrantyEvaluation.objects.filter(
        fault_record_id__in=[row.pk for row in fault_records],
    ).order_by("fault_record_id", "-version", "-id"):
        latest_evaluations.setdefault(evaluation.fault_record_id, evaluation)
    for record in fault_records:
        evaluation = latest_evaluations.get(record.pk)
        if evaluation:
            record._current_warranty_evaluation = evaluation

    repair_requirements = list(
        FactoryReturnRepairMaterialRequirement.objects.select_related(
            "material",
            "material_spec",
            "supplier",
            "requested_by",
            "returned_material_record__material",
            "returned_material_record__material_spec",
            "returned_material_record__supplier",
            "returned_material_record__material_spec_supplier",
            "returned_material_record__recorded_by",
            "inventory_item__source_product_model",
            "inventory_item__process__transfer__from_organization",
            "inventory_item__process__transfer__to_organization",
            "inventory_item__transfer_item__device",
        )
        .filter(
            supplier_id=identity.organization_id,
            notified_at__isnull=False,
        )
        .order_by("-notified_at", "-id")
    )
    if repair_scope_pairs is not None:
        repair_requirements = [
            row
            for row in repair_requirements
            if (row.inventory_item_id, row.material_id) in repair_scope_pairs
        ]

    dispatch_by_inventory = {
        inventory_id: dispatch_id
        for inventory_id, dispatch_id in FactoryReturnWarrantyDispatch.objects.filter(
            supplier_id=identity.organization_id,
            inventory_item_id__in={row.inventory_item_id for row in return_records},
        ).values_list("inventory_item_id", "id")
    }
    return_rows = []
    case_map = {}
    for record in return_records:
        inventory_item = record.inventory_item
        transfer = inventory_item.process.transfer
        dispatch_id = dispatch_by_inventory.get(inventory_item.pk)
        return_rows.append(
            {
                **_serialize_material_return_record(record),
                "dispatch_id": dispatch_id,
                "return_flow_no": record.return_flow_no_snapshot,
                "device_sn": record.device_sn_snapshot,
                "product_model_name": inventory_item.source_product_model.name,
                "return_merchant_name": transfer.from_organization.name,
                "factory_name": transfer.to_organization.name,
                "production_date": (
                    record.source_production_batch.production_date.isoformat()
                ),
                "production_batch_no": record.production_batch_no_snapshot,
                "bom_version": record.bom_version_snapshot,
            }
        )
        case = case_map.setdefault(
            inventory_item.pk,
            {
                "inventory_item_id": inventory_item.pk,
                "dispatch_id": dispatch_id,
                "return_flow_no": record.return_flow_no_snapshot,
                "device_sn": record.device_sn_snapshot,
                "product_model_name": inventory_item.source_product_model.name,
                "return_merchant_name": transfer.from_organization.name,
                "factory_name": transfer.to_organization.name,
                "dismantled_at": record.returned_at.isoformat(),
                "production_date": (
                    record.source_production_batch.production_date.isoformat()
                ),
                "production_batch_no": record.production_batch_no_snapshot,
                "bom_version": record.bom_version_snapshot,
                "returned_material_lines": 0,
                "returned_quantity": Decimal("0"),
                "fault_occurrence_count": 0,
                "material_names": [],
            },
        )
        case["returned_material_lines"] += 1
        case["returned_quantity"] += record.returned_quantity
        case["fault_occurrence_count"] += int(record.is_fault_material)
        case["material_names"].append(record.material_name_snapshot)

    case_rows = []
    for case in case_map.values():
        case["material_count"] = case["returned_material_lines"]
        case["returned_quantity"] = str(case["returned_quantity"])
        case_rows.append(case)

    fault_rows = []
    for record in fault_records:
        inventory_item = record.inventory_item
        transfer = inventory_item.process.transfer
        fault_rows.append(
            {
                **_serialize_fault_record(record),
                "dispatch_id": dispatch_by_inventory.get(inventory_item.pk),
                "return_flow_no": record.return_flow_no_snapshot,
                "device_sn": record.device_sn_snapshot,
                "product_model_name": inventory_item.source_product_model.name,
                "return_merchant_name": transfer.from_organization.name,
                "factory_name": transfer.to_organization.name,
                "production_date": (
                    record.source_production_batch.production_date.isoformat()
                ),
                "production_batch_no": record.production_batch_no_snapshot,
                "bom_version": record.bom_version_snapshot,
            }
        )

    repair_rows = []
    repair_return_quantity = Decimal("0")
    for row in repair_requirements:
        serialized = _serialize_repair_material_requirement(row)
        returned_record = row.returned_material_record
        if returned_record:
            repair_return_quantity += returned_record.returned_quantity
            warranty_evaluation = serialized["actual_return_record"][
                "warranty_evaluation"
            ]
        else:
            warranty_evaluation = _serialize_material_warranty(
                _material_warranty_snapshot(
                    material_spec_id=row.material_spec_id,
                    supplier_id=row.supplier_id,
                    production_date=row.source_production_batch.production_date,
                    evaluated_at=row.requested_at,
                )
            )
        repair_rows.append(
            {
                **serialized,
                "inventory_item_id": row.inventory_item_id,
                "device_sn": row.device_sn_snapshot,
                "product_model_name": row.inventory_item.source_product_model.name,
                "return_flow_no": row.return_flow_no_snapshot,
                "return_merchant_name": (
                    row.inventory_item.process.transfer.from_organization.name
                ),
                "factory_name": (
                    row.inventory_item.process.transfer.to_organization.name
                ),
                "production_batch_no": row.production_batch_no_snapshot,
                "bom_version": row.bom_version_snapshot,
                "warranty_evaluation": warranty_evaluation,
            }
        )

    return {
        "viewer": {
            "role": identity.role.code,
            "role_name": identity.role.name,
            "supplier_id": identity.organization_id,
            "supplier_name": identity.organization.name,
            "scope_name": (
                "本供应商全部退回、故障与修复数据"
                if identity.role.code == "supplier_owner"
                else "本人责任范围内物料"
            ),
        },
        "summary": {
            "return_case_count": len(case_map),
            "returned_material_count": len(return_rows),
            "returned_material_quantity": str(
                sum(
                    (row.returned_quantity for row in return_records),
                    Decimal("0"),
                )
            ),
            "fault_material_count": len(fault_rows),
            "fault_occurrence_count": len(fault_rows),
            "repair_material_count": len(repair_requirements),
            "repair_return_quantity": str(repair_return_quantity),
        },
        "return_cases": case_rows,
        "return_materials": return_rows,
        "fault_materials": fault_rows,
        "repair_material_requests": repair_rows,
        "rules": {
            "physical_returns_and_fault_occurrences_are_separate": True,
            "warranty_is_computed_automatically": True,
            "all_warranty_results_are_visible": True,
            "repair_new_parts_are_recounted_by_future_order_plan": True,
            "supplier_owner_sees_all": True,
            "supplier_sales_filtered_by_responsibility": True,
            "awareness_tasks_are_independent": True,
        },
    }


def get_supplier_warranty_dispatch(*, user, dispatch_id):
    dispatch = (
        FactoryReturnWarrantyDispatch.objects.select_related(
            "supplier__organization_type",
            "confirmed_by",
            "received_by",
            "inventory_item__source_product_model",
            "inventory_item__transfer_item__device",
            "inventory_item__process__transfer__to_organization",
        )
        .filter(pk=dispatch_id)
        .first()
    )
    if not dispatch:
        raise FactoryReturnProcessError("供应商三包协同批次不存在。")
    identity, workitem, _task = _supplier_warranty_dispatch_access(user, dispatch)
    return _serialize_supplier_warranty_dispatch(dispatch, identity, workitem)


@transaction.atomic
def receive_supplier_warranty_dispatch(*, user, dispatch_id):
    dispatch = (
        FactoryReturnWarrantyDispatch.objects.select_for_update()
        .select_related(
            "supplier__organization_type",
            "confirmed_by",
            "received_by",
            "inventory_item__source_product_model",
            "inventory_item__transfer_item__device",
            "inventory_item__process__transfer__to_organization",
        )
        .filter(pk=dispatch_id)
        .first()
    )
    if not dispatch:
        raise FactoryReturnProcessError("供应商三包协同批次不存在。")
    # 兼容旧版小程序的 receive 请求：新流程是独立知晓，不再改写批次或他人任务。
    if dispatch.status == FactoryReturnWarrantyDispatch.Status.NOTIFIED:
        identity, workitem, _task = _supplier_warranty_dispatch_access(
            user,
            dispatch,
        )
        return _serialize_supplier_warranty_dispatch(
            dispatch,
            identity,
            workitem,
        ), False
    if dispatch.status == FactoryReturnWarrantyDispatch.Status.RECEIVED:
        identity, workitem, _task = _supplier_warranty_dispatch_access(
            user,
            dispatch,
        )
        return _serialize_supplier_warranty_dispatch(
            dispatch,
            identity,
            workitem,
        ), False
    identity, workitem, _task = _supplier_warranty_dispatch_access(
        user,
        dispatch,
        require_open_task=True,
    )
    workitem = WorkItem.objects.select_for_update().get(pk=workitem.pk)
    now = timezone.now()
    actor_name = _display_name(user)
    dispatch.status = FactoryReturnWarrantyDispatch.Status.RECEIVED
    dispatch.received_by = user
    dispatch.received_at = now
    dispatch.save(
        update_fields=("status", "received_by", "received_at", "updated_at")
    )
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
    UserTask.objects.filter(workitem=workitem).update(
        state="DONE",
        need_action=False,
        done_at=now,
        done_reason="OTHER_ACTION",
        display_title_snapshot=f"已由{actor_name}接收三包物料通知",
        display_subtitle_snapshot="同供应商协同账号无需重复操作",
        cursor_time=now,
        updated_at=now,
    )
    UserTask.objects.filter(
        workitem=workitem,
        to_openid=user.wx_openid,
    ).update(
        done_reason="SELF_ACTION",
        display_title_snapshot="三包物料通知已接收",
        display_subtitle_snapshot="由你完成",
    )
    return _serialize_supplier_warranty_dispatch(
        dispatch,
        identity,
        workitem,
    ), True
