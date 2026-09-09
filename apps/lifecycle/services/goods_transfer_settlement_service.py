import mimetypes
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.db import transaction
from django.db.models import Max, Sum
from django.utils import timezone

from account.models import UserOrganization
from account.services.auth_service import get_active_identity
from lifecycle.models import (
    GoodsTransferCreditAllocation,
    GoodsTransfer,
    GoodsTransferLogisticsDocument,
    GoodsTransferPayment,
    GoodsTransferSettlement,
)
from workflow.models import WorkItem
from workflow.services.workflow_service import publish_workitem


class GoodsTransferSettlementError(Exception):
    pass


SETTLEMENT_ROLES = {
    "merchant_owner",
    "merchant_sales",
    "merchant_stock",
}
MAX_FILE_SIZE = 10 * 1024 * 1024
MONEY_LIMIT = Decimal("999999999999.99")
DOCUMENT_WORK_TYPE = "GOODS_TRANSFER_DOCUMENTS_UPLOAD"
PAYMENT_UPLOAD_WORK_TYPE = "GOODS_TRANSFER_PAYMENT_UPLOAD"
PAYMENT_CONFIRM_WORK_TYPE = "GOODS_TRANSFER_PAYMENT_CONFIRM"


def _display_name(user):
    return user.wx_nickname or user.get_full_name() or user.username


def _role_openids(organization, role_codes=SETTLEMENT_ROLES):
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


def _identity(user):
    identity = get_active_identity(user)
    if (
        not identity
        or identity.role.code not in SETTLEMENT_ROLES
        or identity.organization.organization_type.code != "MERCHANT"
    ):
        raise GoodsTransferSettlementError("当前岗位无权查看商品流转资金信息。")
    return identity


def _authorized_transfer(user, transfer_id, *, lock=False):
    identity = _identity(user)
    query = GoodsTransfer.objects.select_related(
        "instruction_organization__organization_type",
        "from_organization__organization_type",
        "to_organization__organization_type",
        "initiated_by",
        "received_by",
    )
    if lock:
        query = query.select_for_update()
    transfer = query.filter(pk=transfer_id).first()
    if not transfer:
        raise GoodsTransferSettlementError("商品流转单不存在。")
    if transfer.flow_type != GoodsTransfer.FlowType.SHIPMENT:
        raise GoodsTransferSettlementError("只有向下级发货才建立商品货款闭环。")
    if transfer.status in {
        GoodsTransfer.Status.SCANNING,
        GoodsTransfer.Status.CANCELLED,
    }:
        raise GoodsTransferSettlementError("请先完成逐台扫码并提交发货。")
    if identity.organization_id not in {
        transfer.from_organization_id,
        transfer.to_organization_id,
    }:
        raise GoodsTransferSettlementError("该商品流转资金信息不属于当前组织。")
    side = (
        "SENDER"
        if identity.organization_id == transfer.from_organization_id
        else "RECEIVER"
    )
    return transfer, identity, side


def _parse_amount(value, label):
    raw = str(value or "").strip()
    if not raw:
        raise GoodsTransferSettlementError(f"请填写{label}。")
    try:
        amount = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise GoodsTransferSettlementError(f"{label}格式不正确。") from exc
    if not amount.is_finite() or amount <= 0:
        raise GoodsTransferSettlementError(f"{label}必须大于0。")
    if amount.as_tuple().exponent < -2:
        raise GoodsTransferSettlementError(f"{label}最多保留两位小数。")
    if amount > MONEY_LIMIT:
        raise GoodsTransferSettlementError(f"{label}超出允许范围。")
    return amount.quantize(Decimal("0.01"))


def _validate_image(uploaded_file, label):
    if not uploaded_file:
        raise GoodsTransferSettlementError(f"请上传{label}。")
    if uploaded_file.size <= 0 or uploaded_file.size > MAX_FILE_SIZE:
        raise GoodsTransferSettlementError(f"{label}不能超过10MB。")
    header = uploaded_file.read(12)
    uploaded_file.seek(0)
    if header.startswith(b"\xff\xd8\xff"):
        suffix, content_type = ".jpg", "image/jpeg"
    elif header.startswith(b"\x89PNG\r\n\x1a\n"):
        suffix, content_type = ".png", "image/png"
    elif header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        suffix, content_type = ".webp", "image/webp"
    else:
        raise GoodsTransferSettlementError(
            f"{label}只支持JPG、PNG或WEBP格式。"
        )
    return suffix, Path(uploaded_file.name).name[:255], content_type


