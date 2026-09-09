from django.db import models
from django.db.models import Q

from lifecycle.private_storage import goods_transfer_private_storage


class GoodsTransferAccountCase(models.Model):
    """A manually confirmed vertical commercial leg attached to a goods flow.

    Physical ownership is intentionally handled by ``GoodsTransfer``.  This
    model only records money between an immediate upper merchant and its lower
    merchant.  A sibling transfer therefore has two independent cases instead
    of one B-to-C account relationship.
    """

    class CaseType(models.TextChoices):
        RETURN_CREDIT = "RETURN_CREDIT", "退货冲抵额度"
        TRANSFER_OUT_CREDIT = "TRANSFER_OUT_CREDIT", "调出方退货冲抵"
        TRANSFER_IN_RECEIVABLE = (
            "TRANSFER_IN_RECEIVABLE",
            "调入方新增应收",
        )

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "待提交"
        PENDING_CONFIRM = "PENDING_CONFIRM", "待对方确认"
        REJECTED = "REJECTED", "已驳回"
        CONFIRMED = "CONFIRMED", "已确认"
        NO_AMOUNT = "NO_AMOUNT", "本次无款项"

    transfer = models.ForeignKey(
        "lifecycle.GoodsTransfer",
        on_delete=models.PROTECT,
        related_name="account_cases",
        verbose_name="商品流转单",
    )
    case_type = models.CharField(
        max_length=32,
        choices=CaseType.choices,
        db_index=True,
        verbose_name="账务类型",
    )
    status = models.CharField(
        max_length=24,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
        verbose_name="状态",
    )
    upper_organization = models.ForeignKey(
        "account.Organization",
        on_delete=models.PROTECT,
        related_name="goods_transfer_account_cases_as_upper",
        verbose_name="直属上级商家",
    )
    lower_organization = models.ForeignKey(
        "account.Organization",
        on_delete=models.PROTECT,
        related_name="goods_transfer_account_cases_as_lower",
        verbose_name="直属下级商家",
    )
    creditor_organization = models.ForeignKey(
        "account.Organization",
        on_delete=models.PROTECT,
        related_name="goods_transfer_account_cases_as_creditor",
        verbose_name="应收方",
    )
    debtor_organization = models.ForeignKey(
        "account.Organization",
        on_delete=models.PROTECT,
        related_name="goods_transfer_account_cases_as_debtor",
        verbose_name="应付方",
    )
    proposed_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name="提交金额",
    )
    confirmed_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name="确认金额",
    )
    statement_image = models.FileField(
        storage=goods_transfer_private_storage,
        upload_to="account_case_statements/%Y/%m/",
        blank=True,
        verbose_name="款项关联清单",
    )
    statement_original_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="清单原文件名",
    )
    remark = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="说明",
    )
    submitted_by = models.ForeignKey(
        "account.User",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="submitted_goods_transfer_account_cases",
        verbose_name="提交人",
    )
    submitted_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="提交时间",
    )
    confirmed_by = models.ForeignKey(
        "account.User",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="confirmed_goods_transfer_account_cases",
        verbose_name="确认人",
    )
    confirmed_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="确认时间",
    )
    rejected_by = models.ForeignKey(
        "account.User",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="rejected_goods_transfer_account_cases",
        verbose_name="驳回人",
    )
    rejected_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="驳回时间",
    )
    rejection_reason = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="驳回原因",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        ordering = ("transfer_id", "case_type", "id")
        verbose_name = "商品流转上下级往来账"
        verbose_name_plural = "商品流转上下级往来账"
        constraints = (
            models.UniqueConstraint(
                fields=("transfer", "case_type"),
                name="lifecycle_gt_unique_account_case",
            ),
            models.CheckConstraint(
                condition=~Q(
                    upper_organization=models.F("lower_organization")
                ),
                name="lifecycle_gt_account_vertical_orgs_differ",
            ),
            models.CheckConstraint(
                condition=~Q(
                    creditor_organization=models.F("debtor_organization")
                ),
                name="lifecycle_gt_account_money_orgs_differ",
            ),
            models.CheckConstraint(
                condition=(
                    Q(proposed_amount__isnull=True)
                    | Q(proposed_amount__gt=0)
                ),
                name="lifecycle_gt_account_proposed_positive",
            ),
            models.CheckConstraint(
                condition=(
                    Q(confirmed_amount__isnull=True)
                    | Q(confirmed_amount__gt=0)
                ),
                name="lifecycle_gt_account_confirmed_positive",
            ),
        )

    def __str__(self):
        return f"{self.transfer.flow_no}-{self.get_case_type_display()}"


