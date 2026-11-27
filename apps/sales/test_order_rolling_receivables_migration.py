"""Real 0021→0022 upgrade preserves all historical money and task facts."""

from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class OrderRollingReceivablesMigrationTests(TransactionTestCase):
    def test_upgrade_caps_origin_allocations_preserves_excess_and_all_original_audits(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        old_target = [("sales", "0021_shipment_outbound_and_evidence")]
        executor.migrate(old_target)
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(latest))
        # The downgrade targets only this domain. Account and workflow tables
        # keep their current shape, so fixtures must use those model states.
        retained = [target for target in latest if target[0] in {"account", "workflow"}]
        old = executor.loader.project_state([*old_target, *retained]).apps
        now = timezone.now()
        OrgType = old.get_model("account", "OrganizationType")
        Organization = old.get_model("account", "Organization")
        Relation = old.get_model("account", "OrganizationRelation")
        User = old.get_model("account", "User")
        Brand = old.get_model("product", "Brand")
        Assignment = old.get_model("channel", "MerchantChannelAssignment")
        Plan = old.get_model("sales", "OrderPlan")
        Finance = old.get_model("sales", "OrderPlanFinance")
        Payment = old.get_model("sales", "OrderPlanPayment")
        Cancellation = old.get_model("sales", "OrderPlanCancellationAudit")
        Work = old.get_model("workflow", "WorkItem")
        Task = old.get_model("workflow", "UserTask")
        factory_kind, _ = OrgType.objects.get_or_create(code="OWNER", defaults={"name": "Factory"})
        merchant_kind, _ = OrgType.objects.get_or_create(code="MERCHANT", defaults={"name": "Merchant"})
        factory = Organization.objects.create(code="OR-MIG-F", name="Migration factory", organization_type=factory_kind)
        buyer = Organization.objects.create(code="OR-MIG-B", name="Migration buyer", organization_type=merchant_kind)
        buyer2 = Organization.objects.create(code="OR-MIG-B2", name="Migration other buyer", organization_type=merchant_kind)
        user = User.objects.create(username="order-rolling-migration", wx_openid="order-rolling-migration-wx")
        brand = Brand.objects.create(code="OR-MIG-BRAND", name="Order migration brand")
        relations = {}
        for target in (buyer, buyer2):
            relation = Relation.objects.create(source_organization=factory, target_organization=target, relation_type="DISTRIBUTION")
            assignment = Assignment.objects.create(relation=relation, brand=brand, region="四川省", assigned_by=user)
            relations[target.pk] = (relation, assignment)

        def invoice(index, *, target=buyer, amount="1000", status="MATCHING_PENDING", priced=True, waiting=False):
            relation, assignment = relations[target.pk]
            plan = Plan.objects.create(plan_code=f"OR-MIG-{index}", client_request_id=f"or-mig-{index}",
                factory_organization=factory, merchant_organization=target, merchant_relation=relation,
                channel_assignment=assignment, brand=brand, region_snapshot="四川省", created_by=user,
                total_quantity=5, status=status, submitted_at=now + timedelta(minutes=index))
            finance = Finance.objects.create(order_plan=plan,
                receivable_amount=Decimal(amount) if priced else None,
                statement_image=f"existing-statements/order-{index}.png" if priced else "",
                statement_original_name=f"原清单{index}.png" if priced else "",
                statement_submitted_by=user if priced else None,
                statement_submitted_at=now + timedelta(hours=1, minutes=20-index) if priced else None)
            if waiting:
                Cancellation.objects.create(order_plan=plan, requested_by=user,
                    status="WAITING_PRODUCTION_COMPLETION", assembly_started_offline=True)
            return plan, finance

        first_plan, first = invoice(1)
        second_plan, second = invoice(2, amount="2000")
        cancelled_plan, cancelled = invoice(3, amount="300", status="CANCELLED")
        invoice(4, priced=False)
        invoice(5, amount="500", waiting=True)
        other_plan, other = invoice(6, target=buyer2, amount="400")

        def receipt(finance, sequence, amount, *, confirmed=True, when=now):
            return Payment.objects.create(finance=finance, sequence_no=sequence,
                client_request_id=f"old-payment-{finance.pk}-{sequence}",
                voucher_image=f"original-vouchers/{finance.pk}-{sequence}.png",
                voucher_original_name=f"原凭证{sequence}.png", uploaded_by=user,
                confirmed_amount=Decimal(amount) if confirmed else None,
                confirmed_by=user if confirmed else None,
                confirmed_at=when if confirmed else None,
                status="CONFIRMED" if confirmed else "PENDING")

        paid1 = receipt(first, 1, "400")
        paid2 = receipt(first, 2, "700", when=now+timedelta(minutes=1))
        paid3 = receipt(second, 1, "300")
        cancelled_paid = receipt(cancelled, 1, "350")
        other_paid = receipt(other, 1, "50", when=None)
        pending = receipt(second, 2, "0", confirmed=False)
        work = Work.objects.create(domain="SALES", type="ORDER_PLAN_PAYMENT_CONFIRM", biz_id=second_plan.plan_code,
            biz_ref=f"sales.OrderPlan:{second_plan.pk}", uniq_key="order-rolling-migration-task",
            title="历史收款确认", payload={"order_plan_id": second_plan.pk, "payment_id": pending.pk, "audit": "preserve"})
        Task.objects.create(workitem=work, to_openid=user.wx_openid, state="READ", need_action=True, read_at=now)
        snapshots = {name: list(old.get_model("sales", name).objects.order_by("pk").values())
            for name in ("OrderPlan", "OrderPlanFinance", "OrderPlanPayment", "OrderPlanCancellationAudit")}
        works, tasks = list(Work.objects.order_by("pk").values()), list(Task.objects.order_by("pk").values())

        executor = MigrationExecutor(connection)
        executor.migrate(latest)
        current = executor.loader.project_state(latest).apps
        Account = current.get_model("sales", "OrderPlanReceivableAccount")
        Allocation = current.get_model("sales", "OrderPlanPaymentAllocation")
        self.assertEqual(Account.objects.get(factory_organization_id=factory.pk, merchant_organization_id=buyer.pk).active_finance_id, second.pk)
        self.assertEqual(Account.objects.get(factory_organization_id=factory.pk, merchant_organization_id=buyer2.pk).active_finance_id, other.pk)
        for payment, finance, amount in ((paid1, first, "400"), (paid2, first, "600"),
                (paid3, second, "300"), (cancelled_paid, cancelled, "300"), (other_paid, other, "50")):
            row = Allocation.objects.get(payment_id=payment.pk)
            self.assertEqual(row.finance_id, finance.pk)
            self.assertEqual(row.amount, Decimal(amount))
            self.assertEqual(row.allocated_at, payment.confirmed_at or payment.uploaded_at)
            self.assertIsNone(row.released_at)
        self.assertEqual(Allocation.objects.count(), 5)
        self.assertFalse(Allocation.objects.filter(payment_id=pending.pk).exists())
        self.assertEqual(Payment.objects.get(pk=paid2.pk).confirmed_amount, Decimal("700"))
        for name, expected in snapshots.items():
            actual = list(current.get_model("sales", name).objects.order_by("pk").values())
            if name == "OrderPlanFinance":
                for row in actual:
                    self.assertEqual(row.pop("priced_lines"), [])
                    self.assertEqual(row.pop("pricing_version"), "")
            self.assertEqual(actual, expected)
        self.assertEqual(list(current.get_model("workflow", "WorkItem").objects.order_by("pk").values()), works)
        self.assertEqual(list(current.get_model("workflow", "UserTask").objects.order_by("pk").values()), tasks)
        allocated = list(Allocation.objects.order_by("pk").values())
        MigrationExecutor(connection).migrate(latest)
        self.assertEqual(list(Allocation.objects.order_by("pk").values()), allocated)
        self.assertEqual(Account.objects.count(), 2)
