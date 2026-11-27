"""Independent order receivables; shipment and final accounting are separate gates."""

import hashlib
import re
import warnings
from decimal import Decimal
from pathlib import PurePath
from uuid import uuid4

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from PIL import Image, UnidentifiedImageError

from account.models import UserOrganization
from account.services.auth_service import get_active_identity
from sales.models import OrderPlan, OrderPlanCancellationAudit, OrderPlanFinance, OrderPlanPayment
from workflow.models import WorkItem
from workflow.services.workflow_service import publish_workitem

from .pricing_totals import pricing_amounts
from .order_receivable_service import (
    allocate_payment_fifo, financial_write_errors, invoice_totals, lock_receivable_account,
    pair_context, synchronize_receivable_account,
)


class OrderFinanceError(ValueError):
    pass


FINANCE_TYPES = (
    "ORDER_PLAN_FINANCE_STATEMENT",
    "ORDER_PLAN_PAYMENT_UPLOAD",
    "ORDER_PLAN_PAYMENT_CONFIRM",
)
MAX_IMAGE_BYTES = 10 * 1024 * 1024
IMAGE_FORMATS = {"JPEG": (".jpg", "image/jpeg"), "PNG": (".png", "image/png"), "WEBP": (".webp", "image/webp")}
PENDING_CANCELLATION_STATUSES = (
    OrderPlanCancellationAudit.Status.WAITING_PRODUCTION_COMPLETION,
    OrderPlanCancellationAudit.Status.WAITING_FINISHED_STOCK,
)


def _active(plan):
    return plan.status != OrderPlan.Status.CANCELLED and not OrderPlanCancellationAudit.objects.filter(
        order_plan=plan, status__in=PENDING_CANCELLATION_STATUSES,
    ).exists()


def _money(value):
    text = str(value if value is not None else "").strip()
    if not re.fullmatch(r"[0-9]{1,12}(?:\.[0-9]{1,2})?", text):
        raise OrderFinanceError("金额须为大于零、最多两位小数的数字，且不超过999999999999.99。")
    amount = Decimal(text)
    if not amount.is_finite() or amount <= 0:
        raise OrderFinanceError("金额必须大于零。")
    return amount.quantize(Decimal("0.01"))


def _identity(user):
    return get_active_identity(user) if user and user.is_active else None


def _access(identity, plan):
    if not identity:
        return False, False, False
    role = identity.role.code
    is_assistant = (
        identity.organization_id == plan.factory_organization_id
        and identity.organization.organization_type.code == "OWNER"
        and role == "factory_sales_assistant"
    )
    is_owner = (
        identity.organization_id == plan.merchant_organization_id
        and identity.organization.organization_type.code == "MERCHANT"
        and role == "merchant_owner"
    )
    is_sales = (
        identity.organization_id == plan.factory_organization_id
        and identity.organization.organization_type.code == "OWNER"
        and role == "factory_sales" and identity.user_id == plan.created_by_id
    )
    return is_assistant or is_owner or is_sales, is_assistant, is_owner


def _plan(user, plan_id, *, role=None, lock=False):
    queryset = OrderPlan.objects.select_related("factory_organization", "merchant_organization")
    if lock:
        queryset = queryset.select_for_update()
    try:
        plan = queryset.get(pk=plan_id)
    except (OrderPlan.DoesNotExist, ValueError, TypeError) as exc:
        raise OrderFinanceError("订单不存在或无财务访问权限。") from exc
    allowed, assistant, owner = _access(_identity(user), plan)
    if not allowed or (role == "assistant" and not assistant) or (role == "owner" and not owner):
        raise OrderFinanceError("当前岗位或组织无权执行该财务操作。")
    if role and not _active(plan):
        raise OrderFinanceError("订单已取消或正在取消，不能继续提交财务操作。")
    return plan


def _recipients(plan, role):
    org_id = plan.merchant_organization_id if role == "merchant_owner" else plan.factory_organization_id
    return list(UserOrganization.objects.filter(
        organization_id=org_id, organization__is_active=True,
        organization__organization_type__is_active=True,
        role__code=role, role__is_active=True, is_active=True,
        user__is_active=True, user__wx_openid__isnull=False,
    ).exclude(user__wx_openid="").values_list("user__wx_openid", flat=True))


