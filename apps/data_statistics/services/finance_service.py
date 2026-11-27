"""Read-only, role-scoped receivables/payables from existing financial facts.

One row represents one directed counterparty/source account. Invoice roll-forward
is an entry-routing rule, never another amount to add to the statistics.
"""

from collections import defaultdict
from decimal import Decimal

from django.db.models import F, Q, Sum
from django.db.models.functions import Coalesce

from account.models import OrganizationRelation, OrganizationRelationType
from account.services.role_delegation_service import identity_has_role
from lifecycle.models import (
    GoodsTransfer, GoodsTransferAccountCase, GoodsTransferAccountPayment,
    GoodsTransferCreditAllocation,
)
from lifecycle.services.goods_transfer_account_service import FACTORY_ACCOUNT_ROLES
from sales.models import FactoryAccountAllocation, FactoryAccountFunding, OrderPlanFinance, OrderPlanPayment, OrderPlanPaymentAllocation
from sales.services.order_receivable_service import PENDING_CANCELLATIONS


ZERO = Decimal("0.00")
MERCHANT_PAYABLE_ROLES = {"merchant_owner", "merchant_sales", "merchant_stock"}


def _direct_merchant_ids(root_id):
    return OrganizationRelation.objects.filter(
        source_organization_id=root_id,
        source_organization__organization_type__code="OWNER",
        source_organization__is_active=True,
        target_organization__organization_type__code="MERCHANT",
        target_organization__is_active=True,
        target_organization__factory_root_id=root_id,
        relation_type=OrganizationRelationType.DISTRIBUTION, is_active=True,
    ).values("target_organization_id")


def financial_capabilities(identity):
    """Merchant finance is only a direct merchant's read-only debt to its factory."""
    row = identity.identity
    root = identity.organization.get_factory_root()
    factory = identity.organization_type == "OWNER"
    direct = bool(identity.organization_type == "MERCHANT" and root
                  and OrganizationRelation.objects.filter(
                      source_organization_id=root.pk, target_organization_id=identity.organization.pk,
                      source_organization__organization_type__code="OWNER",
                      source_organization__is_active=True, target_organization__is_active=True,
                      target_organization__factory_root_id=root.pk,
                      relation_type=OrganizationRelationType.DISTRIBUTION, is_active=True,
                  ).exists())
    payable = direct and identity_has_role(row, MERCHANT_PAYABLE_ROLES)
    order_factory = factory and identity.role.code in {"factory_sales", "factory_sales_assistant"}
    return {
        "direct_merchant": direct,
        "goods_transfer": False,
        "transfer_receivable": (factory and identity_has_role(row, FACTORY_ACCOUNT_ROLES)) or payable,
        "order_plan": order_factory or payable,
        "order_plan_creator_only": factory and identity.role.code == "factory_sales",
        "can_open_order_detail": order_factory or (payable and identity.role.code == "merchant_owner"),
    }


def can_view_financial_statistics(identity):
    caps = financial_capabilities(identity)
    return any(caps[key] for key in ("goods_transfer", "transfer_receivable", "order_plan"))


def _money(value):
    return format(value or ZERO, ".2f")


def _root_matches(requested_root, root):
    if root is None:
        return False
    if requested_root in (None, ""):
        return True
    text = str(requested_root)
    return len(text) <= 19 and text.isascii() and text.isdigit() and int(text) == root.pk


def _transfer_root_filter(root_id, prefix="transfer__"):
    return {
        prefix + "instruction_organization__factory_root_id": root_id,
        prefix + "from_organization__factory_root_id": root_id,
        prefix + "to_organization__factory_root_id": root_id,
    }


def _sums(query, key, amount):
    return dict(query.order_by().values(key).annotate(total=Sum(amount)).values_list(key, "total"))


