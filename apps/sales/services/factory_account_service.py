"""Order-independent merchant payments and confirmed factory return credits."""
import re
from decimal import Decimal
from uuid import uuid4

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from account.models import OrganizationRelation, UserOrganization
from sales.models import FactoryAccountFunding, OrderPlan, OrderPlanFinance, OrderPlanPayment
from workflow.models import WorkItem
from workflow.services.workflow_service import publish_workitem

from .order_finance_service import (
    OrderFinanceError, _complete_work, _identity, _image, _money, _reconcile, _same_file,
)
from .pricing_totals import pricing_amounts
from .order_receivable_service import (
    factory_account_context, financial_write_errors, lock_factory_account, synchronize_receivable_account,
)


def _relations(identity, *, allow_price_staff=False):
    from .order_pricing_service import can_maintain_prices
    if not identity or not (
        identity.role.code in {"merchant_owner", "factory_sales_assistant"}
        or (allow_price_staff and can_maintain_prices(identity))
    ):
        raise OrderFinanceError("当前岗位无权办理厂家付款。")
    scope = ({"target_organization_id": identity.organization_id}
             if identity.role.code == "merchant_owner" else {"source_organization_id": identity.organization_id})
    return OrganizationRelation.objects.filter(
        **scope, relation_type="DISTRIBUTION", is_active=True,
        source_organization__organization_type__code="OWNER",
        target_organization__organization_type__code="MERCHANT",
        source_organization__is_active=True, target_organization__is_active=True,
        source_organization__factory_root_id=identity.organization.factory_root_id,
        target_organization__factory_root_id=identity.organization.factory_root_id,
    ).select_related("source_organization", "target_organization")


def _payment(user, payment_id):
    relations = _relations(_identity(user))
    funding = FactoryAccountFunding.objects.filter(pk=payment_id, return_credit__isnull=True).first()
    if not funding or not relations.filter(source_organization_id=funding.account.factory_organization_id,
                                           target_organization_id=funding.account.merchant_organization_id).exists():
        raise OrderFinanceError("付款记录不存在或无权查看。")
    return funding


def accounts(user, *, allow_price_staff=False):
    identity = _identity(user)
    result = []
    for relation in _relations(identity, allow_price_staff=allow_price_staff).order_by("id"):
        context = factory_account_context(factory_organization_id=relation.source_organization_id,
                                          merchant_organization_id=relation.target_organization_id)
        account = context["account"]
        plans = OrderPlan.objects.filter(factory_organization_id=relation.source_organization_id,
                                         merchant_organization_id=relation.target_organization_id)
        rows = {row["finance"].order_plan_id: row for row in context["rows"]}
        orders = []
        device_total = accessory_total = unclassified_total = Decimal("0.00")
        for plan in plans.select_related("finance").order_by("-submitted_at", "-id"):
            finance = getattr(plan, "finance", None)
            row = rows.get(plan.pk)
            amounts = pricing_amounts(finance.priced_lines if finance else [])
            if row:
                if amounts["device_subtotal"] is None:
                    unclassified_total += row["receivable_amount"]
                else:
                    device_total += Decimal(amounts["device_subtotal"])
                    accessory_total += Decimal(amounts["accessory_subtotal"])
            orders.append({
                **amounts,
                "id": plan.pk, "code": plan.plan_code, "status": plan.get_status_display(),
                "receivable": format(finance.receivable_amount, ".2f") if finance and finance.receivable_amount is not None else None,
                "received": format(row["allocated_received_amount"], ".2f") if row else None,
                "credit": format(row["return_credit_amount"], ".2f") if row else None,
                "outstanding": format(row["outstanding_amount"], ".2f") if row else None,
                "can_view_quote": identity.role.code != "merchant_owner" and bool(finance),
                "can_price": identity.role.code == "factory_sales_assistant" and finance
                             and not finance.statement_submitted_at and plan.status != "CANCELLED",
            })
        result.append({
            "factory_id": relation.source_organization_id,
            "factory_name": relation.source_organization.name,
            "merchant_name": relation.target_organization.name,
            "merchant_id": relation.target_organization_id,
            **{key: format(context[key], ".2f") for key in (
                "receivable_amount", "received_amount", "return_credit_amount", "balance_amount",
            )},
            "can_submit": identity.role.code == "merchant_owner",
            "order_count": len(orders),
            "device_receivable": format(device_total, ".2f"),
            "accessory_receivable": format(accessory_total, ".2f"),
            "unclassified_receivable": format(unclassified_total, ".2f"),
            "orders": orders,
            "order_payments": [{
                "id": payment.pk, "order_code": payment.finance.order_plan.plan_code,
                "amount": format(payment.confirmed_amount, ".2f") if payment.confirmed_amount is not None else None,
            } for payment in OrderPlanPayment.objects.filter(finance__order_plan__in=plans)
                .select_related("finance__order_plan").order_by("-uploaded_at", "-pk")],
            "payments": [{
                "id": row.pk,
                "amount": format(row.amount, ".2f") if row.amount is not None else None,
                "can_confirm": row.amount is None and not row.return_credit_id and identity.role.code == "factory_sales_assistant",
                "is_credit": bool(row.return_credit_id),
                "voucher_url": f"/sales/factory-payments/{row.pk}/file/" if row.voucher else "",
            } for row in (account.funding.order_by("-created_at", "-pk") if account else ())],
        })
    return {"accounts": result}