class GoodsTransferCreditAllocation(models.Model):
    """Manual use of a confirmed return credit against one receivable batch."""

    credit_case = models.ForeignKey(
        GoodsTransferAccountCase,
        on_delete=models.PROTECT,
        related_name="credit_allocations",
        verbose_name="退货冲抵额度",
    )
    target_shipment_settlement = models.ForeignKey(
        "lifecycle.GoodsTransferSettlement",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="credit_allocations",
        verbose_name="原发货应收批次",
    )
    target_account_case = models.ForeignKey(
        GoodsTransferAccountCase,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="received_credit_allocations",
        verbose_name="调货新增应收批次",
    )
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        verbose_name="冲抵金额",
    )
    allocated_by = models.ForeignKey(
        "account.User",
        on_delete=models.PROTECT,
        related_name="goods_transfer_credit_allocations",
        verbose_name="冲抵操作人",
    )
    allocated_at = models.DateTimeField(auto_now_add=True, verbose_name="冲抵时间")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        ordering = ("allocated_at", "id")
        verbose_name = "商品流转退货冲抵明细"
        verbose_name_plural = "商品流转退货冲抵明细"
        constraints = (
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="lifecycle_gt_credit_allocation_positive",
            ),
            models.CheckConstraint(
                condition=(
                    (
                        Q(target_shipment_settlement__isnull=False)
                        & Q(target_account_case__isnull=True)
                    )
                    | (
                        Q(target_shipment_settlement__isnull=True)
                        & Q(target_account_case__isnull=False)
                    )
                ),
                name="lifecycle_gt_credit_one_target",
            ),
            models.UniqueConstraint(
                fields=("credit_case", "target_shipment_settlement"),
                condition=Q(target_shipment_settlement__isnull=False),
                name="lifecycle_gt_unique_shipment_credit",
            ),
            models.UniqueConstraint(
                fields=("credit_case", "target_account_case"),
                condition=Q(target_account_case__isnull=False),
                name="lifecycle_gt_unique_case_credit",
            ),
        )

    def __str__(self):
        return f"{self.credit_case}-冲抵{self.amount}"


class GoodsTransferAccountPayment(models.Model):
    """Payment batches for the A-to-C receivable leg of a transfer."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "待确认收款"
        CONFIRMED = "CONFIRMED", "已确认收款"

    account_case = models.ForeignKey(
        GoodsTransferAccountCase,
        on_delete=models.PROTECT,
        related_name="payments",
        verbose_name="调货新增应收",
    )
    sequence_no = models.PositiveIntegerField(verbose_name="付款批次")
    client_request_id = models.CharField(max_length=64, verbose_name="客户端请求标识")
    voucher_image = models.FileField(
        storage=goods_transfer_private_storage,
        upload_to="account_case_payment_vouchers/%Y/%m/",
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
        related_name="uploaded_goods_transfer_account_payments",
        verbose_name="凭证上传人",
    )
    uploaded_at = models.DateTimeField(auto_now_add=True, verbose_name="上传时间")
    confirmed_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name="确认到账金额",
    )
    confirmed_by = models.ForeignKey(
        "account.User",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="confirmed_goods_transfer_account_payments",
        verbose_name="收款确认人",
    )
    confirmed_at = models.DateTimeField(null=True, blank=True, verbose_name="确认时间")
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
        verbose_name = "调货新增应收付款"
        verbose_name_plural = "调货新增应收付款"
        constraints = (
            models.UniqueConstraint(
                fields=("account_case", "sequence_no"),
                name="lifecycle_gt_account_payment_sequence",
            ),
            models.UniqueConstraint(
                fields=("account_case", "client_request_id"),
                name="lifecycle_gt_account_payment_request",
            ),
            models.CheckConstraint(
                condition=(
                    Q(confirmed_amount__isnull=True)
                    | Q(confirmed_amount__gt=0)
                ),
                name="lifecycle_gt_account_payment_positive",
            ),
        )

    def __str__(self):
        return f"{self.account_case}-第{self.sequence_no}笔付款"
