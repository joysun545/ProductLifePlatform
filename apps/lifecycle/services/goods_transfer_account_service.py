import mimetypes
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.db import transaction
from django.db.models import Max, Q, Sum
from django.utils import timezone

from account.models import (
    OrganizationRelation,
    OrganizationRelationType,
    UserOrganization,
)
from account.services.auth_service import get_active_identity
from account.services.role_delegation_service import (
    SENIOR_MANAGER_ROLE_CODES,
    has_open_action_task_copy,
    identity_has_role,
    vacant_role_manager_openids,
)
from lifecycle.models import (
    GoodsTransfer,
    GoodsTransferAccountCase,
    GoodsTransferAccountPayment,
    GoodsTransferCreditAllocation,
    GoodsTransferPayment,
    GoodsTransferSettlement,
)
from lifecycle.services.goods_transfer_service import goods_transfer_root_payload
from workflow.models import WorkItem
from workflow.services.workflow_service import publish_workitem
from .goods_transfer_receivable_service import financial_write_errors


class GoodsTransferAccountError(Exception):
    pass


MERCHANT_ACCOUNTING_DISABLED = "商家向下发货不再形成应收账款，相关货款往来操作已停用。"


MERCHANT_ACCOUNT_ROLES = {
    "merchant_owner",
    "merchant_sales",
    "merchant_stock",
}
FACTORY_ACCOUNT_ROLES = {
    "factory_sales",
    "factory_sales_assistant",
}
ACCOUNT_ROLES = MERCHANT_ACCOUNT_ROLES | FACTORY_ACCOUNT_ROLES
MAX_FILE_SIZE = 10 * 1024 * 1024
MONEY_LIMIT = Decimal("999999999999.99")

SUBMIT_WORK_TYPE = "GOODS_TRANSFER_ACCOUNT_SUBMIT"
CONFIRM_WORK_TYPE = "GOODS_TRANSFER_ACCOUNT_CONFIRM"
PAYMENT_UPLOAD_WORK_TYPE = "GOODS_TRANSFER_ACCOUNT_PAYMENT_UPLOAD"
PAYMENT_CONFIRM_WORK_TYPE = "GOODS_TRANSFER_ACCOUNT_PAYMENT_CONFIRM"
NOTICE_WORK_TYPE = "GOODS_TRANSFER_ACCOUNT_NOTICE"


def _display_name(user):
    return user.wx_nickname or user.get_full_name() or user.username


def _money(value):
    return f"{value:.2f}" if value is not None else None


def _parse_amount(value, label):
    raw = str(value or "").strip()
    if not raw:
        raise GoodsTransferAccountError(f"请填写{label}。")
    try:
        amount = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise GoodsTransferAccountError(f"{label}格式不正确。") from exc
    if not amount.is_finite() or amount <= 0:
        raise GoodsTransferAccountError(f"{label}必须大于0。")
    if amount.as_tuple().exponent < -2:
        raise GoodsTransferAccountError(f"{label}最多保留两位小数。")
    if amount > MONEY_LIMIT:
        raise GoodsTransferAccountError(f"{label}超出允许范围。")
    return amount.quantize(Decimal("0.01"))


def _validate_image(uploaded_file, label):
    if not uploaded_file:
        return None
    if uploaded_file.size <= 0 or uploaded_file.size > MAX_FILE_SIZE:
        raise GoodsTransferAccountError(f"{label}不能超过10MB。")
    header = uploaded_file.read(12)
    uploaded_file.seek(0)
    if header.startswith(b"\xff\xd8\xff"):
        suffix, content_type = ".jpg", "image/jpeg"
    elif header.startswith(b"\x89PNG\r\n\x1a\n"):
        suffix, content_type = ".png", "image/png"
    elif header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        suffix, content_type = ".webp", "image/webp"
    else:
        raise GoodsTransferAccountError(
            f"{label}只支持JPG、PNG或WEBP格式。"
        )
    return suffix, Path(uploaded_file.name).name[:255], content_type


def _identity(user):
    identity = get_active_identity(user)
    if not identity or not identity_has_role(identity, ACCOUNT_ROLES):
        raise GoodsTransferAccountError("当前岗位无权处理商品流转往来账。")
    if identity.organization.organization_type.code not in {"MERCHANT", "OWNER"}:
        raise GoodsTransferAccountError("当前组织无权处理商品流转往来账。")
    return identity


def _require_manager_task_copy(identity, workitem):
    """Require the exact open task before a manager mutates account data."""

    if (
        identity.role.code in SENIOR_MANAGER_ROLE_CODES
        and (
            not workitem
            or not has_open_action_task_copy(
                identity,
                workitem_ids=(workitem.pk,),
            )
        )
    ):
        raise GoodsTransferAccountError(
            "高级经理只能通过本人收到的待办办理该项往来账业务。"
        )


def _role_codes_for_org(organization):
    if organization.organization_type.code == "OWNER":
        return FACTORY_ACCOUNT_ROLES
    return MERCHANT_ACCOUNT_ROLES


def _role_openids(organization):
    return tuple(
        UserOrganization.objects.filter(
            organization=organization,
            is_active=True,
            role__code__in=_role_codes_for_org(organization),
            role__is_active=True,
            user__is_active=True,
        )
        .exclude(user__wx_openid__isnull=True)
        .exclude(user__wx_openid="")
        .values_list("user__wx_openid", flat=True)
        .distinct()
    )


def _action_openids(organization):
    return (*_role_openids(organization),
            *vacant_role_manager_openids(organization, _role_codes_for_org(organization)))


def _is_direct_distribution(upper, lower):
    return OrganizationRelation.objects.filter(
        source_organization=upper,
        target_organization=lower,
        relation_type=OrganizationRelationType.DISTRIBUTION,
        is_active=True,
        source_organization__is_active=True,
        target_organization__is_active=True,
    ).exists()


def _account_link(transfer):
    return (
        "/pages/stock/goods_transfer_accounts/goods_transfer_accounts"
        f"?transfer_id={transfer.pk}"
    )


def _case_key(case, suffix):
    return (
        f"lifecycle:goods-transfer:{case.transfer_id}:account:"
        f"{case.case_type}:{suffix}"
    )