def _validate_logistics_document(uploaded_file):
    if not uploaded_file:
        raise GoodsTransferSettlementError("请上传物流单据。")
    if uploaded_file.size <= 0 or uploaded_file.size > MAX_FILE_SIZE:
        raise GoodsTransferSettlementError("物流单据不能超过10MB。")
    header = uploaded_file.read(12)
    uploaded_file.seek(0)
    if header.startswith(b"%PDF-"):
        return (
            ".pdf",
            Path(uploaded_file.name).name[:255],
            "application/pdf",
            GoodsTransferLogisticsDocument.FileType.PDF,
        )
    suffix, original_name, content_type = _validate_image(
        uploaded_file,
        "物流单据",
    )
    return (
        suffix,
        original_name,
        content_type,
        GoodsTransferLogisticsDocument.FileType.IMAGE,
    )


def _settlement_link(transfer):
    return (
        "/pages/stock/goods_transfer_settlement/goods_transfer_settlement"
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


def _document_key(transfer):
    return f"lifecycle:goods-transfer:{transfer.pk}:commercial-documents"


def _payment_upload_key(transfer, sequence_no):
    return (
        f"lifecycle:goods-transfer:{transfer.pk}:payment-upload:{sequence_no}"
    )


def _payment_confirm_key(transfer, payment):
    return (
        f"lifecycle:goods-transfer:{transfer.pk}:payment:{payment.pk}:confirm"
    )


def _publish_document_task(transfer, actor_openid=""):
    recipients = _role_openids(transfer.from_organization)
    if not recipients:
        return None
    return publish_workitem(
        domain="LOGISTICS",
        type=DOCUMENT_WORK_TYPE,
        biz_id=transfer.flow_no,
        biz_ref=f"lifecycle.GoodsTransfer:{transfer.pk}",
        uniq_key=_document_key(transfer),
        title="商品已发货，待提交货品清单和物流单据",
        summary=(
            f"{transfer.to_organization.name}｜"
            f"共{transfer.items.count()}台"
        ),
        link=_settlement_link(transfer),
        recipients_openids=recipients,
        need_action=True,
        action_mode="ANYONE_DO",
        payload=_task_payload(transfer),
        actor_openid=actor_openid,
        mark_workitem_done_immediately=False,
    )


def _ensure_document_task(transfer, actor_openid=""):
    existing = WorkItem.objects.filter(
        type=DOCUMENT_WORK_TYPE,
        uniq_key=_document_key(transfer),
    ).first()
    if existing:
        return existing
    return _publish_document_task(transfer, actor_openid)


def _publish_payment_upload_task(settlement, sequence_no, actor_openid=""):
    transfer = settlement.transfer
    recipients = _role_openids(transfer.to_organization)
    if not recipients:
        return None
    return publish_workitem(
        domain="LOGISTICS",
        type=PAYMENT_UPLOAD_WORK_TYPE,
        biz_id=transfer.flow_no,
        biz_ref=f"lifecycle.GoodsTransfer:{transfer.pk}",
        uniq_key=_payment_upload_key(transfer, sequence_no),
        title=f"商品第{sequence_no}笔付款待处理",
        summary=f"应收货款 ¥{settlement.receivable_amount}",
        link=_settlement_link(transfer),
        recipients_openids=recipients,
        need_action=True,
        action_mode="ANYONE_DO",
        payload=_task_payload(transfer, payment_sequence=sequence_no),
        actor_openid=actor_openid,
        mark_workitem_done_immediately=False,
    )


def _publish_payment_confirm_task(payment, actor_openid=""):
    settlement = payment.settlement
    transfer = settlement.transfer
    recipients = _role_openids(transfer.from_organization)
    if not recipients:
        return None
    return publish_workitem(
        domain="LOGISTICS",
        type=PAYMENT_CONFIRM_WORK_TYPE,
        biz_id=transfer.flow_no,
        biz_ref=f"lifecycle.GoodsTransfer:{transfer.pk}",
        uniq_key=_payment_confirm_key(transfer, payment),
        title=f"第{payment.sequence_no}笔商品收款待确认",
        summary=(
            f"{transfer.to_organization.name}｜"
            "请依据付款凭证填写实际到账金额"
        ),
        link=_settlement_link(transfer),
        recipients_openids=recipients,
        need_action=True,
        action_mode="ANYONE_DO",
        payload=_task_payload(
            transfer,
            payment_id=payment.pk,
            payment_sequence=payment.sequence_no,
        ),
        actor_openid=actor_openid,
        mark_workitem_done_immediately=False,
    )


def _complete_anyone(workitem, user, *, actor_title, peer_title):
    if workitem.status == "DONE":
        raise GoodsTransferSettlementError(
            "该协作任务已由其他成员完成，请刷新页面。"
        )
    task = (
        workitem.user_tasks.select_for_update()
        .filter(to_openid=user.wx_openid, need_action=True)
        .exclude(state="DONE")
        .first()
    )
    if not task:
        raise GoodsTransferSettlementError("当前账号不是该任务的处理人。")
    now = timezone.now()
    payload = dict(workitem.payload or {})
    payload.update(
        {
            "completed_by_user_id": user.pk,
            "completed_by_name": _display_name(user),
            "completed_at": now.isoformat(),
        }
    )
    completed = WorkItem.objects.filter(pk=workitem.pk, status="OPEN").update(
        status="DONE",
        done_by_openid=user.wx_openid or "",
        done_at=now,
        last_activity_at=now,
        payload=payload,
        updated_at=now,
    )
    if completed != 1:
        raise GoodsTransferSettlementError(
            "该协作任务已由其他成员完成，请刷新页面。"
        )
    workitem.status = "DONE"
    workitem.user_tasks.filter(to_openid=user.wx_openid).update(
        state="DONE",
        need_action=False,
        done_at=now,
        done_reason="SELF_ACTION",
        display_title_snapshot=actor_title,
        display_subtitle_snapshot="由你完成",
        cursor_time=now,
        updated_at=now,
    )
    workitem.user_tasks.exclude(to_openid=user.wx_openid).update(
        state="DONE",
        need_action=False,
        done_at=now,
        done_reason="OTHER_ACTION",
        display_title_snapshot=peer_title,
        display_subtitle_snapshot=(
            f"{_display_name(user)}已处理，同组织无需重复操作"
        ),
        cursor_time=now,
        updated_at=now,
    )


def _complete_document_task_if_ready(settlement, user):
    if (
        not settlement.statement_submitted_at
        or not settlement.logistics_documents.exists()
    ):
        return False
    transfer = settlement.transfer
    workitem = WorkItem.objects.select_for_update().filter(
        type=DOCUMENT_WORK_TYPE,
        uniq_key=_document_key(transfer),
    ).first()
    if not workitem:
        _publish_document_task(transfer, user.wx_openid or "")
        workitem = WorkItem.objects.select_for_update().filter(
            type=DOCUMENT_WORK_TYPE,
            uniq_key=_document_key(transfer),
        ).first()
    if workitem and workitem.status == "OPEN":
        _complete_anyone(
            workitem,
            user,
            actor_title="货品清单和物流单据已提交",
            peer_title=f"{_display_name(user)}已提交发货商业单据",
        )
        return True
    return False


@transaction.atomic
def initialize_goods_transfer_settlement(*, transfer, actor):
    if transfer.flow_type != GoodsTransfer.FlowType.SHIPMENT:
        return None, False
    settlement, created = GoodsTransferSettlement.objects.get_or_create(
        transfer=transfer
    )
    _ensure_document_task(transfer, actor.wx_openid or "")
    return settlement, created


def _payment_rows(settlement):
    prefetched = getattr(
        settlement,
        "_prefetched_objects_cache",
        {},
    ).get("payments")
    if prefetched is not None:
        return list(prefetched)
    return list(
        settlement.payments.select_related("uploaded_by", "confirmed_by").all()
    )


def _logistics_rows(settlement):
    prefetched = getattr(
        settlement,
        "_prefetched_objects_cache",
        {},
    ).get("logistics_documents")
    if prefetched is not None:
        return list(prefetched)
    return list(
        settlement.logistics_documents.select_related("uploaded_by").all()
    )


def _totals(settlement, payments=None):
    payments = payments if payments is not None else _payment_rows(settlement)
    confirmed_total = sum(
        (
            payment.confirmed_amount
            for payment in payments
            if payment.status == GoodsTransferPayment.Status.CONFIRMED
            and payment.confirmed_amount is not None
        ),
        Decimal("0.00"),
    )
    receivable = settlement.receivable_amount
    credited_total = GoodsTransferCreditAllocation.objects.filter(
        target_shipment_settlement=settlement
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    outstanding = (
        None
        if receivable is None
        else receivable - confirmed_total - credited_total
    )
    return confirmed_total, credited_total, outstanding


def _state(settlement, documents, payments, confirmed_total, outstanding):
    if not settlement.statement_submitted_at or not documents:
        return "WAITING_DOCUMENTS", "待补齐发货商业单据"
    if any(
        payment.status == GoodsTransferPayment.Status.PENDING
        for payment in payments
    ):
        return "PAYMENT_CONFIRMING", "付款待确认"
    if outstanding == 0:
        return "FULLY_RECEIVED", "已结清"
    if outstanding < 0:
        return "OVER_RECEIVED", "结算超过应收"
    if confirmed_total == 0:
        return "WAITING_PAYMENT", "待收货方付款或关联退货冲抵"
    if outstanding > 0:
        return "PARTIALLY_RECEIVED", "部分结清"
    return "FULLY_RECEIVED", "已结清"


def _money(value):
    return f"{value:.2f}" if value is not None else None


def _viewer_can_confirm_payment(user, transfer, payment):
    if payment.status != GoodsTransferPayment.Status.PENDING:
        return False
    return WorkItem.objects.filter(
        type=PAYMENT_CONFIRM_WORK_TYPE,
        uniq_key=_payment_confirm_key(transfer, payment),
        status="OPEN",
        user_tasks__to_openid=user.wx_openid,
        user_tasks__need_action=True,
    ).exclude(user_tasks__state="DONE").exists()


def _serialize(settlement, user, identity, side):
    transfer = settlement.transfer
    documents = _logistics_rows(settlement)
    payments = _payment_rows(settlement)
    confirmed_total, credited_total, outstanding = _totals(settlement, payments)
    state, state_name = _state(
        settlement,
        documents,
        payments,
        confirmed_total,
        outstanding,
    )
    pending_exists = any(
        payment.status == GoodsTransferPayment.Status.PENDING
        for payment in payments
    )
    return {
        "state": state,
        "state_name": state_name,
        "physical_status": transfer.status,
        "physical_status_name": transfer.get_status_display(),
        "receivable_amount": _money(settlement.receivable_amount),
        "confirmed_received_amount": _money(confirmed_total),
        "confirmed_credit_amount": _money(credited_total),
        "outstanding_amount": _money(outstanding),
        "commercial_documents_complete": bool(
            settlement.statement_submitted_at and documents
        ),
        "payment_count": len(payments),
        "pending_payment_count": sum(
            1
            for payment in payments
            if payment.status == GoodsTransferPayment.Status.PENDING
        ),
        "goods_transfer": {
            "id": transfer.pk,
            "flow_no": transfer.flow_no,
            "from_organization_name": transfer.from_organization.name,
            "to_organization_name": transfer.to_organization.name,
            "device_count": transfer.items.count(),
        },
        "viewer": {
            "role": identity.role.code,
            "side": side,
        },
        "statement": {
            "submitted": bool(settlement.statement_submitted_at),
            "original_name": settlement.statement_original_name,
            "submitted_by_name": (
                _display_name(settlement.statement_submitted_by)
                if settlement.statement_submitted_by
                else ""
            ),
            "submitted_at": (
                settlement.statement_submitted_at.isoformat()
                if settlement.statement_submitted_at
                else None
            ),
            "preview_path": (
                f"/lifecycle/goods-transfers/{transfer.pk}/settlement/statement-file/"
                if settlement.statement_image
                else ""
            ),
        },
        "logistics_documents": [
            {
                "id": document.pk,
                "sequence_no": document.sequence_no,
                "original_name": document.original_name,
                "file_type": document.file_type,
                "content_type": document.content_type,
                "file_size": document.file_size,
                "remark": document.remark,
                "uploaded_by_name": _display_name(document.uploaded_by),
                "uploaded_at": document.uploaded_at.isoformat(),
                "preview_path": (
                    f"/lifecycle/goods-transfers/{transfer.pk}/settlement/"
                    f"logistics-documents/{document.pk}/file/"
                ),
            }
            for document in documents
        ],
        "actions": {
            "can_submit_statement": (
                side == "SENDER" and not settlement.statement_submitted_at
            ),
            "can_upload_logistics": side == "SENDER",
            "can_upload_payment": (
                side == "RECEIVER"
                and bool(settlement.statement_submitted_at)
                and not pending_exists
                and outstanding is not None
                and outstanding > 0
            ),
        },
        "payments": [
            {
                "id": payment.pk,
                "sequence_no": payment.sequence_no,
                "status": payment.status,
                "status_name": payment.get_status_display(),
                "voucher_original_name": payment.voucher_original_name,
                "voucher_preview_path": (
                    f"/lifecycle/goods-transfers/{transfer.pk}/settlement/"
                    f"payments/{payment.pk}/voucher-file/"
                ),
                "uploaded_by_name": _display_name(payment.uploaded_by),
                "uploaded_at": payment.uploaded_at.isoformat(),
                "confirmed_amount": _money(payment.confirmed_amount),
                "confirmed_by_name": (
                    _display_name(payment.confirmed_by)
                    if payment.confirmed_by
                    else ""
                ),
                "confirmed_at": (
                    payment.confirmed_at.isoformat()
                    if payment.confirmed_at
                    else None
                ),
                "can_confirm": (
                    side == "SENDER"
                    and _viewer_can_confirm_payment(user, transfer, payment)
                ),
            }
            for payment in payments
        ],
    }


def get_goods_transfer_settlement_detail(*, user, transfer_id):
    transfer, identity, side = _authorized_transfer(user, transfer_id)
    settlement, _created = GoodsTransferSettlement.objects.get_or_create(
        transfer=transfer
    )
    settlement.transfer = transfer
    _ensure_document_task(transfer, user.wx_openid or "")
    return _serialize(settlement, user, identity, side)


@transaction.atomic
def submit_goods_transfer_statement(
    *,
    user,
    transfer_id,
    receivable_amount,
    image,
):
    transfer, identity, side = _authorized_transfer(user, transfer_id, lock=True)
    if side != "SENDER":
        raise GoodsTransferSettlementError("只有发货方可以提交货品清单。")
    settlement, _created = GoodsTransferSettlement.objects.select_for_update().get_or_create(
        transfer=transfer
    )
    if settlement.statement_submitted_at:
        raise GoodsTransferSettlementError("该货品清单已经提交。")
    if settlement.payments.exists():
        raise GoodsTransferSettlementError("已有付款记录，不能重新提交清单。")
    amount = _parse_amount(receivable_amount, "货品应收金额")
    suffix, original_name, _content_type = _validate_image(image, "货品清单图片")
    settlement.statement_image.save(
        f"statement-{uuid.uuid4().hex}{suffix}",
        image,
        save=False,
    )
    now = timezone.now()
    settlement.statement_original_name = original_name
    settlement.receivable_amount = amount
    settlement.statement_submitted_by = user
    settlement.statement_submitted_at = now
    settlement.save()
    _complete_document_task_if_ready(settlement, user)
    _publish_payment_upload_task(settlement, 1, user.wx_openid or "")
    return _serialize(settlement, user, identity, side)


@transaction.atomic
def upload_goods_transfer_logistics_document(
    *,
    user,
    transfer_id,
    client_request_id,
    document,
    remark="",
):
    transfer, identity, side = _authorized_transfer(user, transfer_id, lock=True)
    if side != "SENDER":
        raise GoodsTransferSettlementError("只有发货方可以上传物流单据。")
    settlement, _created = GoodsTransferSettlement.objects.select_for_update().get_or_create(
        transfer=transfer
    )
    request_id = str(client_request_id or "").strip()
    if not request_id or len(request_id) > 64:
        raise GoodsTransferSettlementError("物流单据请求标识无效，请重新进入页面。")
    existing = settlement.logistics_documents.filter(
        client_request_id=request_id
    ).first()
    if existing:
        return _serialize(settlement, user, identity, side), False
    suffix, original_name, content_type, file_type = _validate_logistics_document(
        document
    )
    sequence_no = (
        settlement.logistics_documents.aggregate(value=Max("sequence_no"))[
            "value"
        ]
        or 0
    ) + 1
    row = GoodsTransferLogisticsDocument(
        settlement=settlement,
        sequence_no=sequence_no,
        client_request_id=request_id,
        original_name=original_name,
        file_type=file_type,
        content_type=content_type,
        file_size=document.size,
        remark=str(remark or "").strip()[:255],
        uploaded_by=user,
    )
    row.document_file.save(
        f"logistics-{sequence_no}-{uuid.uuid4().hex}{suffix}",
        document,
        save=False,
    )
    row.save()
    _complete_document_task_if_ready(settlement, user)
    return _serialize(settlement, user, identity, side), True


@transaction.atomic
def upload_goods_transfer_payment_voucher(
    *,
    user,
    transfer_id,
    client_request_id,
    image,
):
    transfer, identity, side = _authorized_transfer(user, transfer_id, lock=True)
    if side != "RECEIVER":
        raise GoodsTransferSettlementError("只有收货方可以上传付款凭证。")
    settlement = GoodsTransferSettlement.objects.select_for_update().filter(
        transfer=transfer
    ).first()
    if not settlement or not settlement.statement_submitted_at:
        raise GoodsTransferSettlementError("发货方尚未提交货品清单和应收金额。")
    request_id = str(client_request_id or "").strip()
    if not request_id or len(request_id) > 64:
        raise GoodsTransferSettlementError("付款凭证请求标识无效，请重新进入页面。")
    existing = settlement.payments.filter(
        client_request_id=request_id
    ).select_related("uploaded_by", "confirmed_by").first()
    if existing:
        return _serialize(settlement, user, identity, side), False
    if settlement.payments.filter(
        status=GoodsTransferPayment.Status.PENDING
    ).exists():
        raise GoodsTransferSettlementError("上一笔付款凭证尚未确认，请等待发货方处理。")
    _confirmed_total, _credited_total, outstanding = _totals(settlement)
    if outstanding is None or outstanding <= 0:
        raise GoodsTransferSettlementError("该商品流转已经完成全部收款。")
    suffix, original_name, _content_type = _validate_image(image, "付款凭证")
    sequence_no = (
        settlement.payments.aggregate(value=Max("sequence_no"))["value"] or 0
    ) + 1
    workitem = WorkItem.objects.select_for_update().filter(
        type=PAYMENT_UPLOAD_WORK_TYPE,
        uniq_key=_payment_upload_key(transfer, sequence_no),
    ).first()
    if not workitem:
        _publish_payment_upload_task(
            settlement,
            sequence_no,
            user.wx_openid or "",
        )
        workitem = WorkItem.objects.select_for_update().filter(
            type=PAYMENT_UPLOAD_WORK_TYPE,
            uniq_key=_payment_upload_key(transfer, sequence_no),
        ).first()
    if not workitem:
        raise GoodsTransferSettlementError("未找到收货方付款任务。")
    _complete_anyone(
        workitem,
        user,
        actor_title=f"第{sequence_no}笔付款凭证已上传",
        peer_title=f"{_display_name(user)}已上传第{sequence_no}笔付款凭证",
    )
    payment = GoodsTransferPayment(
        settlement=settlement,
        sequence_no=sequence_no,
        client_request_id=request_id,
        voucher_original_name=original_name,
        uploaded_by=user,
    )
    payment.voucher_image.save(
        f"payment-{sequence_no}-{uuid.uuid4().hex}{suffix}",
        image,
        save=False,
    )
    payment.save()
    _publish_payment_confirm_task(payment, user.wx_openid or "")
    return _serialize(settlement, user, identity, side), True


@transaction.atomic
def confirm_goods_transfer_payment(
    *,
    user,
    transfer_id,
    payment_id,
    amount,
):
    transfer, identity, side = _authorized_transfer(user, transfer_id, lock=True)
    if side != "SENDER":
        raise GoodsTransferSettlementError("只有发货方可以确认实际收款。")
    settlement = GoodsTransferSettlement.objects.select_for_update().filter(
        transfer=transfer
    ).first()
    if not settlement:
        raise GoodsTransferSettlementError("商品流转应收主账不存在。")
    payment = (
        GoodsTransferPayment.objects.select_for_update()
        .select_related("uploaded_by", "confirmed_by", "settlement")
        .filter(pk=payment_id, settlement=settlement)
        .first()
    )
    if not payment:
        raise GoodsTransferSettlementError("付款记录不存在。")
    if payment.status == GoodsTransferPayment.Status.CONFIRMED:
        return _serialize(settlement, user, identity, side), False
    confirmed_amount = _parse_amount(amount, "实际到账金额")
    _confirmed_total, _credited_total, current_outstanding = _totals(settlement)
    if current_outstanding is None or confirmed_amount > current_outstanding:
        raise GoodsTransferSettlementError("实际到账金额超过当前应收余额。")
    workitem = WorkItem.objects.select_for_update().filter(
        type=PAYMENT_CONFIRM_WORK_TYPE,
        uniq_key=_payment_confirm_key(transfer, payment),
    ).first()
    if not workitem:
        raise GoodsTransferSettlementError("未找到收款确认任务。")
    _complete_anyone(
        workitem,
        user,
        actor_title=f"第{payment.sequence_no}笔收款已确认",
        peer_title=f"{_display_name(user)}已确认第{payment.sequence_no}笔收款",
    )
    now = timezone.now()
    payment.confirmed_amount = confirmed_amount
    payment.confirmed_by = user
    payment.confirmed_at = now
    payment.status = GoodsTransferPayment.Status.CONFIRMED
    payment.save(
        update_fields=(
            "confirmed_amount",
            "confirmed_by",
            "confirmed_at",
            "status",
            "updated_at",
        )
    )
    _confirmed_total, _credited_total, outstanding = _totals(settlement)
    if outstanding > 0:
        _publish_payment_upload_task(
            settlement,
            payment.sequence_no + 1,
            user.wx_openid or "",
        )
    return _serialize(settlement, user, identity, side), True


def get_statement_file(*, user, transfer_id):
    transfer, _identity_value, _side = _authorized_transfer(user, transfer_id)
    settlement = GoodsTransferSettlement.objects.filter(transfer=transfer).first()
    if not settlement or not settlement.statement_image:
        raise GoodsTransferSettlementError("货品清单图片不存在。")
    content_type = (
        mimetypes.guess_type(settlement.statement_image.name)[0]
        or "image/jpeg"
    )
    return (
        settlement.statement_image,
        settlement.statement_original_name,
        content_type,
    )


def get_logistics_document_file(*, user, transfer_id, document_id):
    transfer, _identity_value, _side = _authorized_transfer(user, transfer_id)
    document = GoodsTransferLogisticsDocument.objects.filter(
        pk=document_id,
        settlement__transfer=transfer,
    ).first()
    if not document:
        raise GoodsTransferSettlementError("物流单据不存在。")
    return document.document_file, document.original_name, document.content_type


def get_payment_voucher_file(*, user, transfer_id, payment_id):
    transfer, _identity_value, _side = _authorized_transfer(user, transfer_id)
    payment = GoodsTransferPayment.objects.filter(
        pk=payment_id,
        settlement__transfer=transfer,
    ).first()
    if not payment:
        raise GoodsTransferSettlementError("付款凭证不存在。")
    content_type = (
        mimetypes.guess_type(payment.voucher_image.name)[0]
        or "image/jpeg"
    )
    return payment.voucher_image, payment.voucher_original_name, content_type
