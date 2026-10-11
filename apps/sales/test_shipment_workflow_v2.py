"""End-to-end contracts for the V2 logistics / dispatch handoff.

Build stock, reserve material and finish production through the public services.
This exercises real device allocation, task recipients and accounting gates;
no shipment state is fabricated to make the happy path pass.
"""

import io
import json
import tempfile
from unittest.mock import patch

from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from account.models import Organization, OrganizationType
from account.services.auth_service import issue_access_token
from lifecycle.models import DeviceState, LifecycleEvent, OrganizationDevice
from quality_trace.models import BatchQualityAffectedDevice, BatchQualityCase
from quality_trace.services import place_temporary_hold, revoke_temporary_hold
from sales import test_order_plan_material_flow as fixture_module
from sales.models import OrderPlanMaterialPlan, OrderPlanShipment, OrderPlanLogisticsDocument
from sales.services import order_plan_material_flow_service as flow
from sales.test_v2_fixture import V2OrderPlanFixture
from workflow.models import UserTask, WorkItem


class ShipmentWorkflowV2Tests(V2OrderPlanFixture):
    setUp = fixture_module.OrderPlanMaterialFlowTests.setUp
    _create_lot = fixture_module.OrderPlanMaterialFlowTests._create_lot
    _create_v2_plan = fixture_module.OrderPlanMaterialFlowTests._create_v2_plan
    _ready_production_shipment = fixture_module.OrderPlanMaterialFlowTests._ready_production_shipment

    def _printed_shipment(self, request_id="shipment-v2", quantity=5):
        plan, subplan, shipment = self._ready_production_shipment(
            request_id=request_id, quantity=quantity
        )
        self._print(plan, shipment)
        return plan, subplan, shipment

    def _print(self, plan, shipment):
        flow.authorize_material_flow_shipment(
            user=self.sales_user, plan_id=plan.pk, shipment_id=shipment.pk
        )
        flow.confirm_material_flow_shipment_qrcode_print(
            user=self.factory_stock, plan_id=plan.pk, shipment_id=shipment.pk
        )
        shipment.refresh_from_db()

    def _split_shipments(self, request_id="split-logistics", print_second=True):
        plan, subplan, first = self._ready_production_shipment(
            request_id=request_id, quantity=5
        )
        flow.create_material_flow_shipment(
            user=self.sales_user,
            plan_id=plan.pk,
            client_request_id=request_id + "-split",
            production_subplan_id=subplan.pk,
            items=[{
                "production_subplan_item_id": subplan.items.get().pk,
                "quantity": 2,
            }],
        )
        first.refresh_from_db()
        second = OrderPlanShipment.objects.get(
            order_plan=plan, client_request_id=request_id + "-split"
        )
        self._print(plan, first)
        if print_second:
            self._print(plan, second)
        return plan, first, second

    def _logistics(self, plan, shipment, **extra):
        return flow.confirm_material_flow_shipment_logistics(
            user=self.factory_logistics_1,
            plan_id=plan.pk,
            shipment_id=shipment.pk,
            logistics_company="测试承运公司",
            tracking_no="WAYBILL-001",
            **extra,
        )

    def _batch_logistics(self, plan, shipments, **extra):
        return flow.confirm_material_flow_shipments_logistics(
            user=self.factory_logistics_1,
            plan_id=plan.pk,
            shipment_ids=[row.pk for row in shipments],
            logistics_company="测试承运公司",
            tracking_no="WAYBILL-001",
            **extra,
        )

    def _ship(self, plan, shipment):
        return flow.confirm_material_flow_shipment_outbound(
            user=self.factory_stock, plan_id=plan.pk, shipment_id=shipment.pk
        )

    def _receipt(self, plan, shipment, user=None):
        return flow.confirm_material_flow_merchant_receipt(
            user=user or self.merchant_owner,
            plan_id=plan.pk,
            shipment_id=shipment.pk,
        )

    def _workitem(self, plan, shipment, suffix):
        return WorkItem.objects.get(
            uniq_key=f"sales:order-plan:{plan.pk}:shipment:{shipment.pk}:{suffix}"
        )

    def _upload_document(self, plan, request_id="document-001"):
        directory = tempfile.TemporaryDirectory(prefix="pldp-logistics-test-")
        self.addCleanup(directory.cleanup)
        field = OrderPlanLogisticsDocument._meta.get_field("document_file")
        storage_patch = patch.object(field, "storage", FileSystemStorage(location=directory.name))
        storage_patch.start()
        self.addCleanup(storage_patch.stop)
        buffer = io.BytesIO()
        Image.new("RGB", (4, 4), color="white").save(buffer, format="PNG")
        response = self.client.post(
            f"/pldp/api/sales/order-plans/{plan.pk}/material-flow/shipment-logistics-document/",
            {
                "client_request_id": request_id,
                "document": SimpleUploadedFile("waybill.png", buffer.getvalue(), content_type="image/png"),
            },
            HTTP_AUTHORIZATION=self.factory_logistics_1_authorization,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0, response.content)
        result = response.json()["data"]
        self.assertTrue(result["changed"])
        return OrderPlanLogisticsDocument.objects.get(pk=result["document"]["id"])

    def test_single_batch_has_distinct_outbound_and_shipped_facts(self):
        plan, _, shipment = self._printed_shipment()
        self.assertEqual(shipment.status, OrderPlanShipment.Status.AUTHORIZED)
        device_ids = list(shipment.device_allocations.values_list("device_id", flat=True))
        with self.assertRaises(flow.OrderPlanMaterialFlowError):
            self._ship(plan, shipment)
        _, changed = self._logistics(plan, shipment)
        self.assertTrue(changed)
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, OrderPlanShipment.Status.OUTBOUNDED)
        self.assertEqual(shipment.get_status_display(), "已出库待发货")
        self.assertIsNotNone(shipment.logistics_confirmed_at)
        self.assertIsNone(shipment.shipped_at)
        self.assertIsNone(shipment.logistics_document_id)
        self.assertFalse(LifecycleEvent.objects.filter(device_id__in=device_ids, event_type="SHIP").exists())
        self.assertFalse(OrganizationDevice.objects.filter(device_id__in=device_ids, organization=self.merchant).exists())
        with self.assertRaises(flow.OrderPlanMaterialFlowError):
            self._receipt(plan, shipment)
        stock_task = self._workitem(plan, shipment, "stock-outbound")
        self.assertIn("发货", stock_task.title)
        self.assertNotIn("出库发货", stock_task.title)

        _, changed = self._ship(plan, shipment)
        self.assertTrue(changed)
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, OrderPlanShipment.Status.SHIPPED)
        self.assertEqual(shipment.shipped_by_id, self.factory_stock.pk)
        self.assertIsNotNone(shipment.shipped_at)
        self.assertEqual(LifecycleEvent.objects.filter(device_id__in=device_ids, event_type="SHIP").count(), 5)
        self.assertEqual(DeviceState.objects.filter(device_id__in=device_ids, current_organization=self.factory).count(), 5)
        self.assertFalse(OrganizationDevice.objects.filter(device_id__in=device_ids, organization=self.merchant).exists())

    def test_shipped_notice_and_receipt_action_have_correct_distinct_recipients(self):
        plan, _, shipment = self._printed_shipment()
        self._logistics(plan, shipment)
        assistant_tasks_before = set(UserTask.objects.filter(
            to_openid__in=[self.factory_assistant_1.wx_openid, self.factory_assistant_2.wx_openid],
            workitem__biz_ref=f"sales.OrderPlan:{plan.pk}", need_action=True,
        ).values_list("pk", flat=True))
        self._ship(plan, shipment)
        notice = self._workitem(plan, shipment, "shipped-notice")
        self.assertEqual(notice.type, "ORDER_PLAN_V2_SHIPPED_NOTICE")
        merchant_openids = {
            user.wx_openid for user in (
                self.merchant_owner, self.merchant_sales, self.merchant_stock_1, self.merchant_stock_2
            )
        }
        merchant_receipt_openids = {
            user.wx_openid for user in (
                self.merchant_owner, self.merchant_stock_1, self.merchant_stock_2
            )
        }
        self.assertSetEqual(set(notice.user_tasks.values_list("to_openid", flat=True)), merchant_openids | {self.sales_user.wx_openid})
        self.assertFalse(notice.user_tasks.filter(need_action=True).exists())
        receipt = self._workitem(plan, shipment, "merchant-receipt")
        self.assertEqual(receipt.action_mode, "ANYONE_DO")
        self.assertSetEqual(set(receipt.user_tasks.filter(need_action=True).values_list("to_openid", flat=True)), merchant_receipt_openids)
        self.assertSetEqual(set(UserTask.objects.filter(
            to_openid__in=[self.factory_assistant_1.wx_openid, self.factory_assistant_2.wx_openid],
            workitem__biz_ref=f"sales.OrderPlan:{plan.pk}", need_action=True,
        ).values_list("pk", flat=True)), assistant_tasks_before)

        _, changed = self._receipt(plan, shipment, self.merchant_stock_1)
        self.assertTrue(changed)
        self.assertFalse(receipt.user_tasks.exclude(state="DONE").exists())
        self.assertEqual(receipt.user_tasks.get(to_openid=self.merchant_owner.wx_openid).done_reason, "OTHER_ACTION")
        self.assertEqual(receipt.user_tasks.get(to_openid=self.merchant_stock_1.wx_openid).done_reason, "SELF_ACTION")
        shipment.refresh_from_db()
        self.assertEqual(shipment.received_by_id, self.merchant_stock_1.pk)
        self.assertEqual(OrganizationDevice.objects.filter(
            device_id__in=shipment.device_allocations.values("device_id"), organization=self.merchant, status="IN_STOCK",
        ).count(), 5)
        events_before = LifecycleEvent.objects.count()
        _, changed = self._receipt(plan, shipment, self.merchant_owner)
        self.assertFalse(changed)
        self.assertEqual(LifecycleEvent.objects.count(), events_before)

    def test_multiple_batches_share_document_but_retain_independent_receipt_and_accounting(self):
        plan, first, second = self._split_shipments()
        document = self._upload_document(plan)
        _, changed = self._batch_logistics(plan, [first, second], logistics_document_id=document.pk)
        self.assertTrue(changed)
        for shipment in (first, second):
            shipment.refresh_from_db()
            self.assertEqual(shipment.status, OrderPlanShipment.Status.OUTBOUNDED)
            self.assertEqual(shipment.logistics_company, "测试承运公司")
            self.assertEqual(shipment.tracking_no, "WAYBILL-001")
            self.assertEqual(shipment.logistics_document_id, document.pk)
            self.assertEqual(shipment.logistics_confirmed_by_id, self.factory_logistics_1.pk)
            self._ship(plan, shipment)
        self.assertEqual(OrderPlanLogisticsDocument.objects.filter(order_plan=plan).count(), 1)
        first_devices = set(first.device_allocations.values_list("device_id", flat=True))
        second_devices = set(second.device_allocations.values_list("device_id", flat=True))
        self.assertFalse(first_devices & second_devices)
        self.assertEqual(len(first_devices | second_devices), 5)
        with self.assertRaises(flow.OrderPlanMaterialFlowError):
            self._receipt(plan, first, self.merchant_sales)
        self._receipt(plan, first, self.merchant_stock_1)
        second.refresh_from_db()
        self.assertEqual(second.status, OrderPlanShipment.Status.SHIPPED)
        self.assertFalse(OrganizationDevice.objects.filter(device_id__in=second_devices, organization=self.merchant).exists())
        self.assertFalse(WorkItem.objects.filter(uniq_key=f"sales:order-plan:{plan.pk}:accounting-confirm").exists())
        with self.assertRaises(flow.OrderPlanMaterialFlowError):
            flow.confirm_material_flow_accounting(user=self.factory_assistant_1, plan_id=plan.pk)
        self._receipt(plan, second, self.merchant_owner)
        _, changed = flow.confirm_material_flow_accounting(user=self.factory_assistant_1, plan_id=plan.pk)
        self.assertTrue(changed)
        plan.refresh_from_db()
        self.assertEqual(plan.accounted_by_id, self.factory_assistant_1.pk)
        self.assertIsNotNone(plan.accounted_at)

    def test_multiple_batches_can_use_different_waybills(self):
        plan, first, second = self._split_shipments()
        document = self._upload_document(plan)
        self._logistics(plan, first, logistics_document_id=document.pk)
        flow.confirm_material_flow_shipment_logistics(
            user=self.factory_logistics_1, plan_id=plan.pk, shipment_id=second.pk,
            logistics_company="另一承运公司", tracking_no="WAYBILL-002",
        )
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.logistics_document_id, document.pk)
        self.assertIsNone(second.logistics_document_id)
        self.assertEqual(first.tracking_no, "WAYBILL-001")
        self.assertEqual(second.tracking_no, "WAYBILL-002")

    def test_12_and_38_production_subplans_can_ship_together_with_exact_device_trace(self):
        self._create_lot(12_000, suffix="TWELVE")
        self._create_lot(38_000, suffix="THIRTY-EIGHT")
        plan = self._create_v2_plan(request_id="real-12-and-38-subplans", quantity=50)
        material_plan = OrderPlanMaterialPlan.objects.get(order_plan=plan)
        subplans = list(material_plan.production_subplans.order_by("sequence_no"))
        self.assertEqual([row.planned_total_quantity for row in subplans], [12, 38])
        flow.warehouse_review_material_plan(user=self.material_keeper, plan_id=plan.pk)
        flow.release_material_ready_for_production(user=self.material_keeper, plan_id=plan.pk)
        shipments = []
        for subplan in subplans:
            flow.confirm_material_flow_production(
                user=self.factory_production, plan_id=plan.pk, subplan_id=subplan.pk,
            )
            shipment = OrderPlanShipment.objects.get(order_plan=plan, production_subplan=subplan)
            self._print(plan, shipment)
            shipments.append(shipment)
        self._batch_logistics(plan, shipments)
        for shipment, quantity in zip(shipments, (12, 38)):
            shipment.refresh_from_db()
            self.assertEqual(shipment.status, OrderPlanShipment.Status.OUTBOUNDED)
            self.assertEqual(shipment.device_allocations.count(), quantity)
            self._ship(plan, shipment)
        self._receipt(plan, shipments[0])
        shipments[1].refresh_from_db()
        self.assertEqual(shipments[1].status, OrderPlanShipment.Status.SHIPPED)
        self.assertEqual(DeviceState.objects.filter(
            device_id__in=shipments[1].device_allocations.values("device_id"), current_organization=self.factory,
        ).count(), 38)
        with self.assertRaises(flow.OrderPlanMaterialFlowError):
            flow.confirm_material_flow_accounting(user=self.factory_assistant_1, plan_id=plan.pk)

    def test_invalid_bulk_selections_are_rejected_without_state_changes(self):
        plan, _, shipment = self._printed_shipment()
        for selection in ([], [shipment.pk, shipment.pk], [True], [1.5], ["bad"], str(shipment.pk)):
            with self.subTest(selection=selection):
                with self.assertRaises(flow.OrderPlanMaterialFlowError):
                    flow.confirm_material_flow_shipments_logistics(
                        user=self.factory_logistics_1, plan_id=plan.pk, shipment_ids=selection,
                        logistics_company="测试承运公司", tracking_no="WAYBILL-001",
                    )
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, OrderPlanShipment.Status.AUTHORIZED)
        self.assertIsNone(shipment.logistics_confirmed_at)

    def test_quality_hold_blocks_entire_logistics_batch_and_is_rechecked_before_dispatch(self):
        plan, first, second = self._split_shipments()
        device = second.device_allocations.select_related("device__batch").first().device
        quality_case = BatchQualityCase.objects.create(
            case_no="V2-SHIPMENT-QUALITY-HOLD", factory_organization=self.factory,
            scope_type="SHIPMENT_REGRESSION", scope_key=str(second.pk),
            reason="出库前检查", opened_by=self.factory_chief_engineer,
        )
        BatchQualityAffectedDevice.objects.create(
            quality_case=quality_case, device=device, sn_snapshot=device.sn,
            production_batch_snapshot=device.batch.batch_no,
        )
        hold = place_temporary_hold(
            user=self.factory_chief_engineer, case_id=quality_case.pk,
            reason="等待质量检查", duration_hours=24,
        )
        with self.assertRaises(flow.OrderPlanMaterialFlowError):
            self._batch_logistics(plan, [first, second])
        for shipment in (first, second):
            shipment.refresh_from_db()
            self.assertEqual(shipment.status, OrderPlanShipment.Status.AUTHORIZED)
            self.assertIsNone(shipment.logistics_confirmed_at)
            self.assertEqual(self._workitem(plan, shipment, "logistics-confirm").status, "OPEN")
        revoke_temporary_hold(
            user=self.factory_chief_engineer, action_id=hold.pk, reason="检查通过",
        )
        self._batch_logistics(plan, [first, second])
        place_temporary_hold(
            user=self.factory_chief_engineer, case_id=quality_case.pk,
            reason="发货前新增质量问题", duration_hours=24,
        )
        with self.assertRaises(flow.OrderPlanMaterialFlowError):
            self._ship(plan, second)
        second.refresh_from_db()
        self.assertEqual(second.status, OrderPlanShipment.Status.OUTBOUNDED)
        self.assertIsNone(second.shipped_at)

    def test_batch_validation_is_atomic_when_one_batch_has_not_been_printed(self):
        plan, first, second = self._split_shipments(print_second=False)
        task_ids = set(WorkItem.objects.values_list("pk", flat=True))
        with self.assertRaises(flow.OrderPlanMaterialFlowError):
            self._batch_logistics(plan, [first, second])
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, OrderPlanShipment.Status.AUTHORIZED)
        self.assertEqual(second.status, OrderPlanShipment.Status.PLANNED)
        self.assertIsNone(first.logistics_confirmed_at)
        self.assertEqual(first.logistics_company, "")
        self.assertSetEqual(set(WorkItem.objects.values_list("pk", flat=True)), task_ids)
        self.assertEqual(self._workitem(plan, first, "logistics-confirm").status, "OPEN")

    def test_cancelled_pre_logistics_stock_task_is_reused_after_real_outbound(self):
        plan, _, shipment = self._printed_shipment()
        retired = WorkItem.objects.create(
            domain="SALES", type="ORDER_PLAN_V2_STOCK_OUTBOUND", biz_id=plan.plan_code,
            biz_ref=f"sales.OrderPlan:{plan.pk}",
            uniq_key=f"sales:order-plan:{plan.pk}:shipment:{shipment.pk}:stock-outbound",
            title="旧出库任务", status="CANCELLED", action_mode="ANYONE_DO",
            done_at=shipment.qrcode_printed_at,
        )
        retired_task = UserTask.objects.create(
            workitem=retired, to_openid=self.factory_stock.wx_openid,
            need_action=False, state="DONE", done_reason="OTHER_ACTION",
            done_at=shipment.qrcode_printed_at,
        )
        self._logistics(plan, shipment)
        reopened = self._workitem(plan, shipment, "stock-outbound")
        self.assertEqual(reopened.pk, retired.pk)
        self.assertEqual(reopened.status, "OPEN")
        self.assertIsNone(reopened.done_at)
        retired_task.refresh_from_db()
        self.assertTrue(retired_task.need_action)
        self.assertNotEqual(retired_task.state, "DONE")
        _, changed = self._ship(plan, shipment)
        self.assertTrue(changed)
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, OrderPlanShipment.Status.SHIPPED)
        reopened.refresh_from_db()
        self.assertEqual(reopened.status, "DONE")

    def test_same_batch_request_is_idempotent_and_conflicting_retry_is_atomic(self):
        plan, first, second = self._split_shipments()
        self._logistics(plan, first)
        task_count = WorkItem.objects.count()
        _, changed = self._logistics(plan, first)
        self.assertFalse(changed)
        self.assertEqual(WorkItem.objects.count(), task_count)
        with self.assertRaises(flow.OrderPlanMaterialFlowError):
            flow.confirm_material_flow_shipments_logistics(
                user=self.factory_logistics_1, plan_id=plan.pk,
                shipment_ids=[second.pk, first.pk], logistics_company="篡改物流", tracking_no="CHANGED",
            )
        second.refresh_from_db()
        first.refresh_from_db()
        self.assertEqual(second.status, OrderPlanShipment.Status.AUTHORIZED)
        self.assertIsNone(second.logistics_confirmed_at)
        self.assertEqual(first.tracking_no, "WAYBILL-001")
        self._batch_logistics(plan, [first, second])
        task_count = WorkItem.objects.count()
        _, changed = self._batch_logistics(plan, [first, second])
        self.assertFalse(changed)
        self.assertEqual(WorkItem.objects.count(), task_count)
        self._ship(plan, first)
        event_count = LifecycleEvent.objects.count()
        _, changed = self._ship(plan, first)
        self.assertFalse(changed)
        self.assertEqual(LifecycleEvent.objects.count(), event_count)

    def test_wrong_roles_and_other_factory_cannot_confirm_logistics_or_dispatch(self):
        plan, _, shipment = self._printed_shipment()
        foreign_factory = Organization.objects.create(
            name="其他厂家", code="SHIPMENT-OTHER-FACTORY",
            organization_type=OrganizationType.objects.get(code="OWNER"),
        )
        foreign_logistics = self._user_with_identity(
            "foreign-logistics", "openid-foreign-logistics", foreign_factory, self.factory_logistics_role,
        )
        for user in (self.factory_stock, self.sales_user, self.merchant_owner, foreign_logistics):
            with self.subTest(user=user.username):
                with self.assertRaises(flow.OrderPlanMaterialFlowError):
                    flow.confirm_material_flow_shipments_logistics(
                        user=user, plan_id=plan.pk, shipment_ids=[shipment.pk],
                        logistics_company="非法物流", tracking_no="FORBIDDEN",
                    )
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, OrderPlanShipment.Status.AUTHORIZED)
        self._logistics(plan, shipment)
        with self.assertRaises(flow.OrderPlanMaterialFlowError):
            flow.confirm_material_flow_shipment_outbound(
                user=self.factory_logistics_1, plan_id=plan.pk, shipment_id=shipment.pk,
            )
        with self.assertRaises(flow.OrderPlanMaterialFlowError):
            self._receipt(plan, shipment, self.other_merchant_owner)

    def test_cross_order_batch_ids_and_document_cannot_be_mixed(self):
        plan, _, shipment = self._printed_shipment(request_id="first-order")
        other_plan, _, other_shipment = self._printed_shipment(request_id="second-order")
        with self.assertRaises(flow.OrderPlanMaterialFlowError):
            self._batch_logistics(plan, [shipment, other_shipment])
        shipment.refresh_from_db()
        self.assertIsNone(shipment.logistics_confirmed_at)
        foreign_document = self._upload_document(other_plan)
        with self.assertRaises(flow.OrderPlanMaterialFlowError):
            self._logistics(plan, shipment, logistics_document_id=foreign_document.pk)
        shipment.refresh_from_db()
        self.assertIsNone(shipment.logistics_document_id)
        self.assertEqual(shipment.status, OrderPlanShipment.Status.AUTHORIZED)

    def test_bulk_http_api_requires_login_and_dispatches_json_batch(self):
        plan, first, second = self._split_shipments()
        url = f"/pldp/api/sales/order-plans/{plan.pk}/material-flow/shipment-logistics-batch-confirm/"
        body = {"shipment_ids": [first.pk, second.pk], "logistics_company": "测试承运公司", "tracking_no": "WAYBILL-001"}
        response = self.client.post(url, json.dumps(body), content_type="application/json")
        self.assertEqual(response.status_code, 401)
        response = self.client.post(
            url, json.dumps(body), content_type="application/json", HTTP_AUTHORIZATION=self.factory_logistics_1_authorization,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0, response.content)
        self.assertEqual(OrderPlanShipment.objects.filter(order_plan=plan, status=OrderPlanShipment.Status.OUTBOUNDED).count(), 2)

    def test_document_download_is_authenticated_and_scoped_to_order(self):
        plan, _, shipment = self._printed_shipment()
        document = self._upload_document(plan)
        self._logistics(plan, shipment, logistics_document_id=document.pk)
        url = f"/pldp/api/sales/order-plans/{plan.pk}/material-flow/shipment-logistics-documents/{document.pk}/file/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 401)
        response = self.client.get(url, HTTP_AUTHORIZATION=self.factory_logistics_1_authorization)
        self.assertEqual(response.status_code, 200)
        self.assertIn("image/png", response["Content-Type"])
        content = b"".join(response.streaming_content)
        self.assertTrue(content.startswith(b"\x89PNG\r\n\x1a\n"))
        with Image.open(io.BytesIO(content)) as downloaded:
            self.assertEqual(downloaded.size, (4, 4))
            downloaded.verify()
        response = self.client.get(url, HTTP_AUTHORIZATION=f"Bearer {issue_access_token(self.other_merchant_owner)}")
        self.assertFalse(response.status_code == 200 and response.get("Content-Type", "").startswith("image/"))
        if response.status_code == 200:
            self.assertNotEqual(response.json()["code"], 0)

    def test_supplier_with_legitimate_order_access_cannot_see_logistics_evidence(self):
        plan = self._create_v2_plan(request_id="supplier-document-scope", quantity=5)
        flow.warehouse_review_material_plan(user=self.material_keeper, plan_id=plan.pk)
        flow.confirm_supplier_material_delivery(user=self.supplier_owner, plan_id=plan.pk)
        self._create_lot(5_000, suffix="SUPPLIER-DOCUMENT")
        flow.refresh_material_reservations_for_stock(stock=self.material_stock, actor=self.material_keeper)
        flow.release_material_ready_for_production(user=self.material_keeper, plan_id=plan.pk)
        subplan = OrderPlanMaterialPlan.objects.get(order_plan=plan).production_subplans.get()
        flow.confirm_material_flow_production(user=self.factory_production, plan_id=plan.pk, subplan_id=subplan.pk)
        shipment = OrderPlanShipment.objects.get(order_plan=plan, production_subplan=subplan)
        self._print(plan, shipment)
        document = self._upload_document(plan)
        self._logistics(plan, shipment, logistics_document_id=document.pk)
        detail = flow.get_material_flow_detail(user=self.supplier_owner, plan_id=plan.pk)
        self.assertEqual(detail["plan"]["id"], plan.pk)
        self.assertIsNone(detail["shipments"][0]["logistics_document"])
        self.assertEqual(detail["qrcode_batches"], [])
        response = self.client.get(
            f"/pldp/api/sales/order-plans/{plan.pk}/material-flow/shipment-logistics-documents/{document.pk}/file/",
            HTTP_AUTHORIZATION=self.supplier_owner_authorization,
        )
        self.assertNotEqual(response.json()["code"], 0)
