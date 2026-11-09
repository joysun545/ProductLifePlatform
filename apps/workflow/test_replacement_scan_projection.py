"""Current LOCAL scan notices respect the viewer's existing execution scope.

The persisted broad scan recipients reproduce the current publisher's old
shape. Query/open must correct only the reader's inbox view, never complete
the real scan, other recipients, a handoff or the after-sales case.
"""
from copy import deepcopy

from django.test import TestCase
from django.utils import timezone

from account.services.auth_service import issue_access_token
from lifecycle.models import (
    AfterSalesApplication, AfterSalesServiceStationLiaison,
    ReplacementFlow, ReplacementLeg,
)
from lifecycle.services.aftersales_service import AfterSalesService as Service
import lifecycle.test_aftersales_access_basis as fixtures
from workflow.models import UserTask, WorkItem
from workflow.services.task_query import _task_groups, _task_revision


class ReplacementScanProjectionTests(TestCase):
    user = classmethod(fixtures.AfterSalesAccessBasisTests.user.__func__)
    list_url = '/pldp/api/workflow/task/list/'
    group_url = '/pldp/api/workflow/task/group/'
    summary_url = '/pldp/api/workflow/task/summary/'
    open_url = '/pldp/api/workflow/task/open/'

    @classmethod
    def setUpTestData(cls):
        fixtures.AfterSalesAccessBasisTests.setUpTestData.__func__(cls)
        cls.case.execution_stage = AfterSalesApplication.ExecutionStage.AWAITING_REPLACEMENT
        cls.case.save()
        cls.now = timezone.now()
        cls.flow = ReplacementFlow.objects.create(
            application=cls.case, protocol_version=6, mode='LOCAL',
            execution_organization=cls.merchant,
            chain=[cls.factory.pk, cls.merchant.pk],
            activation_method='VEHICLE_PLATE', first_activation_at=cls.now,
            recovery={'state': 'WAIT_PICKUP'},
        )
        cls.old = ReplacementLeg.objects.create(
            flow=cls.flow, direction='OLD', sequence=1,
            from_organization=cls.merchant, to_organization=cls.factory,
            device=cls.device,
        )
        cls.supply = ReplacementLeg.objects.create(
            flow=cls.flow, direction='SUPPLY', sequence=1,
            from_organization=cls.factory, to_organization=cls.merchant,
        )
        Service._publish_task(
            cls.case, step='replacement-scan',
            task_type='AFTERSALES_REPLACEMENT_CHANNEL',
            title='售后换新：请扫描库存新机生命周期码', summary=Service._summary(cls.case),
            recipients_openids=[cls.owner.wx_openid, cls.sales.wx_openid],
        )
        UserTask.objects.filter(workitem__uniq_key=Service._task_key(cls.case, 'replacement-scan')).update(
            state='READ', read_at=cls.now,
        )

    def header(self, user=None):
        return {'HTTP_AUTHORIZATION': 'Bearer ' + issue_access_token(user or self.owner)}

    def read(self, url, user=None, **query):
        response = self.client.get(url, query, **self.header(user))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['code'], 0, response.content)
        return response.json()['data']

    def task(self, user=None):
        return UserTask.objects.select_related('workitem').get(
            workitem__uniq_key=Service._task_key(self.case, 'replacement-scan'),
            to_openid=(user or self.owner).wx_openid,
        )

    def card(self, user=None):
        return next(row for row in self.read(self.list_url, user)['items']
                    if row['group_kind'] == 'AFTERSALES')

    def snapshot(self):
        return {
            'tasks': list(UserTask.objects.order_by('pk').values()),
            'items': list(WorkItem.objects.order_by('pk').values()),
            'cases': list(AfterSalesApplication.objects.order_by('pk').values()),
            'flows': list(ReplacementFlow.objects.order_by('pk').values()),
            'legs': list(ReplacementLeg.objects.order_by('pk').values()),
        }

    def test_owner_old_scan_flag_is_read_only_knowledge_with_correct_stage_text(self):
        before = self.snapshot()
        self.assertEqual(self.read(self.summary_url)['action_pending_count'], 0)
        card = self.card()
        self.assertFalse(card['need_action'])
        self.assertEqual(card['display_state'], '待他岗处理')
        self.assertIn('换新链路待跟进', card['title'])
        self.assertIn('现场换新待服务人员交付', card['summary'])
        self.assertNotIn('旧机待厂家取回', card['summary'])
        group = self.read(self.group_url, group_id=card['group_id'])
        child = group['items'][0]
        self.assertTrue(child['recorded_need_action'])
        self.assertFalse(child['need_action'])
        self.assertFalse(child['replacement_context']['responsible'])
        self.assertEqual(child['replacement_context']['action'], 'scan')
        self.assertEqual(child['workitem_status'], 'OPEN')
        self.assertEqual(self.snapshot(), before)

    def test_real_executor_keeps_scan_responsibility_before_service_mode_is_confirmed(self):
        self.assertEqual(self.read(self.summary_url, self.sales)['action_pending_count'], 1)
        card = self.card(self.sales)
        self.assertTrue(card['need_action'])
        self.assertEqual(card['display_state'], '待处理')
        group = self.read(self.group_url, self.sales, group_id=card['group_id'])
        scope = group['items'][0]['replacement_context']
        self.assertTrue(scope['responsible'])
        self.assertFalse(scope['can_act_now'])
        self.assertEqual(self.read(self.summary_url)['action_pending_count'], 0)

    def test_open_acknowledges_only_this_owner_and_keeps_scan_and_physical_work_open(self):
        before = self.snapshot()
        response = self.client.post(self.open_url, {'usertask_id': self.task().pk}, **self.header())
        self.assertEqual(response.status_code, 200, response.content)
        owner = self.task()
        self.assertEqual(owner.state, 'DONE')
        self.assertTrue(owner.need_action)  # Preserve the recorded recipient classification.
        self.assertEqual(owner.workitem.status, 'OPEN')
        self.assertEqual(self.task(self.sales).state, 'READ')
        after = self.snapshot()
        for key in ('items', 'cases', 'flows', 'legs'):
            self.assertEqual(after[key], before[key])
        self.assertEqual(self.read(self.summary_url, self.sales)['action_pending_count'], 1)

    def test_unread_owner_gets_attention_but_never_scan_action(self):
        UserTask.objects.filter(pk=self.task().pk).update(state='NEW', read_at=None)
        summary = self.read(self.summary_url)
        self.assertEqual((summary['unread_count'], summary['action_pending_count']), (1, 0))
        self.assertEqual(self.card()['display_state'], '待知晓')
        response = self.client.post(self.open_url, {'usertask_id': self.task().pk}, **self.header())
        self.assertEqual(response.status_code, 200, response.content)
        summary = self.read(self.summary_url)
        self.assertEqual((summary['unread_count'], summary['action_pending_count']), (0, 0))
        self.assertEqual(self.card()['display_state'], '待他岗处理')

    def test_conflicting_root_hint_stays_conservative_and_cannot_acknowledge_real_action(self):
        item = self.task().workitem
        item.payload = {**deepcopy(item.payload), 'root_organization_id': self.other_factory.pk}
        item.save(update_fields=['payload'])
        self.assertEqual(self.read(self.summary_url)['action_pending_count'], 1)
        response = self.client.post(self.open_url, {'usertask_id': self.task().pk}, **self.header())
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.task().state, 'READ')
        self.assertNotIn('replacement_context', self.card())

    def test_factory_mode_scan_is_not_reclassified_by_local_scan_policy(self):
        ReplacementFlow.objects.filter(pk=self.flow.pk).update(mode='FACTORY')
        self.assertEqual(self.read(self.summary_url)['action_pending_count'], 1)
        self.assertNotIn('aftersales_context', self.card())

    def test_notice_cannot_be_promoted_to_action_when_assignment_later_changes(self):
        UserTask.objects.filter(pk=self.task().pk).update(need_action=False)
        AfterSalesApplication.objects.filter(pk=self.case.pk).update(assigned_user=self.owner)
        self.assertEqual(self.read(self.summary_url)['action_pending_count'], 0)
        self.assertFalse(self.task().need_action)

    def test_assignment_and_service_facts_change_revision_without_revision_database_reads(self):
        original = self.read(self.summary_url)['revision']
        # QuerySet.update deliberately does not refresh application.updated_at.
        # The marker must include the loaded execution/assignment scope itself.
        AfterSalesApplication.objects.filter(pk=self.case.pk).update(assigned_user=self.plain)
        reassigned = self.read(self.summary_url)['revision']
        self.assertNotEqual(reassigned, original)
        AfterSalesApplication.objects.filter(pk=self.case.pk).update(assigned_user=self.sales)
        actor_before = self.read(self.summary_url, self.sales)['revision']
        ReplacementFlow.objects.filter(pk=self.flow.pk).update(service_mode='SHOP', service_confirmed_at=timezone.now())
        actor_after = self.read(self.summary_url, self.sales)['revision']
        self.assertNotEqual(actor_after, actor_before)
        groups = _task_groups(self.sales.wx_openid)
        with self.assertNumQueries(0):
            _task_revision(groups)

    def test_existing_valid_station_liaison_remains_in_the_unchanged_execution_scope(self):
        AfterSalesServiceStationLiaison.objects.create(application=self.station, identity=self.owner_i)
        self.assertEqual(self.read(self.summary_url)['action_pending_count'], 1)
        card = self.card()
        self.assertTrue(card['replacement_context']['responsible'])
        AfterSalesServiceStationLiaison.objects.filter(application=self.station, identity=self.owner_i).delete()
        self.assertEqual(self.read(self.summary_url)['action_pending_count'], 0)

    def test_completed_scan_does_not_claim_an_old_machine_pickup_is_pending(self):
        Service._complete_tasks(self.case, steps=['replacement-scan'], actor_openid=self.sales.wx_openid)
        ReplacementFlow.objects.filter(pk=self.flow.pk).update(swapped_at=timezone.now())
        self.assertEqual(self.read(self.summary_url, self.sales)['action_pending_count'], 0)
        owner = self.card()
        self.assertNotIn('现场换新待服务人员交付', owner['summary'])
        self.assertNotIn('旧机待厂家取回', owner['summary'])

