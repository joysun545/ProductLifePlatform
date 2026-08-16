import mimetypes
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from account.models import UserOrganization
from sales.models import OrderPlan, OrderPlanFinance, OrderPlanPayment
from workflow.models import WorkItem
from workflow.services.workflow_service import publish_workitem


class OrderFinanceError(Exception):
    pass


FINANCE_DETAIL_ROLES = {
    "factory_sales",
    "factory_sales_assistant",
    "merchant_owner",
}
MAX_IMAGE_SIZE = 10 * 1024 * 1024
MONEY_LIMIT = Decimal("999999999999.99")


def display_name(user):
    return user.wx_nickname or user.get_full_name() or user.username


def role_openids(organization, role_codes):
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
    identity = (
        UserOrganization.objects.select_related(
            "organization",
            "organization__organization_type",
            "role",
        )
        .filter(
            user=user,
            is_active=True,
            user__is_active=True,
            organization__is_active=True,
            role__is_active=True,
        )
        .first()
    )
    if not identity or identity.role.code not in FINANCE_DETAIL_ROLES:
        raise OrderFinanceError("当前岗位无权查看订单资金信息。")
    return identity


def _authorized_plan(user, plan_id, *, lock=False):
    identity = _identity(user)
    query = OrderPlan.objects.select_related(
        "factory_organization",
        "merchant_organization",
        "created_by",
        "brand",
    )
    if lock:
        query = query.select_for_update()
    plan = query.filter(pk=plan_id).first()
    if not plan:
        raise OrderFinanceError("订单计划不存在。")

    role_code = identity.role.code
    allowed = False
    if role_code == "factory_sales_assistant":
        allowed = plan.factory_organization_id == identity.organization_id
    elif role_code == "factory_sales":
        allowed = (
            plan.factory_organization_id == identity.organization_id
            and plan.created_by_id == user.pk
        )
    elif role_code == "merchant_owner":
        allowed = plan.merchant_organization_id == identity.organization_id
    if not allowed:
        raise OrderFinanceError("该订单资金信息不属于当前账号。")
    return plan, identity


def _parse_amount(value, label):
    raw = str(value or "").strip()
    if not raw:
        raise OrderFinanceError(f"请填写{label}。")
    try:
        amount = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise OrderFinanceError(f"{label}格式不正确。") from exc
    if not amount.is_finite() or amount <= 0:
        raise OrderFinanceError(f"{label}必须大于0。")
    if amount.as_tuple().exponent < -2:
        raise OrderFinanceError(f"{label}最多保留两位小数。")
    if amount > MONEY_LIMIT:
        raise OrderFinanceError(f"{label}超出允许范围。")
    return amount.quantize(Decimal("0.01"))


def _validate_image(uploaded_file, label):
    if not uploaded_file:
        raise OrderFinanceError(f"请上传{label}。")
    if uploaded_file.size <= 0 or uploaded_file.size > MAX_IMAGE_SIZE:
        raise OrderFinanceError(f"{label}不能超过10MB。")
    header = uploaded_file.read(12)
    uploaded_file.seek(0)
    if header.startswith(b"\xff\xd8\xff"):
        suffix = ".jpg"
    elif header.startswith(b"\x89PNG\r\n\x1a\n"):
        suffix = ".png"
    elif header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        suffix = ".webp"
    else:
        raise OrderFinanceError(f"{label}只支持JPG、PNG或WEBP格式。")
    return suffix, Path(uploaded_file.name).name[:255]


def _complete_anyone(workitem, user, *, actor_title, peer_title):
    if workitem.status == "DONE":
        raise OrderFinanceError("该协作任务已由其他成员完成，请刷新页面。")
    task = (
        workitem.user_tasks.select_for_update()
        .filter(to_openid=user.wx_openid, need_action=True)
        .exclude(state="DONE")
        .first()
    )
    if not task:
        raise OrderFinanceError("当前账号不是该任务的处理人。")

    now = timezone.now()
    payload = dict(workitem.payload or {})
    payload.update(
        {
            "completed_by_user_id": user.pk,
            "completed_by_name": display_name(user),
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
        raise OrderFinanceError("该协作任务已由其他成员完成，请刷新页面。")

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
            f"{display_name(user)}已处理，同组织无需重复操作"
        ),
        cursor_time=now,
        updated_at=now,
    )


def _finance_link(plan):
    return (
        "/pages/sales/order_finance/order_finance"
        f"?order_plan_id={plan.pk}"
    )


