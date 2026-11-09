"""Current merchant policy: physical shipments and documents, no downstream debt."""
from decimal import Decimal
from datetime import timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from account.models import (
    Organization, OrganizationRelation, OrganizationRole, OrganizationType,
    SeniorManagerProfile, User, UserOrganization,
)
from account.services.auth_service import issue_access_token
from channel.models import MerchantChannelAssignment
from data_statistics.services import get_statistics_identity
from data_statistics.services.dashboard_service import build_dashboard
from data_statistics.services.finance_service import build_financial_statistics
from data_statistics.services.flow_service import build_flow_statistics
from lifecycle.models import GoodsTransfer, GoodsTransferAccountCase, GoodsTransferItem, GoodsTransferSettlement, Device
from manufacturing.models import ProductionBatch
from product.models import Brand, BOM, ProductCategory, ProductModel
from sales.models import OrderPlan, OrderPlanFinance, OrderPlanPayment, OrderPlanPaymentAllocation
from sales.services.order_finance_service import _access


class MerchantPayableDocumentsPolicyTests(TestCase):
    def setUp(self):
        self.n = 0
        self.root = self.org('MP-ROOT', 'OWNER')
        self.other_root = self.org('MP-ROOT-OTHER', 'OWNER')
        self.direct = self.org('MP-DIRECT', 'MERCHANT', self.root)
        self.child = self.org('MP-CHILD', 'MERCHANT', self.root)
        self.peer = self.org('MP-PEER', 'MERCHANT', self.root)
        self.foreign = self.org('MP-FOREIGN', 'MERCHANT', self.other_root)
        self.direct_relation = self.relate(self.root, self.direct)
        self.child_relation = self.relate(self.direct, self.child)
        self.peer_relation = self.relate(self.root, self.peer)
        self.relate(self.other_root, self.foreign)
        self.owner = self.user('MP-OWNER', self.direct, 'merchant_owner')
        self.sales = self.user('MP-SALES', self.direct, 'merchant_sales')
        self.stock = self.user('MP-STOCK', self.direct, 'merchant_stock')
        self.electrician = self.user('MP-ELEC', self.direct, 'merchant_electrician')
        self.child_user = self.user('MP-CHILD-OWNER', self.child, 'merchant_owner')
        self.peer_user = self.user('MP-PEER-OWNER', self.peer, 'merchant_owner')
        self.foreign_user = self.user('MP-FOREIGN-OWNER', self.foreign, 'merchant_owner')
        self.factory_sales = self.user('MP-FACTORY-SALES', self.root, 'factory_sales')
        self.other_sales = self.user('MP-FACTORY-SALES-OTHER', self.root, 'factory_sales')
        self.assistant = self.user('MP-ASSISTANT', self.root, 'factory_sales_assistant')
        self.brand = Brand.objects.create(code='MP-BRAND', name='测试品牌')
        category = ProductCategory.objects.create(code='MP-CAT', name='测试品类')
        self.product = ProductModel.objects.create(code='MP-MODEL', name='测试型号', category=category)
        bom = BOM.objects.create(product_model=self.product, version='TEST', created_by=self.factory_sales)
        batch = ProductionBatch.objects.create(batch_no='MP-BATCH', manufacturer=self.root,
            product_model=self.product, bom=bom, production_date='2026-11-09', quantity=4)
        self.devices = [Device.objects.create(sn=f'MP-SN-{i}', product_model=self.product, batch=batch) for i in range(4)]

    def org(self, code, kind, root=None):
        return Organization.objects.create(code=code, name=code,
            organization_type=OrganizationType.objects.get(code=kind), factory_root=root)

    def relate(self, upper, lower):
        return OrganizationRelation.objects.create(source_organization=upper, target_organization=lower,
            relation_type='DISTRIBUTION')

    def user(self, name, org, role):
        user = User.objects.create_user(username=name, wx_openid='openid-' + name)
        UserOrganization.objects.create(user=user, organization=org,
            role=OrganizationRole.objects.get(code=role, organization_type=org.organization_type))
        return user

    def finance(self, amount='500', buyer=None, creator=None):
        self.n += 1
        buyer, creator = buyer or self.direct, creator or self.factory_sales
        relation = OrganizationRelation.objects.filter(target_organization=buyer, is_active=True).first()
        assignment, _ = MerchantChannelAssignment.objects.get_or_create(relation=relation,
            defaults={'brand': self.brand, 'region': '测试区', 'assigned_by': creator})
        plan = OrderPlan.objects.create(plan_code=f'MP-OP-{self.n}', client_request_id=f'MP-OP-{self.n}',
            factory_organization=self.root, merchant_organization=buyer, merchant_relation=relation,
            channel_assignment=assignment, brand=self.brand, region_snapshot='测试区', created_by=creator,
            total_quantity=1, status='SHIPPED', submitted_at=timezone.now()+timedelta(seconds=self.n))
        return OrderPlanFinance.objects.create(order_plan=plan, receivable_amount=Decimal(amount),
            statement_submitted_at=timezone.now(), statement_submitted_by=self.assistant,
            statement_image='statements/no-private-file-needed.png')

    def shipment(self, *, sender=None, buyer=None, instruction=None, status='PENDING_RECEIPT',
                 submitted=True, kind='SHIPMENT', count=2):
        self.n += 1
        sender, buyer = sender or self.direct, buyer or self.child
        target = {self.child.pk: self.child_user, self.direct.pk: self.owner,
                  self.peer.pk: self.peer_user, self.foreign.pk: self.foreign_user}[buyer.pk]
        transfer = GoodsTransfer.objects.create(flow_no=f'MP-GF-{self.n}', client_request_id=f'MP-GF-{self.n}',
            from_organization=sender, to_organization=buyer, instruction_organization=instruction or sender,
            target_account=target, initiated_by=self.sales, status=status, flow_type=kind,
            submitted_at=timezone.now() if submitted else None)
        for device in self.devices[:count]:
            GoodsTransferItem.objects.create(transfer=transfer, device=device, status='PENDING_RECEIPT', scanned_by=self.sales)
        return transfer

    def data(self, user=None, root=None):
        return build_financial_statistics(get_statistics_identity(user or self.sales), requested_root=root)

    def test_direct_merchant_finance_roles_read_factory_payables_only_without_expanding_writes(self):
        for _ in range(3):
            self.finance()
        for user in (self.owner, self.sales, self.stock):
            data = self.data(user)
            self.assertEqual(data['finance_sides'], ['PAYABLE'])
            self.assertEqual(data['metrics'], {'payable_outstanding_amount': '1500.00'})
            self.assertEqual(len(data['accounts']), 1)
            self.assertEqual(data['accounts'][0]['document_count'], 3)
            self.assertEqual(data['accounts'][0]['side'], 'PAYABLE')
        account = self.data(self.sales)['accounts'][0]
        self.assertIsNone(account['entry_id'])
        self.assertFalse(account['can_open'])
        plan = OrderPlan.objects.first()
        self.assertEqual(_access(self.sales.organization_identity, plan), (False, False, False))
        self.assertEqual(_access(self.stock.organization_identity, plan), (False, False, False))
        self.assertTrue(self.data(self.owner)['accounts'][0]['can_open'])

    def test_confirmed_cash_is_counted_once_and_pending_proof_does_not_reduce_payable(self):
        first, second, third = [self.finance() for _ in range(3)]
        payment = OrderPlanPayment.objects.create(finance=third, sequence_no=1,
            client_request_id='MP-CASH', uploaded_by=self.owner, voucher_image='proof/test.png',
            status='CONFIRMED', confirmed_amount='600', confirmed_by=self.assistant)
        OrderPlanPaymentAllocation.objects.create(payment=payment, finance=first, amount='500')
        OrderPlanPaymentAllocation.objects.create(payment=payment, finance=second, amount='100')
        OrderPlanPayment.objects.create(finance=third, sequence_no=2,
            client_request_id='MP-PENDING', uploaded_by=self.owner, voucher_image='proof/test-pending.png',
            status='PENDING', confirmed_amount=None)
        self.assertEqual(self.data()['metrics']['payable_outstanding_amount'], '900.00')

    def test_lower_merchant_and_inactive_direct_link_have_no_finance_instead_of_fake_zero(self):
        self.finance(buyer=self.child)
        for user in (self.child_user, self.electrician):
            self.assertTrue(self.data(user)['access_denied'])
            self.assertEqual(self.data(user)['metrics'], {})
            self.assertNotIn('FINANCE', build_dashboard(get_statistics_identity(user))['modules'])
        self.direct_relation.is_active = False
        self.direct_relation.save(update_fields=('is_active',))
        self.assertTrue(self.data()['access_denied'])

    def test_old_merchant_debt_records_never_become_new_finance_statistics(self):
        self.finance('700')
        transfer = self.shipment()
        GoodsTransferSettlement.objects.create(transfer=transfer, receivable_amount='9000',
            statement_submitted_at=timezone.now(), statement_submitted_by=self.sales,
            statement_image='old-statement/testing-only.png')
        data = self.data()
        self.assertEqual(data['metrics'], {'payable_outstanding_amount': '700.00'})
        self.assertTrue(all(a['source_kind']=='ORDER_PLAN' for a in data['accounts']))

    def test_factory_order_receivables_remain_creator_scoped_and_direct_only(self):
        self.finance('250')
        self.finance('300', creator=self.other_sales)
        self.finance('9000', buyer=self.child)
        self.assertEqual(self.data(self.factory_sales)['metrics'], {'receivable_outstanding_amount':'250.00'})
        self.assertEqual(self.data(self.assistant)['metrics'], {'receivable_outstanding_amount':'550.00'})
        self.direct_relation.is_active = False
        self.direct_relation.save(update_fields=('is_active',))
        self.assertEqual(self.data(self.assistant)['metrics'], {'receivable_outstanding_amount':'550.00'})
        self.assertTrue(self.data()['access_denied'])

    def test_shipment_counts_include_unpriced_undocumented_physical_dispatches_only(self):
        self.shipment(count=2)
        self.shipment(status='COMPLETED', count=1)
        self.shipment(status='SCANNING', submitted=False)
        self.shipment(status='CANCELLED')
        self.shipment(kind='RETURN')
        self.shipment(instruction=self.other_root)
        self.shipment(sender=self.peer, buyer=self.direct)
        self.assertEqual(GoodsTransferSettlement.objects.count(), 0)
        flow = build_flow_statistics(get_statistics_identity(self.sales))
        self.assertEqual(flow['metrics']['merchant_shipment_count'], 2)
        self.assertEqual(flow['metrics']['merchant_shipped_device_count'], 3)

    def test_senior_manager_requires_current_finance_delegation(self):
        self.finance('500')
        senior = self.user('MP-SENIOR', self.direct, 'merchant_senior_manager')
        profile = SeniorManagerProfile.objects.create(identity=senior.organization_identity)
        self.assertTrue(self.data(senior)['access_denied'])
        profile.managed_roles.add(self.stock.organization_identity.role)
        self.assertEqual(self.data(senior)['metrics']['payable_outstanding_amount'], '500.00')
        self.assertFalse(self.data(senior)['accounts'][0]['can_open'])
        profile.managed_roles.clear()
        self.assertTrue(self.data(senior)['access_denied'])

    def test_factory_owned_transfer_receivable_remains_factory_debt_and_direct_payable(self):
        transfer = self.shipment(sender=self.peer, buyer=self.direct, instruction=self.root,
                                 status='COMPLETED', kind='TRANSFER')
        GoodsTransferAccountCase.objects.create(transfer=transfer,
            case_type='TRANSFER_IN_RECEIVABLE', status='CONFIRMED',
            upper_organization=self.root, lower_organization=self.direct,
            creditor_organization=self.root, debtor_organization=self.direct,
            confirmed_amount='125', confirmed_at=timezone.now())
        self.assertEqual(self.data()['metrics'], {'payable_outstanding_amount':'125.00'})
        self.assertEqual(self.data(self.assistant)['metrics'], {'receivable_outstanding_amount':'125.00'})
    def test_root_and_org_scope_are_explicit_and_reading_never_writes(self):
        self.finance('500')
        self.finance('9000', buyer=self.peer)
        for bad in (self.other_root.pk, 'oops', '9'*5000):
            denied = self.data(root=bad)
            self.assertEqual(denied['metrics'], {})
            self.assertTrue(denied['access_denied'])
        with CaptureQueriesContext(connection) as captured:
            self.assertEqual(self.data()['metrics']['payable_outstanding_amount'], '500.00')
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT','UPDATE','DELETE','REPLACE')) for q in captured))
        response = self.client.get('/pldp/api/data-statistics/finance/',
            {'root_organization_id': self.root.pk}, HTTP_AUTHORIZATION='Bearer '+issue_access_token(self.sales))
        self.assertEqual(response.json()['data']['metrics'], {'payable_outstanding_amount':'500.00'})
        response = self.client.get('/pldp/api/data-statistics/finance/',
            HTTP_AUTHORIZATION='Bearer '+issue_access_token(self.child_user))
        self.assertEqual(response.json()['error_code'], 'STATISTICS_ACCESS_DENIED')
