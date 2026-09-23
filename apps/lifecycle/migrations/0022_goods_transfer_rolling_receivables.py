"""Add pair-level task ownership and auditable payment allocations.

Existing confirmed payments stay allocated to their original invoices. They
are never retrospectively redistributed FIFO and their voucher/accounting
facts and workflow history are left untouched. Current pending task rollover
is reconciled separately by the deployment management command.
"""

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models
from django.db.models.functions import Coalesce


def backfill_receivables(apps, schema_editor):
    alias = schema_editor.connection.alias
    Payment = apps.get_model("lifecycle", "GoodsTransferPayment")
    Allocation = apps.get_model("lifecycle", "GoodsTransferPaymentAllocation")
    Settlement = apps.get_model("lifecycle", "GoodsTransferSettlement")
    Account = apps.get_model("lifecycle", "GoodsTransferReceivableAccount")

    pending = []
    for payment in Payment.objects.using(alias).filter(status="CONFIRMED").order_by("id").iterator():
        if payment.confirmed_amount is None or payment.confirmed_amount <= 0:
            raise RuntimeError(
                "Cannot backfill a confirmed goods-transfer payment without a "
                f"positive confirmed amount: payment_id={payment.pk}."
            )
        pending.append(Allocation(
            payment_id=payment.pk,
            settlement_id=payment.settlement_id,
            amount=payment.confirmed_amount,
            allocated_at=payment.confirmed_at or payment.uploaded_at or payment.created_at,
        ))
        if len(pending) >= 500:
            Allocation.objects.using(alias).bulk_create(pending)
            pending = []
    if pending:
        Allocation.objects.using(alias).bulk_create(pending)

    # Oldest-first processing intentionally replaces each pair's active entry
    # with its newest submitted shipment, not its most recently edited price.
    settlements = (
        Settlement.objects.using(alias).filter(
            transfer__flow_type="SHIPMENT",
            statement_submitted_at__isnull=False,
        )
        .exclude(transfer__status__in=("SCANNING", "CANCELLED"))
        .annotate(receivable_order_at=Coalesce("transfer__submitted_at", "transfer__created_at"))
        .order_by("receivable_order_at", "transfer_id")
        .values_list("id", "transfer__from_organization_id", "transfer__to_organization_id")
    )
    newest = {}
    for settlement_id, seller_id, buyer_id in settlements.iterator():
        newest[(seller_id, buyer_id)] = settlement_id
    Account.objects.using(alias).bulk_create([
        Account(
            seller_organization_id=seller_id,
            buyer_organization_id=buyer_id,
            active_settlement_id=settlement_id,
        )
        for (seller_id, buyer_id), settlement_id in newest.items()
    ], batch_size=500)


class Migration(migrations.Migration):
    dependencies = [("lifecycle", "0021_aftersales_application_workflow")]

    operations = [
        migrations.CreateModel(
            name="GoodsTransferReceivableAccount",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("active_settlement", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="active_receivable_accounts", to="lifecycle.goodstransfersettlement", verbose_name="当前付款任务所属发货单")),
                ("buyer_organization", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="goods_receivable_accounts_as_buyer", to="account.organization", verbose_name="收货方组织")),
                ("seller_organization", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="goods_receivable_accounts_as_seller", to="account.organization", verbose_name="发货方组织")),
            ],
            options={
                "verbose_name": "商品发货累计往来账",
                "verbose_name_plural": "商品发货累计往来账",
                "ordering": ("seller_organization_id", "buyer_organization_id"),
                "constraints": [
                    models.UniqueConstraint(fields=("seller_organization", "buyer_organization"), name="lifecycle_gt_receivable_pair"),
                    models.CheckConstraint(condition=~models.Q(seller_organization=models.F("buyer_organization")), name="lifecycle_gt_receivable_orgs"),
                ],
            },
        ),
        migrations.CreateModel(
            name="GoodsTransferPaymentAllocation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("amount", models.DecimalField(decimal_places=2, max_digits=14, verbose_name="本次抵扣金额")),
                ("allocated_at", models.DateTimeField(default=django.utils.timezone.now, verbose_name="分配时间")),
                ("payment", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="allocations", to="lifecycle.goodstransferpayment", verbose_name="实际到账记录")),
                ("settlement", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="payment_allocations", to="lifecycle.goodstransfersettlement", verbose_name="抵扣的原始发货应收")),
            ],
            options={
                "verbose_name": "商品发货收款分配明细",
                "verbose_name_plural": "商品发货收款分配明细",
                "ordering": ("allocated_at", "id"),
                "constraints": [
                    models.UniqueConstraint(fields=("payment", "settlement"), name="lifecycle_gt_payment_invoice"),
                    models.CheckConstraint(condition=models.Q(amount__gt=0), name="lifecycle_gt_allocation_positive"),
                ],
            },
        ),
        migrations.RunPython(backfill_receivables, migrations.RunPython.noop),
    ]