def _statement_workitem(plan):
    return WorkItem.objects.filter(
        type="ORDER_PLAN_FINANCE_STATEMENT",
        uniq_key=f"sales:order-plan:{plan.pk}:finance-statement",
    ).first()


def _payment_upload_key(plan, sequence_no):
    return f"sales:order-plan:{plan.pk}:payment-upload:{sequence_no}"


def _payment_confirm_key(plan, payment):
    return f"sales:order-plan:{plan.pk}:payment:{payment.pk}:confirm"


def _publish_statement_task(plan, actor_openid=""):
    assistant_openids = role_openids(
        plan.factory_organization,
        {"factory_sales_assistant"},
    )
    if not assistant_openids:
        return None
    return publish_workitem(
        domain="SALES",
        type="ORDER_PLAN_FINANCE_STATEMENT",
        biz_id=plan.plan_code,
        biz_ref=f"sales.OrderPlan:{plan.pk}",
        uniq_key=f"sales:order-plan:{plan.pk}:finance-statement",
        title="订单清单待制作",
        summary=f"{plan.merchant_organization.name}｜共{plan.total_quantity}台",
        link=_finance_link(plan),
        recipients_openids=assistant_openids,
        need_action=True,
        action_mode="ANYONE_DO",
        payload={"order_plan_id": plan.pk, "plan_code": plan.plan_code},
        actor_openid=actor_openid,
        mark_workitem_done_immediately=False,
    )


def _publish_payment_upload_task(finance, sequence_no, actor_openid=""):
    plan = finance.order_plan
    merchant_openids = role_openids(plan.merchant_organization, {"merchant_owner"})
    if not merchant_openids:
        return None
    return publish_workitem(
        domain="SALES",
        type="ORDER_PLAN_PAYMENT_UPLOAD",
        biz_id=plan.plan_code,
        biz_ref=f"sales.OrderPlan:{plan.pk}",
        uniq_key=_payment_upload_key(plan, sequence_no),
        title=f"订单第{sequence_no}笔付款待处理",
        summary=f"应收总额 ¥{finance.receivable_amount}",
        link=_finance_link(plan),
        recipients_openids=merchant_openids,
        need_action=True,
        action_mode="ANYONE_DO",
        payload={
            "order_plan_id": plan.pk,
            "plan_code": plan.plan_code,
            "payment_sequence": sequence_no,
        },
        actor_openid=actor_openid,
        mark_workitem_done_immediately=False,
    )


@transaction.atomic
def initialize_order_finance(*, plan, actor):
    finance, created = OrderPlanFinance.objects.get_or_create(order_plan=plan)
    _publish_statement_task(plan, actor.wx_openid or "")
    return finance, created


def _payment_rows(finance):
    prefetched = getattr(finance, "_prefetched_objects_cache", {}).get("payments")
    if prefetched is not None:
        return list(prefetched)
    return list(
        finance.payments.select_related(
            "uploaded_by",
            "confirmed_by",
        ).all()
    )


def _totals(finance, payments=None):
    payments = payments if payments is not None else _payment_rows(finance)
    confirmed_total = sum(
        (
            payment.confirmed_amount
            for payment in payments
            if payment.status == OrderPlanPayment.Status.CONFIRMED
            and payment.confirmed_amount is not None
        ),
        Decimal("0.00"),
    )
    receivable = finance.receivable_amount
    outstanding = None if receivable is None else receivable - confirmed_total
    return confirmed_total, outstanding


def _finance_state(finance, payments, confirmed_total, outstanding):
    if not finance.statement_submitted_at:
        return "WAITING_STATEMENT", "待制作订单清单"
    if any(payment.status == OrderPlanPayment.Status.PENDING for payment in payments):
        return "PAYMENT_CONFIRMING", "付款待确认"
    if confirmed_total == 0:
        return "WAITING_PAYMENT", "待商家付款"
    if outstanding > 0:
        return "PARTIALLY_RECEIVED", "部分收款"
    if outstanding == 0:
        return "FULLY_RECEIVED", "已全部收款"
    return "OVER_RECEIVED", "收款超过应收"


def _money(value):
    return f"{value:.2f}" if value is not None else None


def _viewer_can_confirm_payment(user, plan, payment):
    if payment.status != OrderPlanPayment.Status.PENDING:
        return False
    workitem = WorkItem.objects.filter(
        type="ORDER_PLAN_PAYMENT_CONFIRM",
        uniq_key=_payment_confirm_key(plan, payment),
        status="OPEN",
        user_tasks__to_openid=user.wx_openid,
        user_tasks__need_action=True,
    ).exclude(user_tasks__state="DONE").first()
    return bool(workitem)


