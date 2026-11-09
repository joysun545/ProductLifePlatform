"""Factory replacement responsibilities and read-only task projections.

The fixture mirrors the reported persisted channel notices: a closed service
case and its unfinished return/supply legs; it is not a claim that the current
HTTP flow naturally produces this exact combination. Deliberately
over-broad historical UserTask flags remain in storage so GET must correct the
current viewer's projection without rewriting physical or notification facts.
"""
from copy import deepcopy

from django.test import TestCase
from django.utils import timezone

from account.models import UserOrganization
from account.services.auth_service import issue_access_token
from lifecycle.models import AfterSalesApplication, ReplacementFlow, ReplacementLeg
from lifecycle.services.aftersales_service import AfterSalesService as Service
import lifecycle.test_aftersales_access_basis as fixtures
from workflow.models import UserTask, WorkItem
from workflow.services.task_query import (
    _task_groups, _task_revision, get_task_group, get_task_list, get_task_summary,
)


class ReplacementTaskProjectionTests(TestCase):
    user = classmethod(fixtures.AfterSalesAccessBasisTests.user.__func__)
    list_url = '/pldp/api/workflow/task/list/'
    group_url = '/pldp/api/workflow/task/group/'
    summary_url = '/pldp/api/workflow/task/summary/'
    open_url = '/pldp/api/workflow/task/open/'

    @classmethod
    def setUpTestData(cls):
        fixtures.AfterSalesAccessBasisTests.setUpTestData.__func__(cls)
        cls.logistics, cls.logistics_i = cls.user('projection-logistics', cls.factory, 'factory_logistics')
        cls.stock, cls.stock_i = cls.user('projection-stock', cls.factory, 'factory_stock')
        cls.now = timezone.now()
        cls.case.status = AfterSalesApplication.Status.CLOSED
        cls.case.execution_stage = AfterSalesApplication.ExecutionStage.CLOSED
        cls.case.closed_at = cls.now
        cls.case.closed_by = cls.af
        cls.case.save()
        cls.flow = ReplacementFlow.objects.create(
            application=cls.case, mode='LOCAL', execution_organization=cls.merchant,
            chain=[cls.factory.pk, cls.merchant.pk], activation_method='VEHICLE_PLATE',
            first_activation_at=cls.now, swapped_at=cls.now,
        )
        cls.old = ReplacementLeg.objects.create(
            flow=cls.flow, direction='OLD', sequence=1,
            from_organization=cls.merchant, to_organization=cls.factory,
            device=cls.device, status='SENT', sent_by=cls.sales, sent_at=cls.now,
        )
        cls.supply = ReplacementLeg.objects.create(
            flow=cls.flow, direction='SUPPLY', sequence=1,
            from_organization=cls.factory, to_organization=cls.merchant, status='PENDING',
        )
        recipients = [cls.af.wx_openid, cls.logistics.wx_openid, cls.stock.wx_openid]
        for leg, action, title in [
            (cls.old, 'receive', '换新交接待签收：BASIS-SN'),
            (cls.supply, 'send', '换新待补发：待发出'),
        ]:
            Service._publish_task(
                cls.case, step=f'replacement-{leg.pk}-{action}',
                task_type='AFTERSALES_REPLACEMENT_CHANNEL', title=title,
                summary=Service._summary(cls.case), recipients_openids=recipients,
                # Older OLD notices did not contain replacement_leg_id.
                payload=({'replacement_leg_id': leg.pk} if leg.direction == 'SUPPLY' else None),
            )
        UserTask.objects.filter(workitem__type='AFTERSALES_REPLACEMENT_CHANNEL').update(
            state='READ', read_at=cls.now,
        )
        # Reproduce the newer, completed close-row with an obsolete title.
        Service._publish_factory_workorder(
            cls.case, topic='factory-close-required',
            title='用户换机已完成，旧机处置已满足，待归因结案',
            summary=Service._summary(cls.case), need_action=False,
        )
        UserTask.objects.filter(workitem__type=Service.FACTORY_WORKORDER_TYPE).update(
            state='DONE', read_at=cls.now, done_at=cls.now, done_reason='SELF_ACTION',
        )

    def header(self, user=None):
        return {'HTTP_AUTHORIZATION': 'Bearer ' + issue_access_token(user or self.af)}

    def read(self, url, user=None, **query):
        response = self.client.get(url, query, **self.header(user))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['code'], 0, response.content)
        return response.json()['data']

    def card(self, user=None):
        page = self.read(self.list_url, user)
        return next(row for row in page['items'] if row.get('group_kind') == 'AFTERSALES')

    def snapshot(self):
        return {
            'tasks': list(UserTask.objects.order_by('pk').values()),
            'workitems': list(WorkItem.objects.order_by('pk').values()),
            'legs': list(ReplacementLeg.objects.order_by('pk').values()),
            'flows': list(ReplacementFlow.objects.order_by('pk').values()),
            'cases': list(AfterSalesApplication.objects.order_by('pk').values()),
        }

    def channel_tasks(self, user=None):
        return UserTask.objects.filter(
            to_openid=(user or self.af).wx_openid,
            workitem__type='AFTERSALES_REPLACEMENT_CHANNEL',
        ).order_by('pk')

    def test_read_only_closed_case_has_no_aftersales_action_but_keeps_real_follow_up(self):
        before = self.snapshot()
        summary = self.read(self.summary_url)
        self.assertEqual(summary['action_pending_count'], 0)
        self.assertEqual(summary['attention_count'], 0)
        card = self.card()
        self.assertEqual(card['action_pending_count'], 0)
        self.assertFalse(card['need_action'])
        self.assertEqual(card['display_state'], '待他岗处理')
        self.assertEqual(card['title'], '售后已结案，换新链路待跟进')
        self.assertEqual(card['aftersales_context']['status'], 'CLOSED')
        self.assertEqual(card['aftersales_context']['unfinished_replacement_leg_count'], 2)
        detail = self.read(self.group_url, group_id=card['group_id'])
        children = [row for row in detail['items'] if row['type'] == 'AFTERSALES_REPLACEMENT_CHANNEL']
        self.assertEqual(len(children), 2)
        self.assertTrue(all(row['state'] == 'READ' and not row['need_action'] for row in children))
        self.assertTrue(all(row['recorded_need_action'] for row in children))
        self.assertTrue(all(row['workitem_status'] == 'OPEN' for row in children))
        self.assertEqual(before, self.snapshot())

    def test_logistics_and_stock_keep_only_their_actual_responsibility_even_before_ready(self):
        for user, word in [(self.logistics, '待签收'), (self.stock, '待补发')]:
            with self.subTest(user=user.pk):
                summary = self.read(self.summary_url, user)
                self.assertEqual(summary['action_pending_count'], 1)
                card = self.card(user)
                self.assertEqual(card['action_pending_count'], 1)
                self.assertTrue(card['need_action'])
                self.assertIn(word, card['title'])
                group = self.read(self.group_url, user, group_id=card['group_id'])
                pending = [row for row in group['items'] if row['need_action'] and row['state'] != 'DONE']
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0]['replacement_context']['responsible'], True)
                if user.pk == self.stock.pk:
                    self.assertFalse(pending[0]['replacement_context']['can_act_now'])

    def test_open_finishes_only_the_current_observers_notifications_not_physical_or_other_tasks(self):
        before = self.snapshot()
        target = self.channel_tasks().first()
        response = self.client.post(self.open_url, {'usertask_id': target.pk}, **self.header())
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(all(row.state == 'DONE' for row in self.channel_tasks()))
        self.assertTrue(all(row.need_action for row in self.channel_tasks()))
        self.assertTrue(all(row.state == 'READ' for row in self.channel_tasks(self.logistics)))
        self.assertTrue(all(row.state == 'READ' for row in self.channel_tasks(self.stock)))
        after = self.snapshot()
        for key in ('workitems', 'legs', 'flows', 'cases'):
            self.assertEqual(after[key], before[key])
        self.assertEqual(self.read(self.summary_url)['action_pending_count'], 0)

    def test_unread_observer_notices_still_count_then_open_acknowledges_knowledge_only(self):
        self.channel_tasks().update(state='NEW', read_at=None)
        summary = self.read(self.summary_url)
        self.assertEqual(summary['unread_count'], 2)
        self.assertEqual(summary['action_pending_count'], 0)
        self.assertEqual(summary['attention_count'], 2)
        card = self.card()
        self.assertEqual(card['display_state'], '待知晓')
        response = self.client.post(self.open_url, {'usertask_id': card['entry_task_id']}, **self.header())
        self.assertEqual(response.status_code, 200, response.content)
        summary = self.read(self.summary_url)
        self.assertEqual((summary['unread_count'], summary['action_pending_count'], summary['attention_count']), (0, 0, 0))
        self.assertEqual(self.card()['display_state'], '待他岗处理')

    def test_conflicting_or_unknown_replacement_association_keeps_original_action_flags(self):
        item = self.channel_tasks().first().workitem
        payload = deepcopy(item.payload)
        payload['application_id'] = self.case.pk + 999
        item.payload = payload
        item.save(update_fields=['payload'])
        self.assertEqual(self.read(self.summary_url)['action_pending_count'], 1)
        response = self.client.post(self.open_url, {'usertask_id': self.channel_tasks().first().pk}, **self.header())
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.channel_tasks().first().state, 'READ')

    def test_real_factory_close_and_recovery_actions_are_not_cleared_or_hidden_by_case_projection(self):
        item = WorkItem.objects.get(type=Service.FACTORY_WORKORDER_TYPE, biz_id=str(self.case.pk))
        task = UserTask.objects.get(workitem=item, to_openid=self.af.wx_openid)
        item.title = '旧机待研判维修或拆机'
        item.status = 'OPEN'
        item.save(update_fields=['title', 'status'])
        task.state = 'READ'
        task.need_action = True
        task.display_title_snapshot = '旧机待研判维修或拆机'
        task.save(update_fields=['state', 'need_action', 'display_title_snapshot'])
        self.assertEqual(self.read(self.summary_url)['action_pending_count'], 1)
        card = self.card()
        self.assertIn('旧机待研判维修或拆机', card['title'])
        self.assertTrue(card['need_action'])
        self.assertEqual(card['entry_task_id'], task.pk)

    def test_role_change_updates_projection_revision_and_invalidates_an_old_sort_cursor(self):
        WorkItem.objects.create(
            domain='SYSTEM', type='UNRELATED', biz_id='unrelated', uniq_key='projection-unrelated',
            title='独立任务', action_mode='EACH_ACK', status='DONE',
        ).user_tasks.create(to_openid=self.af.wx_openid, state='DONE', need_action=False, cursor_time=self.now)
        first = self.read(self.list_url, limit=1)
        self.assertTrue(first['next_cursor'])
        before = self.read(self.summary_url)['revision']
        identity = UserOrganization.objects.get(user=self.af)
        identity.role = self.stock_i.role
        identity.save(update_fields=['role'])
        after = self.read(self.summary_url)
        self.assertEqual(after['action_pending_count'], 1)
        self.assertNotEqual(after['revision'], before)
        response = self.client.get(self.list_url, {'limit': 1, 'cursor': first['next_cursor']}, **self.header())
        self.assertEqual(response.status_code, 409, response.content)
        self.assertTrue(response.json()['data']['refresh_required'])

    def test_application_and_leg_phase_changes_change_revision_without_extra_revision_queries(self):
        before = self.read(self.summary_url)['revision']
        ReplacementLeg.objects.filter(pk=self.supply.pk).update(status='SENT', sent_at=timezone.now())
        after_leg = self.read(self.summary_url)['revision']
        self.assertNotEqual(after_leg, before)
        AfterSalesApplication.objects.filter(pk=self.case.pk).update(
            status='PROCESSING', execution_stage=AfterSalesApplication.ExecutionStage.REPAIRING,
        )
        self.assertNotEqual(self.read(self.summary_url)['revision'], after_leg)
        groups = _task_groups(self.af.wx_openid)
        with self.assertNumQueries(0):
            _task_revision(groups)

    def test_no_historical_observer_to_actor_promotion_and_unrelated_task_stays_pending(self):
        self.channel_tasks(self.stock).update(need_action=False)
        self.assertEqual(self.read(self.summary_url, self.stock)['action_pending_count'], 0)
        Service._publish_task(
            self.case, step='projection-unrelated-review', task_type='AFTERSALES_REVIEW',
            title='实际售后复核待办', summary='独立操作', recipients_openids=[self.af.wx_openid],
        )
        self.assertEqual(self.read(self.summary_url)['action_pending_count'], 1)
        self.assertIn('实际售后复核待办', self.card()['title'])

    def test_ordinary_aftersales_without_replacement_keeps_its_existing_entry_and_title_policy(self):
        application = AfterSalesApplication.objects.create(
            case_no='ORDINARY-AFTERSALES', device=self.device,
            customer_organization=self.customer, factory_organization=self.factory,
            applicant=self.customer_owner, status='PROCESSING',
        )
        Service._publish_task(
            application, step='review', task_type='AFTERSALES_REVIEW',
            title='较早的普通售后待办', summary='普通售后', recipients_openids=[self.af.wx_openid],
        )
        UserTask.objects.filter(workitem__biz_id=str(application.pk)).update(state='READ', read_at=self.now)
        Service._publish_task(
            application, step='notice', task_type='AFTERSALES_NOTICE',
            title='较新的普通售后知晓', summary='普通售后', recipients_openids=[self.af.wx_openid],
            need_action=False,
        )
        card = next(row for row in self.read(self.list_url)['items'] if row['biz_id'] == str(application.pk))
        self.assertEqual(card['title'], '较新的普通售后知晓')
        self.assertEqual(card['entry_task_type'], 'AFTERSALES_NOTICE')
        self.assertEqual(card['action_pending_count'], 1)
        self.assertNotIn('aftersales_context', card)
        self.assertNotIn('display_state', card)