def _posted_credits(root_id):
    """Only actual posted allocations reduce debt; unused credit is not cash."""
    return GoodsTransferCreditAllocation.objects.filter(
        credit_case__status=GoodsTransferAccountCase.Status.CONFIRMED,
        credit_case__case_type__in=(GoodsTransferAccountCase.CaseType.RETURN_CREDIT,
                                   GoodsTransferAccountCase.CaseType.TRANSFER_OUT_CREDIT),
        credit_case__upper_organization__factory_root_id=root_id,
        credit_case__lower_organization__factory_root_id=root_id,
        credit_case__creditor_organization_id=F("credit_case__lower_organization_id"),
        credit_case__debtor_organization_id=F("credit_case__upper_organization_id"),
        **_transfer_root_filter(root_id, "credit_case__transfer__"),
    )


def _new_account(kind, seller, buyer, org_id, root):
    receivable = seller.pk == org_id
    counterparty = buyer if receivable else seller
    return {
        "key": f"{kind}:{seller.pk}:{buyer.pk}",
        "side": "RECEIVABLE" if receivable else "PAYABLE", "source_kind": kind,
        "counterparty_id": counterparty.pk, "counterparty_name": counterparty.name,
        "document_count": 0, "receivable_amount": ZERO, "received_amount": ZERO,
        "credit_amount": ZERO, "outstanding_amount": ZERO, "overpaid_amount": ZERO,
        "entry_id": None, "root_organization": root,
    }


def _case_accounts(identity, root):
    org_id = identity.organization.pk
    cases = list(GoodsTransferAccountCase.objects.filter(
        Q(creditor_organization_id=org_id) | Q(debtor_organization_id=org_id),
        upper_organization_id=root["id"],
        lower_organization_id__in=_direct_merchant_ids(root["id"]),
        case_type=GoodsTransferAccountCase.CaseType.TRANSFER_IN_RECEIVABLE,
        status=GoodsTransferAccountCase.Status.CONFIRMED, confirmed_amount__isnull=False,
        transfer__flow_type=GoodsTransfer.FlowType.TRANSFER,
        creditor_organization_id=F("upper_organization_id"), lower_organization_id=F("debtor_organization_id"),
        upper_organization__factory_root_id=root["id"], lower_organization__factory_root_id=root["id"],
        **_transfer_root_filter(root["id"]),
    ).exclude(transfer__status__in=(GoodsTransfer.Status.SCANNING, GoodsTransfer.Status.CANCELLED))
        .select_related("creditor_organization", "debtor_organization")
        .order_by("confirmed_at", "id"))
    ids = [row.pk for row in cases]
    received = _sums(GoodsTransferAccountPayment.objects.filter(
        account_case_id__in=ids, status=GoodsTransferAccountPayment.Status.CONFIRMED,
    ), "account_case_id", "confirmed_amount")
    credits = _sums(_posted_credits(root["id"]).filter(
        target_account_case_id__in=ids,
        credit_case__upper_organization_id=F("target_account_case__upper_organization_id"),
        credit_case__lower_organization_id=F("target_account_case__lower_organization_id"),
    ), "target_account_case_id", "amount")
    accounts = {}
    for case in cases:
        pair = (case.creditor_organization_id, case.debtor_organization_id)
        account = accounts.setdefault(pair, _new_account(
            "TRANSFER_RECEIVABLE", case.creditor_organization, case.debtor_organization, org_id, root))
        account["document_count"] += 1
        account["receivable_amount"] += case.confirmed_amount
        account["received_amount"] += received.get(case.pk) or ZERO
        account["credit_amount"] += credits.get(case.pk) or ZERO
        account["entry_id"] = case.transfer_id
        account["can_open"] = True
    for account in accounts.values():
        balance = account["receivable_amount"] - account["received_amount"] - account["credit_amount"]
        account["outstanding_amount"], account["overpaid_amount"] = max(balance, ZERO), max(-balance, ZERO)
    return list(accounts.values())