def _submit_key(case):
    revision = (
        f"retry:{case.rejected_at.isoformat()}"
        if case.rejected_at
        else "initial"
    )
    return _case_key(case, f"submit:{revision}")


def _task_payload(case, **extra):
    transfer = case.transfer
    payload = {
        "goods_transfer_id": transfer.pk,
        "flow_no": transfer.flow_no,
        "account_case_id": case.pk,
        "account_case_type": case.case_type,
        "upper_organization_id": case.upper_organization_id,
        "upper_organization_name": case.upper_organization.name,
        "lower_organization_id": case.lower_organization_id,
        "lower_organization_name": case.lower_organization.name,
        "device_count": transfer.items.count(),
    }
    payload.update(extra)
    return payload


def _complete_anyone(workitem, user, *, actor_title, peer_title):
    if workitem.status == "DONE":
        raise GoodsTransferAccountError("该协作任务已由其他成员完成，请刷新页面。")
    task = (
        workitem.user_tasks.select_for_update()
        .filter(to_openid=user.wx_openid, need_action=True)
        .exclude(state="DONE")
        .first()
    )
    if not task:
        raise GoodsTransferAccountError("当前账号不是该任务的处理人。")
    now = timezone.now()
    payload = dict(workitem.payload or {})
    payload.update(
        {
            "completed_by_user_id": user.pk,
            "completed_by_name": _display_name(user),
            "completed_at": now.isoformat(),
        }
    )
    if WorkItem.objects.filter(pk=workitem.pk, status="OPEN").update(
        status="DONE",
        done_by_openid=user.wx_openid or "",
        done_at=now,
        last_activity_at=now,
        payload=payload,
        updated_at=now,
    ) != 1:
        raise GoodsTransferAccountError("该协作任务已由其他成员完成，请刷新页面。")
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
        display_subtitle_snapshot=f"{_display_name(user)}已处理，同组织无需重复操作",
        cursor_time=now,
        updated_at=now,
    )


def _submitter_org(case):
    if case.case_type == GoodsTransferAccountCase.CaseType.RETURN_CREDIT:
        return case.lower_organization
    return case.upper_organization


def _confirmer_org(case):
    if case.case_type == GoodsTransferAccountCase.CaseType.RETURN_CREDIT:
        return case.upper_organization
    return case.lower_organization


def _publish_submit_task(case, actor_openid=""):
    recipients = _action_openids(_submitter_org(case))
    if not recipients:
        return None
    return_title = (
        "退货已由厂家取回，待提交退货冲抵金额"
        if case.upper_organization.organization_type.code == "OWNER"
        else "退货已入库，待提交退货冲抵金额"
    )
    titles = {
        GoodsTransferAccountCase.CaseType.RETURN_CREDIT: return_title,
        GoodsTransferAccountCase.CaseType.TRANSFER_OUT_CREDIT: "调货已完成，待提交调出方冲抵金额",
        GoodsTransferAccountCase.CaseType.TRANSFER_IN_RECEIVABLE: "调货已完成，待提交调入方新增应收",
    }
    return publish_workitem(
        domain="LOGISTICS",
        type=SUBMIT_WORK_TYPE,
        biz_id=case.transfer.flow_no,
        biz_ref=f"lifecycle.GoodsTransfer:{case.transfer_id}",
        uniq_key=_submit_key(case),
        title=titles[case.case_type],
        summary=(
            f"{case.upper_organization.name} ↔ {case.lower_organization.name}｜"
            f"共{case.transfer.items.count()}台"
        ),
        link=_account_link(case.transfer),
        recipients_openids=recipients,
        need_action=True,
        action_mode="ANYONE_DO",
        payload=_task_payload(case),
        actor_openid=actor_openid,
        mark_workitem_done_immediately=False,
    )


def _publish_confirm_task(case, actor_openid=""):
    recipients = _action_openids(_confirmer_org(case))
    if not recipients:
        return None
    amount_text = "本次无款项" if case.proposed_amount is None else f"¥{case.proposed_amount:.2f}"
    return publish_workitem(
        domain="LOGISTICS",
        type=CONFIRM_WORK_TYPE,
        biz_id=case.transfer.flow_no,
        biz_ref=f"lifecycle.GoodsTransfer:{case.transfer_id}",
        uniq_key=_case_key(case, f"confirm:{case.submitted_at.isoformat()}"),
        title=f"{case.get_case_type_display()}待确认",
        summary=(
            f"{case.upper_organization.name} ↔ {case.lower_organization.name}｜"
            f"{amount_text}"
        ),
        link=_account_link(case.transfer),
        recipients_openids=recipients,
        need_action=True,
        action_mode="ANYONE_DO",
        payload=_task_payload(case, proposed_amount=_money(case.proposed_amount)),
        actor_openid=actor_openid,
        mark_workitem_done_immediately=False,
    )


def _publish_notice(case, organization, title, summary, actor_openid=""):
    recipients = _role_openids(organization)
    if not recipients:
        return None
    return publish_workitem(
        domain="LOGISTICS",
        type=NOTICE_WORK_TYPE,
        biz_id=case.transfer.flow_no,
        biz_ref=f"lifecycle.GoodsTransfer:{case.transfer_id}",
        uniq_key=_case_key(case, f"notice:{title}:{case.updated_at.isoformat()}"),
        title=title,
        summary=summary,
        link=_account_link(case.transfer),
        recipients_openids=recipients,
        need_action=False,
        action_mode="EACH_ACK",
        payload=_task_payload(case),
        actor_openid=actor_openid,
        mark_workitem_done_immediately=True,
    )


def _publish_payment_upload_task(case, sequence_no, actor_openid=""):
    recipients = _action_openids(case.lower_organization)
    if not recipients:
        return None
    return publish_workitem(
        domain="LOGISTICS",
        type=PAYMENT_UPLOAD_WORK_TYPE,
        biz_id=case.transfer.flow_no,
        biz_ref=f"lifecycle.GoodsTransfer:{case.transfer_id}",
        uniq_key=_case_key(case, f"payment-upload:{sequence_no}"),
        title=f"调货新增应收第{sequence_no}笔付款待处理",
        summary=f"应收 ¥{case.confirmed_amount:.2f}",
        link=_account_link(case.transfer),
        recipients_openids=recipients,
        need_action=True,
        action_mode="ANYONE_DO",
        payload=_task_payload(case, payment_sequence=sequence_no),
        actor_openid=actor_openid,
        mark_workitem_done_immediately=False,
    )


