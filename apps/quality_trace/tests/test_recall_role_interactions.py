"""Cross-role contracts on a nine-device mixed recall, using a temporary DB."""
from datetime import timedelta
from uuid import uuid4

from django.test import TestCase
from django.utils import timezone
from account.models import Organization, OrganizationRelation, OrganizationRole, OrganizationType, User, UserOrganization
from account.services.auth_service import issue_access_token
from lifecycle.models import (ActivationRecord, CustomerDeviceBinding, CustomerVehicle, Device,
                              DeviceState, DriverVehicleBinding, GoodsTransfer, OrganizationDevice)
from manufacturing.models import ProductionBatch
from product.models import BOM, BOMItem, ProductCategory, ProductModel
from supply_chain.models import Material
from quality_trace.models import BatchQualityCase, BatchQualityAffectedDevice, QualityRecallCustomerHandover
from quality_trace.services.recall_service import create_recall_campaign, launch_recall_campaign, QualityRecallError
from quality_trace.services.recall_handover_service import (select_customer_delivery_method,
    accept_merchant_handover_by_token, submit_customer_direct_shipment,
    accept_factory_pickup_request, submit_customer_pickup_shipment)
from quality_trace.services.recall_batch_service import execute_recall_batch
from quality_trace.services.recall_transport_service import confirm_recall_inventory_batch
from quality_trace.services.recall_disposition_service import (start_recall_post_disposition,
    confirm_recall_disposition_supplier, confirm_recall_disposition_matching,
    complete_recall_disposition_production, complete_recall_device_disposition)
from quality_trace.services.recall_task_service import get_recall_task_scope
from workflow.models import UserTask, WorkItem
from workflow.services.task_query import get_task_list, get_task_summary