def _order_accounts(identity, root, caps):
    org_id = identity.organization.pk
    query = OrderPlanFinance.objects.filter(
        Q(order_plan__factory_organization_id=org_id) | Q(order_plan__merchant_organization_id=org_id),
        statement_submitted_at__isnull=False, receivable_amount__isnull=False,
        order_plan__factory_organization_id=root["id"],
        order_plan__merchant_relation__source_organization_id=root["id"],
        order_plan__merchant_relation__target_organization_id=F("order_plan__merchant_organization_id"),
        order_plan__merchant_relation__relation_type=OrganizationRelationType.DISTRIBUTION,
        order_plan__factory_organization__factory_root_id=root["id"],
        order_plan__merchant_organization__factory_root_id=root["id"],
    ).exclude(order_plan__status="CANCELLED").exclude(
        order_plan__cancellation_audit__status__in=PENDING_CANCELLATIONS)
    if caps["order_plan_creator_only"]:
        query = query.filter(order_plan__created_by_id=identity.user.pk)
    finances = list(query.select_related("order_plan__factory_organization", "order_plan__merchant_organization")
                    .order_by("order_plan__submitted_at", "order_plan_id"))
    ids = [row.pk for row in finances]
    # Existing invoice allocations can originate on a cancelled invoice or on
    # another salesperson's invoice. Count only the visible target's amount;
    # never open the other invoice or sum its entire pair context.
    live_allocations = OrderPlanPaymentAllocation.objects.filter(
        released_at__isnull=True, payment__status=OrderPlanPayment.Status.CONFIRMED,
        payment__finance__order_plan__factory_organization_id=F("finance__order_plan__factory_organization_id"),
        payment__finance__order_plan__merchant_organization_id=F("finance__order_plan__merchant_organization_id"),
        finance__order_plan__factory_organization__factory_root_id=root["id"],
        finance__order_plan__merchant_organization__factory_root_id=root["id"],
    )
    allocated = _sums(live_allocations.filter(finance_id__in=ids), "finance_id", "amount")
    extra = FactoryAccountAllocation.objects.filter(finance_id__in=ids, released_at__isnull=True)
    extra_cash = _sums(extra.filter(funding__return_credit__isnull=True), "finance_id", "amount")
    extra_credit = _sums(extra.filter(funding__return_credit__isnull=False), "finance_id", "amount")
    payments = list(OrderPlanPayment.objects.filter(finance_id__in=ids,
                    status=OrderPlanPayment.Status.CONFIRMED))
    used = _sums(live_allocations.filter(payment_id__in=[p.pk for p in payments]), "payment_id", "amount")
    unused = defaultdict(lambda: ZERO)
    for payment in payments:
        unused[payment.finance_id] += max((payment.confirmed_amount or ZERO) - (used.get(payment.pk) or ZERO), ZERO)
    accounts = {}
    for finance in finances:
        plan = finance.order_plan
        pair = (plan.factory_organization_id, plan.merchant_organization_id)
        account = accounts.setdefault(pair, _new_account(
            "ORDER_PLAN", plan.factory_organization, plan.merchant_organization, org_id, root))
        allocation = (allocated.get(finance.pk) or ZERO) + (extra_cash.get(finance.pk) or ZERO)
        credit = extra_credit.get(finance.pk) or ZERO
        account["credit_amount"] += credit
        account["document_count"] += 1
        account["receivable_amount"] += finance.receivable_amount
        account["received_amount"] += allocation + unused[finance.pk]
        account["outstanding_amount"] += max(finance.receivable_amount - allocation - credit, ZERO)
        account["overpaid_amount"] += unused[finance.pk]
        account["entry_id"] = plan.pk if caps["can_open_order_detail"] else None
        account["can_open"] = caps["can_open_order_detail"]
    if not caps["order_plan_creator_only"]:
        # Match the full-pair detail's cash facts exactly, even between a
        # cancellation decision and the writer releasing old allocations:
        # eligible-origin confirmed cash remains cash in full; excluded-origin
        # cash contributes only its still-live allocation to an eligible target.
        # Summing target allocations plus unused cash would lose the portion
        # still allocated to a now-excluded target, so do not derive cash from
        # those invoice allocation balances in this full-pair view.
        finance_pairs = {row.pk: (row.order_plan.factory_organization_id,
                                 row.order_plan.merchant_organization_id) for row in finances}
        pair_cash = defaultdict(lambda: ZERO)
        for payment in payments:
            pair_cash[finance_pairs[payment.finance_id]] += payment.confirmed_amount or ZERO
        retained = _sums(live_allocations.filter(finance_id__in=ids)
                         .exclude(payment__finance_id__in=ids), "finance_id", "amount")
        for finance_id, amount in retained.items():
            pair_cash[finance_pairs[finance_id]] += amount or ZERO
        funding_rows = FactoryAccountFunding.objects.filter(
            account__factory_organization_id=root["id"],
            account__merchant_organization_id__in=_direct_merchant_ids(root["id"]),
            amount__isnull=False,
        ).select_related("account__factory_organization", "account__merchant_organization")
        if org_id != root["id"]:
            funding_rows = funding_rows.filter(account__merchant_organization_id=org_id)
        pair_credit = defaultdict(lambda: ZERO)
        for funding in funding_rows:
            seller = funding.account.factory_organization
            buyer = funding.account.merchant_organization
            pair = (seller.pk, buyer.pk)
            accounts.setdefault(pair, _new_account("ORDER_PLAN", seller, buyer, org_id, root))
            if funding.return_credit_id:
                pair_credit[pair] += funding.amount
            else:
                pair_cash[pair] += funding.amount
        for pair, account in accounts.items():
            account["received_amount"] = pair_cash[pair]
            account["credit_amount"] = pair_credit[pair]
            balance = account["receivable_amount"] - account["received_amount"] - account["credit_amount"]
            account["outstanding_amount"], account["overpaid_amount"] = max(balance, ZERO), max(-balance, ZERO)
    return list(accounts.values())