def _publish_payment_confirm_task(payment, actor_openid=""):
    case = payment.account_case
    recipients = _action_openids(case.upper_organization)
    if not recipients:
        return None
    return publish_workitem(
        domain="LOGISTICS",
        type=PAYMENT_CONFIRM_WORK_TYPE,
        biz_id=case.transfer.flow_no,
        biz_ref=f"lifecycle.GoodsTransfer:{case.transfer_id}",
        uniq_key=_case_key(case, f"payment:{payment.pk}:confirm"),
        title=f"调货第{payment.sequence_no}笔收款待确认",
        summary=f"{case.lower_organization.name}｜请填写实际到账金额",
        link=_account_link(case.transfer),
        recipients_openids=recipients,
        need_action=True,
        action_mode="ANYONE_DO",
        payload=_task_payload(case, payment_id=payment.pk),
        actor_openid=actor_openid,
        mark_workitem_done_immediately=False,
    )


def _create_case(transfer, case_type, upper, lower, creditor, debtor):
    if upper_id := getattr(upper, "pk", None):
        if upper_id == getattr(lower, "pk", None):
            raise GoodsTransferAccountError("上下级结算组织不能相同。")
    if not _is_direct_distribution(upper, lower):
        raise GoodsTransferAccountError(
            f"{upper.name}与{lower.name}不是有效的直属上下级经销关系。"
        )
    case, created = GoodsTransferAccountCase.objects.get_or_create(
        transfer=transfer,
        case_type=case_type,
        defaults={
            "upper_organization": upper,
            "lower_organization": lower,
            "creditor_organization": creditor,
            "debtor_organization": debtor,
        },
    )
    expected = (
        case.upper_organization_id == upper.pk
        and case.lower_organization_id == lower.pk
        and case.creditor_organization_id == creditor.pk
        and case.debtor_organization_id == debtor.pk
    )
    if not expected:
        raise GoodsTransferAccountError("已有往来账的上下级关系与当前流转不一致。")
    return case, created


@transaction.atomic
def initialize_goods_transfer_accounts(*, transfer, actor=None):
    """Create vertical cases after physical ownership has moved."""

    transfer = GoodsTransfer.objects.select_for_update().select_related(
        "instruction_organization__organization_type",
        "from_organization__organization_type",
        "to_organization__organization_type",
    ).get(pk=transfer.pk)
    factory_return_picked_up = bool(
        transfer.flow_type == GoodsTransfer.FlowType.RETURN
        and transfer.to_organization.organization_type.code == "OWNER"
        and transfer.status == GoodsTransfer.Status.PENDING_FACTORY_POSTING
    )
    factory_return_submitted = bool(
        transfer.flow_type == GoodsTransfer.FlowType.RETURN
        and transfer.to_organization.organization_type.code == "OWNER"
        and transfer.status == GoodsTransfer.Status.PENDING_FACTORY_PICKUP
    )
    if (
        transfer.status != GoodsTransfer.Status.COMPLETED
        and not factory_return_picked_up
        and not factory_return_submitted
    ):
        return [], False
    definitions = []
    if transfer.flow_type == GoodsTransfer.FlowType.RETURN:
        definitions.append(
            (
                GoodsTransferAccountCase.CaseType.RETURN_CREDIT,
                transfer.to_organization,
                transfer.from_organization,
                transfer.from_organization,
                transfer.to_organization,
            )
        )
    elif transfer.flow_type == GoodsTransfer.FlowType.TRANSFER:
        definitions.extend(
            (
                (
                    GoodsTransferAccountCase.CaseType.TRANSFER_OUT_CREDIT,
                    transfer.instruction_organization,
                    transfer.from_organization,
                    transfer.from_organization,
                    transfer.instruction_organization,
                ),
                (
                    GoodsTransferAccountCase.CaseType.TRANSFER_IN_RECEIVABLE,
                    transfer.instruction_organization,
                    transfer.to_organization,
                    transfer.instruction_organization,
                    transfer.to_organization,
                ),
            )
        )
    else:
        return [], False
    cases = []
    any_created = False
    for definition in definitions:
        # Merchant distribution is a physical/document flow, not another debt.
        # Factory-owned return credit remains part of the existing return flow.
        if definition[1].organization_type.code == "MERCHANT":
            continue
        case, created = _create_case(transfer, *definition)
        cases.append(case)
        any_created = any_created or created
        submit_task = WorkItem.objects.filter(
            type=SUBMIT_WORK_TYPE,
            uniq_key=_submit_key(case),
        ).first()
        factory_return_case = bool(
            case.case_type == GoodsTransferAccountCase.CaseType.RETURN_CREDIT
            and transfer.to_organization.organization_type.code == "OWNER"
        )
        if not factory_return_case and case.status in {
            GoodsTransferAccountCase.Status.DRAFT,
            GoodsTransferAccountCase.Status.REJECTED,
        } and not submit_task:
            _publish_submit_task(case, getattr(actor, "wx_openid", "") or "")
    return cases, any_created


def _authorized_transfer(user, transfer_id):
    identity = _identity(user)
    transfer = GoodsTransfer.objects.select_related(
        "instruction_organization__organization_type",
        "from_organization__organization_type",
        "to_organization__organization_type",
    ).filter(pk=transfer_id).first()
    if not transfer:
        raise GoodsTransferAccountError("商品流转单不存在。")
    if transfer.flow_type not in {
        GoodsTransfer.FlowType.RETURN,
        GoodsTransfer.FlowType.TRANSFER,
    }:
        raise GoodsTransferAccountError("本页只处理退货冲抵和调货上下级账务。")
    if identity.organization_id not in {
        transfer.instruction_organization_id,
        transfer.from_organization_id,
        transfer.to_organization_id,
    }:
        raise GoodsTransferAccountError("该商品流转往来账不属于当前组织。")
    factory_return_picked_up = bool(
        transfer.flow_type == GoodsTransfer.FlowType.RETURN
        and transfer.to_organization.organization_type.code == "OWNER"
        and transfer.status == GoodsTransfer.Status.PENDING_FACTORY_POSTING
    )
    if (
        transfer.status != GoodsTransfer.Status.COMPLETED
        and not factory_return_picked_up
    ):
        raise GoodsTransferAccountError("请先完成实物流转和物权转移。")
    return transfer, identity


