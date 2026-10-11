"""Independent material warehouse. No order transitions or workflow writes here."""
import hashlib
import json
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid4

from django.db import IntegrityError, OperationalError, transaction
from django.db.models import Count, F, Q, Sum
from django.utils import timezone

from account.models import OrganizationRelation
from account.services.auth_service import get_active_identity
from account.services.role_delegation_service import identity_has_role
from supply_chain.models import (
    FactoryMaterialLot, FactoryMaterialStock, MaterialSpecSupplier,
    MaterialWarehouseDocument, MaterialWarrantyCase,
)
from supply_chain.services.supplier_responsibility_service import get_responsible_material_ids


class MaterialInventoryError(ValueError):
    pass


class MaterialInventoryPermissionError(MaterialInventoryError):
    pass


MANUAL_MOVEMENT_KINDS = frozenset({"RECEIPT", "SUPPLIER_RETURN", "WARRANTY_SEND"})


def quantity_text(milli):
    return format(Decimal(milli or 0) / 1000, "f").rstrip("0").rstrip(".") if milli and milli % 1000 else str((milli or 0) // 1000)


def _quantity(value):
    if isinstance(value, bool) or len(str(value)) > 30:
        raise MaterialInventoryError("请输入有效数量")
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number <= 0 or number > 1_000_000_000:
            raise ValueError
        scaled = number * 1000
        if scaled != scaled.to_integral_value():
            raise ValueError
        return int(scaled)
    except (InvalidOperation, ValueError, TypeError):
        raise MaterialInventoryError("数量须大于0、不超过十亿，最多三位小数") from None


def _id(value, label="记录"):
    if isinstance(value, bool) or not str(value).isdigit() or int(value) <= 0:
        raise MaterialInventoryError(f"请选择有效的{label}")
    return int(value)


def inventory_identity(user, *, write=False):
    identity = get_active_identity(user) if user.is_active else None
    if not identity:
        raise MaterialInventoryPermissionError("账号没有有效岗位，请重新登录")
    keeper = identity_has_role(identity, "factory_material_stock") and identity.organization.organization_type.code == "OWNER"
    supplier = identity_has_role(identity, {"supplier_owner", "supplier_sales"}) and identity.organization.organization_type.code == "SUPPLIER"
    if not keeper and (write or not supplier):
        raise MaterialInventoryPermissionError("仅厂家物料库管可办理出入库，供应商负责人和负责该物料的销售经理可查看库存")
    return identity, keeper


def _stocks(identity, keeper):
    rows = FactoryMaterialStock.objects.select_related("factory", "supplier", "material_spec__material")
    if keeper:
        return rows.filter(factory_id=identity.organization_id)
    rows = rows.filter(supplier_id=identity.organization_id)
    if identity_has_role(identity, "supplier_sales"):
        rows = rows.filter(material_spec__material_id__in=get_responsible_material_ids(sales_identity=identity))
    return rows


def _stock(identity, keeper, stock_id):
    result = _stocks(identity, keeper).filter(pk=_id(stock_id, "物料库存")).first()
    if not result:
        raise MaterialInventoryError("记录不存在或不在当前岗位可见范围内")
    return result


def _stock_info(stock):
    totals = stock.lots.aggregate(on_hand=Sum("on_hand_milli"), reserved=Sum("reserved_milli"), active=Count("pk", filter=Q(on_hand_milli__gt=0)))
    faults = stock.warranty_cases.aggregate(registered=Sum("registered_milli"), sent=Sum("sent_milli"), replaced=Sum("replaced_milli"))
    on_hand, reserved = totals["on_hand"] or 0, totals["reserved"] or 0
    return {
        "id": stock.pk, "factory_name": stock.factory.name, "supplier_name": stock.supplier.name,
        "supplier_id": stock.supplier_id, "material_spec_id": stock.material_spec_id,
        "material_name": stock.material_spec.material.name, "material_code": stock.material_spec.material.code,
        "spec_name": stock.material_spec.name, "spec_code": stock.material_spec.code,
        "unit": stock.unit, "on_hand": quantity_text(on_hand), "reserved": quantity_text(reserved),
        "available": quantity_text(on_hand - reserved), "active_lot_count": totals["active"],
        "fault_on_hand": quantity_text((faults["registered"] or 0) - (faults["sent"] or 0)),
        "awaiting_replacement": quantity_text((faults["sent"] or 0) - (faults["replaced"] or 0)),
    }


def _lot_info(lot):
    return {
        "id": lot.pk, "batch_code": lot.batch_code, "supplier_batch_code": lot.supplier_batch_code,
        "received": quantity_text(lot.received_milli), "on_hand": quantity_text(lot.on_hand_milli),
        "reserved": quantity_text(lot.reserved_milli), "available": quantity_text(lot.on_hand_milli - lot.reserved_milli),
        "received_at": timezone.localtime(lot.received_at).strftime("%Y-%m-%d %H:%M"),
        "can_return": lot.on_hand_milli > lot.reserved_milli,
    }


def _case_info(case):
    return {
        "id": case.pk, "case_no": case.case_no,
        "source": "正常库存转故障件" if case.source_lot_id else "线下收回故障件",
        "source_batch_code": case.source_lot.batch_code if case.source_lot_id else "",
        "registered": quantity_text(case.registered_milli), "sent": quantity_text(case.sent_milli),
        "replaced": quantity_text(case.replaced_milli),
        "fault_on_hand": quantity_text(case.registered_milli - case.sent_milli),
        "awaiting_replacement": quantity_text(case.sent_milli - case.replaced_milli),
        "can_send": case.sent_milli < case.registered_milli,
        "can_replace": case.replaced_milli < case.sent_milli,
    }


def _document_info(doc):
    return {
        "id": doc.pk, "document_no": doc.document_no, "kind": doc.kind,
        "kind_label": doc.get_kind_display(), "quantity": quantity_text(doc.quantity_milli),
        "batch_code": doc.lot.batch_code if doc.lot_id else "",
        "case_no": doc.warranty_case.case_no if doc.warranty_case_id else "",
        "created_at": timezone.localtime(doc.created_at).strftime("%Y-%m-%d %H:%M"),
        "stock_id": doc.stock_id,
    }


def _page(rows, cursor, serialize):
    if cursor:
        rows = rows.filter(pk__lt=_id(cursor, "分页位置"))
    entries = list(rows.order_by("-pk")[:31])
    return {"items": [serialize(row) for row in entries[:30]], "next_cursor": entries[29].pk if len(entries) > 30 else None}


def inventory_list(*, user, query="", cursor=None):
    identity, keeper = inventory_identity(user)
    rows = _stocks(identity, keeper)
    query = str(query).strip()[:100]
    if query:
        rows = rows.filter(Q(material_spec__material__name__icontains=query) | Q(material_spec__code__icontains=query) | Q(material_spec__name__icontains=query) | Q(supplier__name__icontains=query) | Q(factory__name__icontains=query))
    return {"can_manage": keeper, **_page(rows, cursor, _stock_info)}


def inventory_detail(*, user, stock_id, tab="lots", cursor=None, include_empty=False, case_id=None):
    identity, keeper = inventory_identity(user)
    stock = _stock(identity, keeper, stock_id)
    if tab == "lots":
        rows = stock.lots.all()
        if not include_empty:
            rows = rows.filter(on_hand_milli__gt=0)
        serializer = _lot_info
    elif tab == "warranty":
        rows, serializer = stock.warranty_cases.select_related("source_lot"), _case_info
        if case_id:
            rows = rows.filter(pk=_id(case_id, "三包退货记录"))
    elif tab == "documents":
        rows, serializer = stock.documents.select_related("lot", "warranty_case"), _document_info
    else:
        raise MaterialInventoryError("库存明细类型无效")
    return {"can_manage": keeper, "stock": _stock_info(stock), "tab": tab, **_page(rows, cursor, serializer)}


def inventory_warranty_list(*, user, query="", cursor=None):
    """Read-only cross-material warranty return list for the current visible scope."""
    identity, keeper = inventory_identity(user)
    rows = MaterialWarrantyCase.objects.select_related(
        "stock__factory", "stock__supplier", "stock__material_spec__material", "source_lot",
    ).filter(stock__in=_stocks(identity, keeper))
    query = str(query).strip()[:100]
    if query:
        rows = rows.filter(
            Q(case_no__icontains=query)
            | Q(stock__supplier__name__icontains=query)
            | Q(stock__material_spec__material__name__icontains=query)
            | Q(stock__material_spec__code__icontains=query)
            | Q(stock__material_spec__name__icontains=query)
            | Q(source_lot__batch_code__icontains=query)
        )

    def serialize(case):
        item = _case_info(case)
        item.update({
            "stock_id": case.stock_id,
            "factory_name": case.stock.factory.name,
            "supplier_name": case.stock.supplier.name,
            "material_name": case.stock.material_spec.material.name,
            "spec_name": case.stock.material_spec.name,
            "spec_code": case.stock.material_spec.code,
            "unit": case.stock.unit,
            "created_at": timezone.localtime(case.created_at).strftime("%Y-%m-%d %H:%M"),
        })
        return item

    return {"can_manage": keeper, **_page(rows, cursor, serialize)}


def _supply_options(factory_id):
    suppliers = OrganizationRelation.objects.filter(
        source_organization_id=factory_id, relation_type="SUPPLY", is_active=True,
        target_organization__organization_type__code="SUPPLIER",
        target_organization__organization_type__is_active=True, target_organization__is_active=True,
    ).values_list("target_organization_id", flat=True)
    return MaterialSpecSupplier.objects.filter(supplier_id__in=suppliers).select_related("supplier", "material_spec__material")


def inventory_form(*, user, kind, stock_id=None, lot_id=None, case_id=None):
    identity, _ = inventory_identity(user, write=True)
    if kind not in MANUAL_MOVEMENT_KINDS:
        raise MaterialInventoryError("三包故障件和换回件由售后、拆机或召回流程自动生成，物料库不支持手工登记")
    result = {"kind": kind, "factory_id": identity.organization_id, "units": list(FactoryMaterialStock.Unit.values)}
    if lot_id:
        lot = FactoryMaterialLot.objects.select_related("stock").filter(pk=_id(lot_id), stock__factory_id=identity.organization_id).first()
        if not lot:
            raise MaterialInventoryError("记录不存在或不在当前岗位可见范围内")
        stock_id, result["lot"] = lot.stock_id, _lot_info(lot)
    if case_id:
        case = MaterialWarrantyCase.objects.select_related("source_lot").filter(pk=_id(case_id), stock__factory_id=identity.organization_id).first()
        if not case:
            raise MaterialInventoryError("记录不存在或不在当前岗位可见范围内")
        stock_id, result["case"] = case.stock_id, _case_info(case)
    if stock_id:
        result["stock"] = _stock_info(_stock(identity, True, stock_id))
    return result


def inventory_catalog(*, user, query="", cursor=None):
    identity, _ = inventory_identity(user, write=True)
    rows = _supply_options(identity.organization_id)
    query = str(query).strip()[:100]
    if query:
        rows = rows.filter(Q(supplier__name__icontains=query) | Q(material_spec__material__name__icontains=query) | Q(material_spec__code__icontains=query) | Q(material_spec__name__icontains=query))

    def serialize(option):
        stock = FactoryMaterialStock.objects.filter(factory_id=identity.organization_id, supplier_id=option.supplier_id, material_spec_id=option.material_spec_id).first()
        return {
            "id": option.pk, "supplier_id": option.supplier_id, "material_spec_id": option.material_spec_id,
            "supplier_name": option.supplier.name, "material_name": option.material_spec.material.name,
            "spec_name": option.material_spec.name, "spec_code": option.material_spec.code,
            "stock_id": stock.pk if stock else None, "unit": stock.unit if stock else "",
        }

    return {"units": list(FactoryMaterialStock.Unit.values), **_page(rows, cursor, serialize)}


def _number(prefix):
    return f"{prefix}-{timezone.localtime():%Y%m%d%H%M%S}-{uuid4().hex[:12].upper()}"


def _new_lot(stock, user, amount, supplier_batch_code=""):
    return FactoryMaterialLot.objects.create(
        stock=stock, batch_code=_number("ML"), supplier_batch_code=supplier_batch_code,
        received_milli=amount, on_hand_milli=amount, received_by=user,
    )


def _deduct(lot, amount):
    updated = FactoryMaterialLot.objects.filter(pk=lot.pk, on_hand_milli__gte=F("reserved_milli") + amount).update(on_hand_milli=F("on_hand_milli") - amount)
    if updated != 1:
        raise MaterialInventoryError("数量超过该批次可用库存，请刷新后核对；已预占数量不能退货")


def _payload(data):
    kind = data.get("kind")
    if kind not in MaterialWarehouseDocument.Kind.values:
        raise MaterialInventoryError("出入库操作类型无效")
    try:
        request_id = UUID(str(data.get("request_id")))
    except (ValueError, TypeError, AttributeError):
        raise MaterialInventoryError("提交标识无效，请重新打开表单") from None
    result = {"kind": kind, "quantity_milli": _quantity(data.get("quantity"))}
    # Exactly the fields that affect the movement form its idempotency fingerprint.
    if kind == "RECEIPT" or (kind == "WARRANTY_REGISTER" and data.get("source") == "EXTERNAL"):
        result.update(supplier_id=_id(data.get("supplier_id"), "供应商"), material_spec_id=_id(data.get("material_spec_id"), "物料规格"))
        unit = data.get("unit")
        if unit not in FactoryMaterialStock.Unit.values:
            raise MaterialInventoryError("请选择库存计量单位")
        result["unit"] = unit
    elif kind in {"SUPPLIER_RETURN", "WARRANTY_REGISTER"}:
        result["lot_id"] = _id(data.get("lot_id"), "物料批次")
    else:
        result["case_id"] = _id(data.get("case_id"), "三包记录")
    if kind == "WARRANTY_REGISTER":
        if data.get("source") not in {"EXTERNAL", "STOCK"}:
            raise MaterialInventoryError("请选择故障件来源")
        result["source"] = data["source"]
    if kind in {"RECEIPT", "WARRANTY_REPLACE"}:
        batch = str(data.get("supplier_batch_code") or "").strip()
        if len(batch) > 128:
            raise MaterialInventoryError("供应商原始批号最多128个字符")
        result["supplier_batch_code"] = batch
    digest = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return request_id, digest, result


def _replay(factory_id, request_id, digest):
    doc = MaterialWarehouseDocument.objects.select_related("lot", "warranty_case").filter(factory_id=factory_id, request_id=request_id).first()
    if not doc:
        return None
    if doc.request_hash != digest:
        raise MaterialInventoryError("本次提交已成功，但内容与原记录不同。请先核对流水，再发起新操作")
    return {"already_applied": True, "document": _document_info(doc)}


def submit_movement(*, user, data):
    try:
        identity, _ = inventory_identity(user, write=True)
        if data.get("kind") not in MANUAL_MOVEMENT_KINDS:
            raise MaterialInventoryError("三包故障件和换回件由售后、拆机或召回流程自动生成，物料库不支持手工登记")
        if _id(data.get("expected_factory_id"), "当前厂家") != identity.organization_id:
            raise MaterialInventoryPermissionError("当前厂家身份已变化，请返回物料库重新打开表单")
        request_id, digest, payload = _payload(data)
        factory_id = identity.organization_id
        replay = _replay(factory_id, request_id, digest)
        if replay:
            return replay
        with transaction.atomic():
            return _apply(user, identity, request_id, digest, payload)
    except IntegrityError:
        # A concurrent retry can win the unique request key. The loser rolls
        # back its entire movement before returning the committed document.
        replay = _replay(factory_id, request_id, digest)
        if replay:
            return replay
        raise MaterialInventoryError("库存正在更新，请刷新后重试；本次未重复记账") from None
    except OperationalError as exc:
        if "locked" not in str(exc).lower() and "deadlock" not in str(exc).lower():
            raise
        raise MaterialInventoryError("库存正在更新，请稍后使用同一表单重试") from None


def _apply(user, identity, request_id, digest, payload):
    kind, amount = payload["kind"], payload["quantity_milli"]
    lot = case = None
    if "supplier_id" in payload:
        option = _supply_options(identity.organization_id).filter(supplier_id=payload["supplier_id"], material_spec_id=payload["material_spec_id"]).first()
        if not option:
            raise MaterialInventoryError("该供应商与厂家无有效供应链路，或尚未配置此物料规格的供应关系")
        stock, _ = FactoryMaterialStock.objects.get_or_create(
            factory_id=identity.organization_id, supplier_id=option.supplier_id,
            material_spec_id=option.material_spec_id, defaults={"unit": payload["unit"]},
        )
        if stock.unit != payload["unit"]:
            raise MaterialInventoryError(f"该物料库存固定使用{stock.unit}，请按原单位入库")
    elif "lot_id" in payload:
        lot = FactoryMaterialLot.objects.select_related("stock").filter(pk=payload["lot_id"], stock__factory_id=identity.organization_id).first()
        if not lot:
            raise MaterialInventoryError("记录不存在或不在当前岗位可见范围内")
        stock = lot.stock
    else:
        case = MaterialWarrantyCase.objects.select_related("stock").filter(pk=payload["case_id"], stock__factory_id=identity.organization_id).first()
        if not case:
            raise MaterialInventoryError("记录不存在或不在当前岗位可见范围内")
        stock = case.stock
    # Real write lock even on SQLite; all future reservations/consumption must
    # use the same stock row and conditional quantity updates.
    FactoryMaterialStock.objects.filter(pk=stock.pk).update(revision=F("revision") + 1)
    replay = _replay(identity.organization_id, request_id, digest)
    if replay:
        return replay
    if kind == "RECEIPT":
        lot = _new_lot(stock, user, amount, payload["supplier_batch_code"])
    elif kind == "SUPPLIER_RETURN":
        _deduct(lot, amount)
    elif kind == "WARRANTY_REGISTER":
        if lot:
            _deduct(lot, amount)
        case = MaterialWarrantyCase.objects.create(
            stock=stock, source_lot=lot, case_no=_number("MWC"),
            registered_milli=amount, registered_by=user,
        )
    elif kind == "WARRANTY_SEND":
        changed = MaterialWarrantyCase.objects.filter(pk=case.pk, registered_milli__gte=F("sent_milli") + amount).update(sent_milli=F("sent_milli") + amount)
        if changed != 1:
            raise MaterialInventoryError("三包退货数量超过尚未退给供应商的故障件数量")
    elif kind == "WARRANTY_REPLACE":
        changed = MaterialWarrantyCase.objects.filter(pk=case.pk, sent_milli__gte=F("replaced_milli") + amount).update(replaced_milli=F("replaced_milli") + amount)
        if changed != 1:
            raise MaterialInventoryError("换回数量超过已退供应商、尚未换回的数量")
        lot = _new_lot(stock, user, amount, payload["supplier_batch_code"])
    doc = MaterialWarehouseDocument.objects.create(
        factory_id=identity.organization_id, stock=stock, document_no=_number("MW"),
        request_id=request_id, request_hash=digest, kind=kind, quantity_milli=amount,
        lot=lot, warranty_case=case, created_by=user,
    )
    if kind == MaterialWarehouseDocument.Kind.RECEIPT:
        # Keep receipt independent from the order-plan workflow: it only makes
        # newly available stock eligible for existing V2 reservations.  The
        # local import prevents the inventory module from depending on sales
        # while Django loads models and preserves this module for installations
        # that have not yet received the V2 order-plan service.
        try:
            from importlib import import_module

            order_plan_material_flow_service = import_module(
                "apps.sales.services.order_plan_material_flow_service"
            )
        except ModuleNotFoundError as exc:
            if exc.name != "apps.sales.services.order_plan_material_flow_service":
                raise
        else:
            refresh_material_reservations_for_stock = getattr(
                order_plan_material_flow_service,
                "refresh_material_reservations_for_stock",
                None,
            )
            if refresh_material_reservations_for_stock:
                # Do not catch reservation failures.  This runs in the same
                # transaction as the receipt, so a real consistency failure
                # rolls back both changes instead of recording stock without
                # its corresponding reservation update.  The hook must not
                # publish tasks or advance an order-plan node.
                refresh_material_reservations_for_stock(
                    stock=stock,
                    actor=user,
                    # Preserve warehouse independence while allowing the V2
                    # reservation hook to attribute only allocations made
                    # from this freshly received lot.  It does not link the
                    # movement UI to any order-plan action.
                    receipt_lot=lot,
                )
    return {"already_applied": False, "document": _document_info(doc)}