def _serialize(finance, user, role_code, *, include_payments):
    plan = finance.order_plan
    payments = _payment_rows(finance)
    confirmed_total, outstanding = _totals(finance, payments)
    state, state_name = _finance_state(
        finance,
        payments,
        confirmed_total,
        outstanding,
    )
    pending_exists = any(
        payment.status == OrderPlanPayment.Status.PENDING for payment in payments
    )
    data = {
        "state": state,
        "state_name": state_name,
        "statement_submitted": bool(finance.statement_submitted_at),
        "receivable_amount": _money(finance.receivable_amount),
        "confirmed_received_amount": _money(confirmed_total),
        "outstanding_amount": _money(outstanding),
        "payment_count": len(payments),
        "pending_payment_count": sum(
            1 for payment in payments
            if payment.status == OrderPlanPayment.Status.PENDING
        ),
        "detail_link": _finance_link(plan),
    }
    if not include_payments:
        return data

    data.update(
        {
            "order_plan": {
                "id": plan.pk,
                "plan_code": plan.plan_code,
                "merchant_name": plan.merchant_organization.name,
                "factory_name": plan.factory_organization.name,
                "sales_manager_name": display_name(plan.created_by),
                "total_quantity": plan.total_quantity,
            },
            "viewer": {"role": role_code},
            "statement": {
                "submitted": bool(finance.statement_submitted_at),
                "original_name": finance.statement_original_name,
                "submitted_by_name": (
                    display_name(finance.statement_submitted_by)
                    if finance.statement_submitted_by else ""
                ),
                "submitted_at": (
                    finance.statement_submitted_at.isoformat()
                    if finance.statement_submitted_at else None
                ),
                "preview_path": (
                    f"/sales/order-plans/{plan.pk}/finance/statement-image/"
                    if finance.statement_image else ""
                ),
            },
            "actions": {
                "can_submit_statement": (
                    role_code == "factory_sales_assistant"
                    and not finance.statement_submitted_at
                    and not payments
                ),
                "can_upload_payment": (
                    role_code == "merchant_owner"
                    and bool(finance.statement_submitted_at)
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
                        f"/sales/order-plans/{plan.pk}/finance/payments/"
                        f"{payment.pk}/voucher-image/"
                    ),
                    "uploaded_by_name": display_name(payment.uploaded_by),
                    "uploaded_at": payment.uploaded_at.isoformat(),
                    "confirmed_amount": _money(payment.confirmed_amount),
                    "confirmed_by_name": (
                        display_name(payment.confirmed_by)
                        if payment.confirmed_by else ""
                    ),
                    "confirmed_at": (
                        payment.confirmed_at.isoformat()
                        if payment.confirmed_at else None
                    ),
                    "can_confirm": (
                        role_code == "factory_sales_assistant"
                        and _viewer_can_confirm_payment(user, plan, payment)
                    ),
                }
                for payment in payments
            ],
        }
    )
    return data


def serialize_finance_summary(plan, user, role_code):
    if role_code not in FINANCE_DETAIL_ROLES:
        return None
    if role_code == "factory_sales" and plan.created_by_id != user.pk:
        return None
    try:
        finance = plan.finance
    except OrderPlanFinance.DoesNotExist:
        return {
            "state": "WAITING_STATEMENT",
            "state_name": "待制作订单清单",
            "statement_submitted": False,
            "receivable_amount": None,
            "confirmed_received_amount": "0.00",
            "outstanding_amount": None,
            "payment_count": 0,
            "pending_payment_count": 0,
            "detail_link": _finance_link(plan),
        }
    return _serialize(finance, user, role_code, include_payments=False)


def get_order_finance_detail(*, user, plan_id):
    plan, identity = _authorized_plan(user, plan_id)
    finance, _created = OrderPlanFinance.objects.get_or_create(order_plan=plan)
    finance.order_plan = plan
    return _serialize(finance, user, identity.role.code, include_payments=True)