def _authorized_case(user, transfer_id, case_id=None, case_type=None, lock=False):
    transfer, identity = _authorized_transfer(user, transfer_id)
    initialize_goods_transfer_accounts(transfer=transfer, actor=user)
    query = GoodsTransferAccountCase.objects.select_related(
        "transfer__instruction_organization__organization_type",
        "transfer__from_organization__organization_type",
        "transfer__to_organization__organization_type",
        "upper_organization__organization_type",
        "lower_organization__organization_type",
        "creditor_organization",
        "debtor_organization",
        "submitted_by",
        "confirmed_by",
        "rejected_by",
    )
    if lock:
        query = query.select_for_update()
    query = query.filter(transfer=transfer)
    if case_id is not None:
        query = query.filter(pk=case_id)
    if case_type is not None:
        query = query.filter(case_type=str(case_type or "").upper())
    case = query.first()
    if not case:
        raise GoodsTransferAccountError("往来账记录不存在。")
    if case.upper_organization.organization_type.code == "MERCHANT":
        raise GoodsTransferAccountError(MERCHANT_ACCOUNTING_DISABLED)
    if identity.organization_id not in {
        case.upper_organization_id,
        case.lower_organization_id,
    }:
        # This is the privacy boundary that prevents B from seeing A-C prices.
        raise GoodsTransferAccountError("该往来账不属于当前组织。")
    return case, identity


def _confirmed_payment_total(case):
    return case.payments.filter(
        status=GoodsTransferAccountPayment.Status.CONFIRMED
    ).aggregate(total=Sum("confirmed_amount"))["total"] or Decimal("0.00")


def _allocated_to_case_total(case):
    return case.received_credit_allocations.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")


def _credit_allocated_total(case):
    return case.credit_allocations.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")


def _case_outstanding(case):
    if (
        case.case_type
        != GoodsTransferAccountCase.CaseType.TRANSFER_IN_RECEIVABLE
        or case.status != GoodsTransferAccountCase.Status.CONFIRMED
        or case.confirmed_amount is None
    ):
        return None
    return (
        case.confirmed_amount
        - _confirmed_payment_total(case)
        - _allocated_to_case_total(case)
    )


def _shipment_received_total(settlement):
    from .goods_transfer_receivable_service import invoice_totals
    return invoice_totals(settlement)["received_amount"]


def _shipment_credit_total(settlement):
    return settlement.credit_allocations.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")


def _shipment_outstanding(settlement):
    if settlement.receivable_amount is None:
        return None
    return (
        settlement.receivable_amount
        - _shipment_received_total(settlement)
        - _shipment_credit_total(settlement)
    )


def _candidate_receivables(case):
    upper_id = case.upper_organization_id
    lower_id = case.lower_organization_id
    rows = []
    shipment_settlements = GoodsTransferSettlement.objects.select_related(
        "transfer"
    ).filter(
        transfer__flow_type=GoodsTransfer.FlowType.SHIPMENT,
        transfer__from_organization_id=upper_id,
        transfer__to_organization_id=lower_id,
        receivable_amount__isnull=False,
    )
    for settlement in shipment_settlements:
        outstanding = _shipment_outstanding(settlement)
        if outstanding is not None and outstanding > 0:
            rows.append(
                {
                    "target_kind": "SHIPMENT",
                    "target_id": settlement.pk,
                    "flow_no": settlement.transfer.flow_no,
                    "amount": _money(settlement.receivable_amount),
                    "outstanding_amount": _money(outstanding),
                    "created_at": settlement.created_at.isoformat(),
                }
            )
    transfer_cases = GoodsTransferAccountCase.objects.select_related(
        "transfer"
    ).filter(
        case_type=GoodsTransferAccountCase.CaseType.TRANSFER_IN_RECEIVABLE,
        status=GoodsTransferAccountCase.Status.CONFIRMED,
        upper_organization_id=upper_id,
        lower_organization_id=lower_id,
    ).exclude(pk=case.pk)
    for target in transfer_cases:
        outstanding = _case_outstanding(target)
        if outstanding is not None and outstanding > 0:
            rows.append(
                {
                    "target_kind": "TRANSFER_RECEIVABLE",
                    "target_id": target.pk,
                    "flow_no": target.transfer.flow_no,
                    "amount": _money(target.confirmed_amount),
                    "outstanding_amount": _money(outstanding),
                    "created_at": target.created_at.isoformat(),
                }
            )
    rows.sort(key=lambda row: (row["created_at"], row["target_kind"], row["target_id"]))
    return rows


def _pair_summary(upper, lower):
    shipment_rows = GoodsTransferSettlement.objects.filter(
        transfer__flow_type=GoodsTransfer.FlowType.SHIPMENT,
        transfer__from_organization=upper,
        transfer__to_organization=lower,
        receivable_amount__isnull=False,
    )
    shipment_receivable = shipment_rows.aggregate(total=Sum("receivable_amount"))["total"] or Decimal("0.00")
    shipment_received = GoodsTransferPayment.objects.filter(
        settlement__in=shipment_rows,
        status=GoodsTransferPayment.Status.CONFIRMED,
    ).aggregate(total=Sum("confirmed_amount"))["total"] or Decimal("0.00")
    account_receivable_cases = GoodsTransferAccountCase.objects.filter(
        case_type=GoodsTransferAccountCase.CaseType.TRANSFER_IN_RECEIVABLE,
        status=GoodsTransferAccountCase.Status.CONFIRMED,
        upper_organization=upper,
        lower_organization=lower,
    )
    transfer_receivable = account_receivable_cases.aggregate(total=Sum("confirmed_amount"))["total"] or Decimal("0.00")
    transfer_received = GoodsTransferAccountPayment.objects.filter(
        account_case__in=account_receivable_cases,
        status=GoodsTransferAccountPayment.Status.CONFIRMED,
    ).aggregate(total=Sum("confirmed_amount"))["total"] or Decimal("0.00")
    credit_cases = GoodsTransferAccountCase.objects.filter(
        case_type__in=(
            GoodsTransferAccountCase.CaseType.RETURN_CREDIT,
            GoodsTransferAccountCase.CaseType.TRANSFER_OUT_CREDIT,
        ),
        status=GoodsTransferAccountCase.Status.CONFIRMED,
        upper_organization=upper,
        lower_organization=lower,
    )
    credit_total = credit_cases.aggregate(total=Sum("confirmed_amount"))["total"] or Decimal("0.00")
    gross = shipment_receivable + transfer_receivable
    received = shipment_received + transfer_received
    net = gross - received - credit_total
    return {
        "upper_organization_id": upper.pk,
        "upper_organization_name": upper.name,
        "lower_organization_id": lower.pk,
        "lower_organization_name": lower.name,
        "gross_receivable_amount": _money(gross),
        "confirmed_received_amount": _money(received),
        "confirmed_credit_amount": _money(credit_total),
        "net_receivable_amount": _money(max(net, Decimal("0.00"))),
        "pending_credit_amount": _money(max(-net, Decimal("0.00"))),
    }