def _sync(account, user):
    sync = synchronize_receivable_account(account)
    finance = sync["active_finance"] or OrderPlanFinance.objects.filter(
        order_plan__factory_organization_id=account.factory_organization_id,
        order_plan__merchant_organization_id=account.merchant_organization_id,
    ).first()
    if finance:
        _reconcile(finance, user)


@financial_write_errors(OrderFinanceError)
@transaction.atomic
def submit(user, factory_id, request_id, voucher):
    identity = _identity(user)
    relation = _relations(identity).filter(source_organization_id=factory_id).first()
    if not relation or identity.role.code != "merchant_owner":
        raise OrderFinanceError("只有厂家直属商家负责人可以提交厂家付款。")
    request = str(request_id or "")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", request):
        raise OrderFinanceError("付款请求标识无效。")
    _, suffix = _image(voucher)
    account = lock_factory_account(factory_organization_id=relation.source_organization_id,
                                   merchant_organization_id=relation.target_organization_id)
    key = "payment:" + request
    if len(key) > 64:
        raise OrderFinanceError("付款请求标识过长。")
    row = account.funding.filter(request_id=key).first()
    if row:
        if row.submitted_by_id != user.pk or not _same_file(row.voucher, voucher):
            raise OrderFinanceError("该付款请求标识已被其他凭证使用。")
        return {"payment_id": row.pk}
    recipients = list(UserOrganization.objects.filter(
        organization_id=account.factory_organization_id, is_active=True, role__is_active=True,
        role__code="factory_sales_assistant", user__is_active=True,
    ).exclude(user__wx_openid="").exclude(user__wx_openid__isnull=True).values_list("user__wx_openid", flat=True))
    if not recipients:
        raise OrderFinanceError("厂家尚未配置有效的销售助理。")
    row = FactoryAccountFunding(account=account, request_id=key, submitted_by=user)
    row.voucher.save(f"{uuid4().hex}{suffix}", voucher, save=False)
    try:
        row.save()
        publish_workitem(
            domain="SALES", type="FACTORY_PAYMENT_CONFIRM", biz_id=str(row.pk),
            biz_ref=f"sales.FactoryAccountFunding:{row.pk}", uniq_key=f"factory-payment:{row.pk}:confirm",
            title="直属商家付款待确认", summary=relation.target_organization.name,
            link=f"/pages/sales/factory_payments/factory_payments?payment_id={row.pk}",
            recipients_openids=recipients, need_action=True, action_mode="ANYONE_DO",
            actor_openid=user.wx_openid, mark_workitem_done_immediately=False,
        )
    except Exception:
        row.voucher.delete(save=False)
        raise
    return {"payment_id": row.pk}


