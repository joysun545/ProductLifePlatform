from django.db import models
from django.db.models import Q
from django.utils import timezone

from sales.private_storage import order_finance_private_storage


class OrderPlanFinance(models.Model):
    priced_lines = models.JSONField(default=list, blank=True)
    pricing_version = models.CharField(max_length=8, blank=True, default="")
    """Independent receivable ledger for one order plan."""

    order_plan = models.OneToOneField(
        "sales.OrderPlan",
        on_delete=models.PROTECT,
        related_name="finance",
        verbose_name="订单计划",
    )
    statement_image = models.FileField(
        storage=order_finance_private_storage,
        upload_to="statements/%Y/%m/",
        blank=True,
        verbose_name="订单清单截图",
    )
    statement_original_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="清单原文件名",
    )
    receivable_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name="应收总额",
    )
    statement_submitted_by = models.ForeignKey(
        "account.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="submitted_order_finance_statements",
        verbose_name="清单提交人",
    )
    statement_submitted_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="清单提交时间",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        ordering = ("-created_at", "-id")
        verbose_name = "订单资金主账"
        verbose_name_plural = "订单资金主账"
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(receivable_amount__isnull=True)
                    | Q(receivable_amount__gt=0)
                ),
                name="sales_finance_receivable_positive_or_null",
            ),
        ]

    def __str__(self):
        return f"{self.order_plan.plan_code}-资金主账"