def build_financial_statistics(identity, requested_root=None):
    """Read current business facts without creating ledgers, snapshots or tasks."""
    root_org = identity.organization.get_factory_root()
    root = {"id": root_org.pk, "name": root_org.name} if root_org else None
    if not can_view_financial_statistics(identity):
        return {"domain": "FINANCE", "metrics": {}, "accounts": [], "access_denied": True,
                "root_organization": root, "read_only": True,
                "metric_note": "当前岗位无权查看资金往来统计。"}
    if not _root_matches(requested_root, root_org):
        return {"domain": "FINANCE", "metrics": {}, "accounts": [], "access_denied": True,
                "root_organization": root, "read_only": True,
                "metric_note": "当前选择的根组织不属于本组织资金账，请切换至" + (root_org.name if root_org else "有效组织身份") + "。"}
    caps = financial_capabilities(identity)
    accounts = []
    if caps["transfer_receivable"]:
        accounts += _case_accounts(identity, root)
    if caps["order_plan"]:
        accounts += _order_accounts(identity, root, caps)
    side = "RECEIVABLE" if identity.organization_type == "OWNER" else "PAYABLE"
    key = "receivable_outstanding_amount" if side == "RECEIVABLE" else "payable_outstanding_amount"
    metrics = {key: _money(sum((a["outstanding_amount"] for a in accounts if a["side"] == side), ZERO))}
    note = (f"当前根组织：{root_org.name}；仅统计本组织“{identity.organization.name}”的厂家货款。"
            "商家向下发货仅保留货品清单、物流和发货数量，不形成应收、应付账款。"
            "仅已确认到账及已分配冲抵减少余额；未分配退货额度不重复抵减。")
    if side == "PAYABLE":
        note += "应付统计仅限与本厂家有有效直属经销关系的商家；统计可见不等于有付款或确认收款权限。"
    if caps["order_plan_creator_only"]:
        note += "厂家订单仅统计本人创建的有效订单。"
    money_keys = ("receivable_amount", "received_amount", "credit_amount", "outstanding_amount", "overpaid_amount")
    for account in accounts:
        account["balance_amount"] = _money(account["outstanding_amount"] - account["overpaid_amount"])
        for key in money_keys:
            account[key] = _money(account[key])
    accounts.sort(key=lambda row: (row["side"] != "RECEIVABLE", row["source_kind"], row["counterparty_id"]))
    return {"domain": "FINANCE", "metrics": metrics, "accounts": accounts, "metric_note": note,
            "root_organization": root, "scope": {"organization_id": identity.organization.pk,
            "role_code": identity.role.code, **caps}, "finance_sides": [side], "read_only": True, "access_denied": False}