def _key(plan, stage):
    return f"sales:order-plan:{plan.pk}:finance:{stage}"


def _task(plan, *, stage, work_type, title, role, user=None, payment=None, sequence_no=None):
    # Replays must never reopen a completed work item or reset peer inboxes.
    existing = WorkItem.objects.filter(uniq_key=_key(plan, stage)).first()
    if existing:
        return existing
    # Preserve an earlier installation's equivalent task even if its key used
    # a different spelling. Never reset an existing read/done state.
    previous = WorkItem.objects.filter(biz_ref=f"sales.OrderPlan:{plan.pk}", type=work_type)
    if stage == "statement":
        existing = previous.order_by("id").first()
    elif payment:
        existing = previous.filter(payload__payment_id=payment.pk).order_by("id").first()
    else:
        existing = previous.filter(payload__sequence_no=sequence_no).order_by("id").first()
    if existing:
        return existing
    payload = {
        "order_plan_id": plan.pk, "plan_code": plan.plan_code,
        "factory_id": plan.factory_organization_id,
        "merchant_id": plan.merchant_organization_id,
        "inbox_group_key": f"order-plan:{plan.pk}",
        "inbox_group_kind": "ORDER_PLAN",
        "inbox_group_label": f"订单计划：{plan.plan_code}",
    }
    if payment:
        payload.update(payment_id=payment.pk, sequence_no=payment.sequence_no)
    elif sequence_no is not None:
        payload["sequence_no"] = sequence_no
    return publish_workitem(
        domain="SALES", type=work_type, biz_id=plan.plan_code,
        biz_ref=f"sales.OrderPlan:{plan.pk}", uniq_key=_key(plan, stage),
        title=title, summary=f"{plan.plan_code} · 独立订单财务",
        link=f"/pages/sales/order_plan_detail/order_plan_detail?order_plan_id={plan.pk}",
        recipients_openids=_recipients(plan, role), need_action=True,
        action_mode="ANYONE_DO", payload=payload,
        actor_openid=(getattr(user, "wx_openid", "") or ""),
        mark_workitem_done_immediately=False,
    )


def _complete(plan, stage, user):
    work = WorkItem.objects.select_for_update().filter(uniq_key=_key(plan, stage), status="OPEN").first()
    if not work:
        candidates = WorkItem.objects.select_for_update().filter(biz_ref=f"sales.OrderPlan:{plan.pk}", status="OPEN")
        if stage == "statement":
            work = candidates.filter(type="ORDER_PLAN_FINANCE_STATEMENT").order_by("id").first()
        else:
            _, sequence, action = stage.split(":")
            work_type = "ORDER_PLAN_PAYMENT_UPLOAD" if action == "upload" else "ORDER_PLAN_PAYMENT_CONFIRM"
            work = candidates.filter(type=work_type, payload__sequence_no=int(sequence)).order_by("id").first()
    if not work:
        return
    _complete_work(work, user)


def _complete_work(work, user):
    now = timezone.now()
    name = user.wx_nickname or user.username
    work.status = "DONE"
    work.done_at = now
    work.done_by_openid = user.wx_openid or ""
    work.last_activity_at = now
    work.payload = dict(work.payload or {}, completed_by_user_id=user.pk, completed_by_name=name, completed_at=now.isoformat())
    work.save(update_fields=["status", "done_at", "done_by_openid", "last_activity_at", "payload", "updated_at"])
    work.user_tasks.filter(to_openid=user.wx_openid).update(
        state="DONE", need_action=False, done_at=now, done_reason="SELF_ACTION",
        display_subtitle_snapshot="由你完成", cursor_time=now, updated_at=now,
    )
    work.user_tasks.exclude(to_openid=user.wx_openid).update(
        state="DONE", need_action=False, done_at=now, done_reason="OTHER_ACTION",
        display_subtitle_snapshot=f"已由{name}完成", cursor_time=now, updated_at=now,
    )


def _payment_task(plan, sequence, user=None):
    return _task(plan, stage=f"payment:{sequence}:upload", work_type="ORDER_PLAN_PAYMENT_UPLOAD",
                 title=f"订单第{sequence}笔付款待处理", role="merchant_owner", user=user, sequence_no=sequence)