def _serialize_payment(payment, user, case):
    can_confirm = bool(
        payment.status == GoodsTransferAccountPayment.Status.PENDING
        and user.organization_identity.organization_id == case.upper_organization_id
        and WorkItem.objects.filter(
            type=PAYMENT_CONFIRM_WORK_TYPE,
            uniq_key=_case_key(case, f"payment:{payment.pk}:confirm"),
            status="OPEN",
            user_tasks__to_openid=user.wx_openid,
            user_tasks__need_action=True,
        ).exclude(user_tasks__state="DONE").exists()
    )
    return {
        "id": payment.pk,
        "sequence_no": payment.sequence_no,
        "status": payment.status,
        "status_name": payment.get_status_display(),
        "voucher_original_name": payment.voucher_original_name,
        "voucher_preview_path": (
            f"/lifecycle/goods-transfers/{case.transfer_id}/accounts/"
            f"{case.pk}/payments/{payment.pk}/voucher-file/"
        ),
        "uploaded_by_name": _display_name(payment.uploaded_by),
        "uploaded_at": payment.uploaded_at.isoformat(),
        "confirmed_amount": _money(payment.confirmed_amount),
        "confirmed_by_name": _display_name(payment.confirmed_by) if payment.confirmed_by else "",
        "confirmed_at": payment.confirmed_at.isoformat() if payment.confirmed_at else None,
        "can_confirm": can_confirm,
    }


def _serialize_case(case, user, identity):
    submitter_org = _submitter_org(case)
    confirmer_org = _confirmer_org(case)
    is_credit = case.case_type in {
        GoodsTransferAccountCase.CaseType.RETURN_CREDIT,
        GoodsTransferAccountCase.CaseType.TRANSFER_OUT_CREDIT,
    }
    allocated = _credit_allocated_total(case) if is_credit else _allocated_to_case_total(case)
    available = (
        case.confirmed_amount - allocated
        if is_credit
        and case.status == GoodsTransferAccountCase.Status.CONFIRMED
        and case.confirmed_amount is not None
        else None
    )
    outstanding = _case_outstanding(case)
    pending_payment = case.payments.filter(
        status=GoodsTransferAccountPayment.Status.PENDING
    ).exists()
    factory_return_case = bool(
        case.case_type == GoodsTransferAccountCase.CaseType.RETURN_CREDIT
        and case.upper_organization.organization_type.code == "OWNER"
    )
    can_submit = bool(
        not factory_return_case
        and identity.organization_id == submitter_org.pk
        and case.status in {
            GoodsTransferAccountCase.Status.DRAFT,
            GoodsTransferAccountCase.Status.REJECTED,
        }
    )
    can_confirm = bool(
        not factory_return_case
        and identity.organization_id == confirmer_org.pk
        and case.status == GoodsTransferAccountCase.Status.PENDING_CONFIRM
    )
    can_allocate = bool(
        is_credit
        and identity.organization_id == case.upper_organization_id
        and case.status == GoodsTransferAccountCase.Status.CONFIRMED
        and available is not None
        and available > 0
    )
    can_upload_payment = bool(
        case.case_type == GoodsTransferAccountCase.CaseType.TRANSFER_IN_RECEIVABLE
        and identity.organization_id == case.lower_organization_id
        and case.status == GoodsTransferAccountCase.Status.CONFIRMED
        and outstanding is not None
        and outstanding > 0
        and not pending_payment
    )
    allocations = []
    for allocation in case.credit_allocations.select_related(
        "target_shipment_settlement__transfer",
        "target_account_case__transfer",
        "allocated_by",
    ):
        target = allocation.target_shipment_settlement or allocation.target_account_case
        allocations.append(
            {
                "id": allocation.pk,
                "target_kind": "SHIPMENT" if allocation.target_shipment_settlement_id else "TRANSFER_RECEIVABLE",
                "target_id": target.pk,
                "flow_no": target.transfer.flow_no,
                "amount": _money(allocation.amount),
                "allocated_by_name": _display_name(allocation.allocated_by),
                "allocated_at": allocation.allocated_at.isoformat(),
            }
        )
    payments = list(case.payments.select_related("uploaded_by", "confirmed_by"))
    return {
        "id": case.pk,
        "case_type": case.case_type,
        "case_type_name": case.get_case_type_display(),
        "status": case.status,
        "status_name": case.get_status_display(),
        "upper_organization": {"id": case.upper_organization_id, "name": case.upper_organization.name},
        "lower_organization": {"id": case.lower_organization_id, "name": case.lower_organization.name},
        "creditor_organization": {"id": case.creditor_organization_id, "name": case.creditor_organization.name},
        "debtor_organization": {"id": case.debtor_organization_id, "name": case.debtor_organization.name},
        "proposed_amount": _money(case.proposed_amount),
        "confirmed_amount": _money(case.confirmed_amount),
        "allocated_amount": _money(allocated),
        "available_credit_amount": _money(available),
        "outstanding_amount": _money(outstanding),
        "statement": {
            "exists": bool(case.statement_image),
            "original_name": case.statement_original_name,
            "preview_path": (
                f"/lifecycle/goods-transfers/{case.transfer_id}/accounts/{case.pk}/statement-file/"
                if case.statement_image else ""
            ),
        },
        "remark": case.remark,
        "submitted_by_name": _display_name(case.submitted_by) if case.submitted_by else "",
        "submitted_at": case.submitted_at.isoformat() if case.submitted_at else None,
        "confirmed_by_name": _display_name(case.confirmed_by) if case.confirmed_by else "",
        "confirmed_at": case.confirmed_at.isoformat() if case.confirmed_at else None,
        "rejection_reason": case.rejection_reason,
        "actions": {
            "can_submit": can_submit,
            "can_confirm": can_confirm,
            "can_allocate": can_allocate,
            "can_upload_payment": can_upload_payment,
        },
        "allocation_candidates": _candidate_receivables(case) if can_allocate else [],
        "allocations": allocations,
        "payments": [_serialize_payment(row, user, case) for row in payments],
        "pair_summary": _pair_summary(case.upper_organization, case.lower_organization),
    }


