"""Upgrade real 0020 rows without replaying shipment or finance events."""
from decimal import Decimal

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class ShipmentUpgradeMigrationTests(TransactionTestCase):
    def test_0020_upgrade_preserves_history_and_reuses_pending_task(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        old_target = [('sales', '0020_make_material_stock_v2_the_only_new_order_flow')]
        executor.migrate(old_target)
        # The downgrade targets only this domain. Account and workflow tables
        # keep their current shape, so fixtures must use those model states.
        retained = [target for target in latest if target[0] in {"account", "workflow"}]
        old = executor.loader.project_state([*old_target, *retained]).apps
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(latest))

        def model(app, name):
            return old.get_model(app, name)

        now = timezone.now()
        factory_type, _ = model('account', 'OrganizationType').objects.get_or_create(
            code='OWNER', defaults={'name': 'Manufacturer'})
        merchant_type, _ = model('account', 'OrganizationType').objects.get_or_create(
            code='MERCHANT', defaults={'name': 'Merchant'})
        factory = model('account', 'Organization').objects.create(
            code='MIGRATION-FACTORY', name='Migration factory', organization_type=factory_type)
        merchant = model('account', 'Organization').objects.create(
            code='MIGRATION-MERCHANT', name='Migration merchant', organization_type=merchant_type)
        user = model('account', 'User').objects.create(
            username='migration-stock', wx_openid='migration-stock-openid')
        role, _ = model('account', 'OrganizationRole').objects.get_or_create(
            code='factory_stock', defaults={'name': 'Stock', 'organization_type': factory_type})
        model('account', 'UserOrganization').objects.create(user=user, organization=factory, role=role)
        relation = model('account', 'OrganizationRelation').objects.create(
            source_organization=factory, target_organization=merchant, relation_type='DISTRIBUTION')
        brand = model('product', 'Brand').objects.create(code='MIGRATION', name='Migration brand')
        assignment = model('channel', 'MerchantChannelAssignment').objects.create(
            relation=relation, brand=brand, region='Test region', assigned_by=user)
        plan = model('sales', 'OrderPlan').objects.create(
            plan_code='MIGRATION-PLAN', client_request_id='migration-plan',
            factory_organization=factory, merchant_organization=merchant,
            merchant_relation=relation, channel_assignment=assignment, brand=brand,
            region_snapshot='Test region', created_by=user, total_quantity=6,
            flow_version='MATERIAL_STOCK_V2', status='PRODUCTION_COMPLETED')
        finance = model('sales', 'OrderPlanFinance').objects.create(
            order_plan=plan, receivable_amount=Decimal('1234.56'), statement_submitted_at=now,
            statement_image='statements/existing.png', statement_submitted_by=user)

        shipments = []
        for sequence, status in enumerate(['AUTHORIZED', 'AUTHORIZED', 'AUTHORIZED', 'SHIPPED', 'RECEIVED', 'CANCELLED'], 1):
            shipments.append(model('sales', 'OrderPlanShipment').objects.create(
                order_plan=plan, sequence_no=sequence, client_request_id=f'migration-{sequence}',
                status=status, qrcode_printed_at=now, logistics_company='Existing carrier',
                tracking_no=f'EXISTING-{sequence}', logistics_confirmed_by=user,
                logistics_confirmed_at=None if sequence == 3 else now,
                shipped_at=now if status in {'SHIPPED', 'RECEIVED'} else None,
                received_at=now if status == 'RECEIVED' else None))
        key = f'sales:order-plan:{plan.pk}:shipment:{shipments[0].pk}:stock-outbound'
        work = model('workflow', 'WorkItem').objects.create(
            domain='SALES', type='ORDER_PLAN_V2_STOCK_OUTBOUND', biz_id=plan.plan_code,
            uniq_key=key, title='Old outbound task', status='OPEN',
            payload={'existing_audit_reference': 'keep-this'})
        task = model('workflow', 'UserTask').objects.create(
            workitem=work, to_openid=user.wx_openid, need_action=True, state='NEW')

        executor = MigrationExecutor(connection)
        executor.migrate(latest)
        current = executor.loader.project_state(latest).apps
        Shipment = current.get_model('sales', 'OrderPlanShipment')
        Work = current.get_model('workflow', 'WorkItem')
        Task = current.get_model('workflow', 'UserTask')
        self.assertEqual(list(Shipment.objects.filter(order_plan_id=plan.pk).order_by('sequence_no').values_list('status', flat=True)),
                         ['OUTBOUNDED', 'OUTBOUNDED', 'AUTHORIZED', 'SHIPPED', 'RECEIVED', 'CANCELLED'])
        upgraded = Work.objects.get(uniq_key=key)
        self.assertEqual(upgraded.pk, work.pk)
        self.assertEqual(upgraded.payload['existing_audit_reference'], 'keep-this')
        self.assertIn('待确认发货', upgraded.title)
        self.assertEqual(Task.objects.get(workitem_id=work.pk, to_openid=user.wx_openid).pk, task.pk)
        new_key = f'sales:order-plan:{plan.pk}:shipment:{shipments[1].pk}:stock-outbound'
        self.assertEqual(Task.objects.filter(workitem__uniq_key=new_key, to_openid=user.wx_openid, need_action=True).count(), 1)
        self.assertEqual(Shipment.objects.get(pk=shipments[3].pk).shipped_at, now)
        self.assertEqual(Shipment.objects.get(pk=shipments[4].pk).received_at, now)
        for shipment in Shipment.objects.filter(order_plan_id=plan.pk):
            self.assertEqual(shipment.tracking_no, f'EXISTING-{shipment.sequence_no}')
        saved_finance = current.get_model('sales', 'OrderPlanFinance').objects.get(pk=finance.pk)
        self.assertEqual(saved_finance.receivable_amount, Decimal('1234.56'))
        self.assertEqual(saved_finance.statement_image.name, 'statements/existing.png')
        self.assertEqual(saved_finance.statement_submitted_at, now)
        # Running migrate a second time must not manufacture another task.
        counts = (Work.objects.count(), Task.objects.count())
        MigrationExecutor(connection).migrate(latest)
        self.assertEqual((Work.objects.count(), Task.objects.count()), counts)
