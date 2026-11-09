"""Scope boundaries, cumulative upgrade and read-only observer regression."""
import json
from django.test import TestCase
from django.utils import timezone
from account.models import Organization, OrganizationRelation
from channel.models import MerchantChannelAssignment
from lifecycle.models import Device, DeviceState, OrganizationDevice
from quality_trace.models import QualityRecallDevice, BatchQualityAffectedDevice
from quality_trace.services.case_service import QualityCaseError
from quality_trace.services.recall_service import launch_recall_campaign
from quality_trace.services.recall_task_service import publish_recall_tasks, get_recall_task_scope
from quality_trace.services.recall_notice_service import sync_recall_policy_notices
from quality_trace.services.recall_disposition_service import start_recall_post_disposition
from workflow.models import WorkItem, UserTask
from workflow.services.workflow_service import publish_workitem
from workflow.services.task_query import get_task_list, get_task_group, get_task_summary
from workflow.services.task_action import open_task
from .test_recall_batch import RecallEvidenceUploadMixin
from . import test_recall_role_interactions as fixtures


class RecallScopePolicyTests(RecallEvidenceUploadMixin, TestCase):
    make_user = classmethod(fixtures.RecallRoleInteractionTests.make_user.__func__)
    setUpTestData = classmethod(fixtures.RecallRoleInteractionTests.setUpTestData.__func__)
    get, detail, assert_scope = fixtures.RecallRoleInteractionTests.get, fixtures.RecallRoleInteractionTests.detail, fixtures.RecallRoleInteractionTests.assert_scope
    select, run_batch, return_all = fixtures.RecallRoleInteractionTests.select, fixtures.RecallRoleInteractionTests.run_batch, fixtures.RecallRoleInteractionTests.return_all

    def test_no_global_title_reason_or_count_in_scoped_pages_or_task_children(self):
        self.campaign.reason = '本批全量9台，商家库存8台、用户已激活1台'
        self.campaign.supersession_reason = '全量9台'
        self.campaign.save(update_fields=['reason', 'supersession_reason'])
        users = [self.owner, self.driver, self.factory_users['factory_sales']]
        for actors in self.merchant_users.values():
            users.extend(actors.values())
        for user in users:
            detail = self.detail(user)
            self.assertFalse(detail['viewer']['full_campaign_access'])
            self.assertIsNone(detail['progress'])
            self.assertIsNone(detail['inventory_posting'])
            scope = get_recall_task_scope(user=user, campaign_id=self.campaign.pk)
            card = next(item for item in get_task_list(user.wx_openid)['items'] if item['group_kind'] == 'QUALITY_RECALL')
            children = get_task_group(user.wx_openid, card['group_id'])
            text = json.dumps([detail, scope, children, self.get(user, 'recalls/')], ensure_ascii=False)
            for global_text in (self.campaign.title, self.campaign.reason, '全量9台'):
                self.assertNotIn(global_text, text)
            self.assertEqual(detail['device_count'], len(detail['devices']))
            self.assertEqual(scope['scope']['device_count'], len(detail['devices']))
        self.assertEqual(self.detail(self.chief)['title'], self.campaign.title)

    def test_factory_sales_only_owns_its_channel_even_when_another_channel_is_same_factory(self):
        other = Organization.objects.create(name='其他直属商家', code='POL-OTHER', organization_type=self.direct.organization_type)
        relation = OrganizationRelation.objects.create(source_organization=self.factory, target_organization=other,
                                                       relation_type='DISTRIBUTION')
        second_sales = self.make_user(self.factory, 'factory_sales', 'second-channel')
        MerchantChannelAssignment.objects.create(relation=relation, brand=MerchantChannelAssignment.objects.first().brand,
                                                  region='另一区域', assigned_by=second_sales)
        device = Device.objects.create(sn='POL-OTHER-SN', product_model=self.devices[0].product_model, batch=self.devices[0].batch)
        DeviceState.objects.create(device=device, current_organization=other, status='IN_STOCK',
                                   install_status='NOT_INSTALLED', activation_status='NOT_ACTIVATED')
        OrganizationDevice.objects.create(device=device, organization=other, status='IN_STOCK')
        membership = BatchQualityAffectedDevice.objects.create(quality_case=self.campaign.quality_case,
            device=device, sn_snapshot=device.sn)
        row = QualityRecallDevice.objects.create(campaign=self.campaign, device=device, sn_snapshot=device.sn,
            case_membership=membership, status_updated_at=timezone.now())
        self.campaign.device_count = 10
        self.campaign.save(update_fields=['device_count'])
        publish_recall_tasks(user=self.chief, campaign_id=self.campaign.pk)
        first = self.detail(self.factory_users['factory_sales'])
        second = self.detail(second_sales)
        self.assertEqual(first['device_count'], 9)
        self.assertEqual([item['id'] for item in second['devices']], [row.pk])
        self.assertEqual(second['device_count'], 1)
        self.assertEqual(self.detail(self.chief)['device_count'], 10)
        self.assertEqual(self.detail(self.merchant_users[self.direct.pk]['merchant_owner'])['device_count'], 9)
        self.get(self.factory_users['factory_sales'], f'recall-devices/{row.pk}/', allowed=False)
        self.get(second_sales, f'recall-devices/{self.rows[0].pk}/', allowed=False)
        first_notice = WorkItem.objects.get(uniq_key=f'quality-recall:{self.campaign.pk}:sales-user:{self.factory_users["factory_sales"].pk}')
        second_notice = WorkItem.objects.get(uniq_key=f'quality-recall:{self.campaign.pk}:sales-user:{second_sales.pk}')
        self.assertEqual(first_notice.payload['device_count'], 9)
        self.assertEqual(second_notice.payload['recall_device_ids'], [row.pk])
        # A merged factory arrival contains both channels. Each sales manager
        # receives only its own part, even though a full-view role receives all.
        from types import SimpleNamespace
        from quality_trace.services.recall_notice_service import publish_sales_batch_notice
        row.refresh_from_db()
        operation = SimpleNamespace(pk=999, campaign=self.campaign, campaign_id=self.campaign.pk, action='RECEIVE')
        publish_sales_batch_notice(operation=operation,
            legs=[SimpleNamespace(recall_device=item) for item in [self.rows[0], row]], actor=self.chief)
        notices = WorkItem.objects.filter(uniq_key__startswith='quality-recall-batch:999:sales-user:')
        self.assertEqual(notices.count(), 2)
        for notice in notices:
            self.assertEqual(notice.payload['device_count'], 1)
            self.assertNotIn('2台', notice.summary)
        # No channel record means no automatic whole-factory access.
        unassigned = self.make_user(self.factory, 'factory_sales', 'unassigned')
        self.get(unassigned, f'recalls/{self.campaign.pk}/', allowed=False)

    def test_production_and_matching_only_see_devices_with_their_disposition_responsibility(self):
        self.return_all()
        for role in ('factory_production', 'factory_matching'):
            self.get(self.factory_users[role], f'recalls/{self.campaign.pk}/', allowed=False)
        start_recall_post_disposition(user=self.chief, campaign_id=self.campaign.pk,
            method='REPAIR_WITH_PARTS', recall_device_ids=[self.rows[0].pk, self.rows[1].pk], note='换件')
        start_recall_post_disposition(user=self.chief, campaign_id=self.campaign.pk,
            method='REPAIR_NO_PARTS', recall_device_ids=[self.rows[3].pk], note='维修')
        matching = self.assert_scope(self.factory_users['factory_matching'], [0, 1])
        production = self.assert_scope(self.factory_users['factory_production'], [0, 1, 3])
        self.assertIsNone(matching['post_disposition'])
        self.assertIsNone(production['inventory_posting'])
        self.assertTrue(production['devices'][-1]['post_disposition']['available_actions']['production_complete'])
        self.assertFalse(matching['viewer']['full_campaign_access'])

    def test_factory_observers_receive_notice_without_device_totals_or_operations(self):
        from account.services.auth_service import issue_access_token
        for role in ('factory_admin', 'factory_manager'):
            user = self.factory_users[role]
            detail = self.detail(user)
            self.assertTrue(detail['viewer']['observer_only'])
            self.assertFalse(detail['viewer']['full_campaign_access'])
            self.assertFalse(detail['show_device_count'])
            self.assertIsNone(detail['device_count'])
            self.assertEqual(detail['devices'], [])
            self.assertEqual(detail['view_context']['stages'], [])
            self.assertIsNone(detail['inventory_posting'])
            self.get(user, f'recall-devices/{self.rows[0].pk}/', allowed=False)
            self.assertNotIn('8台', json.dumps(get_recall_task_scope(user=user, campaign_id=self.campaign.pk), ensure_ascii=False))
            for action in ('launch', 'complete', 'progress', 'batch-operations'):
                response = self.client.post(f'/pldp/api/quality-trace/recalls/{self.campaign.pk}/{action}/',
                    data='{}', content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {issue_access_token(user)}')
                self.assertEqual(response.status_code, 200)
                self.assertNotEqual(response.json()['code'], 0)
            with self.assertRaises(QualityCaseError):
                launch_recall_campaign(user=user, campaign_id=self.campaign.pk)
            tasks = UserTask.objects.filter(to_openid=user.wx_openid, workitem__biz_id=str(self.campaign.pk))
            self.assertTrue(tasks.filter(workitem__type='QUALITY_RECALL_OBSERVER_NOTICE').exists())
            self.assertFalse(tasks.filter(need_action=True).exists())

    def test_policy_backfill_is_idempotent_preserves_movements_and_does_not_close_chiefs_task(self):
        admin = self.factory_users['factory_admin']
        old = publish_workitem(domain='SYSTEM', type='QUALITY_RECALL_OVERDUE_ESCALATION',
            biz_id=str(self.campaign.pk), biz_ref=f'QualityRecallCampaign:{self.campaign.pk}',
            uniq_key='POL-LEGACY', title='全批9台待处理', summary='库存8台、已激活1台',
            recipients_openids=[admin.wx_openid, self.chief.wx_openid], need_action=True,
            action_mode='ANYONE_DO', mark_workitem_done_immediately=False)
        before = list(self.campaign.devices.values('status', 'responsibility_snapshot'))
        detail = get_task_group(admin.wx_openid, get_task_list(admin.wx_openid)['items'][0]['group_id'])
        self.assertFalse(detail['need_action'])
        self.assertEqual(get_task_summary(admin.wx_openid)['action_pending_count'], 0)
        self.assertNotIn('9台', json.dumps(detail, ensure_ascii=False))
        first = sync_recall_policy_notices(campaign=self.campaign)
        second = sync_recall_policy_notices(campaign=self.campaign)
        self.assertEqual(first['observer_action_flags_removed'], 1)
        self.assertEqual(second, {'created_recipient_count': 0, 'observer_action_flags_removed': 0})
        self.assertEqual(list(self.campaign.devices.values('status', 'responsibility_snapshot')), before)
        admin_task = old.user_tasks.get(to_openid=admin.wx_openid)
        open_task(admin.wx_openid, admin_task.pk)
        old.refresh_from_db()
        self.assertEqual(old.status, 'OPEN')
        self.assertEqual(old.user_tasks.get(to_openid=self.chief.wx_openid).state, 'NEW')
        self.assertTrue(old.user_tasks.get(to_openid=self.chief.wx_openid).need_action)
        self.assertEqual(old.user_tasks.get(to_openid=admin.wx_openid).state, 'DONE')