def get_goods_transfer_accounts(*, user, transfer_id):
    transfer, identity = _authorized_transfer(user, transfer_id)
    initialize_goods_transfer_accounts(transfer=transfer, actor=user)
    cases = list(
        GoodsTransferAccountCase.objects.select_related(
            "transfer",
            "upper_organization__organization_type",
            "lower_organization__organization_type",
            "creditor_organization",
            "debtor_organization",
            "submitted_by",
            "confirmed_by",
            "rejected_by",
        ).filter(transfer=transfer, upper_organization__organization_type__code="OWNER").filter(
            Q(upper_organization=identity.organization)
            | Q(lower_organization=identity.organization)
        )
    )
    return {
        "root_organization": goods_transfer_root_payload(transfer),
        "goods_transfer": {
            "id": transfer.pk,
            "flow_no": transfer.flow_no,
            "flow_type": transfer.flow_type,
            "flow_type_name": transfer.get_flow_type_display(),
            "from_organization_name": transfer.from_organization.name,
            "to_organization_name": transfer.to_organization.name,
            "instruction_organization_name": transfer.instruction_organization.name,
            "device_count": transfer.items.count(),
        },
        "viewer": {
            "organization_id": identity.organization_id,
            "organization_name": identity.organization.name,
            "role": identity.role.code,
            "role_name": identity.role.name,
        },
        "cases": [_serialize_case(case, user, identity) for case in cases],
        "merchant_accounts_enabled": bool(
            (transfer.flow_type == GoodsTransfer.FlowType.RETURN and transfer.to_organization.organization_type.code == "OWNER")
            or (transfer.flow_type == GoodsTransfer.FlowType.TRANSFER and transfer.instruction_organization.organization_type.code == "OWNER")
        ),
        "disabled_reason": MERCHANT_ACCOUNTING_DISABLED if not cases else "",
    }


@transaction.atomic
def submit_goods_transfer_account_case(
    *, user, transfer_id, case_type, amount=None, no_amount=False, remark="", image=None
):
    case, identity = _authorized_case(
        user, transfer_id, case_type=case_type, lock=True
    )
    if (
        case.case_type == GoodsTransferAccountCase.CaseType.RETURN_CREDIT
        and case.upper_organization.organization_type.code == "OWNER"
    ):
        raise GoodsTransferAccountError(
            "退厂退货冲抵金额由厂家销售助理在物流确认取回后登记。"
        )
    if identity.organization_id != _submitter_org(case).pk:
        raise GoodsTransferAccountError("当前组织不是该往来账的提交方。")
    if case.status not in {
        GoodsTransferAccountCase.Status.DRAFT,
        GoodsTransferAccountCase.Status.REJECTED,
    }:
        raise GoodsTransferAccountError("该往来金额已经提交，不能重复处理。")
    no_amount = str(no_amount).lower() in {"1", "true", "yes", "on"}
    proposed = None if no_amount else _parse_amount(amount, "关联金额")
    current_submit_key = _submit_key(case)
    submit_workitem = WorkItem.objects.select_for_update().filter(
        type=SUBMIT_WORK_TYPE,
        uniq_key=current_submit_key,
        status="OPEN",
    ).first()
    _require_manager_task_copy(identity, submit_workitem)
    validated_image = _validate_image(image, "款项关联清单")
    if validated_image:
        suffix, original_name, _content_type = validated_image
        case.statement_image.save(
            f"account-case-{case.pk}-{uuid.uuid4().hex}{suffix}",
            image,
            save=False,
        )
        case.statement_original_name = original_name
    now = timezone.now()
    case.proposed_amount = proposed
    case.confirmed_amount = None
    case.remark = str(remark or "").strip()[:255]
    case.submitted_by = user
    case.submitted_at = now
    case.confirmed_by = None
    case.confirmed_at = None
    case.rejected_by = None
    case.rejected_at = None
    case.rejection_reason = ""
    case.status = GoodsTransferAccountCase.Status.PENDING_CONFIRM
    case.save()
    if submit_workitem:
        _complete_anyone(
            submit_workitem,
            user,
            actor_title=f"{case.get_case_type_display()}已提交",
            peer_title=f"{_display_name(user)}已提交{case.get_case_type_display()}",
        )
    _publish_confirm_task(case, user.wx_openid or "")
    return get_goods_transfer_accounts(user=user, transfer_id=transfer_id)