def _reconcile(finance, user=None):
    from .order_receivable_task_service import reconcile_order_receivable_tasks
    return reconcile_order_receivable_tasks(finance, user=user)


def _current_context(finance):
    context = pair_context(finance)
    active_finance = context["active_finance"]
    if not active_finance or active_finance.pk != finance.pk:
        code = active_finance.order_plan.plan_code if active_finance else "最新有效订单"
        raise OrderFinanceError(f"本单付款入口已结转，请进入 {code} 办理累计货款。")
    return context


def _complete_payment_action(plan, user, *, payment=None, sequence=None):
    work_type = "ORDER_PLAN_PAYMENT_CONFIRM" if payment else "ORDER_PLAN_PAYMENT_UPLOAD"
    query = WorkItem.objects.select_for_update().filter(
        biz_ref=f"sales.OrderPlan:{plan.pk}", type=work_type, status="OPEN",
    )
    query = query.filter(payload__payment_id=payment.pk) if payment else query.filter(payload__sequence_no=sequence)
    work = query.order_by("pk").first()
    if work:
        _complete_work(work, user)


@transaction.atomic
def initialize_order_finance(plan, user=None):
    """Called within order creation; safe to repeat without resetting tasks."""
    lock_receivable_account(plan)
    plan = OrderPlan.objects.select_for_update().get(pk=plan.pk)
    finance, _ = OrderPlanFinance.objects.get_or_create(order_plan=plan)
    if not _active(plan):
        return finance
    if not finance.statement_submitted_at:
        _task(plan, stage="statement", work_type="ORDER_PLAN_FINANCE_STATEMENT",
              title="订单清单待确认计价", role="factory_sales_assistant", user=user)
    else:
        _reconcile(finance, user)
    return finance


def _image(upload):
    if not upload or not getattr(upload, "size", 0) or upload.size > MAX_IMAGE_BYTES:
        raise OrderFinanceError("请上传不超过10MB的清晰图片。")
    original = PurePath(str(upload.name).replace("\\", "/")).name[:255]
    try:
        upload.seek(0)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            im = Image.open(upload)
            fmt = im.format
            if fmt not in IMAGE_FORMATS or im.width * im.height > 40000000:
                raise OrderFinanceError("只支持JPEG、PNG或WebP图片，且像素总数不得超过4000万。")
            im.verify()
        upload.seek(0)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        if isinstance(exc, OrderFinanceError):
            raise
        raise OrderFinanceError("图片文件无效，请重新选择图片。") from exc
    return original, IMAGE_FORMATS[fmt][0]


def _digest(stream):
    stream.seek(0)
    digest = hashlib.sha256()
    for part in iter(lambda: stream.read(65536), b""):
        digest.update(part)
    stream.seek(0)
    return digest.digest()


def _same_file(field, upload):
    try:
        with field.open("rb") as existing:
            return _digest(existing) == _digest(upload)
    except (OSError, ValueError):
        return False


@financial_write_errors(OrderFinanceError)
@transaction.atomic
def submit_order_finance_statement(*, user, plan_id, receivable_amount, statement_file):
    authorized = _plan(user, plan_id, role="assistant")
    lock_receivable_account(authorized)
    plan = _plan(user, plan_id, role="assistant", lock=True)
    amount = _money(receivable_amount)
    original, suffix = _image(statement_file)
    finance = initialize_order_finance(plan, user)
    if finance.statement_submitted_at:
        if finance.receivable_amount == amount and _same_file(finance.statement_image, statement_file):
            return serialize_order_finance(user=user, plan=plan)
        raise OrderFinanceError("订单清单已提交，不能覆盖应收金额或凭证。")
    saved_name = None
    try:
        finance.statement_image.save(f"{uuid4().hex}{suffix}", statement_file, save=False)
        saved_name = finance.statement_image.name
        finance.statement_original_name = original
        finance.receivable_amount = amount
        finance.statement_submitted_by = user
        finance.statement_submitted_at = timezone.now()
        finance.save()
        _complete(plan, "statement", user)
        _reconcile(finance, user)
        return serialize_order_finance(user=user, plan=plan)
    except Exception:
        if saved_name:
            finance.statement_image.storage.delete(saved_name)
        raise


