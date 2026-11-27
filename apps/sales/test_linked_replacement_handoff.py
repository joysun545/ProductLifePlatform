from decimal import Decimal
from io import BytesIO
from tempfile import TemporaryDirectory
import uuid

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from PIL import Image

from account.models import Organization, OrganizationRole, SeniorManagerProfile
from account.services.auth_service import issue_access_token
from lifecycle.models import (
    ActivationRecord, AfterSalesApplication, CustomerDeviceBinding, CustomerVehicle,
    Device, DeviceState, GoodsTransfer, InstallationRecord, OrganizationDevice, QRCode,
    ReplacementFlow, ReplacementInventoryEntry,
)
from lifecycle.services.goods_transfer_service import (
    create_goods_transfer, scan_goods_transfer_device, submit_goods_transfer,
)
from lifecycle.services.goods_transfer_settlement_service import (
    submit_goods_transfer_statement, upload_goods_transfer_logistics_document,
)
from lifecycle import test_goods_transfer as transfer_fixture
from manufacturing.models import ProductionBatch
from sales import test_order_plan_material_flow as order_fixture
from sales.models import OrderPlanFinance, OrderPlanShipmentDevice, ReplacementSupplyPlan
from sales.services.order_finance_service import submit_order_finance_statement
from sales.services.order_plan_material_flow_service import (
    authorize_material_flow_shipment, confirm_material_flow_shipment_logistics,
    confirm_material_flow_shipment_outbound, confirm_material_flow_shipment_qrcode_print,
    upload_material_flow_logistics_document,
)
from sales import test_v2_fixture as order_base
from workflow.models import WorkItem