@financial_write_errors(OrderFinanceError)
@transaction.atomic
def confirm(user, payment_id, amount):
    identity = _identity(user)
    if not identity or identity.role.code != "factory_sales_assistant":
        raise OrderFinanceError("只有厂家销售助理可以确认收款。")
    row = _payment(user, payment_id)
    account = lock_factory_account(factory_organization_id=row.account.factory_organization_id,
                                   merchant_organization_id=row.account.merchant_organization_id)
    row = account.funding.select_for_update().get(pk=row.pk)
    amount = _money(amount)
    if row.amount is not None:
        if amount != row.amount:
            raise OrderFinanceError("已入账金额不能覆盖。")
        return {"payment_id": row.pk}
    row.amount, row.confirmed_by, row.confirmed_at = amount, user, timezone.now()
    row.save(update_fields=("amount", "confirmed_by", "confirmed_at"))
    work = WorkItem.objects.select_for_update().get(uniq_key=f"factory-payment:{row.pk}:confirm")
    _complete_work(work, user)
    _sync(account, user)
    return {"payment_id": row.pk}


def pending_return_credit(case):
    """Return the unused legacy credit and its posting evidence, without writes."""
    from lifecycle.models import FactoryReturnProcess, GoodsTransferAccountCase

    process = FactoryReturnProcess.objects.filter(
        account_case=case, transfer_id=case.transfer_id,
        phase=FactoryReturnProcess.Phase.POSTED, posted_at__isnull=False,
    ).first()
    upper, lower, transfer = case.upper_organization, case.lower_organization, case.transfer
    if (
        not process
        or case.case_type != GoodsTransferAccountCase.CaseType.RETURN_CREDIT
        or case.status != GoodsTransferAccountCase.Status.CONFIRMED
        or not case.confirmed_by_id
        or not case.confirmed_at
        or not case.confirmed_amount
        or case.confirmed_amount <= 0
        or transfer.flow_type != "RETURN" or transfer.status != "COMPLETED"
        or transfer.to_organization_id != upper.pk or transfer.from_organization_id != lower.pk
        or case.creditor_organization_id != lower.pk or case.debtor_organization_id != upper.pk
        or upper.organization_type.code != "OWNER" or upper.factory_root_id != upper.pk
        or lower.organization_type.code != "MERCHANT" or lower.factory_root_id != upper.pk
    ):
        return None
    if FactoryAccountFunding.objects.filter(return_credit=case).exists():
        return None
    used = case.credit_allocations.aggregate(total=Sum("amount"))["total"] or 0
    unused = case.confirmed_amount - used
    return (unused, max(process.posted_at, case.confirmed_at)) if unused > 0 else None


@transaction.atomic
def post_return_credit(case, user):
    """Move unused confirmed return credit after inventory posting."""
    from lifecycle.models import GoodsTransferAccountCase

    case = GoodsTransferAccountCase.objects.select_for_update().get(pk=case.pk)
    pending = pending_return_credit(case)
    if not pending:
        return None
    amount, posted_at = pending
    account = lock_factory_account(factory_organization_id=case.upper_organization_id,
                                   merchant_organization_id=case.lower_organization_id)
    funding, created = account.funding.get_or_create(return_credit=case, defaults={
        "request_id": f"return:{case.pk}", "submitted_by_id": case.submitted_by_id or case.confirmed_by_id,
        "confirmed_by_id": case.confirmed_by_id, "confirmed_at": posted_at, "amount": amount,
    })
    if created:
        _sync(account, user)
        return funding
    return None


def voucher_for(user, payment_id):
    row = _payment(user, payment_id)
    if not row.voucher:
        raise OrderFinanceError("付款凭证不存在。")
    try:
        return row.voucher.open("rb")
    except OSError as exc:
        raise OrderFinanceError("付款凭证不存在。") from exc
