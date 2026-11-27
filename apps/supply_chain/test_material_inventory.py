"""Physical balances, access boundaries, replay safety and old-flow isolation."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from time import sleep
from uuid import uuid4

from django.db import IntegrityError, close_old_connections, transaction
from django.test import TestCase, TransactionTestCase

from account.models import Organization, OrganizationRelation, OrganizationRole, OrganizationType, User, UserOrganization
from account.services.auth_service import issue_access_token
from product.models import ProductCategory
from supply_chain.models import (
    FactoryMaterialStock, FactoryMaterialLot, Material, MaterialSpec, MaterialSpecSupplier,
    MaterialWarehouseDocument, MaterialWarrantyCase, SupplierMaterialBatch, SupplierSalesResponsibility,
)
from supply_chain.services.material_inventory_service import MaterialInventoryError, submit_movement
from workflow.models import UserTask, WorkItem


ROOT = "/pldp/api/supply-chain/material-inventory/"


class MaterialInventoryTests(TestCase):
    def setUp(self):
        self.factory = self.org("factory-a", "OWNER")
        self.other_factory = self.org("factory-b", "OWNER")
        self.supplier = self.org("supplier-a", "SUPPLIER")
        self.other_supplier = self.org("supplier-b", "SUPPLIER")
        self.other_factory_supplier = self.org("supplier-c", "SUPPLIER")
        for factory, supplier in (
            (self.factory, self.supplier),
            (self.factory, self.other_supplier),
            (self.other_factory, self.other_factory_supplier),
        ):
            OrganizationRelation.objects.create(source_organization=factory, target_organization=supplier, relation_type="SUPPLY")
        self.keeper = self.user("keeper", "factory_material_stock", self.factory)
        self.keeper2 = self.user("keeper2", "factory_material_stock", self.other_factory)
        self.owner = self.user("owner", "supplier_owner", self.supplier)
        self.other_owner = self.user("owner2", "supplier_owner", self.other_supplier)
        self.sales = self.user("sales", "supplier_sales", self.supplier)
        self.sales2 = self.user("sales2", "supplier_sales", self.supplier)
        self.category = ProductCategory.objects.create(code="MI-CAT", name="测试品类")
        self.material = Material.objects.create(code="MI-MAT", name="控制器")
        self.material.product_categories.add(self.category)
        self.material2 = Material.objects.create(code="MI-MAT-2", name="电机")
        self.material2.product_categories.add(self.category)
        self.spec = MaterialSpec.objects.create(material=self.material, code="MI-SPEC", name="规格一")
        self.spec2 = MaterialSpec.objects.create(material=self.material2, code="MI-SPEC-2", name="规格二")
        for spec, supplier in ((self.spec, self.supplier), (self.spec2, self.supplier), (self.spec, self.other_supplier)):
            MaterialSpecSupplier.objects.create(material_spec=spec, supplier=supplier)

    def org(self, code, kind):
        return Organization.objects.create(code="MI-" + code, name=code, organization_type=OrganizationType.objects.get(code=kind))

    def user(self, name, role, org):
        user = User.objects.create_user(username="mi-" + name, wx_openid="mi-openid-" + name)
        UserOrganization.objects.create(user=user, role=OrganizationRole.objects.get(code=role), organization=org)
        return user

    def auth(self, user):
        return {"HTTP_AUTHORIZATION": "Bearer " + issue_access_token(user)}

    def get(self, path="", user=None, data=None):
        return self.client.get(ROOT + path, data=data or {}, **self.auth(user or self.keeper)).json()

    def post(self, payload, user=None):
        return self.client.post(ROOT + "documents/", json.dumps(payload), content_type="application/json", **self.auth(user or self.keeper)).json()

    def payload(self, **changes):
        data = {"kind": "RECEIPT", "expected_factory_id": self.factory.pk, "request_id": str(uuid4()), "quantity": "10", "supplier_id": self.supplier.pk, "material_spec_id": self.spec.pk, "unit": "件"}
        data.update(changes)
        return data

    def receive(self, user=None, **changes):
        result = self.post(self.payload(**changes), user=user)
        self.assertEqual(result["code"], 0, result)
        return FactoryMaterialLot.objects.get(batch_code=result["data"]["document"]["batch_code"])

    def move(self, kind, **changes):
        return self.post(self.payload(kind=kind, **changes))

    def test_every_actual_receipt_gets_new_lot_and_more_than_two_is_allowed(self):
        codes = [self.receive(supplier_batch_code="VENDOR-SAME").batch_code for _ in range(4)]
        self.assertEqual(len(set(codes)), 4)
        result = self.get()["data"]["items"][0]
        self.assertEqual((result["on_hand"], result["available"], result["active_lot_count"]), ("40", "40", 4))
        self.assertEqual(FactoryMaterialStock.objects.count(), 1)
        self.assertEqual(SupplierMaterialBatch.objects.count(), 0)

    def test_decimal_quantities_have_no_accumulating_rounding_error(self):
        lot = self.receive(quantity="0.3", unit="千克")
        for _ in range(3):
            self.assertEqual(self.move("SUPPLIER_RETURN", lot_id=lot.pk, quantity="0.1")["code"], 0)
        lot.refresh_from_db()
        self.assertEqual(lot.on_hand_milli, 0)
        detail = self.get(f"{lot.stock_id}/")["data"]
        self.assertEqual(detail["items"], [])
        self.assertEqual(detail["stock"]["on_hand"], "0")
        self.assertEqual(len(self.get(f"{lot.stock_id}/", data={"include_empty": "1"})["data"]["items"]), 1)

    def test_idempotent_receipt_and_conflicting_replay(self):
        payload = self.payload()
        first, again = self.post(payload), self.post(payload)
        self.assertEqual(first["code"], 0)
        self.assertTrue(again["data"]["already_applied"])
        self.assertEqual(first["data"]["document"], again["data"]["document"])
        changed = self.post({**payload, "quantity": "11"})
        self.assertEqual(changed["code"], 1)
        self.assertEqual(FactoryMaterialLot.objects.count(), 1)
        self.assertEqual(MaterialWarehouseDocument.objects.count(), 1)

    def test_reservation_is_not_returnable_and_failed_return_does_not_write(self):
        lot = self.receive()
        lot.reserved_milli = 7000
        lot.save(update_fields=("reserved_milli",))
        docs = MaterialWarehouseDocument.objects.count()
        self.assertEqual(self.move("SUPPLIER_RETURN", lot_id=lot.pk, quantity="4")["code"], 1)
        self.assertEqual(MaterialWarehouseDocument.objects.count(), docs)
        self.assertEqual(self.move("SUPPLIER_RETURN", lot_id=lot.pk, quantity="3")["code"], 0)
        lot.refresh_from_db()
        self.assertEqual((lot.on_hand_milli, lot.reserved_milli), (7000, 7000))
        self.assertEqual(self.get(f"{lot.stock_id}/")["data"]["stock"]["available"], "0")

    def test_ordinary_return_cannot_become_negative_or_be_repeated(self):
        lot = self.receive()
        data = self.payload(kind="SUPPLIER_RETURN", lot_id=lot.pk, quantity="10")
        self.assertEqual(self.post(data)["code"], 0)
        self.assertTrue(self.post(data)["data"]["already_applied"])
        self.assertEqual(self.move("SUPPLIER_RETURN", lot_id=lot.pk, quantity="1")["code"], 1)
        lot.refresh_from_db()
        self.assertEqual(lot.on_hand_milli, 0)

    def test_warranty_returns_are_separate_and_manual_fault_registration_or_replacement_is_rejected(self):
        lot = self.receive(quantity="20")
        case = MaterialWarrantyCase.objects.create(
            stock=lot.stock, source_lot=lot, case_no="MWC-AUTOMATIC", registered_milli=5000, registered_by=self.keeper,
        )
        FactoryMaterialLot.objects.filter(pk=lot.pk).update(on_hand_milli=15000)
        stock = self.get(f"{lot.stock_id}/")["data"]["stock"]
        self.assertEqual((stock["on_hand"], stock["fault_on_hand"]), ("15", "5"))
        self.assertEqual(self.move("WARRANTY_REGISTER", source="STOCK", lot_id=lot.pk, quantity="1")["code"], 1)
        self.assertEqual(self.move("WARRANTY_REPLACE", case_id=case.pk, quantity="1")["code"], 1)
        self.assertEqual(self.move("WARRANTY_SEND", case_id=case.pk, quantity="3")["code"], 0)
        self.assertEqual(self.move("WARRANTY_SEND", case_id=case.pk, quantity="3")["code"], 1)
        stock = self.get(f"{lot.stock_id}/")["data"]["stock"]
        self.assertEqual((stock["on_hand"], stock["fault_on_hand"], stock["awaiting_replacement"]), ("15", "2", "3"))
        case.refresh_from_db()
        self.assertEqual((case.registered_milli, case.sent_milli, case.replaced_milli), (5000, 3000, 0))

    def test_manual_external_fault_registration_is_rejected_without_creating_stock(self):
        result = self.move("WARRANTY_REGISTER", source="EXTERNAL", quantity="4")
        self.assertEqual(result["code"], 1, result)
        self.assertEqual(FactoryMaterialLot.objects.count(), 0)
        self.assertEqual(MaterialWarrantyCase.objects.count(), 0)

    def test_unit_cannot_be_changed_by_later_receipts(self):
        self.receive(unit="千克")
        result = self.post(self.payload(unit="件"))
        self.assertEqual(result["code"], 1)
        self.assertEqual(FactoryMaterialLot.objects.count(), 1)

    def test_invalid_input_leaves_no_stock_or_journal(self):
        for value in ["0", "-1", "NaN", "Infinity", "0.0001", "1000000001", True, None, {}, []]:
            with self.subTest(value=value):
                self.assertEqual(self.post(self.payload(quantity=value))["code"], 1)
        for changes in [{"request_id": "invalid"}, {"supplier_id": True}, {"unit": "箱"}, {"kind": "FAKE"}, {"supplier_batch_code": "x" * 129}]:
            self.assertEqual(self.post(self.payload(**changes))["code"], 1)
        self.assertEqual(MaterialWarehouseDocument.objects.count(), 0)
        self.assertEqual(FactoryMaterialStock.objects.count(), 0)

    def test_only_linked_suppliers_and_real_spec_supply_can_receive(self):
        self.assertEqual(len(self.get("catalog/")["data"]["items"]), 3)
        OrganizationRelation.objects.filter(source_organization=self.factory, target_organization=self.supplier).update(is_active=False)
        self.assertEqual(self.post(self.payload())["code"], 1)
        self.assertEqual(self.post(self.payload(material_spec_id=self.spec2.pk, supplier_id=self.other_supplier.pk))["code"], 1)
        self.assertEqual(len(self.get("catalog/")["data"]["items"]), 1)

    def test_factory_isolation_on_list_detail_form_return_and_warranty(self):
        lot = self.receive()
        self.assertEqual(self.get(user=self.keeper2)["data"]["items"], [])
        for path in (f"{lot.stock_id}/", f"form/?kind=SUPPLIER_RETURN&lot_id={lot.pk}"):
            self.assertEqual(self.get(path, user=self.keeper2)["code"], 1)
        self.assertEqual(self.post(self.payload(kind="SUPPLIER_RETURN", expected_factory_id=self.other_factory.pk, lot_id=lot.pk, quantity="1"), user=self.keeper2)["code"], 1)
        case = MaterialWarrantyCase.objects.create(stock=lot.stock, case_no="MWC-ISOLATED", registered_milli=1000, registered_by=self.keeper)
        self.assertEqual(self.post(self.payload(kind="WARRANTY_SEND", expected_factory_id=self.other_factory.pk, case_id=case.pk, quantity="1"), user=self.keeper2)["code"], 1)

    def test_open_form_from_previous_factory_cannot_write_after_role_shift(self):
        result = self.post(self.payload(), user=self.keeper2)
        self.assertEqual(result["code"], 403, result)
        self.assertEqual(FactoryMaterialLot.objects.count(), 0)

    def test_suppliers_only_see_their_supply_and_never_warehouse_write_controls(self):
        lot = self.receive()
        self.receive(supplier_id=self.other_supplier.pk)
        rows = self.get(user=self.owner)["data"]
        self.assertFalse(rows["can_manage"])
        self.assertEqual(len(rows["items"]), 1)
        self.assertEqual(rows["items"][0]["supplier_id"], self.supplier.pk)
        self.assertEqual(self.get(f"{lot.stock_id}/", user=self.other_owner)["code"], 1)
        self.assertEqual(self.get(f"{lot.stock_id}/", user=self.owner)["code"], 0)
        for user in (self.owner, self.sales):
            self.assertEqual(self.post(self.payload(), user=user)["code"], 403)
            self.assertEqual(self.get("catalog/", user=user)["code"], 403)
            self.assertEqual(self.get("form/?kind=RECEIPT", user=user)["code"], 403)

    def test_sales_responsibility_is_scoped_by_material(self):
        a = self.receive()
        b = self.receive(material_spec_id=self.spec2.pk)
        self.assertEqual(self.get(user=self.sales)["data"]["items"], [])
        SupplierSalesResponsibility.objects.create(supplier=self.supplier, sales_identity=UserOrganization.objects.get(user=self.sales), scope_type="MATERIAL", material=self.material, assigned_by=self.owner)
        SupplierSalesResponsibility.objects.create(supplier=self.supplier, sales_identity=UserOrganization.objects.get(user=self.sales2), scope_type="MATERIAL", material=self.material2, assigned_by=self.owner)
        self.assertEqual([x["id"] for x in self.get(user=self.sales)["data"]["items"]], [a.stock_id])
        self.assertEqual([x["id"] for x in self.get(user=self.sales2)["data"]["items"]], [b.stock_id])
        self.assertEqual(self.get(f"{b.stock_id}/?tab=documents", user=self.sales)["code"], 1)
        self.assertEqual(self.get(f"{a.stock_id}/?tab=warranty", user=self.sales2)["code"], 1)

    def test_disabled_role_org_membership_and_wrong_org_role_are_denied(self):
        identity = UserOrganization.objects.get(user=self.keeper)
        targets = [identity, identity.role, self.factory, self.factory.organization_type]
        for target in targets:
            with self.subTest(target=type(target).__name__):
                target.is_active = False; target.save(update_fields=("is_active",))
                self.assertEqual(self.post(self.payload())["code"], 403)
                target.is_active = True; target.save(update_fields=("is_active",))
        identity.organization = self.supplier
        identity.save(update_fields=("organization",))
        self.assertEqual(self.get()["code"], 403)

    def test_finished_goods_keeper_and_other_roles_do_not_gain_material_actions(self):
        for role in ("factory_admin", "factory_stock", "factory_sales", "factory_production", "factory_matching", "factory_logistics"):
            user = self.user(role, role, self.factory)
            self.assertEqual(self.get(user=user)["code"], 403)
            self.assertEqual(self.post(self.payload(), user=user)["code"], 403)

    def test_pagination_does_not_hide_third_or_older_lots(self):
        for _ in range(33):
            self.receive()
        stock = FactoryMaterialStock.objects.get()
        page1 = self.get(f"{stock.pk}/")["data"]
        page2 = self.get(f"{stock.pk}/", data={"cursor": page1["next_cursor"]})["data"]
        ids = [x["id"] for x in page1["items"] + page2["items"]]
        self.assertEqual(len(set(ids)), 33)
        self.assertIsNone(page2["next_cursor"])
        self.assertEqual(page2["stock"]["on_hand"], "330")

    def test_warranty_list_filters_current_scope_and_opens_single_case(self):
        lot = self.receive()
        case = MaterialWarrantyCase.objects.create(stock=lot.stock, source_lot=lot, case_no="MWC-NORTH", registered_milli=3000, registered_by=self.keeper)
        self.receive(supplier_id=self.other_supplier.pk)
        rows = self.get("warranty/", data={"q": "NORTH"})["data"]
        self.assertEqual([item["id"] for item in rows["items"]], [case.pk])
        self.assertEqual(rows["items"][0]["stock_id"], lot.stock_id)
        detail = self.get(f"{lot.stock_id}/", data={"tab": "warranty", "case_id": case.pk})["data"]
        self.assertEqual([item["id"] for item in detail["items"]], [case.pk])
        self.assertEqual(self.get("warranty/", user=self.other_owner)["data"]["items"], [])

    def test_database_constraints_block_negative_or_excess_reservations(self):
        lot = self.receive()
        for update in [{"on_hand_milli": -1}, {"reserved_milli": 10001}, {"on_hand_milli": 10001}]:
            with self.subTest(update=update), self.assertRaises(IntegrityError), transaction.atomic():
                FactoryMaterialLot.objects.filter(pk=lot.pk).update(**update)

    def test_malformed_requests_and_unauthenticated_access(self):
        self.assertEqual(self.client.get(ROOT).status_code, 401)
        self.assertEqual(self.client.get(ROOT + "documents/", **self.auth(self.keeper)).status_code, 405)
        for value in ("[]", "null", "{bad"):
            response = self.client.post(ROOT + "documents/", value, content_type="application/json", **self.auth(self.keeper))
            self.assertEqual(response.json()["code"], 1)

    def test_warehouse_does_not_create_or_complete_old_order_tasks(self):
        before = {model: list(model.objects.order_by("pk").values()) for model in (WorkItem, UserTask, SupplierMaterialBatch)}
        lot = self.receive()
        self.move("SUPPLIER_RETURN", lot_id=lot.pk, quantity="1")
        MaterialWarrantyCase.objects.create(stock=lot.stock, case_no="MWC-WORKFLOW", registered_milli=1000, registered_by=self.keeper)
        for model, rows in before.items():
            self.assertEqual(list(model.objects.order_by("pk").values()), rows)

    def test_return_document_remains_visible_after_supply_link_removed(self):
        lot = self.receive()
        OrganizationRelation.objects.filter(source_organization=self.factory, target_organization=self.supplier).update(is_active=False)
        self.assertEqual(self.move("SUPPLIER_RETURN", lot_id=lot.pk, quantity="10")["code"], 0)
        data = self.get(f"{lot.stock_id}/", data={"tab": "documents"})["data"]
        self.assertEqual(len(data["items"]), 2)


class MaterialInventoryConcurrencyTests(TransactionTestCase):
    def test_concurrent_retries_and_returns_conserve_inventory(self):
        from account.test_reference import restore_account_test_reference

        restore_account_test_reference()
        factory = Organization.objects.create(code="MI-RACE-F", name="并发厂家", organization_type=OrganizationType.objects.get(code="OWNER"))
        supplier = Organization.objects.create(code="MI-RACE-S", name="并发供应商", organization_type=OrganizationType.objects.get(code="SUPPLIER"))
        OrganizationRelation.objects.create(source_organization=factory, target_organization=supplier, relation_type="SUPPLY")
        material = Material.objects.create(code="MI-RACE-M", name="并发物料")
        spec = MaterialSpec.objects.create(code="MI-RACE-SP", name="并发规格", material=material)
        MaterialSpecSupplier.objects.create(material_spec=spec, supplier=supplier)
        user = User.objects.create_user(username="mi-race", wx_openid="mi-race")
        UserOrganization.objects.create(user=user, organization=factory, role=OrganizationRole.objects.get(code="factory_material_stock"))
        receipt = {"kind": "RECEIPT", "expected_factory_id": factory.pk, "supplier_id": supplier.pk, "material_spec_id": spec.pk, "quantity": "10", "unit": "件", "request_id": str(uuid4())}

        def run_pair(payloads):
            barrier = Barrier(2)

            def worker(payload):
                close_old_connections()
                barrier.wait(timeout=10)
                try:
                    for attempt in range(10):
                        try:
                            return submit_movement(user=user, data=payload)
                        except MaterialInventoryError as exc:
                            if "正在更新" not in str(exc) or attempt == 9:
                                return {"error": str(exc)}
                            sleep(0.01 * (attempt + 1))
                finally:
                    close_old_connections()

            with ThreadPoolExecutor(max_workers=2) as pool:
                return list(pool.map(worker, payloads))

        results = run_pair([receipt, receipt])
        self.assertTrue(all("document" in row for row in results), results)
        self.assertEqual(FactoryMaterialLot.objects.count(), 1)
        lot = FactoryMaterialLot.objects.get()
        self.assertEqual(lot.on_hand_milli, 10000)
        returns = [{"kind": "SUPPLIER_RETURN", "expected_factory_id": factory.pk, "lot_id": lot.pk, "quantity": "8", "request_id": str(uuid4())} for _ in range(2)]
        results = run_pair(returns)
        self.assertEqual(sum("document" in row for row in results), 1, results)
        lot.refresh_from_db()
        self.assertEqual(lot.on_hand_milli, 2000)
        self.assertEqual(MaterialWarehouseDocument.objects.count(), 2)