class LinkedReplacementHandoffTests(TestCase):
    """A 10-device normal order and one allocated replacement have separate custody."""

    _user_with_identity = order_base.V2OrderPlanFixture._user_with_identity
    _accepted_direct_relation = order_base.V2OrderPlanFixture._accepted_direct_relation
    _product_with_bom = order_base.V2OrderPlanFixture._product_with_bom
    _material_with_supplier = order_base.V2OrderPlanFixture._material_with_supplier
    _create_lot = order_fixture.OrderPlanMaterialFlowTests._create_lot
    _create_v2_plan = order_fixture.OrderPlanMaterialFlowTests._create_v2_plan
    _ready_production_shipment = order_fixture.OrderPlanMaterialFlowTests._ready_production_shipment

    def setUp(self):
        order_fixture.OrderPlanMaterialFlowTests.setUp(self)
        files = self.enterContext(TemporaryDirectory())
        self.enterContext(self.settings(
            ORDER_FINANCE_PRIVATE_ROOT=files + "/finance",
            ORDER_LOGISTICS_PRIVATE_ROOT=files + "/logistics",
            GOODS_TRANSFER_PRIVATE_ROOT=files + "/transfer",
            AFTERSALES_PRIVATE_ROOT=files + "/aftersales",
        ))
        self.downstream = Organization.objects.create(
            name="测试下级商家", code="B7-DOWNSTREAM",
            organization_type=self.merchant.organization_type, factory_root=self.factory,
        )
        self.downstream_owner = self._actor("b7-downstream-owner", self.downstream, "merchant_owner")
        transfer_fixture.GoodsTransferApiTests._relation(
            self.merchant_owner, self.merchant_owner_role, self.downstream_owner, self.downstream,
        )
        self.downstream_keeper = self._actor("b7-downstream-manager", self.downstream, "merchant_senior_manager")
        profile = SeniorManagerProfile.objects.create(identity=self.downstream_keeper.organization_identity)
        profile.managed_roles.add(self.merchant_stock_role)
        self.operator = self._actor("b7-installer", self.downstream, "merchant_electrician")
        self.aftersales = self._actor("b7-factory-aftersales", self.factory, "factory_aftersales")
        self.customer = Organization.objects.create(
            name="测试用户", code="B7-CUSTOMER",
            organization_type_id=OrganizationRole.objects.get(code="customer_owner").organization_type_id,
            factory_root=self.factory,
        )
        self.customer_owner = self._actor("b7-customer", self.customer, "customer_owner")
        self.stock_batch = ProductionBatch.objects.create(
            batch_no="B7-EXISTING-STOCK", product_model=self.generator, bom=self.generator_bom,
            manufacturer=self.factory, production_date="2026-11-01", quantity=4,
        )

    def _actor(self, name, org, role_code):
        role = OrganizationRole.objects.get(organization_type=org.organization_type, code=role_code)
        return self._user_with_identity(name, f"openid-{name}", org, role)

    @staticmethod
    def _auth(user):
        return {"HTTP_AUTHORIZATION": f"Bearer {issue_access_token(user)}"}

    def _get(self, user, path, *, allowed=True):
        body = self.client.get(path, **self._auth(user)).json()
        self.assertEqual(body["code"] == 0, allowed, body)
        return body.get("data")

    def _post(self, user, path, data, *, allowed=True, multipart=False):
        kwargs = {} if multipart else {"content_type": "application/json"}
        body = self.client.post(path, data, **kwargs, **self._auth(user)).json()
        self.assertEqual(body["code"] == 0, allowed, body)
        return body.get("data")

    @staticmethod
    def _image(name="evidence.png"):
        stream = BytesIO()
        Image.new("RGB", (2, 2), "white").save(stream, format="PNG")
        return SimpleUploadedFile(name, stream.getvalue(), content_type="image/png")

    def _device(self, organization, code, status="IN_STOCK"):
        device = Device.objects.create(sn=code, product_model=self.generator, batch=self.stock_batch)
        DeviceState.objects.create(device=device, current_organization=organization, status=status)
        OrganizationDevice.objects.create(device=device, organization=organization, status=status)
        QRCode.objects.create(code=code, device=device, status="ACTIVE")
        return device

    def _after(self, user, action, *, allowed=True, **data):
        return self._post(user, self.case_url + "escalation/", {"action": action, **data}, allowed=allowed)

    def _proof(self, user, name, purpose="REPAIR_RETURN_SHIPMENT_DOCUMENT", **context):
        return self._post(user, self.case_url + "media/", {
            "media_type": "IMAGE", "purpose": purpose, "file": self._image(name), **context,
        }, multipart=True)["id"]

    def _old_return(self, user, leg):
        context={"evidence_action":"SEND_REPLACEMENT_LEG","leg_id":leg.pk}
        self._after(user, "SEND_REPLACEMENT_LEG", leg_id=leg.pk,
                    device_image_ids=[self._proof(user, "old-device.png", **context,
                        evidence_kind="DEVICE",client_upload_id=str(uuid.uuid4()))],
                    logistics_document_ids=[self._proof(user, "old-waybill.png", **context,
                        evidence_kind="LOGISTICS",client_upload_id=str(uuid.uuid4()))])

    def _replacement_ready(self):
        old = self._device(self.customer, "B7-OLD", "ACTIVATED")
        self._device(self.downstream, "B7-LOCAL-SWAP")
        vehicle = CustomerVehicle.objects.create(
            customer_organization=self.customer, vehicle_no="渝A12345", normalized_vehicle_no="渝A12345",
        )
        ActivationRecord.objects.create(
            device=old, organization=self.customer, operator=self.customer_owner,
            activation_method="VEHICLE_PLATE", vehicle_no=vehicle.vehicle_no,
        )
        CustomerDeviceBinding.objects.create(
            device=old, customer_organization=self.customer, source_organization=self.downstream,
            vehicle=vehicle, vehicle_no=vehicle.vehicle_no, customer_type="FLEET",
        )
        InstallationRecord.objects.create(device=old, organization=self.downstream, operator=self.operator)
        case = AfterSalesApplication.objects.create(
            case_no="B7-AFTERSALES", device=old, customer_organization=self.customer,
            factory_organization=self.factory, applicant=self.customer_owner,
            assigned_user=self.operator, assigned_organization=self.downstream,
            dispatch_route="ORIGINAL_INSTALLER", status="PROCESSING", execution_stage="CONTACT_INSTALLER",
        )
        self.case_url = f"/pldp/api/lifecycle/after-sales/applications/{case.pk}/"
        self._proof(self.operator, "repair.png", purpose="REPAIR_EVIDENCE")
        self._after(self.operator, "REQUEST", requested="REPLACEMENT", holder="EXECUTOR", reason="无法现场修复")
        self._after(self.aftersales, "DECIDE", decision="REPLACEMENT", replacement_flow_version=3,
                    execution_mode="LOCAL", receiving_address="厂家收货地址")
        self._after(self.operator, "CONFIRM_REPLACEMENT_SERVICE", service_mode="SHOP")
        self._after(self.operator, "SCAN_SWAP", qr_text="B7-LOCAL-SWAP")
        flow = ReplacementFlow.objects.get(application=case)
        old_first, old_last = flow.legs.filter(direction="OLD").order_by("sequence")
        self._old_return(self.operator, old_first)
        self._after(self.merchant_stock_1, "RECEIVE_REPLACEMENT_LEG", leg_id=old_first.pk, confirmed=True)
        self._old_return(self.merchant_stock_1, old_last)
        self._after(self.factory_logistics_1, "FACTORY_PICKUP", confirmed=True)
        return flow

    def _ship_normal(self, order, shipment):
        authorize_material_flow_shipment(user=self.sales_user, plan_id=order.pk, shipment_id=shipment.pk)
        confirm_material_flow_shipment_qrcode_print(
            user=self.factory_stock, plan_id=order.pk, shipment_id=shipment.pk,
        )
        document, changed = upload_material_flow_logistics_document(
            user=self.factory_logistics_1, plan_id=order.pk, client_request_id="b7-normal-document",
            document=self._image(),
        )
        self.assertTrue(changed)
        confirm_material_flow_shipment_logistics(
            user=self.factory_logistics_1, plan_id=order.pk, shipment_id=shipment.pk,
            logistics_company="测试物流", tracking_no="B7-10-PLUS-1",
            logistics_document_id=document["document"]["id"],
        )
        confirm_material_flow_shipment_outbound(
            user=self.factory_stock, plan_id=order.pk, shipment_id=shipment.pk,
        )

    def _downstream_carrier(self):
        device = self._device(self.merchant, "B7-PREVIOUS-STOCK")
        actor = self.merchant_stock_1
        transfer, changed = create_goods_transfer(
            user=actor, flow_type="SHIPMENT", client_request_id="b7-downstream-carrier",
            target_account_id=self.downstream_owner.pk,
        )
        self.assertTrue(changed)
        transfer_id = transfer["id"]
        scan_goods_transfer_device(user=actor, transfer_id=transfer_id, qr_text=device.qrcode.code)
        submit_goods_transfer(user=actor, transfer_id=transfer_id)
        submit_goods_transfer_statement(
            user=actor, transfer_id=transfer_id, receivable_amount="500.00", image=self._image(),
        )
        upload_goods_transfer_logistics_document(
            user=actor, transfer_id=transfer_id, client_request_id="b7-downstream-document",
            document=self._image(),
        )
        return GoodsTransfer.objects.get(pk=transfer_id)

    def _assert_normal(self, order, shipment, ids, replacement, status):
        order.refresh_from_db()
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, status)
        self.assertEqual(order.total_quantity, 10)
        self.assertEqual(order.items.get().quantity, 10)
        self.assertEqual(shipment.items.get().quantity, 10)
        self.assertEqual(set(shipment.device_allocations.values_list("device_id", flat=True)), ids)
        self.assertFalse(OrderPlanShipmentDevice.objects.filter(device=replacement).exists())
        self.assertEqual(OrderPlanFinance.objects.get(order_plan=order).receivable_amount, Decimal("10000.00"))
        self.assertEqual(order.finance.payments.count(), 0)
        finance = self._get(self.merchant_owner, f"/pldp/api/sales/order-plans/{order.pk}/finance/")
        self.assertEqual((finance["receivable_amount"], finance["outstanding_amount"]), ("10000.00", "10000.00"))

    def _card(self, detail, plan, leg, shipment_id):
        self.assertIn("replacement_supply_items", detail)
        row, = detail["replacement_supply_items"]
        self.assertEqual((row["id"], row["plan_id"], row["application_id"], row["quantity"]),
                         (plan.items.get().pk, plan.pk, leg.flow.application_id, 1))
        self.assertEqual(row["shipment_id"], shipment_id)
        self.assertEqual(row["status"], leg.status)
        self.assertIn(f"application_id={leg.flow.application_id}", row["link"])
        return row

    def _assert_task(self, user, leg, step):
        work = WorkItem.objects.get(
            type="AFTERSALES_REPLACEMENT_CHANNEL",
            uniq_key__contains=f":replacement-{leg.pk}-{step}:",
        )
        self.assertTrue(work.user_tasks.filter(to_openid=user.wx_openid, need_action=True).exists())
        self.assertFalse(work.user_tasks.filter(to_openid=self.other_merchant_owner.wx_openid).exists())
        self.assertFalse(work.user_tasks.filter(to_openid=self.merchant_sales.wx_openid).exists())
        self.assertEqual(work.payload["root_organization_id"], self.factory.pk)
        self.assertEqual(work.payload["replacement_leg_id"], leg.pk)

    def test_linked_supply_can_reach_downstream_before_normal_goods_are_received(self):
        flow = self._replacement_ready()
        first, second = flow.legs.filter(direction="SUPPLY").order_by("sequence")
        replacement = self._device(self.factory, "B7-FACTORY-SUPPLY")
        order, _, shipment = self._ready_production_shipment(request_id="b7-normal-ten", quantity=10)
        plan = ReplacementSupplyPlan.objects.get(normal_order=order)
        self.assertEqual(plan.items.get().leg_id, first.pk)
        submit_order_finance_statement(
            user=self.factory_assistant_1, plan_id=order.pk,
            receivable_amount="10000.00", statement_file=self._image(),
        )
        self._ship_normal(order, shipment)
        ids = set(shipment.device_allocations.values_list("device_id", flat=True))
        self.assertEqual(len(ids), 10)
        sales_base = f"/pldp/api/sales/order-plans/{order.pk}/material-flow/"
        order_url = sales_base + "detail/"
        self._assert_normal(order, shipment, ids, replacement, "SHIPPED")
        self._get(self.other_merchant_owner, order_url, allowed=False)
        self._get(self.downstream_keeper, order_url, allowed=False)
        factory_card = self._card(self._get(self.factory_stock, order_url), plan, first, None)
        self.assertEqual([(a["leg_id"], a["code"]) for a in factory_card["actions"]],
                         [(first.pk, "SEND_REPLACEMENT_LEG")])
        merchant_plan = self._card(self._get(self.merchant_owner, order_url), plan, first, None)
        self.assertEqual(merchant_plan["actions"], [])

        self._after(self.factory_stock, "SEND_REPLACEMENT_LEG",
                    leg_id=first.pk, qr_text="B7-FACTORY-SUPPLY", shipment_id=shipment.pk)
        first.refresh_from_db()
        self.assertEqual((first.status, first.shipment_id, first.device_id), ("SENT", shipment.pk, replacement.pk))
        self._assert_task(self.merchant_owner, first, "receive")
        self._assert_task(self.merchant_stock_1, first, "receive")
        for actor in (self.merchant_owner, self.merchant_stock_1):
            detail = self._get(actor, order_url)
            card = self._card(detail, plan, first, shipment.pk)
            self.assertEqual([(a["leg_id"], a["code"]) for a in card["actions"]],
                             [(first.pk, "RECEIVE_REPLACEMENT_LEG")])
            self.assertEqual(detail["shipments"][0]["quantity"], 10)
        sales_detail = self._get(self.merchant_sales, order_url)
        self.assertEqual(sales_detail["replacement_supply_items"], [])
        sales_flow = self._get(self.merchant_sales, self.case_url)["replacement_flow"]
        self.assertFalse(next(leg for leg in sales_flow["legs"] if leg["id"] == first.pk)["can_receive"])
        self._after(self.merchant_sales, "RECEIVE_REPLACEMENT_LEG",
                    leg_id=first.pk, confirmed=True, allowed=False)
        self._after(self.other_merchant_owner, "RECEIVE_REPLACEMENT_LEG",
                    leg_id=first.pk, confirmed=True, allowed=False)
        self._after(self.merchant_stock_1, "SEND_REPLACEMENT_LEG",
                    leg_id=second.pk, qr_text="B7-FACTORY-SUPPLY", allowed=False)

        self._proof(self.merchant_stock_1, "receive-at-direct-merchant.png")
        self._after(self.merchant_stock_1, "RECEIVE_REPLACEMENT_LEG",
                    leg_id=first.pk, confirmed=True, receipt_note="线下已核对补发机")
        first.refresh_from_db()
        card = self._card(self._get(self.merchant_stock_1, order_url), plan, first, shipment.pk)
        self.assertEqual([(a["leg_id"], a["code"]) for a in card["actions"]],
                         [(second.pk, "SEND_REPLACEMENT_LEG")])
        self._assert_task(self.merchant_stock_1, second, "send")
        carrier = self._downstream_carrier()
        carrier_url = f"/pldp/api/lifecycle/goods-transfers/{carrier.pk}/"
        carrier_detail = self._get(self.downstream_keeper, carrier_url)
        self.assertEqual(carrier_detail["replacement_supply_items"], [])
        self.assertEqual(carrier_detail["pending_replacement_demands"], [])
        sender_detail = self._get(self.merchant_stock_1, carrier_url)
        self.assertEqual([row["leg_id"] for row in sender_detail["pending_replacement_demands"]], [second.pk])
        self._after(self.merchant_stock_1, "SEND_REPLACEMENT_LEG", leg_id=second.pk,
                    qr_text="B7-FACTORY-SUPPLY", shipment_id="G:999999", allowed=False)
        self._after(self.merchant_stock_1, "SEND_REPLACEMENT_LEG", leg_id=second.pk,
                    qr_text="B7-FACTORY-SUPPLY", shipment_id=f"G:{carrier.pk}",
                    note="已核对补发设备并完成线下转发")
        second.refresh_from_db()
        self.assertEqual((second.status, second.device_id), ("SENT", replacement.pk))
        self.assertEqual(second.logistics["shared_goods_transfer_id"], carrier.pk)
        self._assert_task(self.downstream_keeper, second, "receive")
        carrier_detail = self._get(self.downstream_keeper, carrier_url)
        downstream_card = self._card(carrier_detail, plan, second, f"G:{carrier.pk}")
        self.assertEqual(carrier_detail["item_count"], 1)
        self.assertEqual([(a["leg_id"], a["code"]) for a in downstream_card["actions"]],
                         [(second.pk, "RECEIVE_REPLACEMENT_LEG")])

        self._proof(self.downstream_keeper, "receive-at-downstream.png")
        self._after(self.downstream_keeper, "RECEIVE_REPLACEMENT_LEG",
                    leg_id=second.pk, confirmed=True, receipt_note="下级已核对并签收")
        second.refresh_from_db()
        self.assertEqual(second.received_by_id, self.downstream_keeper.pk)
        self.assertEqual(self._card(self._get(self.downstream_keeper, carrier_url),
                                   plan, second, f"G:{carrier.pk}")["actions"], [])
        self._after(self.downstream_keeper, "RECEIVE_REPLACEMENT_LEG",
                    leg_id=second.pk, confirmed=True, receipt_note="重复签收", allowed=False)
        self.assertEqual(
            OrganizationDevice.objects.filter(device=replacement, organization=self.downstream,
                                              status="IN_STOCK").count(), 1,
        )
        self.assertCountEqual(
            ReplacementInventoryEntry.objects.filter(device=replacement)
            .values_list("organization_id", "kind", "quantity"),
            [(self.merchant.pk, "SUPPLY_IN", 1), (self.merchant.pk, "SUPPLY_OUT", -1),
             (self.downstream.pk, "SUPPLY_IN", 1)],
        )
        self.assertEqual(carrier.items.count(), 1)
        self.assertFalse(carrier.items.filter(device=replacement).exists())
        self.assertIsNone(carrier.settlement.receivable_amount)
        self._assert_normal(order, shipment, ids, replacement, "SHIPPED")
        normal_receipt_task = WorkItem.objects.get(
            type="ORDER_PLAN_V2_MERCHANT_RECEIPT",
            uniq_key=f"sales:order-plan:{order.pk}:shipment:{shipment.pk}:merchant-receipt",
        )
        self.assertEqual(normal_receipt_task.status, "OPEN")
        self.assertFalse(OrganizationDevice.objects.filter(device_id__in=ids, organization=self.merchant).exists())

        self._post(self.merchant_stock_1, sales_base + "shipment-receipt/", {"shipment_id": shipment.pk})
        self._assert_normal(order, shipment, ids, replacement, "RECEIVED")
        final_card = self._card(self._get(self.merchant_stock_1, order_url), plan, first, shipment.pk)
        self.assertEqual(final_card["actions"], [])
        self.assertEqual([(row["id"], row["status"]) for row in final_card["node_legs"]],
                         [(first.pk, "RECEIVED"), (second.pk, "RECEIVED")])
        normal_receipt_task.refresh_from_db()
        self.assertEqual(normal_receipt_task.status, "DONE")
        self.assertEqual(OrganizationDevice.objects.filter(device_id__in=ids, organization=self.merchant,
                                                           status="IN_STOCK").count(), 10)
        self.assertFalse(OrganizationDevice.objects.filter(device=replacement, organization=self.merchant).exists())