class OrderPlanPayment(models.Model):
    """One merchant voucher and the amount confirmed by a sales assistant."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "待确认收款"
        CONFIRMED = "CONFIRMED", "已确认收款"

    finance = models.ForeignKey(
        OrderPlanFinance,
        on_delete=models.PROTECT,
        related_name="payments",
        verbose_name="订单资金主账",
    )
    sequence_no = models.PositiveIntegerField(verbose_name="付款批次")
    client_request_id = models.CharField(
        max_length=64,
        verbose_name="客户端请求标识",
    )
    voucher_image = models.FileField(
        storage=order_finance_private_storage,
        upload_to="payment_vouchers/%Y/%m/",
        verbose_name="付款凭证",
    )
    voucher_original_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="凭证原文件名",
    )
    uploaded_by = models.ForeignKey(
        "account.User",
        on_delete=models.PROTECT,
        related_name="uploaded_order_payment_vouchers",
        verbose_name="凭证上传人",
    )
    uploaded_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        verbose_name="凭证上传时间",
    )
    confirmed_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name="确认收款金额",
    )
    confirmed_by = models.ForeignKey(
        "account.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="confirmed_order_payments",
        verbose_name="收款确认人",
    )
    confirmed_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="收款确认时间",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
        verbose_name="收款状态",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        ordering = ("sequence_no", "id")
        verbose_name = "订单付款记录"
        verbose_name_plural = "订单付款记录"
        constraints = [
            models.UniqueConstraint(
                fields=("finance", "sequence_no"),
                name="sales_unique_finance_payment_sequence",
            ),
            models.UniqueConstraint(
                fields=("finance", "client_request_id"),
                name="sales_unique_finance_payment_request",
            ),
            models.CheckConstraint(
                condition=(
                    Q(confirmed_amount__isnull=True)
                    | Q(confirmed_amount__gt=0)
                ),
                name="sales_payment_amount_positive_or_null",
            ),
        ]

    def __str__(self):
        return f"{self.finance.order_plan.plan_code}-第{self.sequence_no}笔付款"


class OrderPlanReceivableAccount(models.Model):
    """Cumulative order receivables for one directed factory/merchant pair."""

    factory_organization = models.ForeignKey(
        "account.Organization", on_delete=models.PROTECT,
        related_name="order_receivable_accounts_as_factory", verbose_name="厂家组织",
    )
    merchant_organization = models.ForeignKey(
        "account.Organization", on_delete=models.PROTECT,
        related_name="order_receivable_accounts_as_merchant", verbose_name="商家组织",
    )
    active_finance = models.ForeignKey(
        OrderPlanFinance, on_delete=models.PROTECT, null=True, blank=True,
        related_name="active_receivable_accounts", verbose_name="当前付款任务所属订单",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        ordering = ("factory_organization_id", "merchant_organization_id")
        verbose_name = "订单累计往来账"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(
                fields=("factory_organization", "merchant_organization"),
                name="sales_receivable_pair_unique",
            ),
            models.CheckConstraint(
                condition=~Q(factory_organization=models.F("merchant_organization")),
                name="sales_receivable_distinct_orgs",
            ),
        ]


class OrderPlanPaymentAllocation(models.Model):
    """Cash allocated to an invoice; cancellation releases retain their history."""

    payment = models.ForeignKey(
        OrderPlanPayment, on_delete=models.PROTECT, related_name="allocations",
        verbose_name="实际到账记录",
    )
    finance = models.ForeignKey(
        OrderPlanFinance, on_delete=models.PROTECT, related_name="payment_allocations",
        verbose_name="抵扣的原始订单应收",
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2, verbose_name="分配金额")
    allocated_at = models.DateTimeField(default=timezone.now, verbose_name="分配时间")
    released_at = models.DateTimeField(null=True, blank=True, db_index=True, verbose_name="解除分配时间")
    release_reason = models.CharField(max_length=64, blank=True, default="", verbose_name="解除分配原因")

    class Meta:
        ordering = ("allocated_at", "id")
        verbose_name = "订单收款分配明细"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(
                fields=("payment", "finance"), condition=Q(released_at__isnull=True),
                name="sales_active_payment_invoice",
            ),
            models.CheckConstraint(condition=Q(amount__gt=0), name="sales_allocation_positive"),
        ]


class FactoryAccountFunding(models.Model):
    """A standalone receipt or a posted factory return credit."""

    account = models.ForeignKey(OrderPlanReceivableAccount, on_delete=models.PROTECT, related_name="funding")
    request_id = models.CharField(max_length=64)
    return_credit = models.OneToOneField(
        "lifecycle.GoodsTransferAccountCase", on_delete=models.PROTECT, null=True, blank=True,
    )
    voucher = models.FileField(storage=order_finance_private_storage, upload_to="factory_payments/%Y/%m/", blank=True)
    submitted_by = models.ForeignKey("account.User", on_delete=models.PROTECT, related_name="+")
    confirmed_by = models.ForeignKey("account.User", on_delete=models.PROTECT, related_name="+", null=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("account", "request_id"), name="sales_factory_funding_request"),
            models.CheckConstraint(condition=Q(amount__isnull=True) | Q(amount__gt=0), name="sales_factory_funding_amount"),
        ]


class FactoryAccountAllocation(models.Model):
    funding = models.ForeignKey(FactoryAccountFunding, on_delete=models.PROTECT, related_name="allocations")
    finance = models.ForeignKey(OrderPlanFinance, on_delete=models.PROTECT, related_name="factory_account_allocations")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    released_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("funding", "finance"), condition=Q(released_at__isnull=True),
                                    name="sales_factory_funding_live_allocation"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="sales_factory_allocation_amount"),
        ]


class FactoryProductPrice(models.Model):
    factory = models.ForeignKey("account.Organization", on_delete=models.PROTECT)
    product_model = models.ForeignKey("product.ProductModel", on_delete=models.PROTECT)
    prices = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("factory", "product_model"), name="sales_unique_factory_product_price"),
        ]


class MerchantPricePolicy(models.Model):
    account = models.OneToOneField(OrderPlanReceivableAccount, on_delete=models.PROTECT, related_name="price_policy")
    tier = models.CharField(max_length=1, choices=[(value, value) for value in "ABC"])
    overrides = models.JSONField(default=dict)


class FactoryAccessoryPrice(models.Model):
    factory = models.ForeignKey("account.Organization", on_delete=models.PROTECT)
    material_spec = models.ForeignKey("supply_chain.MaterialSpec", on_delete=models.PROTECT)
    prices = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("factory", "material_spec"), name="sales_unique_factory_accessory_price"),
        ]
