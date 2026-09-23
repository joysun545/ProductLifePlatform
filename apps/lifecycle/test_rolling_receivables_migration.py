"""Upgrade pre-rolling invoice history without reallocating recorded receipts."""

from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class RollingReceivablesMigrationTests(TransactionTestCase):
    serialized_rollback = True

    def test_backfill_preserves_original_payment_invoice_and_all_audit_facts(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        old_target = [("lifecycle", "0021_aftersales_application_workflow")]
        executor.migrate(old_target)
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(latest))
        old = executor.loader.project_state(old_target).apps
        now = timezone.now()
        OrgType = old.get_model("account", "OrganizationType")
        Organization = old.get_model("account", "Organization")
        User = old.get_model("account", "User")
        Transfer = old.get_model("lifecycle", "GoodsTransfer")
        Settlement = old.get_model("lifecycle", "GoodsTransferSettlement")
        Payment = old.get_model("lifecycle", "GoodsTransferPayment")
        Work = old.get_model("workflow", "WorkItem")
        Task = old.get_model("workflow", "UserTask")
        kind, _ = OrgType.objects.get_or_create(code="MERCHANT", defaults={"name": "Merchant"})
        seller = Organization.objects.create(code="ROLL-MIG-S", name="Migration seller", organization_type=kind)
        buyer = Organization.objects.create(code="ROLL-MIG-B", name="Migration buyer", organization_type=kind)
        other = Organization.objects.create(code="ROLL-MIG-C", name="Other buyer", organization_type=kind)
        user = User.objects.create(username="rolling-migration", wx_openid="rolling-migration-wx")

        def invoice(index, *, target=buyer, status="COMPLETED", amount="1000.00", submitted=True):
            transfer = Transfer.objects.create(
                flow_no=f"ROLL-MIG-{index}", client_request_id=f"ROLL-MIG-{index}", flow_type="SHIPMENT",
                instruction_organization=seller, from_organization=seller, to_organization=target,
                target_account=user, initiated_by=user, status=status,
                submitted_at=now + timedelta(minutes=index),
            )
            settlement = Settlement.objects.create(
                transfer=transfer, receivable_amount=Decimal(amount) if amount else None,
                statement_image=f"existing-statements/migration-{index}.png",
                statement_original_name=f"原清单{index}.png",
                statement_submitted_at=now + timedelta(minutes=index + 20) if submitted else None,
                statement_submitted_by=user,
            )
            return transfer, settlement

        first_flow, first = invoice(1)
        second_flow, second = invoice(2, amount="2000.00")
        invoice(3, status="CANCELLED", amount="3000.00")
        invoice(4, amount=None, submitted=False)
        third_flow, third = invoice(5, target=other, amount="5000.00")
        paid = Payment.objects.create(
            settlement=second, sequence_no=1, client_request_id="historically-on-second",
            voucher_image="existing-payment-vouchers/history.png", voucher_original_name="付款原凭证.png",
            uploaded_by=user, confirmed_amount=Decimal("400.00"), confirmed_by=user,
            confirmed_at=now, status="CONFIRMED",
        )
        pending = Payment.objects.create(
            settlement=first, sequence_no=1, client_request_id="older-pending-proof",
            voucher_image="existing-payment-vouchers/pending.png", uploaded_by=user,
            status="PENDING",
        )
        other_paid = Payment.objects.create(
            settlement=third, sequence_no=1, client_request_id="other-buyer-payment",
            voucher_image="existing-payment-vouchers/other.png", uploaded_by=user,
            confirmed_amount=Decimal("50.00"), confirmed_by=user,
            confirmed_at=None, status="CONFIRMED",
        )
        work = Work.objects.create(
            domain="LOGISTICS", type="GOODS_TRANSFER_PAYMENT_CONFIRM", biz_id=first_flow.flow_no,
            biz_ref=f"lifecycle.GoodsTransfer:{first_flow.pk}", uniq_key="rolling-migration-task",
            title="历史收款确认", payload={"goods_transfer_id": first_flow.pk, "payment_id": pending.pk, "audit": "preserve"},
        )
        Task.objects.create(workitem=work, to_openid=user.wx_openid, state="NEW", need_action=True)
        snapshots = {
            "GoodsTransfer": list(Transfer.objects.order_by("id").values()),
            "GoodsTransferSettlement": list(Settlement.objects.order_by("id").values()),
            "GoodsTransferPayment": list(Payment.objects.order_by("id").values()),
        }
        work_snapshot = list(Work.objects.order_by("id").values())
        task_snapshot = list(Task.objects.order_by("id").values())

        executor = MigrationExecutor(connection)
        executor.migrate(latest)
        current = executor.loader.project_state(latest).apps
        Account = current.get_model("lifecycle", "GoodsTransferReceivableAccount")
        Allocation = current.get_model("lifecycle", "GoodsTransferPaymentAllocation")
        account = Account.objects.get(seller_organization_id=seller.pk, buyer_organization_id=buyer.pk)
        self.assertEqual(account.active_settlement_id, second.pk)
        self.assertEqual(Account.objects.get(seller_organization_id=seller.pk, buyer_organization_id=other.pk).active_settlement_id, third.pk)
        allocation = Allocation.objects.get(payment_id=paid.pk)
        self.assertEqual(allocation.settlement_id, second.pk)
        self.assertEqual(allocation.amount, Decimal("400.00"))
        self.assertEqual(allocation.allocated_at, now)
        self.assertFalse(Allocation.objects.filter(payment_id=pending.pk).exists())
        self.assertFalse(Allocation.objects.filter(settlement_id=first.pk).exists())
        self.assertEqual(Allocation.objects.get(payment_id=other_paid.pk).allocated_at, other_paid.uploaded_at)
        self.assertEqual(Allocation.objects.count(), 2)
        for name, expected in snapshots.items():
            self.assertEqual(list(current.get_model("lifecycle", name).objects.order_by("id").values()), expected)
        self.assertEqual(list(current.get_model("workflow", "WorkItem").objects.order_by("id").values()), work_snapshot)
        self.assertEqual(list(current.get_model("workflow", "UserTask").objects.order_by("id").values()), task_snapshot)
        allocation_snapshot = list(Allocation.objects.order_by("id").values())
        MigrationExecutor(connection).migrate(latest)
        self.assertEqual(list(Allocation.objects.order_by("id").values()), allocation_snapshot)
        self.assertEqual(Account.objects.count(), 2)