@transaction.atomic
def submit_order_statement(*, user, plan_id, receivable_amount, image):
    plan, identity = _authorized_plan(user, plan_id, lock=True)
    if identity.role.code != "factory_sales_assistant":
        raise OrderFinanceError("只有厂家销售助理可以提交订单清单。")
    finance, _created = OrderPlanFinance.objects.select_for_update().get_or_create(
        order_plan=plan
    )
    if finance.statement_submitted_at:
        raise OrderFinanceError("该订单清单已经提交。")
    if finance.payments.exists():
        raise OrderFinanceError("该订单已有付款记录，不能重新提交清单。")

    amount = _parse_amount(receivable_amount, "应收总额")
    suffix, original_name = _validate_image(image, "订单清单截图")
    workitem = WorkItem.objects.select_for_update().filter(
        type="ORDER_PLAN_FINANCE_STATEMENT",
        uniq_key=f"sales:order-plan:{plan.pk}:finance-statement",
    ).first()
    if not workitem:
        _publish_statement_task(plan, user.wx_openid or "")
        workitem = WorkItem.objects.select_for_update().filter(
            uniq_key=f"sales:order-plan:{plan.pk}:finance-statement"
        ).first()
    if not workitem:
        raise OrderFinanceError("未找到订单清单任务。")

    _complete_anyone(
        workitem,
        user,
        actor_title="订单清单已提交",
        peer_title=f"{display_name(user)}已提交订单清单",
    )
    finance.statement_image.save(
        f"statement-{uuid.uuid4().hex}{suffix}",
        image,
        save=False,
    )
    now = timezone.now()
    finance.statement_original_name = original_name
    finance.receivable_amount = amount
    finance.statement_submitted_by = user
    finance.statement_submitted_at = now
    finance.save()
    _publish_payment_upload_task(finance, 1, user.wx_openid or "")
    return _serialize(finance, user, identity.role.code, include_payments=True)


@transaction.atomic
def upload_payment_voucher(*, user, plan_id, client_request_id, image):
    plan, identity = _authorized_plan(user, plan_id, lock=True)
    if identity.role.code != "merchant_owner":
        raise OrderFinanceError("只有商家负责人账号可以上传付款凭证。")
    finance = OrderPlanFinance.objects.select_for_update().filter(
        order_plan=plan
    ).first()
    if not finance or not finance.statement_submitted_at:
        raise OrderFinanceError("厂家销售助理尚未提交订单清单。")

    client_request_id = str(client_request_id or "").strip()
    if not client_request_id or len(client_request_id) > 64:
        raise OrderFinanceError("付款凭证请求标识无效，请重新进入页面。")
    existing = finance.payments.filter(
        client_request_id=client_request_id
    ).select_related("uploaded_by", "confirmed_by").first()
    if existing:
        return _serialize(finance, user, identity.role.code, include_payments=True), False
    if finance.payments.filter(status=OrderPlanPayment.Status.PENDING).exists():
        raise OrderFinanceError("上一笔付款凭证尚未确认，请等待厂家处理。")

    payments = _payment_rows(finance)
    _confirmed_total, outstanding = _totals(finance, payments)
    if outstanding is None or outstanding <= 0:
        raise OrderFinanceError("该订单已经完成全部收款。")
    suffix, original_name = _validate_image(image, "付款凭证")
    sequence_no = (
        finance.payments.aggregate(value=Max("sequence_no"))["value"] or 0
    ) + 1
    workitem = WorkItem.objects.select_for_update().filter(
        type="ORDER_PLAN_PAYMENT_UPLOAD",
        uniq_key=_payment_upload_key(plan, sequence_no),
    ).first()
    if not workitem:
        _publish_payment_upload_task(finance, sequence_no, user.wx_openid or "")
        workitem = WorkItem.objects.select_for_update().filter(
            uniq_key=_payment_upload_key(plan, sequence_no)
        ).first()
    if not workitem:
        raise OrderFinanceError("未找到商家付款任务。")
    _complete_anyone(
        workitem,
        user,
        actor_title=f"第{sequence_no}笔付款凭证已上传",
        peer_title=f"{display_name(user)}已上传第{sequence_no}笔付款凭证",
    )

    payment = OrderPlanPayment(
        finance=finance,
        sequence_no=sequence_no,
        client_request_id=client_request_id,
        voucher_original_name=original_name,
        uploaded_by=user,
    )
    payment.voucher_image.save(
        f"payment-{sequence_no}-{uuid.uuid4().hex}{suffix}",
        image,
        save=False,
    )
    payment.save()

    payload = {
        "order_plan_id": plan.pk,
        "plan_code": plan.plan_code,
        "payment_id": payment.pk,
        "payment_sequence": sequence_no,
    }
    if plan.created_by.wx_openid:
        publish_workitem(
            domain="SALES",
            type="ORDER_PLAN_PAYMENT_UPLOADED_NOTICE",
            biz_id=plan.plan_code,
            biz_ref=f"sales.OrderPlan:{plan.pk}",
            uniq_key=(
                f"sales:order-plan:{plan.pk}:payment:{payment.pk}:sales-notice"
            ),
            title=f"商家已上传第{sequence_no}笔付款凭证",
            summary=plan.merchant_organization.name,
            link=_finance_link(plan),
            recipients_openids=(plan.created_by.wx_openid,),
            need_action=False,
            action_mode="EACH_ACK",
            payload=payload,
            actor_openid=user.wx_openid or "",
            mark_workitem_done_immediately=True,
        )
    assistant_openids = role_openids(
        plan.factory_organization,
        {"factory_sales_assistant"},
    )
    if assistant_openids:
        publish_workitem(
            domain="SALES",
            type="ORDER_PLAN_PAYMENT_CONFIRM",
            biz_id=plan.plan_code,
            biz_ref=f"sales.OrderPlan:{plan.pk}",
            uniq_key=_payment_confirm_key(plan, payment),
            title=f"第{sequence_no}笔收款待确认",
            summary=f"{plan.merchant_organization.name}｜请依据凭证填写到账金额",
            link=_finance_link(plan),
            recipients_openids=assistant_openids,
            need_action=True,
            action_mode="ANYONE_DO",
            payload=payload,
            actor_openid=user.wx_openid or "",
            mark_workitem_done_immediately=False,
        )
    return _serialize(finance, user, identity.role.code, include_payments=True), True