@transaction.atomic
def confirm_goods_transfer_account_case(
    *, user, transfer_id, case_id, action, rejection_reason=""
):
    case, identity = _authorized_case(user, transfer_id, case_id=case_id, lock=True)
    if (
        case.case_type == GoodsTransferAccountCase.CaseType.RETURN_CREDIT
        and case.upper_organization.organization_type.code == "OWNER"
    ):
        raise GoodsTransferAccountError("退厂退货冲抵金额不经过商家提交确认流程。")
    if identity.organization_id != _confirmer_org(case).pk:
        raise GoodsTransferAccountError("当前组织不是该往来账的确认方。")
    if case.status != GoodsTransferAccountCase.Status.PENDING_CONFIRM:
        raise GoodsTransferAccountError("该往来金额不在待确认状态。")
    workitem = WorkItem.objects.select_for_update().filter(
        type=CONFIRM_WORK_TYPE,
        uniq_key=_case_key(case, f"confirm:{case.submitted_at.isoformat()}"),
        status="OPEN",
    ).first()
    if not workitem:
        raise GoodsTransferAccountError("未找到对应的金额确认任务。")
    action = str(action or "").upper()
    now = timezone.now()
    if action == "REJECT":
        reason = str(rejection_reason or "").strip()
        if not reason:
            raise GoodsTransferAccountError("驳回时必须填写原因。")
        _complete_anyone(
            workitem,
            user,
            actor_title=f"{case.get_case_type_display()}已驳回",
            peer_title=f"{_display_name(user)}已驳回{case.get_case_type_display()}",
        )
        case.status = GoodsTransferAccountCase.Status.REJECTED
        case.rejected_by = user
        case.rejected_at = now
        case.rejection_reason = reason[:255]
        case.save(update_fields=("status", "rejected_by", "rejected_at", "rejection_reason", "updated_at"))
        _publish_submit_task(case, user.wx_openid or "")
        return get_goods_transfer_accounts(user=user, transfer_id=transfer_id)
    if action != "CONFIRM":
        raise GoodsTransferAccountError("确认操作类型无效。")
    _complete_anyone(
        workitem,
        user,
        actor_title=f"{case.get_case_type_display()}已确认",
        peer_title=f"{_display_name(user)}已确认{case.get_case_type_display()}",
    )
    case.confirmed_amount = case.proposed_amount
    case.confirmed_by = user
    case.confirmed_at = now
    case.status = (
        GoodsTransferAccountCase.Status.NO_AMOUNT
        if case.proposed_amount is None
        else GoodsTransferAccountCase.Status.CONFIRMED
    )
    case.save(update_fields=("confirmed_amount", "confirmed_by", "confirmed_at", "status", "updated_at"))
    _publish_notice(
        case,
        _submitter_org(case),
        f"{case.get_case_type_display()}已确认",
        "本次无款项" if case.confirmed_amount is None else f"确认金额 ¥{case.confirmed_amount:.2f}",
        user.wx_openid or "",
    )
    if (
        case.case_type == GoodsTransferAccountCase.CaseType.TRANSFER_IN_RECEIVABLE
        and case.status == GoodsTransferAccountCase.Status.CONFIRMED
    ):
        _publish_payment_upload_task(case, 1, user.wx_openid or "")
    return get_goods_transfer_accounts(user=user, transfer_id=transfer_id)


@financial_write_errors(GoodsTransferAccountError)
@transaction.atomic
def allocate_goods_transfer_credit(
    *, user, transfer_id, case_id, target_kind, target_id, amount
):
    case, identity = _authorized_case(user, transfer_id, case_id=case_id, lock=True)
    if identity.role.code in SENIOR_MANAGER_ROLE_CODES:
        raise GoodsTransferAccountError(
            "高级经理不能在没有本人待办任务时办理退货冲抵分配。"
        )
    if case.case_type not in {
        GoodsTransferAccountCase.CaseType.RETURN_CREDIT,
        GoodsTransferAccountCase.CaseType.TRANSFER_OUT_CREDIT,
    } or case.status != GoodsTransferAccountCase.Status.CONFIRMED:
        raise GoodsTransferAccountError("只有已确认的退货冲抵额度可以关联应收批次。")
    if identity.organization_id != case.upper_organization_id:
        raise GoodsTransferAccountError("只有直属上级组织可以执行应收冲抵。")
    allocation_amount = _parse_amount(amount, "冲抵金额")
    available = case.confirmed_amount - _credit_allocated_total(case)
    if allocation_amount > available:
        raise GoodsTransferAccountError("冲抵金额超过当前可用退货冲抵余额。")
    target_kind = str(target_kind or "").upper()
    target_id = int(target_id)
    defaults = {"amount": allocation_amount, "allocated_by": user}
    if target_kind == "SHIPMENT":
        target = GoodsTransferSettlement.objects.select_related("transfer").filter(
            pk=target_id,
            transfer__from_organization_id=case.upper_organization_id,
            transfer__to_organization_id=case.lower_organization_id,
        ).first()
        if not target:
            raise GoodsTransferAccountError("原发货应收批次不存在或不属于该上下级关系。")
        from .goods_transfer_receivable_service import lock_receivable_account
        lock_receivable_account(target.transfer)
        target = GoodsTransferSettlement.objects.select_for_update().select_related("transfer").get(pk=target.pk)
        outstanding = _shipment_outstanding(target)
        if outstanding is None or allocation_amount > outstanding:
            raise GoodsTransferAccountError("冲抵金额超过该发货批次应收余额。")
        allocation, created = GoodsTransferCreditAllocation.objects.get_or_create(
            credit_case=case,
            target_shipment_settlement=target,
            defaults=defaults,
        )
    elif target_kind == "TRANSFER_RECEIVABLE":
        target = GoodsTransferAccountCase.objects.select_for_update().filter(
            pk=target_id,
            case_type=GoodsTransferAccountCase.CaseType.TRANSFER_IN_RECEIVABLE,
            status=GoodsTransferAccountCase.Status.CONFIRMED,
            upper_organization_id=case.upper_organization_id,
            lower_organization_id=case.lower_organization_id,
        ).first()
        if not target:
            raise GoodsTransferAccountError("调货新增应收批次不存在或不属于该上下级关系。")
        outstanding = _case_outstanding(target)
        if outstanding is None or allocation_amount > outstanding:
            raise GoodsTransferAccountError("冲抵金额超过该调货应收余额。")
        allocation, created = GoodsTransferCreditAllocation.objects.get_or_create(
            credit_case=case,
            target_account_case=target,
            defaults=defaults,
        )
    else:
        raise GoodsTransferAccountError("请选择有效的应收批次。")
    if not created:
        raise GoodsTransferAccountError("该退货额度已经关联过此应收批次。")
    if target_kind == "SHIPMENT":
        from .goods_transfer_receivable_task_service import reconcile_receivable_tasks
        reconcile_receivable_tasks(target, user.wx_openid or "")
    _publish_notice(
        case,
        case.lower_organization,
        "退货冲抵已关联应收批次",
        f"{allocation.amount:.2f}元已关联至{target.transfer.flow_no}",
        user.wx_openid or "",
    )
    return get_goods_transfer_accounts(user=user, transfer_id=transfer_id)