class RecallRoleInteractionTests(TestCase):
    @classmethod
    def make_user(cls, org, role, suffix=''):
        name = f'rr-{org.pk}-{role}-{suffix}'
        user = User.objects.create_user(username=name, wx_openid=name)
        UserOrganization.objects.create(user=user, organization=org, role=OrganizationRole.objects.get(
            organization_type=org.organization_type, code=role))
        return user

    @classmethod
    def setUpTestData(cls):
        def org(kind, name):
            return Organization.objects.create(name=name, code=f'RR-{name}',
                organization_type=OrganizationType.objects.get(code=kind))
        cls.factory = org('OWNER', '厂家')
        cls.direct = org('MERCHANT', '直属商家')
        cls.lower = org('MERCHANT', '下级商家')
        cls.second = org('MERCHANT', '第二下级商家')
        cls.customer = org('CUSTOMER', '用户组织')
        cls.supplier = org('SUPPLIER', '供应商')
        cls.other_supplier = org('SUPPLIER', '其他供应商')
        for parent, child in ((cls.factory, cls.direct), (cls.direct, cls.lower),
                              (cls.direct, cls.second), (cls.lower, cls.customer),
                              (cls.direct, cls.customer)):
            OrganizationRelation.objects.create(source_organization=parent, target_organization=child,
                                                relation_type='DISTRIBUTION')
        cls.factory_users = {role: cls.make_user(cls.factory, role) for role in (
            'factory_admin', 'factory_manager', 'factory_chief_engineer', 'factory_production', 'factory_matching',
            'factory_logistics', 'factory_sales', 'factory_sales_assistant')}
        from channel.models import MerchantChannelAssignment
        from product.models import Brand
        MerchantChannelAssignment.objects.create(
            relation=OrganizationRelation.objects.get(source_organization=cls.factory, target_organization=cls.direct),
            brand=Brand.objects.create(name='RR品牌', code='RR-BRAND'), region='测试区域',
            assigned_by=cls.factory_users['factory_sales'])
        cls.chief = cls.factory_users['factory_chief_engineer']
        cls.merchant_users = {organization.pk: {role: cls.make_user(organization, role) for role in (
            'merchant_owner', 'merchant_sales', 'merchant_stock')}
            for organization in (cls.direct, cls.lower, cls.second)}
        cls.owner = cls.make_user(cls.customer, 'customer_owner')
        cls.driver = cls.make_user(cls.customer, 'driver')
        cls.unrelated_driver = cls.make_user(cls.customer, 'driver', 'unrelated')
        cls.supplier_owner = cls.make_user(cls.supplier, 'supplier_owner')
        cls.supplier_sales = cls.make_user(cls.supplier, 'supplier_sales')
        cls.other_supplier_owner = cls.make_user(cls.other_supplier, 'supplier_owner')
        cls.vehicle = CustomerVehicle.objects.create(customer_organization=cls.customer, vehicle_no='RR-VEHICLE')
        DriverVehicleBinding.objects.create(vehicle=cls.vehicle, driver=cls.driver)
        category = ProductCategory.objects.create(name='RR品类', code='RR-C')
        model = ProductModel.objects.create(name='RR型号', code='RR-M', category=category)
        material = Material.objects.create(code='RR-MAT', name='物料')
        batches = []
        for index, supplier in enumerate((cls.supplier, cls.other_supplier)):
            bom = BOM.objects.create(product_model=model, version=str(index), created_by=cls.chief)
            BOMItem.objects.create(bom=bom, material=material, supplier=supplier)
            batches.append(ProductionBatch.objects.create(batch_no=f'RR-B-{index}', manufacturer=cls.factory,
                product_model=model, bom=bom, quantity=9, production_date=timezone.localdate()))
        cls.devices = []
        locations = [cls.customer, cls.lower, cls.lower, cls.second, cls.second, cls.second,
                     cls.direct, cls.direct, cls.direct]
        for index, organization in enumerate(locations):
            device = Device.objects.create(sn=f'RR-SN-{index}', product_model=model, batch=batches[int(index == 8)])
            cls.devices.append(device)
            DeviceState.objects.create(device=device, current_organization=organization,
                status='ACTIVATED' if index == 0 else 'IN_STOCK',
                install_status='INSTALLED' if index == 0 else 'NOT_INSTALLED',
                activation_status='ACTIVATED' if index == 0 else 'NOT_ACTIVATED')
            OrganizationDevice.objects.create(device=device, organization=organization,
                                               status='SOLD' if index == 0 else 'IN_STOCK')
        CustomerDeviceBinding.objects.create(device=cls.devices[0], customer_organization=cls.customer,
            source_organization=cls.lower, vehicle=cls.vehicle, accepted_by=cls.owner)
        ActivationRecord.objects.create(device=cls.devices[0], organization=cls.customer,
                                         operator=cls.owner, vehicle_no=cls.vehicle.vehicle_no)
        case = BatchQualityCase.objects.create(case_no='RR-CASE', factory_organization=cls.factory,
            scope_type='TEST', scope_key='RR', reason='混合召回', opened_by=cls.chief)
        for device in cls.devices:
            BatchQualityAffectedDevice.objects.create(quality_case=case, device=device, sn_snapshot=device.sn,
                                                       product_model_snapshot={'id': model.pk, 'name': model.name})
        cls.campaign = create_recall_campaign(user=cls.chief, case_id=case.pk, title='库存8台、已激活1台',
            reason='混合召回验证', deadline=timezone.localdate()+timedelta(days=7), risk_level='', remedy_type='')
        launch_recall_campaign(user=cls.chief, campaign_id=cls.campaign.pk)
        cls.rows = list(cls.campaign.devices.order_by('device_id'))
        cls.handover = QualityRecallCustomerHandover.objects.get(campaign=cls.campaign)

    def get(self, user, path, allowed=True):
        user.refresh_from_db()
        response = self.client.get('/pldp/api/quality-trace/'+path,
                                   HTTP_AUTHORIZATION=f'Bearer {issue_access_token(user)}')
        self.assertEqual(response.status_code, 200, response.content)
        result = response.json()
        if allowed:
            self.assertEqual(result['code'], 0, result)
        else:
            self.assertNotEqual(result['code'], 0)
        return result['data']

    def detail(self, user):
        return self.get(user, f'recalls/{self.campaign.pk}/')

    def assert_scope(self, user, indexes):
        data = self.detail(user)
        expected = {self.devices[index].pk for index in indexes}
        self.assertEqual({row['device_id'] for row in data['devices']}, expected)
        self.assertEqual(data['device_count'], len(expected))
        listed = self.get(user, 'recalls/')['items']
        self.assertEqual(next(item['device_count'] for item in listed if item['id'] == self.campaign.pk), len(expected))
        for row in self.rows:
            self.get(user, f'recall-devices/{row.pk}/', allowed=row.device_id in expected)
        return data

    def run_batch(self, user, action, rows):
        return execute_recall_batch(user=user, campaign_id=self.campaign.pk, action=action,
            recall_device_ids=[row.pk for row in rows], client_request_id=uuid4().hex,
            evidence={'storage_key': 'private/recall/rr.jpg'}, logistics_provider='测试物流',
            tracking_no='RR-TRACK', isolation_location='召回隔离库')

    def select(self, method, user=None):
        return select_customer_delivery_method(user=user or self.owner, handover_id=self.handover.pk,
            delivery_method=method, payload={'contact_name': '测试用户', 'contact_phone': '13800000000',
                'pickup_address': '测试地址', 'pickup_time_window': '明天下午'})

    def return_all(self):
        self.select('MERCHANT_HANDOVER')
        self.handover.refresh_from_db()
        accept_merchant_handover_by_token(user=self.merchant_users[self.lower.pk]['merchant_sales'],
            token=self.handover.merchant_transfer_token, receipt_note='核验接管')
        lower_stock = self.merchant_users[self.lower.pk]['merchant_stock']
        second_owner = self.merchant_users[self.second.pk]['merchant_owner']
        self.run_batch(lower_stock, 'DISPATCH', self.rows[:3])
        self.run_batch(second_owner, 'DISPATCH', self.rows[3:6])
        self.run_batch(self.merchant_users[self.direct.pk]['merchant_owner'], 'RECEIVE', self.rows[:3])
        self.run_batch(self.merchant_users[self.direct.pk]['merchant_stock'], 'RECEIVE', self.rows[3:6])
        self.run_batch(self.merchant_users[self.direct.pk]['merchant_sales'], 'DISPATCH', self.rows)
        self.run_batch(self.factory_users['factory_logistics'], 'RECEIVE', self.rows)
        self.run_batch(self.factory_users['factory_sales_assistant'], 'ISOLATE', self.rows)
        confirm_recall_inventory_batch(user=self.factory_users['factory_sales_assistant'], campaign_id=self.campaign.pk)

    def test_all_roles_share_consistent_list_detail_and_single_device_scope(self):
        for role in ('factory_chief_engineer', 'factory_sales_assistant', 'factory_logistics', 'factory_sales'):
            data = self.assert_scope(self.factory_users[role], range(9))
            self.assertEqual(data['viewer']['full_campaign_access'], role != 'factory_sales')
        for role in ('factory_matching', 'factory_production'):
            self.get(self.factory_users[role], f'recalls/{self.campaign.pk}/', allowed=False)
        for role in ('factory_admin', 'factory_manager'):
            data = self.detail(self.factory_users[role])
            self.assertEqual(data['devices'], [])
            self.assertIsNone(data['device_count'])
            self.assertFalse(data['batch_flow']['enabled'])
        for org, indexes in ((self.direct, range(9)), (self.lower, range(3)), (self.second, range(3, 6))):
            for user in self.merchant_users[org.pk].values():
                data = self.assert_scope(user, indexes)
                self.assertTrue(data['batch_flow']['enabled'])
        for user in (self.owner, self.driver):
            data = self.assert_scope(user, [0])
            self.assertIsNone(data['progress'])
            self.assertIsNone(data['inventory_posting'])
            self.assertTrue(data['devices'][0]['handover']['available_actions']['select_delivery_method'])
            self.assertNotIn('recipients', str(data['devices'][0]['responsibility']))
        for user in (self.unrelated_driver, self.supplier_owner, self.supplier_sales):
            self.get(user, f'recalls/{self.campaign.pk}/', allowed=False)
            self.assertEqual(self.get(user, 'recalls/')['items'], [])

    def test_wechat_swaps_do_not_transfer_scope_even_with_old_factory_recipients(self):
        snapshots = list(self.campaign.devices.values_list('responsibility_snapshot', flat=True))
        for source, target in ((self.owner, self.chief),
                               (self.driver, self.merchant_users[self.direct.pk]['merchant_owner'])):
            source_id, target_id = source.wx_openid, target.wx_openid
            User.objects.filter(pk=source.pk).update(wx_openid='rr-temporary')
            User.objects.filter(pk=target.pk).update(wx_openid=source_id)
            User.objects.filter(pk=source.pk).update(wx_openid=target_id)
            UserTask.objects.filter(to_openid=source_id).update(to_openid='rr-temporary')
            UserTask.objects.filter(to_openid=target_id).update(to_openid=source_id)
            UserTask.objects.filter(to_openid='rr-temporary').update(to_openid=target_id)
        for user in (self.owner, self.driver):
            self.assert_scope(user, [0])
            scope = get_recall_task_scope(user=user, campaign_id=self.campaign.pk)['scope']
            self.assertEqual(scope['device_count'], 1)
            card = next(item for item in get_task_list(user.wx_openid)['items'] if item['group_kind'] == 'QUALITY_RECALL')
            self.assertEqual(card['recall_context']['device_count'], 1)
        self.assert_scope(self.chief, range(9))
        self.assertEqual(list(self.campaign.devices.values_list('responsibility_snapshot', flat=True)), snapshots)
        # A newly occupied slot gets the business scope even if absent at launch.
        new_sales = self.make_user(self.second, 'merchant_sales', 'new')
        self.assert_scope(new_sales, range(3, 6))

    def test_dispatched_sibling_waits_for_actual_receiver_and_partial_receipt_is_shared(self):
        actors = self.merchant_users[self.second.pk]
        self.run_batch(actors['merchant_sales'], 'DISPATCH', self.rows[3:6])
        for user in actors.values():
            data = self.assert_scope(user, range(3, 6))
            self.assertEqual(data['batch_flow']['groups'], [])
            self.assertIn('已发出至直属商家，等待其确认接收', data['batch_flow']['empty_message'])
        direct = self.merchant_users[self.direct.pk]
        self.assertIn('实物到货后', self.detail(direct['merchant_owner'])['view_context']['stage_summary'])
        self.run_batch(direct['merchant_owner'], 'RECEIVE', self.rows[3:4])
        for user in direct.values():
            groups = self.detail(user)['batch_flow']['groups']
            self.assertEqual({group['action']: group['device_count'] for group in groups}, {'DISPATCH': 4, 'RECEIVE': 2})
        self.run_batch(direct['merchant_stock'], 'RECEIVE', self.rows[4:6])
        self.assertFalse(WorkItem.objects.filter(type='QUALITY_RECALL_BATCH_RECEIPT', status='OPEN').exists())
        before = get_task_summary(actors['merchant_owner'].wx_openid)
        card = next(item for item in get_task_list(actors['merchant_owner'].wx_openid)['items'] if item['group_kind'] == 'QUALITY_RECALL')
        self.assertEqual(card['display_state'], '跟踪中')
        self.assertEqual(before, get_task_summary(actors['merchant_owner'].wx_openid))
        self.assertFalse(GoodsTransfer.objects.exists())

    def test_customer_merchant_handover_keeps_driver_owner_and_source_merchant_in_sync(self):
        self.select('MERCHANT_HANDOVER', user=self.driver)
        for user in (self.owner, self.driver):
            data = self.detail(user)['devices'][0]
            self.assertTrue(data['handover']['merchant_transfer_token'])
            self.assertIn('下级商家', data['next_step'])
            self.assertIn('出示', data['next_step'])
        self.handover.refresh_from_db()
        with self.assertRaises(QualityRecallError):
            accept_merchant_handover_by_token(user=self.merchant_users[self.direct.pk]['merchant_owner'],
                                             token=self.handover.merchant_transfer_token, receipt_note='不应接管')
        accept_merchant_handover_by_token(user=self.merchant_users[self.lower.pk]['merchant_stock'],
                                         token=self.handover.merchant_transfer_token, receipt_note='接管')
        for user in (self.owner, self.driver):
            data = self.detail(user)['devices'][0]
            self.assertIn('已接管', data['current_stage_label'])
            self.assertFalse(data['handover']['available_actions']['select_delivery_method'])
            self.assertEqual(data['handover']['merchant_transfer_token'], '')
        self.assertEqual(self.detail(self.merchant_users[self.lower.pk]['merchant_sales'])['batch_flow']['groups'][0]['device_count'], 3)
        self.assertEqual(ActivationRecord.objects.count(), 1)
        self.assertEqual(CustomerDeviceBinding.objects.get().status, 'ACTIVE')

    def test_customer_direct_ship_routes_to_factory_not_merchant_receipt(self):
        self.select('CUSTOMER_DIRECT_SHIP')
        self.assertTrue(self.detail(self.driver)['devices'][0]['handover']['available_actions']['submit_direct_shipment'])
        submit_customer_direct_shipment(user=self.driver, handover_id=self.handover.pk,
            logistics_provider='直寄物流', tracking_no='DIRECT', evidence={'storage_key': 'private/rr.jpg'})
        for user in (self.owner, self.driver):
            self.assertIn('厂家', self.detail(user)['devices'][0]['next_step'])
        self.assertEqual(self.detail(self.factory_users['factory_logistics'])['batch_flow']['groups'][0]['action'], 'RECEIVE')
        for user in self.merchant_users[self.lower.pk].values():
            self.assertFalse(any(self.rows[0].pk == device['recall_device_id'] for group in self.detail(user)['batch_flow']['groups'] for device in group['devices']))

    def test_factory_pickup_buttons_follow_customer_and_factory_responsibilities(self):
        self.select('FACTORY_PICKUP')
        for role in ('factory_chief_engineer', 'factory_sales_assistant', 'factory_logistics', 'factory_sales'):
            user = self.factory_users[role]
            actions = self.detail(user)['devices'][0]['handover']['available_actions']
            self.assertEqual(actions['accept_factory_pickup'], role in ('factory_chief_engineer', 'factory_logistics'))
        accept_factory_pickup_request(user=self.factory_users['factory_logistics'], handover_id=self.handover.pk,
                                     pickup_scheduled_at=timezone.now()+timedelta(days=1))
        for user in (self.owner, self.driver):
            actions = self.detail(user)['devices'][0]['handover']['available_actions']
            self.assertTrue(actions['submit_factory_pickup_shipment'])
            self.assertFalse(actions['select_delivery_method'])
        submit_customer_pickup_shipment(user=self.owner, handover_id=self.handover.pk,
            logistics_provider='取件物流', tracking_no='PICKUP', evidence={'storage_key': 'private/rr.jpg'})
        self.assertEqual(self.detail(self.factory_users['factory_logistics'])['batch_flow']['groups'][0]['action'], 'RECEIVE')

    def test_nine_devices_return_and_all_three_dispositions_preserve_other_roles(self):
        self.return_all()
        self.assertTrue(self.detail(self.chief)['post_disposition']['can_start'])
        self.assertFalse(self.detail(self.factory_users['factory_logistics'])['batch_flow']['groups'])
        sets = [('REPAIR_WITH_PARTS', self.rows[:3]), ('REPAIR_NO_PARTS', self.rows[3:6]), ('DISASSEMBLE', self.rows[6:])]
        for method, rows in sets:
            dispositions = start_recall_post_disposition(user=self.chief, campaign_id=self.campaign.pk,
                method=method, recall_device_ids=[row.pk for row in rows], note='角色协同验证')
            for item in dispositions:
                if method == 'REPAIR_WITH_PARTS':
                    self.assertFalse(self.detail(self.supplier_sales)['devices'][0]['post_disposition']['available_actions']['supplier_confirm'])
                    for confirmation in item.supplier_confirmations.all():
                        confirm_recall_disposition_supplier(user=self.supplier_owner, confirmation_id=confirmation.pk)
                    confirm_recall_disposition_matching(user=self.factory_users['factory_matching'], disposition_id=item.pk,
                                                         replacement_parts=[{'part_no': 'RR-PART', 'quantity': 1}])
                complete_recall_disposition_production(user=self.factory_users['factory_production'], disposition_id=item.pk,
                                                      completion_note='已检测')
                if method != 'DISASSEMBLE':
                    complete_recall_device_disposition(user=self.chief, disposition_id=item.pk, completion_note='复核通过')
        self.assert_scope(self.owner, [0])
        self.assert_scope(self.driver, [0])
        for user in self.merchant_users[self.second.pk].values():
            data = self.assert_scope(user, range(3, 6))
            self.assertEqual(data['view_context']['actionable_device_count'], 0)
        data = self.detail(self.other_supplier_owner)
        self.assertEqual([row['device_id'] for row in data['devices']], [self.devices[8].pk])
        self.assertIsNone(data['devices'][0]['handover'])
        self.assertEqual(data['devices'][0]['transport_legs'], [])
        self.assertTrue(self.detail(self.chief)['post_disposition']['can_complete_campaign'])

    def test_supplier_confirmation_access_is_per_device_and_per_organization(self):
        self.return_all()
        items = start_recall_post_disposition(user=self.chief, campaign_id=self.campaign.pk,
            method='REPAIR_WITH_PARTS', recall_device_ids=[self.rows[0].pk, self.rows[8].pk], note='不同供应商')
        for user in (self.supplier_owner, self.supplier_sales):
            data = self.assert_scope(user, [0])['devices'][0]
            self.assertEqual(data['post_disposition']['available_actions']['supplier_confirm'], user.pk == self.supplier_owner.pk)
        self.assert_scope(self.other_supplier_owner, [8])
        with self.assertRaises(QualityRecallError):
            confirm_recall_disposition_supplier(user=self.supplier_sales, confirmation_id=items[0].supplier_confirmations.get().pk)
        with self.assertRaises(QualityRecallError):
            confirm_recall_disposition_supplier(user=self.supplier_owner, confirmation_id=items[1].supplier_confirmations.get().pk)

    def test_deadline_uses_business_local_date_at_midnight(self):
        from datetime import date, datetime, timezone as datetime_timezone
        from quality_trace.services.recall_progress_service import calculate_recall_progress
        self.campaign.deadline = date(2026, 9, 8)
        self.campaign.save(update_fields=['deadline'])
        with timezone.override('Asia/Shanghai'):
            before = calculate_recall_progress(campaign_id=self.campaign.pk,
                at=datetime(2026, 9, 8, 15, 59, tzinfo=datetime_timezone.utc))
            after = calculate_recall_progress(campaign_id=self.campaign.pk,
                at=datetime(2026, 9, 8, 16, 0, tzinfo=datetime_timezone.utc))
        self.assertFalse(before.is_overdue)
        self.assertTrue(after.is_overdue)

    def test_closed_campaign_has_no_customer_or_batch_actions_and_rejects_submission(self):
        self.select('MERCHANT_HANDOVER')
        self.campaign.status = 'SUPERSEDED'
        self.campaign.save(update_fields=['status'])
        for user in (self.owner, self.driver):
            data = self.detail(user)
            self.assertEqual(data['view_context']['actionable_device_count'], 0)
            self.assertFalse(any(data['devices'][0]['handover']['available_actions'].values()))
            self.assertEqual(data['devices'][0]['handover']['merchant_transfer_token'], '')
        with self.assertRaises(QualityRecallError):
            self.select('CUSTOMER_DIRECT_SHIP')
        self.handover.refresh_from_db()
        with self.assertRaises(QualityRecallError):
            accept_merchant_handover_by_token(user=self.merchant_users[self.lower.pk]['merchant_owner'],
                                             token=self.handover.merchant_transfer_token, receipt_note='不能继续')

    def test_scan_preview_displays_exact_device_without_accepting_it(self):
        self.select('MERCHANT_HANDOVER')
        self.handover.refresh_from_db()
        path = f'recall-handovers/merchant-transfer/accept/?token={self.handover.merchant_transfer_token}'
        actors = self.merchant_users[self.lower.pk]
        for user in actors.values():
            data = self.get(user, path)
            self.assertEqual(data['sn'], self.devices[0].sn)
            self.assertTrue(data['can_accept'])
        self.handover.refresh_from_db()
        self.assertIsNone(self.handover.merchant_received_at)
        for user in (self.owner, self.chief, self.merchant_users[self.direct.pk]['merchant_owner']):
            self.get(user, path, allowed=False)
        accept_merchant_handover_by_token(user=actors['merchant_stock'], token=self.handover.merchant_transfer_token,
                                         receipt_note='核对完成')
        self.assertFalse(self.get(actors['merchant_owner'], path)['can_accept'])
        self.assertTrue(self.get(actors['merchant_sales'], path)['already_received'])