@transaction.atomic
def confirm_payment_received(*, user, plan_id, payment_id, amount):
    plan, identity = _authorized_plan(user, plan_id, lock=True)
    if identity.role.code != "factory_sales_assistant":
        raise OrderFinanceError("只有厂家销售助理可以确认收款。")
    finance = OrderPlanFinance.objects.select_for_update().filter(
        order_plan=plan
    ).first()
    if not finance:
        raise OrderFinanceError("订单资金主账不存在。")
    payment = (
        OrderPlanPayment.objects.select_for_update()
        .select_related("uploaded_by", "confirmed_by")
        .filter(pk=payment_id, finance=finance)
        .first()
    )
    if not payment:
        raise OrderFinanceError("付款记录不存在。")
    if payment.status == OrderPlanPayment.Status.CONFIRMED:
        return _serialize(finance, user, identity.role.code, include_payments=True), False

    confirmed_amount = _parse_amount(amount, "实际到账金额")
    workitem = WorkItem.objects.select_for_update().filter(
        type="ORDER_PLAN_PAYMENT_CONFIRM",
        uniq_key=_payment_confirm_key(plan, payment),
    ).first()
    if not workitem:
        raise OrderFinanceError("未找到收款确认任务。")
    _complete_anyone(
        workitem,
        user,
        actor_title=f"第{payment.sequence_no}笔收款已确认",
        peer_title=(
            f"{display_name(user)}已确认第{payment.sequence_no}笔收款"
        ),
    )

    now = timezone.now()
    payment.confirmed_amount = confirmed_amount
    payment.confirmed_by = user
    payment.confirmed_at = now
    payment.status = OrderPlanPayment.Status.CONFIRMED
    payment.save(
        update_fields=(
            "confirmed_amount",
            "confirmed_by",
            "confirmed_at",
            "status",
            "updated_at",
        )
    )
    confirmed_total, outstanding = _totals(finance)
    if outstanding > 0:
        _publish_payment_upload_task(
            finance,
            payment.sequence_no + 1,
            user.wx_openid or "",
        )
    return _serialize(finance, user, identity.role.code, include_payments=True), True


def get_statement_file(*, user, plan_id):
    plan, _identity_value = _authorized_plan(user, plan_id)
    finance = OrderPlanFinance.objects.filter(order_plan=plan).first()
    if not finance or not finance.statement_image:
        raise OrderFinanceError("订单清单截图不存在。")
    content_type = mimetypes.guess_type(finance.statement_image.name)[0] or "image/jpeg"
    return finance.statement_image, finance.statement_original_name, content_type


def get_payment_voucher_file(*, user, plan_id, payment_id):
    plan, _identity_value = _authorized_plan(user, plan_id)
    payment = OrderPlanPayment.objects.filter(
        pk=payment_id,
        finance__order_plan=plan,
    ).first()
    if not payment:
        raise OrderFinanceError("付款凭证不存在。")
    content_type = mimetypes.guess_type(payment.voucher_image.name)[0] or "image/jpeg"
    return payment.voucher_image, payment.voucher_original_name, content_type