@transaction.atomic
def upload_goods_transfer_account_payment(
    *, user, transfer_id, case_id, client_request_id, image
):
    case, identity = _authorized_case(user, transfer_id, case_id=case_id, lock=True)
    if (
        case.case_type != GoodsTransferAccountCase.CaseType.TRANSFER_IN_RECEIVABLE
        or case.status != GoodsTransferAccountCase.Status.CONFIRMED
    ):
        raise GoodsTransferAccountError("该往来账不能上传付款凭证。")
    if identity.organization_id != case.lower_organization_id:
        raise GoodsTransferAccountError("只有调入下级商家可以上传付款凭证。")
    request_id = str(client_request_id or "").strip()
    if not request_id or len(request_id) > 64:
        raise GoodsTransferAccountError("付款请求标识无效，请重新进入页面。")
    existing = case.payments.filter(client_request_id=request_id).first()
    if existing:
        return get_goods_transfer_accounts(user=user, transfer_id=transfer_id), False
    if case.payments.filter(status=GoodsTransferAccountPayment.Status.PENDING).exists():
        raise GoodsTransferAccountError("上一笔付款尚未确认。")
    outstanding = _case_outstanding(case)
    if outstanding is None or outstanding <= 0:
        raise GoodsTransferAccountError("该调货应收已经结清。")
    validated_image = _validate_image(image, "付款凭证")
    if not validated_image:
        raise GoodsTransferAccountError("请上传付款凭证。")
    suffix, original_name, _content_type = validated_image
    sequence_no = (case.payments.aggregate(value=Max("sequence_no"))["value"] or 0) + 1
    workitem = WorkItem.objects.select_for_update().filter(
        type=PAYMENT_UPLOAD_WORK_TYPE,
        uniq_key=_case_key(case, f"payment-upload:{sequence_no}"),
        status="OPEN",
    ).first()
    if not workitem:
        _publish_payment_upload_task(case, sequence_no, user.wx_openid or "")
        workitem = WorkItem.objects.select_for_update().filter(
            type=PAYMENT_UPLOAD_WORK_TYPE,
            uniq_key=_case_key(case, f"payment-upload:{sequence_no}"),
            status="OPEN",
        ).first()
    if not workitem:
        raise GoodsTransferAccountError("未找到付款任务。")
    _complete_anyone(
        workitem,
        user,
        actor_title=f"调货第{sequence_no}笔付款凭证已上传",
        peer_title=f"{_display_name(user)}已上传调货第{sequence_no}笔付款凭证",
    )
    payment = GoodsTransferAccountPayment(
        account_case=case,
        sequence_no=sequence_no,
        client_request_id=request_id,
        voucher_original_name=original_name,
        uploaded_by=user,
    )
    payment.voucher_image.save(
        f"account-payment-{sequence_no}-{uuid.uuid4().hex}{suffix}", image, save=False
    )
    payment.save()
    _publish_payment_confirm_task(payment, user.wx_openid or "")
    return get_goods_transfer_accounts(user=user, transfer_id=transfer_id), True


@transaction.atomic
def confirm_goods_transfer_account_payment(
    *, user, transfer_id, case_id, payment_id, amount
):
    case, identity = _authorized_case(user, transfer_id, case_id=case_id, lock=True)
    if identity.organization_id != case.upper_organization_id:
        raise GoodsTransferAccountError("只有直属上级商家可以确认实际收款。")
    payment = GoodsTransferAccountPayment.objects.select_for_update().filter(
        pk=payment_id,
        account_case=case,
    ).first()
    if not payment:
        raise GoodsTransferAccountError("付款记录不存在。")
    if payment.status == GoodsTransferAccountPayment.Status.CONFIRMED:
        return get_goods_transfer_accounts(user=user, transfer_id=transfer_id), False
    confirmed_amount = _parse_amount(amount, "实际到账金额")
    outstanding = _case_outstanding(case)
    if outstanding is None or confirmed_amount > outstanding:
        raise GoodsTransferAccountError("实际到账金额超过当前应收余额。")
    workitem = WorkItem.objects.select_for_update().filter(
        type=PAYMENT_CONFIRM_WORK_TYPE,
        uniq_key=_case_key(case, f"payment:{payment.pk}:confirm"),
        status="OPEN",
    ).first()
    if not workitem:
        raise GoodsTransferAccountError("未找到收款确认任务。")
    _complete_anyone(
        workitem,
        user,
        actor_title=f"调货第{payment.sequence_no}笔收款已确认",
        peer_title=f"{_display_name(user)}已确认调货第{payment.sequence_no}笔收款",
    )
    now = timezone.now()
    payment.confirmed_amount = confirmed_amount
    payment.confirmed_by = user
    payment.confirmed_at = now
    payment.status = GoodsTransferAccountPayment.Status.CONFIRMED
    payment.save(update_fields=("confirmed_amount", "confirmed_by", "confirmed_at", "status", "updated_at"))
    if _case_outstanding(case) > 0:
        _publish_payment_upload_task(case, payment.sequence_no + 1, user.wx_openid or "")
    return get_goods_transfer_accounts(user=user, transfer_id=transfer_id), True


def get_goods_transfer_account_statement_file(*, user, transfer_id, case_id):
    case, _identity_value = _authorized_case(user, transfer_id, case_id=case_id)
    if not case.statement_image:
        raise GoodsTransferAccountError("款项关联清单不存在。")
    content_type = mimetypes.guess_type(case.statement_image.name)[0] or "image/jpeg"
    return case.statement_image, case.statement_original_name, content_type


def get_goods_transfer_account_payment_file(
    *, user, transfer_id, case_id, payment_id
):
    case, _identity_value = _authorized_case(user, transfer_id, case_id=case_id)
    payment = GoodsTransferAccountPayment.objects.filter(
        pk=payment_id,
        account_case=case,
    ).first()
    if not payment:
        raise GoodsTransferAccountError("付款凭证不存在。")
    content_type = mimetypes.guess_type(payment.voucher_image.name)[0] or "image/jpeg"
    return payment.voucher_image, payment.voucher_original_name, content_type