@financial_write_errors(OrderFinanceError)
@transaction.atomic
def submit_order_payment(*, user, plan_id, client_request_id, voucher_file):
    authorized = _plan(user, plan_id, role="owner")
    lock_receivable_account(authorized)
    plan = _plan(user, plan_id, role="owner", lock=True)
    request_id = str(client_request_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", request_id):
        raise OrderFinanceError("付款请求标识无效，请刷新页面后重试。")
    original, suffix = _image(voucher_file)
    finance = OrderPlanFinance.objects.select_for_update().filter(order_plan=plan).first()
    if not finance or not finance.statement_submitted_at:
        raise OrderFinanceError("请等待厂家销售助理提交订单清单及应收总额。")
    # Older versions enforced request IDs per invoice only. Prefer a matching
    # original-invoice record before enforcing the new pair-wide retry scope.
    existing = finance.payments.filter(client_request_id=request_id).first()
    if existing is None:
        existing = OrderPlanPayment.objects.filter(
            finance__order_plan__factory_organization_id=plan.factory_organization_id,
            finance__order_plan__merchant_organization_id=plan.merchant_organization_id,
            client_request_id=request_id,
        ).order_by("pk").first()
    if existing:
        if existing.uploaded_by_id == user.pk and _same_file(existing.voucher_image, voucher_file):
            return serialize_order_finance(user=user, plan=plan)
        raise OrderFinanceError("该付款请求标识已使用，不能替换原凭证。")
    _reconcile(finance, user)
    context = _current_context(finance)
    payments = list(finance.payments.all())
    if context["pending_payments"]:
        raise OrderFinanceError("已有付款凭证等待确认，请勿重复上传。")
    if context["outstanding_amount"] <= 0:
        raise OrderFinanceError("累计款项已收齐，无需继续提交付款凭证。")
    sequence = max((p.sequence_no for p in payments), default=0) + 1
    payment = OrderPlanPayment(finance=finance, sequence_no=sequence, client_request_id=request_id,
                               voucher_original_name=original, uploaded_by=user)
    saved_name = None
    try:
        payment.voucher_image.save(f"{uuid4().hex}{suffix}", voucher_file, save=False)
        saved_name = payment.voucher_image.name
        payment.save()
        _complete_payment_action(plan, user, sequence=sequence)
        _reconcile(finance, user)
        return serialize_order_finance(user=user, plan=plan)
    except Exception:
        if saved_name:
            payment.voucher_image.storage.delete(saved_name)
        raise


@financial_write_errors(OrderFinanceError)
@transaction.atomic
def confirm_order_payment(*, user, plan_id, payment_id, confirmed_amount):
    authorized = _plan(user, plan_id, role="assistant")
    lock_receivable_account(authorized)
    plan = _plan(user, plan_id, role="assistant", lock=True)
    amount = _money(confirmed_amount)
    payment = OrderPlanPayment.objects.select_for_update().select_related("finance__order_plan").filter(
        pk=payment_id,
        finance__order_plan__factory_organization_id=plan.factory_organization_id,
        finance__order_plan__merchant_organization_id=plan.merchant_organization_id,
    ).first()
    if not payment:
        raise OrderFinanceError("付款记录不存在或不属于当前厂家与商家的往来账。")
    if payment.status == OrderPlanPayment.Status.CONFIRMED:
        if payment.confirmed_amount == amount:
            return serialize_order_finance(user=user, plan=plan)
        raise OrderFinanceError("该笔收款已确认，不能重复修改到账金额。")
    finance = OrderPlanFinance.objects.select_related("order_plan").filter(order_plan=plan).first()
    if not finance or not finance.statement_submitted_at:
        raise OrderFinanceError("请等待厂家销售助理提交订单清单及应收总额。")
    _reconcile(finance, user)
    context = _current_context(finance)
    if payment.pk not in {row.pk for row in context["pending_payments"]}:
        raise OrderFinanceError("凭证所属订单已取消或正在取消，不能确认该笔收款。")
    _complete_payment_action(plan, user, payment=payment)
    payment.confirmed_amount = amount
    payment.confirmed_by = user
    payment.confirmed_at = timezone.now()
    payment.status = OrderPlanPayment.Status.CONFIRMED
    payment.save(update_fields=["confirmed_amount", "confirmed_by", "confirmed_at", "status", "updated_at"])
    allocate_payment_fifo(payment)
    _reconcile(finance, user)
    return serialize_order_finance(user=user, plan=plan)


def _date(value):
    return value.isoformat() if value else None


def serialize_order_finance(*, user, plan):
    identity = _identity(user)
    allowed, assistant, owner = _access(identity, plan)
    if not allowed:
        return None
    finance = OrderPlanFinance.objects.select_related("order_plan").filter(order_plan=plan).first()
    receivable = finance.receivable_amount if finance else None
    zero = Decimal("0.00")
    totals = invoice_totals(finance) if finance else {
        "received_amount": zero, "allocated_received_amount": zero,
        "unapplied_amount": zero, "outstanding_amount": zero, "overpaid_amount": zero,
    }
    submitted = bool(finance and finance.statement_submitted_at)
    active = _active(plan)
    context = pair_context(finance) if finance else None
    current = context["active_finance"] if context else None
    is_current = bool(current and current.pk == finance.pk)
    pair_access = assistant or owner
    finance_ids = [row["finance"].pk for row in context["rows"]] if context else []
    if finance:
        if is_current and pair_access:
            payment_scope = Q(finance_id__in=finance_ids) | Q(
                allocations__finance_id__in=finance_ids, allocations__released_at__isnull=True,
            )
        elif pair_access:
            payment_scope = Q(finance=finance) | Q(allocations__finance=finance, allocations__released_at__isnull=True)
        else:
            # Sales managers keep their original creator-only finance scope.
            payment_scope = Q(finance=finance)
        payments = list(OrderPlanPayment.objects.filter(payment_scope).distinct()
                        .select_related("finance__order_plan")
                        .prefetch_related("allocations__finance__order_plan")
                        .order_by("uploaded_at", "pk"))
    else:
        payments = []
    if not active and finance:
        # Cancelled-order pages show the original cash audit; excluded balances
        # must not be presented as current payable debt or available pair credit.
        original_received = sum((p.confirmed_amount or zero for p in payments
                                 if p.finance_id == finance.pk and p.status == OrderPlanPayment.Status.CONFIRMED), zero)
        totals = dict(totals, received_amount=original_received,
                      outstanding_amount=max((receivable or zero) - original_received, zero),
                      overpaid_amount=max(original_received - (receivable or zero), zero))
    pending_ids = {p.pk for p in context["pending_payments"]} if context else set()
    payment_rows = []
    for payment in payments:
        source = payment.finance.order_plan
        allocations = []
        for allocation in payment.allocations.all():
            target = allocation.finance.order_plan
            if allocation.released_at is None and _access(identity, target)[0]:
                allocations.append({
                    "order_plan_id": target.pk, "plan_code": target.plan_code,
                    "amount": format(allocation.amount, ".2f"),
                })
        payment_rows.append({
            "id": payment.pk, "sequence_no": payment.sequence_no,
            "status": payment.status, "status_name": payment.get_status_display(),
            "source_order_plan_id": source.pk, "source_plan_code": source.plan_code,
            "voucher_url": f"/pldp/api/sales/order-plans/{source.pk}/finance/payments/{payment.pk}/file/",
            "voucher_original_name": payment.voucher_original_name,
            "uploaded_at": _date(payment.uploaded_at),
            "confirmed_amount": format(payment.confirmed_amount, ".2f") if payment.confirmed_amount is not None else None,
            "confirmed_at": _date(payment.confirmed_at), "allocations": allocations,
            "can_confirm": bool(active and assistant and is_current and payment.pk in pending_ids),
        })
    rolling = {"enabled": False}
    if context and pair_access:
        rolling = {
            "enabled": True, "is_current": is_current,
            "active_order_plan_id": current.order_plan_id if current else None,
            "active_plan_code": current.order_plan.plan_code if current else "",
            "receivable_amount": format(context["receivable_amount"], ".2f"),
            "received_amount": format(context["received_amount"], ".2f"),
            "outstanding_amount": format(context["outstanding_amount"], ".2f"),
            "balance_amount": format(context["balance_amount"], ".2f"),
            "return_credit_amount": format(context["return_credit_amount"], ".2f"),
            "overpaid_amount": format(context["overpaid_amount"], ".2f"),
            "carry_forward_amount": format(context["carry_forward_amount"], ".2f"),
            "orders": [{
                "order_plan_id": row["finance"].order_plan_id,
                "plan_code": row["finance"].order_plan.plan_code,
                **{key: format(row[key] or zero, ".2f") for key in (
                    "receivable_amount", "received_amount", "allocated_received_amount",
                    "outstanding_amount", "overpaid_amount",
                )},
                "is_current": row["finance"].pk == current.pk,
            } for row in context["rows"]],
        }
    own_sequences = [p.sequence_no for p in payments if finance and p.finance_id == finance.pk]
    return {
        "can_view": True,
        "receivable_amount": format(receivable, ".2f") if receivable is not None else None,
        "received_amount": format(totals["received_amount"], ".2f"),
        "allocated_received_amount": format(totals["allocated_received_amount"], ".2f"),
        "unapplied_amount": format(totals["unapplied_amount"], ".2f"),
        "outstanding_amount": format(totals["outstanding_amount"] or zero, ".2f"),
        "return_credit_amount": format(totals.get("return_credit_amount", zero), ".2f"),
        "overpaid_amount": format(totals["overpaid_amount"], ".2f"),
        "rolling_receivables": rolling,
        "excluded_from_rolling": not active,
        "statement_submitted_at": _date(finance.statement_submitted_at) if finance else None,
        "statement_original_name": finance.statement_original_name if finance else "",
        "statement_url": f"/pldp/api/sales/order-plans/{plan.pk}/finance/statement/file/" if finance and finance.statement_image else "",
        "can_submit_statement": False,
        "can_price_order": bool(active and assistant and not submitted),
        "priced_lines": finance.priced_lines if finance else [],
        **pricing_amounts(finance.priced_lines if finance else []),
        "pricing_version": finance.pricing_version if finance else "",
        "can_open_factory_payment": bool(owner),
        "can_upload_payment": False,
        "next_sequence_no": max(own_sequences, default=0) + 1,
        "payments": payment_rows,
    }


def get_order_finance(*, user, plan_id):
    return serialize_order_finance(user=user, plan=_plan(user, plan_id))


def get_order_finance_file(*, user, plan_id, payment_id=None):
    plan = _plan(user, plan_id)
    try:
        if payment_id is None:
            finance = OrderPlanFinance.objects.get(order_plan=plan)
            field = finance.statement_image
        else:
            payment = OrderPlanPayment.objects.get(pk=payment_id, finance__order_plan=plan)
            field = payment.voucher_image
        if not field:
            raise OrderFinanceError("凭证尚未上传。")
        stream = field.open("rb")
    except (ObjectDoesNotExist, OSError, ValueError) as exc:
        raise OrderFinanceError("凭证不存在或当前账号无权查看。") from exc
    suffix = PurePath(field.name).suffix.lower()
    mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}.get(suffix, "application/octet-stream")
    # Never expose the private storage path or an original user-supplied name.
    return stream, mime, f"order-{plan.pk}-{'payment-' + str(payment_id) if payment_id else 'statement'}{suffix}"


@transaction.atomic
def cancel_order_finance_tasks(plan, user=None):
    lock_receivable_account(plan)
    now = timezone.now()
    items = WorkItem.objects.select_for_update().filter(
        biz_ref=f"sales.OrderPlan:{plan.pk}", type__in=FINANCE_TYPES, status="OPEN",
    )
    for item in items:
        item.status = "CANCELLED"
        item.done_at = now
        item.done_by_openid = getattr(user, "wx_openid", "") or ""
        item.last_activity_at = now
        item.save(update_fields=["status", "done_at", "done_by_openid", "last_activity_at", "updated_at"])
        item.user_tasks.update(state="DONE", need_action=False, done_at=now, done_reason="OTHER_ACTION",
                               display_subtitle_snapshot="订单已取消", cursor_time=now, updated_at=now)
    finance = OrderPlanFinance.objects.select_related("order_plan").filter(order_plan=plan).first()
    if finance:
        _reconcile(finance, user)
