"""Lot-aware order material flow.

The active application has one order flow: every new plan uses material stock
and carries ``MATERIAL_STOCK_V2``.

The module owns the physical preparation part of a new order:

* sales freezes the production BOM when creating the plan;
* returned/recall finished goods are reserved before a production quantity is
  calculated;
* normal material lots are reserved by the smallest/oldest available lot;
* the warehouse keeper reviews shortages and immediately creates supplier
  action tasks while the development-test policy is active;
* the former scheduled dispatcher remains available, but is frozen for new
  deliveries during development testing;
* a normal material receipt can call ``refresh_material_reservations_for_stock``
  without turning the independent material warehouse page into an order page.

All quantities held by material inventory are integer thousandths. BOM rows
are floats, so they are rounded *up* to a thousandth per device: material
planning must never under-reserve due to a float conversion.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Iterable
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from django.db import transaction
from django.db.models import F, Max, Prefetch, Q, Sum
from django.utils import timezone

from account.models import UserOrganization
from account.services.auth_service import get_active_identity
from account.services.role_delegation_service import (
    SENIOR_MANAGER_ROLE_CODES,
    delegated_role_user_ids,
    effective_role_codes,
    identity_can_perform_shared_operation,
    identity_has_role,
    shared_operation_recipient_role_codes,
)
from channel.models import MerchantChannelAssignment
from lifecycle.models import (
    Device,
    DeviceState,
    FactoryReturnInventoryItem,
    LifecycleEvent,
    OrganizationDevice,
)
from lifecycle.services.lifecycle_service import LifecycleService
from manufacturing.models import ProductionBatch
from product.models import BOM, BOMItem, ProductModel
from quality_trace.models import QualityRecallReusableInventory
from sales.models import (
    FactoryMaterialSupplyPolicy,
    FactoryFinishedGoodsInventoryItem,
    OrderPlan,
    OrderPlanAccessoryCompatibility,
    OrderPlanAccessoryItem,
    OrderPlanAccessoryLotAllocation,
    OrderPlanCancellationAudit,
    OrderPlanItem,
    OrderPlanItemBOMConfirmation,
    OrderPlanLogisticsDocument,
    OrderPlanMaterialLotAllocation,
    OrderPlanMaterialPlan,
    OrderPlanMaterialPlanningRun,
    OrderPlanMaterialRequirement,
    OrderPlanProductionSubplan,
    OrderPlanProductionSubplanItem,
    OrderPlanSalesBOMSelection,
    OrderPlanShipment,
    OrderPlanShipmentAccessoryItem,
    OrderPlanShipmentDevice,
    OrderPlanShipmentItem,
    OrderPlanSupplyDelivery,
    OrderPlanSupplyDeliveryLine,
    OrderPlanSupplyDeliveryRequirement,
    OrderPlanReturnInventoryAllocation,
)
from sales.services.order_plan_reissue_service import (
    ensure_reissue_qrcodes,
    mark_return_inventory_ready_to_ship,
    reserve_return_inventory,
    serialize_item_reissue,
)
from quality_trace.services import enforce_device_operation, quality_gate_atomic
from sales.services.order_plan_service import (
    _get_direct_relation,
    _get_sales_identity,
    _new_code,
    _serialize_plan,
)
from sales.services.order_plan_flow_guard import material_flow_status_name
from sales.services.order_finance_service import (
    OrderFinanceError,
    initialize_order_finance,
    serialize_order_finance,
    cancel_order_finance_tasks,
)
from sales.services.order_receivable_service import (
    financial_write_errors,
    lock_receivable_account,
)
from supply_chain.models import (
    FactoryMaterialLot,
    FactoryMaterialStock,
    MaterialWarehouseDocument,
    Material,
    MaterialSpec,
    MaterialSpecSupplier,
    OrganizationMaterialStock,
    OrganizationMaterialLot,
)
from supply_chain.services.supplier_responsibility_service import (
    SupplierResponsibilityError,
    get_responsible_material_ids,
    get_supplier_identity,
    get_supplier_task_recipients,
    get_v2_supplier_delivery_snapshot_access,
)
from workflow.models import UserTask, WorkItem
from workflow.services.workflow_service import publish_workitem


class OrderPlanMaterialFlowError(ValueError):
    """The current actor or data cannot advance a V2 material-flow order."""


FLOW_KIND = "ORDER_PLAN"
ALGORITHM_VERSION = "stock-lot-v2"
FACTORY_PLAN_VIEW_ROLES = frozenset({
    "factory_admin", "factory_manager", "factory_sales", "factory_matching",
    "factory_material_stock", "factory_production", "factory_stock",
    "factory_sales_assistant", "factory_logistics", "factory_chief_engineer",
})
V2_LINK = "/pages/sales/order_plan_detail/order_plan_detail?order_plan_id={plan_id}"
# Production policy historically used 10:00 / 22:00 dispatch windows.  Keep
# the setting and the dispatcher for later re-enablement, but freeze that
# behaviour for development tests: a warehouse confirmation must immediately
# place the delivery task in every eligible supplier inbox.
DISPATCH_HOURS = (10, 22)
SUPPLIER_DELIVERY_DISPATCH_MODE = "IMMEDIATE_TEST"

# The initial operating policy is intentionally centralised here rather than
# hidden in the split scorer.  These limits apply **per product model**, not
# to the complete order plan.  An order may contain many models.  A model is
# normally produced as one plan of any practical size.  Only a deliberately
# created *tail-stock* child plan is limited to 5--50 units: it exists to clear
# a small remaining material lot, while the complementary main plan carries
# the balance (for example 15 + 135).  There may be at most two child plans.
MIN_SPLIT_SUBPLAN_QUANTITY = 5
MAX_TAIL_STOCK_SUBPLAN_QUANTITY = 50
MAX_SUBPLAN_COUNT = 2


# The order detail is shared by every participating role, but its timeline is
# deliberately *not* a global progress indicator.  A node is lit only for the
# actor who can handle it now, or for a recipient of the corresponding
# awareness task.  Keeping the two maps explicit makes that rule auditable and
# avoids deriving a mobile permission from a role name in the frontend.
_TIMELINE_NODE_BY_ACTION_CODE = {
    "warehouse-review": "MATERIAL_REVIEW",
    "supplier-confirm": "SUPPLY",
    "material-ready": "MATERIAL_READY",
    "production-confirm": "PRODUCTION",
    # Sales authorization is the start of this shipment's QR/packing stage;
    # a voluntary split is an action in the same stage.
    "shipment-create": "QRCODE",
    "shipment-authorize": "QRCODE",
    "shipment-qrcode-print": "QRCODE",
    "shipment-logistics-confirm": "SHIPMENT",
    "shipment-outbound": "SHIPMENT",
    "shipment-receipt": "MERCHANT_RECEIPT",
    "accounting-confirm": "ACCOUNTING",
}

# Action work items must still be open and assigned to the current recipient.
# Otherwise a former handler would remain lit after another collaborator (or
# the handler themself) has completed the action.
_TIMELINE_NODE_BY_OPEN_ACTION_TASK = {
    "ORDER_PLAN_MATERIAL_REVIEW": "MATERIAL_REVIEW",
    "ORDER_PLAN_SUPPLIER_DELIVERY": "SUPPLY",
    "ORDER_PLAN_V2_PRODUCTION_CONFIRM": "PRODUCTION",
    "ORDER_PLAN_V2_SHIPMENT_AUTHORIZATION": "QRCODE",
    "ORDER_PLAN_V2_QRCODE_PRINT": "QRCODE",
    "ORDER_PLAN_V2_SHIPMENT_LOGISTICS_CONFIRM": "SHIPMENT",
    "ORDER_PLAN_V2_STOCK_OUTBOUND": "SHIPMENT",
    "ORDER_PLAN_V2_MERCHANT_RECEIPT": "MERCHANT_RECEIPT",
    "ORDER_PLAN_V2_ACCOUNTING_CONFIRM": "ACCOUNTING",
}

# Notice work items are intentionally created as completed records.  Their
# recipient is still entitled to see the related live node highlighted while
# that node is unfinished, even though there is no button to press.
_TIMELINE_NODE_BY_AWARENESS_TASK = {
    "ORDER_PLAN_MATERIAL_CREATED_NOTICE": "MATERIAL_REVIEW",
    "ORDER_PLAN_SUPPLY_QUEUE_NOTICE": "SUPPLY",
    "ORDER_PLAN_SUPPLY_DISPATCHED_NOTICE": "SUPPLY",
    "ORDER_PLAN_SUPPLIER_CONFIRMED_NOTICE": "RECEIPT",
    "ORDER_PLAN_MATERIAL_READY_NOTICE": "MATERIAL_READY",
    "ORDER_PLAN_V2_PRODUCTION_COMPLETED_NOTICE": "QRCODE",
    "ORDER_PLAN_FINISHED_STOCK_READY": "QRCODE",
    "ORDER_PLAN_V2_SHIPPED_NOTICE": "MERCHANT_RECEIPT",
}


def _display_name(user):
    return user.wx_nickname or user.get_full_name() or user.username


def _quantity_text(milli):
    value = Decimal(int(milli or 0)) / Decimal("1000")
    text = format(value, "f")
    # Never trim zeros from an integer: ``100`` is a quantity of one hundred,
    # not one.  Only a fractional decimal representation has removable tail
    # zeros, for example ``1.500`` -> ``1.5``.
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _actor_openid(user):
    return (user.wx_openid or "").strip()


def _role_openids(organization, role_codes: Iterable[str]):
    return list(
        UserOrganization.objects.filter(
            organization=organization,
            role__code__in=tuple(role_codes),
            role__is_active=True,
            is_active=True,
            user__is_active=True,
            user__wx_openid__isnull=False,
        )
        .exclude(user__wx_openid="")
        .values_list("user__wx_openid", flat=True)
        .distinct()
    )


def _factory_identity(user, role_codes, *, message):
    identity = get_active_identity(user) if user and user.is_active else None
    if (
        not identity
        or not identity_has_role(identity, role_codes)
        or identity.organization.organization_type.code != "OWNER"
    ):
        raise OrderPlanMaterialFlowError(message)
    return identity


def _material_keeper_identity(user):
    return _factory_identity(
        user,
        {"factory_material_stock"},
        message="只有厂家物料库管可以核对物料和确认物料齐套。",
    )


def _production_identity(user):
    return _factory_identity(
        user,
        {"factory_production"},
        message="只有厂家生产经理可以确认生产完成。",
    )


def _sales_identity(user):
    try:
        return _get_sales_identity(user)
    except Exception as exc:  # keep the public V2 error type stable
        raise OrderPlanMaterialFlowError(str(exc)) from exc


def _sales_task_identity(user):
    """Allow a delegated senior manager to handle its invited sales task."""

    return _factory_identity(
        user,
        {"factory_sales"},
        message="只有厂家销售经理或获授权的高级经理可以处理该销售任务。",
    )


def _sales_actor_user_ids(identity):
    if identity.role.code in {"factory_senior_manager", "merchant_senior_manager"}:
        return delegated_role_user_ids(identity, "factory_sales")
    if identity.role.code == "factory_sales":
        return {identity.user_id}
    return set()


def _plan_queryset(*, lock=False):
    queryset = OrderPlan.objects.select_related(
        "factory_organization",
        "merchant_organization",
        "brand",
        "created_by",
        "shipment_authorized_by",
    )
    return queryset.select_for_update() if lock else queryset


def _v2_plan_for_factory(identity, plan_id, *, lock=False):
    try:
        plan_id = int(plan_id)
    except (TypeError, ValueError) as exc:
        raise OrderPlanMaterialFlowError("订单计划参数无效。") from exc
    plan = _plan_queryset(lock=lock).filter(
        pk=plan_id,
        factory_organization=identity.organization,
        flow_version=OrderPlan.FlowVersion.MATERIAL_STOCK_V2,
    ).first()
    if not plan:
        raise OrderPlanMaterialFlowError("订单计划不存在、不是物料库存流程或不属于当前厂家。")
    return plan


def _v2_plan_by_id(plan_id, *, lock=False):
    try:
        plan_id = int(plan_id)
    except (TypeError, ValueError) as exc:
        raise OrderPlanMaterialFlowError("订单计划参数无效。") from exc
    plan = _plan_queryset(lock=lock).filter(
        pk=plan_id,
        flow_version=OrderPlan.FlowVersion.MATERIAL_STOCK_V2,
    ).first()
    if not plan:
        raise OrderPlanMaterialFlowError("订单计划不存在或不是物料库存流程。")
    return plan


def _plan_link(plan):
    return V2_LINK.format(plan_id=plan.pk)


def _group_payload(plan, **extra):
    payload = {
        "order_plan_id": plan.pk,
        "plan_code": plan.plan_code,
        "factory_id": plan.factory_organization_id,
        "merchant_id": plan.merchant_organization_id,
        "merchant_name": plan.merchant_organization.name,
        "brand_name": plan.brand.name,
        "region": plan.region_snapshot,
        "inbox_group_key": f"order-plan:{plan.pk}",
        "inbox_group_kind": FLOW_KIND,
        "inbox_group_label": f"订单计划：{plan.plan_code}",
    }
    payload.update(extra)
    return payload


def _new_workitem(
    *,
    plan,
    work_type,
    stage_key,
    title,
    summary,
    recipients,
    need_action,
    action_mode="EACH_ACK",
    payload=None,
    actor_openid="",
    done_immediately=False,
):
    return publish_workitem(
        domain="SALES",
        type=work_type,
        biz_id=plan.plan_code,
        biz_ref=f"sales.OrderPlan:{plan.pk}",
        uniq_key=f"sales:order-plan:{plan.pk}:{stage_key}",
        title=title,
        summary=summary,
        link=_plan_link(plan),
        recipients_openids=recipients,
        need_action=need_action,
        action_mode=action_mode,
        payload=_group_payload(plan, **(payload or {})),
        actor_openid=actor_openid,
        mark_workitem_done_immediately=done_immediately,
    )


def _complete_workitem_anyone(
    workitem, user, *, actor_title=None, peer_title=None, allow_no_task=False
):
    """Complete one shared action task with a compare-and-set style update."""

    now = timezone.now()
    actor_openid = _actor_openid(user)
    if not actor_openid:
        raise OrderPlanMaterialFlowError("当前账号没有有效微信标识，请重新登录。")
    task = workitem.user_tasks.select_for_update().filter(
        to_openid=actor_openid,
        need_action=True,
    ).exclude(state="DONE").first()
    if not task and not allow_no_task:
        raise OrderPlanMaterialFlowError("当前账号不是这项任务的处理人。")
    if workitem.status == "DONE":
        return False
    payload = dict(workitem.payload or {})
    payload.update(
        {
            "completed_by_user_id": user.pk,
            "completed_by_name": _display_name(user),
            "completed_at": now.isoformat(),
        }
    )
    changed = WorkItem.objects.filter(pk=workitem.pk, status="OPEN").update(
        status="DONE",
        done_by_openid=actor_openid,
        done_at=now,
        last_activity_at=now,
        payload=payload,
        updated_at=now,
    )
    if changed != 1:
        return False
    workitem.status = "DONE"
    workitem.payload = payload
    workitem.user_tasks.filter(to_openid=actor_openid).update(
        state="DONE",
        need_action=False,
        done_at=now,
        done_reason="SELF_ACTION",
        display_title_snapshot=actor_title or workitem.title,
        display_subtitle_snapshot="由你完成",
        cursor_time=now,
        updated_at=now,
    )
    workitem.user_tasks.exclude(to_openid=actor_openid).update(
        state="DONE",
        need_action=False,
        done_at=now,
        done_reason="OTHER_ACTION",
        display_title_snapshot=peer_title or workitem.title,
        display_subtitle_snapshot=f"{_display_name(user)}已处理，同组织无需重复操作",
        cursor_time=now,
        updated_at=now,
    )
    return True


def _complete_workitem_all(workitem, user, *, allow_owner=False):
    """Complete an OWNER_OR_SALES task.

    The supplier owner can close the full delivery.  A supplier sales manager
    can close only its own task; the work item closes after every sales task
    configured in the immutable dispatch payload has completed.
    """

    now = timezone.now()
    actor_openid = _actor_openid(user)
    if not actor_openid:
        raise OrderPlanMaterialFlowError("当前账号没有有效微信标识，请重新登录。")
    if workitem.status == "DONE":
        # An already-completed shared task is a harmless retry, not a new
        # completion by the current caller.  Returning ``completed=True``
        # here let a different supplier user overwrite the audited
        # ``supplier_confirmed_by`` field in the caller.
        return False, False
    actor_task = workitem.user_tasks.select_for_update().filter(
        to_openid=actor_openid, need_action=True
    ).exclude(state="DONE").first()
    if not actor_task:
        raise OrderPlanMaterialFlowError("当前账号不是这项供应配送任务的处理人。")
    payload = dict(workitem.payload or {})
    scopes = payload.get("recipient_scopes") or {}
    scope = scopes.get(actor_openid) if isinstance(scopes, dict) else None
    if not isinstance(scope, dict):
        raise OrderPlanMaterialFlowError("供应责任快照缺失，请联系管理员核对。")
    is_owner = scope.get("role") == "supplier_owner"
    if is_owner and not allow_owner:
        raise OrderPlanMaterialFlowError("当前任务不允许负责人代为确认。")

    processed = list(payload.get("processed_actors") or [])
    if not any(item.get("openid") == actor_openid for item in processed if isinstance(item, dict)):
        processed.append(
            {
                "openid": actor_openid,
                "name": _display_name(user),
                "role": scope.get("role", ""),
                "at": now.isoformat(),
            }
        )
    payload["processed_actors"] = processed
    actor_task.state = "DONE"
    actor_task.need_action = False
    actor_task.done_at = now
    actor_task.done_reason = "SELF_ACTION"
    actor_task.cursor_time = now
    actor_task.updated_at = now
    actor_task.save(
        update_fields=(
            "state", "need_action", "done_at", "done_reason", "cursor_time", "updated_at"
        )
    )

    # Delivery policy is OWNER_OR_ALL_SALES, not OWNER_AND_ALL_SALES.  The
    # owner may close immediately; otherwise every *responsible sales* task
    # must finish.  The owner task itself must not block the all-sales path.
    # If the immutable dispatch snapshot contains an unassigned material,
    # there is no complete responsible-sales set and owner confirmation is
    # required.
    sales_openids = {
        openid
        for openid, recipient_scope in scopes.items()
        if isinstance(recipient_scope, dict)
        and recipient_scope.get("role") == "supplier_sales"
    }
    unassigned_material_ids = payload.get("unassigned_material_ids") or []
    remaining_sales = (
        workitem.user_tasks.filter(to_openid__in=sales_openids)
        .exclude(state="DONE")
        .exists()
        if sales_openids
        else False
    )
    completed = is_owner or (
        bool(sales_openids)
        and not remaining_sales
        and not unassigned_material_ids
    )
    if completed:
        workitem.status = "DONE"
        workitem.done_by_openid = actor_openid
        workitem.done_at = now
        workitem.last_activity_at = now
        workitem.payload = payload
        workitem.save(
            update_fields=(
                "status", "done_by_openid", "done_at", "last_activity_at", "payload", "updated_at"
            )
        )
        workitem.user_tasks.exclude(to_openid=actor_openid).exclude(state="DONE").update(
            state="DONE",
            need_action=False,
            done_at=now,
            done_reason="OTHER_ACTION",
            cursor_time=now,
            updated_at=now,
        )
    else:
        workitem.payload = payload
        workitem.last_activity_at = now
        workitem.save(update_fields=("payload", "last_activity_at", "updated_at"))
    return True, completed


def _positive_integer(value, label):
    if isinstance(value, bool):
        raise OrderPlanMaterialFlowError(f"{label}必须是正整数。")
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise OrderPlanMaterialFlowError(f"{label}必须是正整数。") from exc
    if value <= 0:
        raise OrderPlanMaterialFlowError(f"{label}必须大于0。")
    return value


def _optional_positive_integer(value, label):
    if value in (None, ""):
        return None
    return _positive_integer(value, label)


def _positive_quantity_milli(value, label):
    try:
        amount = Decimal(str(value))
    except Exception as exc:
        raise OrderPlanMaterialFlowError(f"{label}必须是大于0且最多三位小数的数量。") from exc
    if not amount.is_finite() or amount <= 0:
        raise OrderPlanMaterialFlowError(f"{label}必须大于0。")
    scaled = amount * Decimal("1000")
    if scaled != scaled.to_integral_value():
        raise OrderPlanMaterialFlowError(f"{label}最多只能填写三位小数。")
    result = int(scaled)
    if result <= 0:
        raise OrderPlanMaterialFlowError(f"{label}必须大于0。")
    return result


def _quantity_from_milli(value):
    return format((Decimal(int(value or 0)) / Decimal("1000")).normalize(), "f")


def _accessory_stock_snapshot(*, factory_id, material_spec_ids):
    spec_ids = {int(value) for value in material_spec_ids if value}
    if not spec_ids:
        return {}, {}
    units_by_spec = defaultdict(set)
    for row in FactoryMaterialStock.objects.filter(
        factory_id=factory_id,
        material_spec_id__in=spec_ids,
    ).values("material_spec_id", "unit"):
        units_by_spec[row["material_spec_id"]].add(row["unit"])
    availability = {
        row["stock__material_spec_id"]: int(row["available_milli"] or 0)
        for row in FactoryMaterialLot.objects.filter(
            stock__factory_id=factory_id,
            stock__material_spec_id__in=spec_ids,
        )
        .values("stock__material_spec_id")
        .annotate(available_milli=Sum(F("on_hand_milli") - F("reserved_milli")))
    }
    # A bad unit configuration for one spec should not block the whole order
    # plan form. Such a spec is omitted from the catalog and rejected if a
    # caller submits it; valid, unrelated accessory specs remain usable.
    units = {
        spec_id: next(iter(values))
        for spec_id, values in units_by_spec.items()
        if len(values) == 1
    }
    return units, availability


def _serialize_accessory_catalog(factory_id):
    compatibility = list(
        OrderPlanAccessoryCompatibility.objects.filter(
            factory_organization_id=factory_id,
            is_active=True,
        ).select_related(
            "product_model__category",
            "material_spec__material",
        ).order_by(
            "product_model__category__code",
            "material_spec__material__code",
            "material_spec__code",
            "product_model__code",
        )
    )
    spec_ids = {row.material_spec_id for row in compatibility}
    units, availability = _accessory_stock_snapshot(
        factory_id=factory_id,
        material_spec_ids=spec_ids,
    )
    categories = {}
    for row in compatibility:
        # A stock record supplies the authoritative quantity unit. A mapped
        # item without any factory stock record cannot be ordered yet.
        unit = units.get(row.material_spec_id)
        if not unit:
            continue
        category = row.product_model.category
        category_row = categories.setdefault(
            category.pk,
            {"id": category.pk, "name": category.name, "specs": {}},
        )
        spec = row.material_spec
        spec_row = category_row["specs"].setdefault(
            spec.pk,
            {
                "id": spec.pk,
                "material_id": spec.material_id,
                "material_name": spec.material.name,
                "material_spec_name": spec.name,
                "material_spec_code": spec.code,
                "display_name": f"{spec.material.name} · {spec.name}",
                "unit": unit,
                "available_milli": availability.get(spec.pk, 0),
                "available_quantity": _quantity_from_milli(availability.get(spec.pk, 0)),
                "models": [],
            },
        )
        spec_row["models"].append(
            {
                "id": row.product_model_id,
                "name": row.product_model.name,
                "code": row.product_model.code,
            }
        )
    return [
        {
            "id": category["id"],
            "name": category["name"],
            "specs": [
                {**spec, "models": sorted(spec["models"], key=lambda item: (item["code"], item["id"]))}
                for spec in sorted(category["specs"].values(), key=lambda item: (item["material_name"], item["material_spec_name"], item["id"]))
            ],
        }
        for category in sorted(categories.values(), key=lambda item: (item["name"], item["id"]))
    ]


def _validated_accessory_entries(*, factory_id, raw_items):
    if raw_items in (None, ""):
        return []
    if not isinstance(raw_items, list):
        raise OrderPlanMaterialFlowError("配件计划数据格式不正确。")
    if len(raw_items) > 50:
        raise OrderPlanMaterialFlowError("单个订单计划最多包含50项配件。")
    entries = []
    seen = set()
    required_by_spec = defaultdict(int)
    for index, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            raise OrderPlanMaterialFlowError(f"第{index}项配件数据格式不正确。")
        category_id = _positive_integer(raw.get("category_id"), f"第{index}项配件品类")
        product_model_id = _positive_integer(raw.get("product_model_id"), f"第{index}项适配型号")
        material_spec_id = _positive_integer(raw.get("material_spec_id"), f"第{index}项配件规格")
        quantity_milli = _positive_quantity_milli(raw.get("quantity"), f"第{index}项配件数量")
        key = (material_spec_id, product_model_id)
        if key in seen:
            raise OrderPlanMaterialFlowError("同一适配型号和物料规格不能重复添加。")
        seen.add(key)
        compatibility = OrderPlanAccessoryCompatibility.objects.select_related(
            "material_spec__material", "product_model__category"
        ).filter(
            factory_organization_id=factory_id,
            material_spec_id=material_spec_id,
            product_model_id=product_model_id,
            product_model__category_id=category_id,
            is_active=True,
        ).first()
        if not compatibility:
            raise OrderPlanMaterialFlowError(
                f"第{index}项配件与所选厂家、品类或产品型号不匹配。"
            )
        entries.append(
            {
                "index": index,
                "category": compatibility.product_model.category,
                "product_model": compatibility.product_model,
                "material_spec": compatibility.material_spec,
                "quantity_milli": quantity_milli,
            }
        )
        required_by_spec[material_spec_id] += quantity_milli

    units, availability = _accessory_stock_snapshot(
        factory_id=factory_id,
        material_spec_ids=required_by_spec,
    )
    specs = {
        row.pk: row
        for row in MaterialSpec.objects.select_related("material").filter(pk__in=required_by_spec)
    }
    for spec_id, required in required_by_spec.items():
        spec = specs[spec_id]
        unit = units.get(spec_id)
        if not unit:
            raise OrderPlanMaterialFlowError(f"配件“{spec.material.name}·{spec.name}”尚未建立厂家库存单位。")
        available = availability.get(spec_id, 0)
        if required > available:
            raise OrderPlanMaterialFlowError(
                f"配件“{spec.material.name}·{spec.name}”库存不足："
                f"需求{_quantity_from_milli(required)}{unit}，"
                f"当前可用{_quantity_from_milli(available)}{unit}。"
            )
        for entry in entries:
            if entry["material_spec"].pk == spec_id:
                entry["unit_snapshot"] = unit
    return entries


def _reserve_accessory_item(*, accessory_item, actor):
    remaining = accessory_item.quantity_milli
    lots = list(
        FactoryMaterialLot.objects.select_for_update()
        .select_related("stock")
        .filter(
            stock__factory_id=accessory_item.order_plan.factory_organization_id,
            stock__material_spec_id=accessory_item.material_spec_id,
            on_hand_milli__gt=F("reserved_milli"),
        )
        .order_by("received_at", "id")
    )
    available = sum(max(0, lot.on_hand_milli - lot.reserved_milli) for lot in lots)
    if available < remaining:
        raise OrderPlanMaterialFlowError(
            f"配件“{accessory_item.material_spec.material.name}·{accessory_item.material_spec.name}”库存刚刚变化，"
            f"需要{_quantity_from_milli(remaining)}{accessory_item.unit_snapshot}，"
            f"当前可用{_quantity_from_milli(available)}{accessory_item.unit_snapshot}。请刷新后重试。"
        )
    for lot in lots:
        if remaining <= 0:
            break
        amount = min(remaining, lot.on_hand_milli - lot.reserved_milli)
        if amount <= 0:
            continue
        changed = FactoryMaterialLot.objects.filter(
            pk=lot.pk,
            on_hand_milli__gte=F("reserved_milli") + amount,
        ).update(reserved_milli=F("reserved_milli") + amount)
        if changed != 1:
            raise OrderPlanMaterialFlowError("配件库存正在更新，请刷新后重试。")
        OrderPlanAccessoryLotAllocation.objects.create(
            accessory_item=accessory_item,
            material_lot=lot,
            reserved_milli=amount,
            reserved_by=actor,
        )
        remaining -= amount
    if remaining:
        raise OrderPlanMaterialFlowError("配件库存预占失败，请刷新后重试。")


def _milli_per_device(value, *, material_name):
    try:
        decimal = Decimal(str(value))
    except Exception as exc:
        raise OrderPlanMaterialFlowError(f"{material_name}的BOM单台用量无效。") from exc
    if not decimal.is_finite() or decimal <= 0:
        raise OrderPlanMaterialFlowError(f"{material_name}的BOM单台用量必须大于0。")
    scaled = (decimal * Decimal("1000")).to_integral_value(rounding=ROUND_CEILING)
    if scaled <= 0:
        raise OrderPlanMaterialFlowError(f"{material_name}的BOM单台用量必须大于0。")
    return int(scaled)


def _serialize_bom_option(bom, *, default=False, source=""):
    return {
        "id": bom.pk,
        "version": bom.version,
        "version_no": bom.version_no,
        "remark": bom.remark or "",
        "display_name": (
            f"{bom.version} · {bom.remark}" if bom.remark else bom.version
        ),
        "is_default": default,
        "source": source,
    }


def _material_queryset(item, bom):
    bom_material_ids = bom.items.values_list("material_id", flat=True)
    supplier_queryset = MaterialSpecSupplier.objects.select_related(
        "supplier", "supplier__organization_type"
    ).filter(
        supplier__is_active=True,
        supplier__organization_type__is_active=True,
        supplier__organization_type__code="SUPPLIER",
    ).order_by("supplier__name", "supplier_id")
    spec_queryset = MaterialSpec.objects.prefetch_related(
        Prefetch("suppliers", queryset=supplier_queryset)
    ).order_by("code", "id")
    return Material.objects.filter(
        Q(product_categories__id=item.category_id) | Q(pk__in=bom_material_ids)
    ).distinct().prefetch_related(
        Prefetch("specs", queryset=spec_queryset)
    ).order_by("code", "id")


def _serialize_materials(item, bom):
    bom_items = {
        row.material_id: row for row in bom.items.select_related(
            "material", "material_spec", "supplier"
        )
    }
    rows = []
    for material in _material_queryset(item, bom):
        bom_item = bom_items.get(material.pk)
        rows.append({
            "id": material.pk,
            "code": material.code,
            "name": material.name,
            "is_key_part": material.is_key_part,
            "deleted": bom_item is None,
            "selected_spec_id": bom_item.material_spec_id if bom_item else None,
            "selected_spec_name": bom_item.material_spec.name if bom_item and bom_item.material_spec else "",
            "selected_supplier_id": bom_item.supplier_id if bom_item else None,
            "selected_supplier_name": bom_item.supplier.name if bom_item and bom_item.supplier else "",
            "quantity": bom_item.quantity if bom_item else 1,
            "remark": bom_item.remark if bom_item else "",
            "specs": [{
                "id": spec.pk,
                "code": spec.code,
                "name": spec.name,
                "suppliers": [{"id": relation.supplier_id, "code": relation.supplier.code, "name": relation.supplier.name}
                              for relation in spec.suppliers.all()],
            } for spec in material.specs.all()],
        })
    return rows


def _normalize_material_override(item, source_bom, raw_materials):
    if not isinstance(raw_materials, list):
        raise OrderPlanMaterialFlowError(f"{item.product_model.name}的物料数据格式无效。")
    materials = {material.pk: material for material in _material_queryset(item, source_bom)}
    submitted = {}
    for raw in raw_materials:
        if not isinstance(raw, dict):
            raise OrderPlanMaterialFlowError(f"{item.product_model.name}的物料数据格式无效。")
        material_id = _optional_positive_integer(raw.get("material_id"), "物料")
        if material_id in submitted:
            raise OrderPlanMaterialFlowError("同一物料不能重复提交。")
        submitted[material_id] = raw
    if set(submitted) != set(materials):
        raise OrderPlanMaterialFlowError(f"{item.product_model.name}的物料清单已变化，请刷新页面后重试。")
    source_items = {row.material_id: row for row in source_bom.items.select_related("material_spec", "supplier")}
    normalized = []
    for material_id, material in materials.items():
        raw = submitted[material_id]
        if bool(raw.get("deleted")):
            continue
        source = source_items.get(material_id)
        spec_id = _optional_positive_integer(raw.get("material_spec_id"), f"{material.name}的物料规格")
        supplier_id = _optional_positive_integer(raw.get("supplier_id"), f"{material.name}的供应商")
        changed = source is None or spec_id != source.material_spec_id or supplier_id != source.supplier_id
        if changed:
            if not spec_id or not supplier_id:
                raise OrderPlanMaterialFlowError(f"请为{material.name}选择物料规格和供应商。")
            if not MaterialSpec.objects.filter(pk=spec_id, material_id=material_id).exists():
                raise OrderPlanMaterialFlowError(f"{material.name}的物料规格不匹配。")
            if not MaterialSpecSupplier.objects.filter(
                material_spec_id=spec_id, supplier_id=supplier_id,
                supplier__is_active=True, supplier__organization_type__code="SUPPLIER",
                supplier__organization_type__is_active=True,
            ).exists():
                raise OrderPlanMaterialFlowError(f"{material.name}所选供应商没有有效供应关系。")
        normalized.append({
            "material_id": material_id,
            "material_spec_id": spec_id,
            "supplier_id": supplier_id,
            "quantity": source.quantity if source else 1,
            "remark": str(raw.get("remark") or ""),
        })
    if not normalized:
        raise OrderPlanMaterialFlowError(f"{item.product_model.name}不能删除全部物料。")
    signature = lambda rows: sorted((row["material_id"], row["material_spec_id"], row["supplier_id"], float(row["quantity"]), row["remark"]) for row in rows)
    source_rows = [{
        "material_id": row.material_id,
        "material_spec_id": row.material_spec_id,
        "supplier_id": row.supplier_id,
        "quantity": row.quantity,
        "remark": row.remark or "",
    } for row in source_items.values()]
    return normalized, signature(normalized) != signature(source_rows)


def _create_sales_bom_version(*, item, source_bom, rows, user):
    ProductModel.objects.select_for_update().get(pk=item.product_model_id)
    version_no = (BOM.objects.filter(product_model_id=item.product_model_id).aggregate(value=Max("version_no")).get("value") or 0) + 1
    bom = BOM.objects.create(
        product_model_id=item.product_model_id,
        version_no=version_no,
        version=f"v{version_no}",
        remark=f"订单计划{item.plan.plan_code}销售冻结；继承自{source_bom.version}",
        is_active=True,
        created_by=user,
    )
    BOMItem.objects.bulk_create([BOMItem(bom=bom, **row) for row in rows])
    return bom


def _previous_v2_bom(*, factory_id, merchant_id, product_model_id):
    return (
        OrderPlanSalesBOMSelection.objects.select_related("frozen_bom")
        .filter(
            order_plan_item__plan__factory_organization_id=factory_id,
            order_plan_item__plan__merchant_organization_id=merchant_id,
            order_plan_item__product_model_id=product_model_id,
            order_plan_item__plan__flow_version=OrderPlan.FlowVersion.MATERIAL_STOCK_V2,
        )
        .exclude(order_plan_item__plan__status=OrderPlan.Status.CANCELLED)
        .order_by("-selected_at", "-id")
        .first()
    )


def get_material_flow_options(user, *, merchant_id=None):
    """Creation options with every active BOM selectable by sales."""

    try:
        from sales.services.order_plan_service import get_order_plan_options

        data = get_order_plan_options(user)
    except Exception as exc:
        raise OrderPlanMaterialFlowError(str(exc)) from exc

    # A selected merchant changes only the *suggested* BOM.  The public list
    # of active BOMs stays the same, and creation still validates the selected
    # ID again inside the transaction.  Keeping this as a suggestion prevents
    # a stale mini-program page from silently changing a sales decision.
    selected_merchant_id = None
    if merchant_id not in (None, ""):
        try:
            selected_merchant_id = int(merchant_id)
        except (TypeError, ValueError) as exc:
            raise OrderPlanMaterialFlowError("直属商家参数无效。") from exc
        visible_merchant_ids = {
            int(row["id"])
            for row in data.get("direct_merchants", [])
            if row.get("id") not in (None, "")
        }
        if selected_merchant_id not in visible_merchant_ids:
            raise OrderPlanMaterialFlowError("只能为本人直属商家查询BOM建议。")

    identity = _sales_identity(user)
    model_ids = [
        model["id"]
        for category in data.get("categories", [])
        for model in category.get("models", [])
        if model.get("id")
    ]
    boms_by_model = defaultdict(list)
    for bom in BOM.objects.filter(
        product_model_id__in=model_ids,
        is_active=True,
    ).order_by("product_model_id", "version_no", "id"):
        boms_by_model[bom.product_model_id].append(bom)

    category_rows = []
    public_boms = {}
    for category in data.get("categories", []):
        category_copy = dict(category)
        models = []
        for model in category.get("models", []):
            model_copy = dict(model)
            boms = boms_by_model.get(model_copy["id"], [])
            previous = (
                _previous_v2_bom(
                    factory_id=identity.organization_id,
                    merchant_id=selected_merchant_id,
                    product_model_id=model_copy["id"],
                )
                if selected_merchant_id
                else None
            )
            previous_bom_id = (
                previous.frozen_bom_id
                if previous and previous.frozen_bom.is_active
                and any(item.pk == previous.frozen_bom_id for item in boms)
                else None
            )
            default_bom = previous_bom_id or (boms[-1].pk if boms else None)
            options = [
                _serialize_bom_option(
                    item,
                    default=item.pk == default_bom,
                    source="previous_order" if item.pk == previous_bom_id else "latest_active" if item.pk == default_bom else "",
                )
                for item in boms
            ]
            model_copy["available_boms"] = options
            model_copy["default_bom"] = next((item for item in options if item["is_default"]), None)
            model_copy["recommended_bom_id"] = previous_bom_id
            models.append(model_copy)
            public_boms[str(model_copy["id"])] = options
        category_copy["models"] = models
        category_rows.append(category_copy)

    data["categories"] = category_rows
    data["bom_options_by_product_model"] = public_boms
    data["accessory_categories"] = _serialize_accessory_catalog(identity.organization_id)
    data["selected_merchant_id"] = selected_merchant_id
    data["material_flow_version"] = 2
    return data


def get_material_flow_bom_detail(
    *, user, category_id, product_model_id, source_bom_id
):
    """Return editable BOM source rows for sales before a V2 plan exists.

    The sales manager receives a read-only source snapshot before creation;
    submitted changes become a new frozen BOM version for this order.
    """

    _sales_identity(user)
    category_id = _positive_integer(category_id, "产品品类")
    product_model_id = _positive_integer(product_model_id, "产品型号")
    source_bom_id = _positive_integer(source_bom_id, "BOM版本")
    product_model = ProductModel.objects.select_related("category").filter(
        pk=product_model_id,
        category_id=category_id,
    ).first()
    if not product_model:
        raise OrderPlanMaterialFlowError("产品型号与品类不匹配或不存在。")
    bom = BOM.objects.filter(
        pk=source_bom_id,
        product_model=product_model,
        is_active=True,
    ).first()
    if not bom:
        raise OrderPlanMaterialFlowError("所选BOM不属于该产品型号或已停用。")

    draft_item = OrderPlanItem(
        category_id=product_model.category_id,
        product_model=product_model,
    )
    return {
        "product_model": {
            "id": product_model.pk,
            "code": product_model.code,
            "name": product_model.name,
        },
        "bom": _serialize_bom_option(bom),
        "materials": _serialize_materials(draft_item, bom),
    }


def _validated_creation_items(raw_items):
    if not isinstance(raw_items, list) or not raw_items:
        raise OrderPlanMaterialFlowError("请至少添加一个产品型号。")
    if len(raw_items) > 50:
        raise OrderPlanMaterialFlowError("单个订单计划最多包含50个产品型号。")

    entries = []
    model_ids = set()
    for index, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            raise OrderPlanMaterialFlowError(f"第{index}项产品数据格式不正确。")
        category_id = _positive_integer(raw.get("category_id"), f"第{index}项品类")
        model_id = _positive_integer(raw.get("product_model_id"), f"第{index}项型号")
        if model_id in model_ids:
            raise OrderPlanMaterialFlowError("同一个产品型号不能重复添加。")
        model_ids.add(model_id)
        entries.append(
            {
                "index": index,
                "category_id": category_id,
                "product_model_id": model_id,
                "quantity": _positive_integer(raw.get("quantity"), f"第{index}项数量"),
                "use_return_inventory": bool(raw.get("use_return_inventory", False)),
                "source_bom_id": _positive_integer(raw.get("source_bom_id"), f"第{index}项BOM版本"),
                # Kept API-ready for the sales-side BOM-editor interaction.
                "materials": raw.get("materials"),
            }
        )
    models = {
        model.pk: model
        for model in ProductModel.objects.select_related("category").filter(pk__in=model_ids)
    }
    for entry in entries:
        product_model = models.get(entry["product_model_id"])
        if not product_model:
            raise OrderPlanMaterialFlowError(f"第{entry['index']}项产品型号不存在。")
        if product_model.category_id != entry["category_id"]:
            raise OrderPlanMaterialFlowError(f"第{entry['index']}项产品型号与品类不匹配。")
        bom = BOM.objects.filter(
            pk=entry["source_bom_id"],
            product_model=product_model,
            is_active=True,
        ).first()
        if not bom:
            raise OrderPlanMaterialFlowError(
                f"第{entry['index']}项所选BOM不属于{product_model.name}或已停用。"
            )
        entry["category"] = product_model.category
        entry["product_model"] = product_model
        entry["source_bom"] = bom
    return entries


def _bom_snapshot(bom):
    return {
        "bom_id": bom.pk,
        "version": bom.version,
        "version_no": bom.version_no,
        "items": [
            {
                "bom_item_id": row.pk,
                "material_id": row.material_id,
                "material_name": row.material.name,
                "material_spec_id": row.material_spec_id,
                "material_spec_name": row.material_spec.name if row.material_spec_id else "",
                "supplier_id": row.supplier_id,
                "supplier_name": row.supplier.name if row.supplier_id else "",
                "quantity_per_device": str(row.quantity),
                "remark": row.remark or "",
            }
            for row in bom.items.select_related("material", "material_spec", "supplier").order_by("id")
        ],
    }


def _validate_bom_supply_lines(bom, product_name):
    lines = list(
        bom.items.select_related("material", "material_spec", "supplier").order_by("id")
    )
    if not lines:
        raise OrderPlanMaterialFlowError(f"{product_name}的BOM没有物料行，不能创建订单。")
    for line in lines:
        if not line.material_spec_id or not line.supplier_id:
            raise OrderPlanMaterialFlowError(
                f"{product_name}的BOM物料“{line.material.name}”未设置规格或供应商。"
            )
        if line.material_spec.material_id != line.material_id:
            raise OrderPlanMaterialFlowError(
                f"{product_name}的BOM物料“{line.material.name}”规格不匹配。"
            )
        supplied = MaterialSpecSupplier.objects.filter(
            material_spec_id=line.material_spec_id,
            supplier_id=line.supplier_id,
            supplier__is_active=True,
            supplier__organization_type__code="SUPPLIER",
            supplier__organization_type__is_active=True,
        ).exists()
        if not supplied:
            raise OrderPlanMaterialFlowError(
                f"{product_name}的BOM物料“{line.material.name}”没有有效供应关系。"
            )
        _milli_per_device(line.quantity, material_name=line.material.name)
    return lines


def _normalize_optional_material_override(order_item, source_bom, raw_materials, user):
    """Create a new BOM only when the sales payload really changes it.

    The current mini-program submits only ``source_bom_id``.  Keeping this
    branch here makes the service ready for the next BOM editor without making
    the old matching-manager confirmation route part of V2 again.
    """

    if raw_materials is None:
        return source_bom, False
    try:
        rows, changed = _normalize_material_override(order_item, source_bom, raw_materials)
        if not changed:
            return source_bom, False
        return _create_sales_bom_version(
            item=order_item,
            source_bom=source_bom,
            rows=rows,
            user=user,
        ), True
    except OrderPlanMaterialFlowError as exc:
        raise OrderPlanMaterialFlowError(str(exc)) from exc


def _candidate_first_subplan_quantity(production_rows):
    """Choose an optional small-tail-stock production slice.

    A product model is normally one production plan, irrespective of its
    quantity.  Splitting is an exception used only to clear a remaining lot
    of at most 50 devices.  The first slice therefore has to be a complete,
    stock-feasible material bundle *and* exhaust at least one such small lot.
    The rest is one unrestricted main production plan.

    This prevents two opposite mistakes: forcing ``50 + 50 + ...`` for a
    normal large order, and creating a meaningless ``5 + remainder`` split
    merely because a small quantity happens to be in stock.
    """

    total = sum(row["production_quantity"] for row in production_rows)
    if total < MIN_SPLIT_SUBPLAN_QUANTITY * 2:
        return None
    factory_id = production_rows[0]["order_item"].plan.factory_organization_id
    material_keys = set()
    for row in production_rows:
        for bom_item in row["bom_lines"]:
            material_keys.add((bom_item.supplier_id, bom_item.material_spec_id))
    if not material_keys:
        return None

    lot_filter = Q()
    for supplier_id, spec_id in material_keys:
        lot_filter |= Q(
            stock__supplier_id=supplier_id,
            stock__material_spec_id=spec_id,
        )
    lots_by_key = defaultdict(list)
    for lot in (
        FactoryMaterialLot.objects.select_related("stock")
        .filter(
            lot_filter,
            stock__factory_id=factory_id,
            on_hand_milli__gt=F("reserved_milli"),
        )
        .order_by("stock__supplier_id", "stock__material_spec_id", "on_hand_milli", "received_at", "id")
    ):
        available = lot.on_hand_milli - lot.reserved_milli
        if available > 0:
            lots_by_key[(lot.stock.supplier_id, lot.stock.material_spec_id)].append(
                (available, lot.pk)
            )

    # Only the tail-clearing child is constrained to 5--50.  The remaining
    # main child plan is intentionally not capped: 15 + 135 is valid.
    lower = MIN_SPLIT_SUBPLAN_QUANTITY
    upper = min(MAX_TAIL_STOCK_SUBPLAN_QUANTITY, total - MIN_SPLIT_SUBPLAN_QUANTITY)
    if lower > upper:
        return None
    best = None
    for candidate in range(lower, upper + 1):
        slices = _split_item_quantities(production_rows, candidate)
        if len(slices) != 2:
            continue
        first_required = defaultdict(int)
        for slice_row in slices[0]:
            row = slice_row["row"]
            for bom_item in row["bom_lines"]:
                first_required[(bom_item.supplier_id, bom_item.material_spec_id)] += (
                    _milli_per_device(
                        bom_item.quantity,
                        material_name=bom_item.material.name,
                    )
                    * slice_row["quantity"]
                )
        # Every material of every item in the first child plan must be
        # completely present.  Any short material means this split would only
        # create a second task without enabling earlier production.
        if not first_required or any(
            not any(available >= required for available, _lot_id in lots_by_key[key])
            for key, required in first_required.items()
        ):
            continue

        cleared_lot_sizes = []
        for key, required in first_required.items():
            selected_available, _selected_lot_id = min(
                (lot for lot in lots_by_key[key] if lot[0] >= required),
                key=lambda lot: (lot[0], lot[1]),
            )
            if selected_available == required:
                cleared_lot_sizes.append(selected_available)
        # Do not create a second production operation unless this first child
        # clears at least one complete small-tail lot.  A partial use of every
        # lot is not a meaningful inventory-consolidation split.
        if not cleared_lot_sizes:
            continue
        # Prefer clearing more lots; for an equal count clear the smaller
        # tails first, then choose the smaller operational child plan.
        score = (
            len(cleared_lot_sizes),
            -max(cleared_lot_sizes),
            -sum(cleared_lot_sizes),
            -candidate,
        )
        if best is None or score > best[0]:
            best = (score, candidate)
    if best:
        return best[1]
    return None


def _split_item_quantities(production_rows, first_total):
    total = sum(row["production_quantity"] for row in production_rows)
    if not first_total or first_total <= 0 or first_total >= total:
        return [
            [{"row": row, "quantity": row["production_quantity"]} for row in production_rows]
        ]
    first = []
    remainders = []
    assigned = 0
    for index, row in enumerate(production_rows):
        raw = Decimal(row["production_quantity"]) * Decimal(first_total) / Decimal(total)
        value = int(raw.to_integral_value(rounding="ROUND_FLOOR"))
        value = min(value, row["production_quantity"])
        first.append({"row": row, "quantity": value})
        assigned += value
        remainders.append((raw - value, index))
    for _remainder, index in sorted(remainders, key=lambda value: (-value[0], value[1])):
        if assigned >= first_total:
            break
        row = production_rows[index]
        if first[index]["quantity"] < row["production_quantity"]:
            first[index]["quantity"] += 1
            assigned += 1
    if assigned != first_total or not any(item["quantity"] for item in first):
        return [[{"row": row, "quantity": row["production_quantity"]} for row in production_rows]]
    second = [
        {"row": row, "quantity": row["production_quantity"] - first[index]["quantity"]}
        for index, row in enumerate(production_rows)
    ]
    return [
        [item for item in first if item["quantity"] > 0],
        [item for item in second if item["quantity"] > 0],
    ]


def _production_rows_by_product_model(production_rows):
    """Keep every product model's traceability split independent.

    ``OrderPlanMaterialPlan`` remains the order-level planning container, but
    its child production subplans are model-specific.  Mixing two product
    models in one subplan makes a global two-subplan ceiling and can couple
    unrelated BOM-lot decisions.
    """

    groups = defaultdict(list)
    for row in production_rows:
        groups[row["order_item"].product_model_id].append(row)
    return [groups[key] for key in sorted(groups)]


def _subplan_slice_totals_are_valid(slice_totals):
    """Validate one model's normal-or-tail-split production quantities."""

    if not slice_totals or len(slice_totals) > MAX_SUBPLAN_COUNT:
        return False
    if any(total <= 0 for total in slice_totals):
        return False
    if len(slice_totals) == 1:
        return True
    # The first slice is the intentionally selected small tail.  Its
    # complement is the model's ordinary main production quantity and has no
    # artificial 50-unit ceiling.
    return MIN_SPLIT_SUBPLAN_QUANTITY <= slice_totals[0] <= MAX_TAIL_STOCK_SUBPLAN_QUANTITY


def _create_subplans_and_requirements(material_plan, production_rows, user):
    result = []
    next_sequence_no = 1
    for model_rows in _production_rows_by_product_model(production_rows):
        first_total = _candidate_first_subplan_quantity(model_rows)
        slices = _split_item_quantities(model_rows, first_total)
        slice_totals = [sum(row["quantity"] for row in slice_rows) for slice_rows in slices]
        product_name = model_rows[0]["order_item"].product_model.name
        if not _subplan_slice_totals_are_valid(slice_totals):
            raise OrderPlanMaterialFlowError(
                f"产品型号“{product_name}”生产子计划数量无效；"
                "常规生产为一个计划，消解尾货时仅允许拆为5至50台的尾货计划加一个主计划。"
            )
        for slice_rows in slices:
            subplan = OrderPlanProductionSubplan.objects.create(
                material_plan=material_plan,
                sequence_no=next_sequence_no,
                planned_total_quantity=sum(row["quantity"] for row in slice_rows),
            )
            next_sequence_no += 1
            result.append(subplan)
            for slice_row in slice_rows:
                row = slice_row["row"]
                order_item = row["order_item"]
                sub_item = OrderPlanProductionSubplanItem.objects.create(
                    production_subplan=subplan,
                    order_plan_item=order_item,
                    quantity=slice_row["quantity"],
                    production_batch_code=_new_code("PB"),
                )
                stock_units = {
                    stock.material_spec_id: stock.unit
                    for stock in FactoryMaterialStock.objects.filter(
                        factory_id=material_plan.order_plan.factory_organization_id,
                        supplier_id__in=[item.supplier_id for item in row["bom_lines"]],
                        material_spec_id__in=[item.material_spec_id for item in row["bom_lines"]],
                    )
                }
                for bom_item in row["bom_lines"]:
                    per_device = _milli_per_device(
                        bom_item.quantity,
                        material_name=bom_item.material.name,
                    )
                    OrderPlanMaterialRequirement.objects.create(
                        production_subplan_item=sub_item,
                        sales_bom_selection=row["selection"],
                        bom_item=bom_item,
                        material=bom_item.material,
                        material_spec=bom_item.material_spec,
                        supplier=bom_item.supplier,
                        unit_snapshot=stock_units.get(bom_item.material_spec_id, ""),
                        quantity_milli_per_device=per_device,
                        required_milli=per_device * slice_row["quantity"],
                    )
    return result


def _allocation_available(allocation):
    return max(
        0,
        allocation.reserved_milli - allocation.consumed_milli - allocation.released_milli,
    )


def _reserve_requirement(
    requirement,
    *,
    actor,
    receipt_lot_id=None,
    receipt_requirement_deltas=None,
):
    """Reserve one complete material lot for one subplan requirement.

    A requirement represents one material/spec in one production subplan.  It
    must never be pieced together from two lots: otherwise devices in that
    subplan cannot retain a single, auditable material-batch reference.
    """

    remaining = requirement.required_milli - requirement.reserved_milli
    if remaining <= 0:
        return 0
    lot_rows = list(
        FactoryMaterialLot.objects.select_for_update()
        .select_related("stock")
        .filter(
            stock__factory_id=requirement.production_subplan_item.production_subplan.material_plan.order_plan.factory_organization_id,
            stock__supplier_id=requirement.supplier_id,
            stock__material_spec_id=requirement.material_spec_id,
            on_hand_milli__gt=F("reserved_milli"),
        )
        .order_by("on_hand_milli", "received_at", "id")
    )
    # Choose the smallest complete lot, which preserves the original inventory
    # reduction preference without breaking one-lot traceability.
    for lot in lot_rows:
        available = lot.on_hand_milli - lot.reserved_milli
        if available < remaining:
            continue
        amount = remaining
        changed = FactoryMaterialLot.objects.filter(
            pk=lot.pk,
            on_hand_milli__gte=F("reserved_milli") + amount,
        ).update(reserved_milli=F("reserved_milli") + amount)
        if changed != 1:
            # A concurrent normal receipt / cancellation changed the lot.  The
            # outer transaction will roll back and the caller can retry.
            raise OrderPlanMaterialFlowError("物料库存正在更新，请刷新后重试。")
        OrderPlanMaterialLotAllocation.objects.create(
            requirement=requirement,
            material_lot=lot,
            reserved_milli=amount,
            reserved_by=actor,
        )
        # A normal material receipt remains an independent warehouse action.
        # When its hook has the freshly created lot, retain only the portion
        # that was *newly allocated from that exact lot*.  Existing
        # allocations (and allocations from an older lot) must never be
        # reclassified as this supplier-delivery receipt.
        if (
            receipt_lot_id is not None
            and receipt_requirement_deltas is not None
            and lot.pk == receipt_lot_id
        ):
            receipt_requirement_deltas[requirement.pk] += amount
        requirement.reserved_milli = F("reserved_milli") + amount
        requirement.save(update_fields=("reserved_milli", "updated_at"))
        requirement.refresh_from_db(fields=("reserved_milli",))
        return amount
    return 0


def _reserve_open_requirements(*, material_plan, actor, stock=None, receipt_lot=None):
    requirements = OrderPlanMaterialRequirement.objects.select_for_update().select_related(
        "production_subplan_item__production_subplan__material_plan__order_plan",
        "material",
        "material_spec",
        "supplier",
    ).filter(
        production_subplan_item__production_subplan__material_plan=material_plan,
        reserved_milli__lt=F("required_milli"),
    )
    if stock is not None:
        requirements = requirements.filter(
            supplier_id=stock.supplier_id,
            material_spec_id=stock.material_spec_id,
        )
    receipt_lot_id = getattr(receipt_lot, "pk", None) if receipt_lot is not None else None
    receipt_requirement_deltas = defaultdict(int)
    totals = {
        "requirements": 0,
        "reserved_milli": 0,
        # Internal receipt-attribution data used only by the independent
        # warehouse receipt hook.  It is intentionally keyed by requirement
        # rather than by the material-plan total, because one supplier
        # delivery can be complete while another is still outstanding.
        "receipt_requirement_deltas": receipt_requirement_deltas,
    }
    for requirement in requirements.order_by(
        "production_subplan_item__production_subplan__sequence_no",
        "production_subplan_item_id",
        "id",
    ):
        amount = _reserve_requirement(
            requirement,
            actor=actor,
            receipt_lot_id=receipt_lot_id,
            receipt_requirement_deltas=receipt_requirement_deltas,
        )
        if amount:
            totals["requirements"] += 1
            totals["reserved_milli"] += amount
    return totals


def _material_summary(requirements):
    rows = defaultdict(lambda: {"required": 0, "reserved": 0, "consumed": 0})
    for requirement in requirements:
        key = (requirement.supplier_id, requirement.material_spec_id)
        row = rows[key]
        row["required"] += requirement.required_milli
        row["reserved"] += requirement.reserved_milli
        row["consumed"] += requirement.consumed_milli
    return rows


def _all_materials_reserved(material_plan):
    return not OrderPlanMaterialRequirement.objects.filter(
        production_subplan_item__production_subplan__material_plan=material_plan,
        reserved_milli__lt=F("required_milli"),
    ).exists()


def _delivery_requirements_reserved(delivery):
    """Whether this delivery's own planned shortages are fully reserved.

    Do not use the whole material plan here.  A multi-supplier order may have
    one supplier's material ready while another supplier still has a genuine
    shortage; closing the first delivery is both accurate and useful in the
    dynamic order detail.
    """

    allocations = list(
        OrderPlanSupplyDeliveryRequirement.objects.select_related("requirement").filter(
            delivery_line__delivery=delivery
        )
    )
    return bool(allocations) and all(
        allocation.requirement.reserved_milli >= allocation.requirement.required_milli
        for allocation in allocations
    )


def _delivery_fully_received(delivery):
    """True only after every supplier-confirmed delivery line is received."""

    return not delivery.lines.filter(
        confirmed_milli__gt=0,
        received_milli__lt=F("confirmed_milli"),
    ).exists()


def _all_delivery_commitments_confirmed(material_plan):
    """Whether every delivery created by the first review is supplier-confirmed.

    No rows means the plan was fully covered by normal stock, which is a valid
    no-supplier-delivery path.  A cancelled delivery never satisfies this
    predicate: cancellation must finish the whole order instead of releasing
    production from an obsolete shortage snapshot.
    """

    return not material_plan.supply_deliveries.exclude(
        status__in=(
            OrderPlanSupplyDelivery.Status.SUPPLIER_CONFIRMED,
            OrderPlanSupplyDelivery.Status.RECEIVING,
            OrderPlanSupplyDelivery.Status.SATISFIED,
        )
    ).exists()


def _next_dispatch_window(now=None):
    if SUPPLIER_DELIVERY_DISPATCH_MODE == "IMMEDIATE_TEST":
        return now or timezone.now()
    now = timezone.localtime(now or timezone.now())
    for hour in DISPATCH_HOURS:
        candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if now < candidate:
            return candidate
    return (now + timedelta(days=1)).replace(
        hour=DISPATCH_HOURS[0], minute=0, second=0, microsecond=0
    )


def _dispatch_window_key(when):
    local = timezone.localtime(when)
    return local.strftime("%Y%m%d%H")


def _serialize_order_accessory_item(row):
    return {
        "id": row.pk,
        "category_id": row.category_id,
        "category_name": row.category.name,
        "product_model_id": row.product_model_id,
        "product_model_name": row.product_model.name,
        "material_id": row.material_spec.material_id,
        "material_name": row.material_spec.material.name,
        "material_spec_id": row.material_spec_id,
        "material_spec_name": row.material_spec.name,
        "quantity_milli": row.quantity_milli,
        "quantity": _quantity_from_milli(row.quantity_milli),
        "unit": row.unit_snapshot,
        "status": row.status,
        "status_name": row.get_status_display(),
    }


def _serialize_order_accessory_items(plan):
    return [
        _serialize_order_accessory_item(row)
        for row in plan.accessory_items.select_related(
            "category", "product_model", "material_spec__material"
        ).order_by("id")
    ]


def _creation_result(plan):
    data = _serialize_plan(plan)
    data["flow_version"] = plan.flow_version
    data["detail_link"] = _plan_link(plan)
    data["accessory_items"] = _serialize_order_accessory_items(plan)
    from lifecycle.services.replacement_flow_service import pending_for_plan
    return {"plan": data, "pending_replacement_demands": pending_for_plan(plan)}


@transaction.atomic
def create_material_flow_order_plan(
    *, user, merchant_id, client_request_id, items, accessories=None, remark=""
):
    """Sales creates a V2 plan and immediately reserves finished goods/lots."""

    identity = _sales_identity(user)
    try:
        relation = _get_direct_relation(user, identity, merchant_id)
    except Exception as exc:
        raise OrderPlanMaterialFlowError(str(exc)) from exc
    client_request_id = str(client_request_id or "").strip()
    if not client_request_id or len(client_request_id) > 64:
        raise OrderPlanMaterialFlowError("客户端请求标识无效，请刷新页面后重试。")
    existing = _plan_queryset().filter(
        created_by=user,
        client_request_id=client_request_id,
    ).first()
    if existing:
        if existing.flow_version != OrderPlan.FlowVersion.MATERIAL_STOCK_V2:
            raise OrderPlanMaterialFlowError("该提交标识已用于旧订单，请刷新页面后重新创建。")
        initialize_order_finance(existing, user=user)
        return _creation_result(existing), False

    assignment = (
        MerchantChannelAssignment.objects.select_for_update()
        .select_related("brand")
        .filter(relation=relation, assigned_by=user, is_active=True)
        .first()
    )
    if not assignment:
        raise OrderPlanMaterialFlowError("请先为该直接商家指定负责区域和品牌。")
    entries = _validated_creation_items(items)
    accessory_entries = _validated_accessory_entries(
        factory_id=identity.organization_id,
        raw_items=accessories,
    )
    plan = OrderPlan.objects.create(
        plan_code=_new_code("OP"),
        client_request_id=client_request_id,
        factory_organization=identity.organization,
        merchant_organization=relation.target_organization,
        merchant_relation=relation,
        channel_assignment=assignment,
        brand=assignment.brand,
        region_snapshot=assignment.region,
        created_by=user,
        creator_role_code=identity.role.code,
        total_quantity=sum(entry["quantity"] for entry in entries),
        remark=str(remark or "").strip()[:2000],
        status=OrderPlan.Status.MATCHING_PENDING,
        flow_version=OrderPlan.FlowVersion.MATERIAL_STOCK_V2,
    )

    for entry in accessory_entries:
        accessory_item = OrderPlanAccessoryItem.objects.create(
            order_plan=plan,
            category=entry["category"],
            product_model=entry["product_model"],
            material_spec=entry["material_spec"],
            quantity_milli=entry["quantity_milli"],
            unit_snapshot=entry["unit_snapshot"],
        )
        _reserve_accessory_item(accessory_item=accessory_item, actor=user)

    production_rows = []
    for entry in entries:
        order_item = OrderPlanItem.objects.create(
            plan=plan,
            category=entry["category"],
            product_model=entry["product_model"],
            quantity=entry["quantity"],
            return_inventory_quantity=0,
            production_quantity=entry["quantity"],
            order_batch_code=_new_code("OB", entry["product_model"].code),
            lifecycle_code="",  # created only when a physical production batch is made
        )
        # ``lifecycle_code`` is required by the shared device schema.
        from lifecycle.utils.id_generator import gen_life_id

        order_item.lifecycle_code = gen_life_id()
        allocations = reserve_return_inventory(
            order_plan_item=order_item,
            requested_quantity=entry["quantity"] if entry["use_return_inventory"] else 0,
            actor=user,
        )
        order_item.return_inventory_quantity = len(allocations)
        order_item.production_quantity = entry["quantity"] - len(allocations)
        order_item.save(
            update_fields=(
                "return_inventory_quantity",
                "production_quantity",
                "lifecycle_code",
                "updated_at",
            )
        )

        frozen_bom, has_changes = _normalize_optional_material_override(
            order_item,
            entry["source_bom"],
            entry["materials"],
            user,
        )
        # A plan which is completely satisfied by finished-goods inventory
        # does not consume this order's production BOM at all.  Keep the
        # selected BOM as an audit snapshot, but do not reject the order just
        # because a historical BOM has no supplier/spec mapping that would
        # only be required for new production.
        bom_lines = (
            _validate_bom_supply_lines(frozen_bom, order_item.product_model.name)
            if order_item.production_quantity
            else []
        )
        selection = OrderPlanSalesBOMSelection.objects.create(
            order_plan_item=order_item,
            source_bom=entry["source_bom"],
            frozen_bom=frozen_bom,
            selected_by=user,
            has_changes=has_changes,
            selection_snapshot=_bom_snapshot(frozen_bom),
        )
        # Compatibility fact only: old code/data statistics can continue to
        # resolve an item's final BOM, but the V2 actor remains the sales
        # manager and no matching-manager task is created.
        OrderPlanItemBOMConfirmation.objects.create(
            order_plan_item=order_item,
            source_bom=entry["source_bom"],
            confirmed_bom=frozen_bom,
            confirmed_by=user,
            has_changes=has_changes,
        )
        if order_item.production_quantity:
            production_rows.append(
                {
                    "order_item": order_item,
                    "production_quantity": order_item.production_quantity,
                    "selection": selection,
                    "frozen_bom": frozen_bom,
                    "bom_lines": bom_lines,
                }
            )
        else:
            order_item.status = OrderPlan.Status.MATCHING_COMPLETED
            order_item.save(update_fields=("status", "updated_at"))

    material_plan = OrderPlanMaterialPlan.objects.create(
        order_plan=plan,
        algorithm_version=ALGORITHM_VERSION,
        status=(
            OrderPlanMaterialPlan.Status.WAITING_WAREHOUSE_REVIEW
            if production_rows
            else OrderPlanMaterialPlan.Status.PRODUCTION_COMPLETED
        ),
    )
    if production_rows:
        _create_subplans_and_requirements(material_plan, production_rows, user)
        reservation_result = _reserve_open_requirements(material_plan=material_plan, actor=user)
        requirements = list(
            OrderPlanMaterialRequirement.objects.filter(
                production_subplan_item__production_subplan__material_plan=material_plan
            ).select_related("material", "material_spec", "supplier")
        )
        OrderPlanMaterialPlanningRun.objects.create(
            material_plan=material_plan,
            run_no=1,
            algorithm_version=ALGORITHM_VERSION,
            input_snapshot={
                "production_quantity": sum(row["production_quantity"] for row in production_rows),
                "subplan_count": material_plan.production_subplans.count(),
                "rule": "existing_stock_then_smallest_lot_then_minimize_split",
            },
            output_snapshot={
                "reserved_milli": reservation_result["reserved_milli"],
                "requirements": [
                    {
                        "material_id": row.material_id,
                        "material_spec_id": row.material_spec_id,
                        "supplier_id": row.supplier_id,
                        "required_milli": row.required_milli,
                        "reserved_milli": row.reserved_milli,
                    }
                    for row in requirements
                ],
            },
            score_snapshot={
                "stock_first": True,
                "subplan_count": material_plan.production_subplans.count(),
            },
            created_by=user,
        )
        keeper_openids = _role_openids(plan.factory_organization, {"factory_material_stock"})
        if not keeper_openids:
            raise OrderPlanMaterialFlowError("厂家尚未配置有效物料库管，不能提交需要新生产的订单。")
        _new_workitem(
            plan=plan,
            work_type="ORDER_PLAN_MATERIAL_REVIEW",
            stage_key="material-review",
            title="新订单计划待核对物料",
            summary=(
                f"{plan.merchant_organization.name}｜新生产"
                f"{sum(row['production_quantity'] for row in production_rows)}台；"
                "请核对物料预占和配送缺口。"
            ),
            recipients=keeper_openids,
            need_action=True,
            action_mode="ANYONE_DO",
            payload={"material_plan_id": material_plan.pk},
            actor_openid=_actor_openid(user),
        )
        _new_workitem(
            plan=plan,
            work_type="ORDER_PLAN_MATERIAL_CREATED_NOTICE",
            stage_key="material-created:matching",
            title="订单物料计划已生成",
            summary="物料库管将核对库存和供应配送计划。",
            recipients=_role_openids(plan.factory_organization, {"factory_matching"}),
            need_action=False,
            payload={"material_plan_id": material_plan.pk},
            actor_openid=_actor_openid(user),
            done_immediately=True,
        )
    else:
        # No material production is required.  The later V2 shipment bridge can
        # operate directly on the reserved finished-goods allocations.
        plan.status = OrderPlan.Status.PRODUCTION_COMPLETED
        plan.save(update_fields=("status", "updated_at"))
        _new_workitem(
            plan=plan,
            work_type="ORDER_PLAN_FINISHED_STOCK_READY",
            stage_key="finished-stock-ready",
            title="订单已由成品库存满足",
            summary="无需新生产；请按后续分批打码、发货流程办理。",
            recipients=_role_openids(plan.factory_organization, {"factory_sales", "factory_stock"}),
            need_action=False,
            payload={"material_plan_id": material_plan.pk},
            actor_openid=_actor_openid(user),
            done_immediately=True,
        )

    # Finished goods are a first-class source in V2.  They may be dispatched
    # before the production remainder completes, so their shipment is created
    # independently instead of being forced into a later production subplan.
    _ensure_finished_inventory_shipment(
        plan=plan,
        actor_openid=_actor_openid(user),
    )

    initialize_order_finance(plan, user=user)
    from sales.services.replacement_supply_service import auto_attach
    auto_attach(plan,user)
    return _creation_result(plan), True


def _next_shipment_sequence(plan):
    return (
        OrderPlanShipment.objects.filter(order_plan=plan).aggregate(
            value=Max("sequence_no")
        )["value"]
        or 0
    ) + 1


def _sales_openid_for_plan(plan):
    openid = _actor_openid(plan.created_by)
    if not openid:
        raise OrderPlanMaterialFlowError(
            "创建订单的厂家销售经理没有有效微信账号，不能下达分批发货任务。"
        )
    return openid


def _shipment_label(shipment):
    if shipment.production_subplan_id:
        return f"子计划{shipment.production_subplan.sequence_no}·发货批次{shipment.sequence_no}"
    return f"成品库存·发货批次{shipment.sequence_no}"


def _shipment_quantity(shipment):
    return sum(shipment.items.values_list("quantity", flat=True))


def _shipment_accessory_rows(shipment):
    return [
        {
            "id": row.pk,
            "order_plan_accessory_item_id": row.accessory_item_id,
            "category_id": row.accessory_item.category_id,
            "category_name": row.accessory_item.category.name,
            "product_model_id": row.accessory_item.product_model_id,
            "product_model_name": row.accessory_item.product_model.name,
            "material_id": row.accessory_item.material_spec.material_id,
            "material_name": row.accessory_item.material_spec.material.name,
            "material_spec_id": row.accessory_item.material_spec_id,
            "material_spec_name": row.accessory_item.material_spec.name,
            "quantity_milli": row.quantity_milli,
            "quantity": _quantity_from_milli(row.quantity_milli),
            "unit": row.unit_snapshot,
            "status": row.accessory_item.status,
            "status_name": row.accessory_item.get_status_display(),
        }
        for row in shipment.accessory_items.select_related(
            "accessory_item__category",
            "accessory_item__product_model",
            "accessory_item__material_spec__material",
        ).order_by("id")
    ]


def _shipment_accessory_summary(accessory_rows):
    return "；配件 " + "、".join(
        f"{row['material_name']} {row['quantity']}{row['unit']}"
        for row in accessory_rows
    ) if accessory_rows else ""


def _shipment_payload(shipment):
    shipment = (
        OrderPlanShipment.objects.select_related(
            "order_plan__factory_organization",
            "order_plan__merchant_organization",
            "order_plan__brand",
            "production_subplan",
            "logistics_confirmed_by",
            "logistics_document",
        )
        .prefetch_related(
            "items__order_plan_item__product_model",
            "items__production_subplan_item",
            "accessory_items__accessory_item__category",
            "accessory_items__accessory_item__product_model",
            "accessory_items__accessory_item__material_spec__material",
        )
        .get(pk=shipment.pk)
    )
    accessory_rows = _shipment_accessory_rows(shipment)
    return {
        "shipment_id": shipment.pk,
        "status": shipment.status,
        "status_name": shipment.get_status_display(),
        "shipment_sequence_no": shipment.sequence_no,
        "shipment_label": _shipment_label(shipment),
        "production_subplan_id": shipment.production_subplan_id,
        "order_plan_id": shipment.order_plan_id,
        "plan_code": shipment.order_plan.plan_code,
        "merchant_name": shipment.order_plan.merchant_organization.name,
        "brand_name": shipment.order_plan.brand.name,
        "quantity": _shipment_quantity(shipment),
        "logistics_company": shipment.logistics_company,
        "tracking_no": shipment.tracking_no,
        "logistics_document": _serialize_logistics_document(shipment.logistics_document),
        "outbound_at": shipment.logistics_confirmed_at.isoformat() if shipment.logistics_confirmed_at else None,
        "shipped_at": shipment.shipped_at.isoformat() if shipment.shipped_at else None,
        "logistics_confirmed_at": (
            shipment.logistics_confirmed_at.isoformat()
            if shipment.logistics_confirmed_at
            else None
        ),
        "logistics_confirmed_by_name": (
            _display_name(shipment.logistics_confirmed_by)
            if shipment.logistics_confirmed_by_id
            else ""
        ),
        "items": [
            {
                "shipment_item_id": row.pk,
                "order_plan_item_id": row.order_plan_item_id,
                "production_subplan_item_id": row.production_subplan_item_id,
                "product_model_id": row.order_plan_item.product_model_id,
                "product_model_name": row.order_plan_item.product_model.name,
                "quantity": row.quantity,
            }
            for row in shipment.items.all()
        ],
        "accessory_items": accessory_rows,
        "accessory_summary": _shipment_accessory_summary(accessory_rows),
    }


def _attach_accessories_to_first_shipment(*, plan, shipment):
    if not plan.accessory_items.exists():
        return
    if OrderPlanShipmentAccessoryItem.objects.filter(shipment__order_plan=plan).exists():
        return
    accessory_items = list(
        plan.accessory_items.select_for_update()
        .select_related("material_spec")
        .order_by("id")
    )
    for accessory_item in accessory_items:
        allocations = list(
            OrderPlanAccessoryLotAllocation.objects.select_for_update()
            .filter(
                accessory_item=accessory_item,
                status=OrderPlanAccessoryLotAllocation.Status.RESERVED,
                shipment_item__isnull=True,
            )
            .order_by("material_lot__received_at", "id")
        )
        amount = sum(row.reserved_milli for row in allocations)
        if amount != accessory_item.quantity_milli or not allocations:
            raise OrderPlanMaterialFlowError(
                f"配件“{accessory_item.material_spec}”批次预占记录不完整，不能安排首批发货。"
            )
        shipment_item = OrderPlanShipmentAccessoryItem.objects.create(
            shipment=shipment,
            accessory_item=accessory_item,
            quantity_milli=amount,
            unit_snapshot=accessory_item.unit_snapshot,
        )
        OrderPlanAccessoryLotAllocation.objects.filter(
            pk__in=[row.pk for row in allocations]
        ).update(shipment_item=shipment_item, updated_at=timezone.now())


def _publish_shipment_authorization_task(*, plan, shipment, actor_openid):
    sales_openid = _sales_openid_for_plan(plan)
    payload = _shipment_payload(shipment)
    return _new_workitem(
        plan=plan,
        work_type="ORDER_PLAN_V2_SHIPMENT_AUTHORIZATION",
        stage_key=f"shipment:{shipment.pk}:authorization",
        title=f"{payload['shipment_label']}待授权发货",
        summary=(
            f"{plan.merchant_organization.name}｜{payload['quantity']}台{payload.get('accessory_summary', '')}；"
            "授权后由厂家成品库管打码贴包，物流经理确认出库，再由成品库管确认发货。"
        ),
        recipients=(sales_openid,),
        need_action=True,
        action_mode="ANYONE_DO",
        payload=payload,
        actor_openid=actor_openid,
    )


def _publish_shipment_qrcode_task(*, plan, shipment, actor_openid):
    recipients = _role_openids(plan.factory_organization, {"factory_stock"})
    if not recipients:
        raise OrderPlanMaterialFlowError("厂家尚未配置有效成品库管，不能下达打码任务。")
    payload = _shipment_payload(shipment)
    return _new_workitem(
        plan=plan,
        work_type="ORDER_PLAN_V2_QRCODE_PRINT",
        stage_key=f"shipment:{shipment.pk}:qrcode-print",
        title=f"{payload['shipment_label']}待打码贴包",
        summary=(
            f"{plan.merchant_organization.name}｜{payload['quantity']}台；"
            "请按本发货批次生成二维码、打印并线下贴包装。"
        ),
        recipients=recipients,
        need_action=True,
        action_mode="ANYONE_DO",
        payload=payload,
        actor_openid=actor_openid,
    )


def _publish_shipment_logistics_task(*, plan, shipment, actor_openid):
    """Ask the factory logistics manager for one shipment's two identifiers.

    Every shipment has independently traceable logistics, so both the task key and
    the persisted data must be scoped to ``shipment`` rather than to the
    complete order plan.
    """

    recipients = _role_openids(plan.factory_organization, {"factory_logistics"})
    if not recipients:
        raise OrderPlanMaterialFlowError("厂家尚未配置有效物流经理，不能填写本批物流信息。")
    payload = _shipment_payload(shipment)
    return _new_workitem(
        plan=plan,
        work_type="ORDER_PLAN_V2_SHIPMENT_LOGISTICS_CONFIRM",
        stage_key=f"shipment:{shipment.pk}:logistics-confirm",
        title=f"{payload['shipment_label']}待确认线下出库",
        summary=(
            f"{plan.merchant_organization.name}｜{payload['quantity']}台{payload.get('accessory_summary', '')}；"
            "请填写物流公司、物流单号，可上传物流凭证；确认线下出库后推送成品库管发货。"
        ),
        recipients=recipients,
        need_action=True,
        action_mode="ANYONE_DO",
        payload=payload,
        actor_openid=actor_openid,
    )


def _publish_shipment_outbound_task(*, plan, shipment, actor_openid):
    # Migration 0017 retired the pre-logistics outbound task. Once logistics
    # really confirms this batch, reuse and reopen that exact historical key.
    # publish_workitem intentionally never reopens CANCELLED rows on its own.
    if shipment.status != OrderPlanShipment.Status.OUTBOUNDED:
        raise OrderPlanMaterialFlowError("本批尚未确认线下出库，不能下达发货任务。")
    WorkItem.objects.filter(
        type="ORDER_PLAN_V2_STOCK_OUTBOUND",
        uniq_key=f"sales:order-plan:{plan.pk}:shipment:{shipment.pk}:stock-outbound",
        status="CANCELLED",
    ).update(status="OPEN", done_at=None, done_by_openid="", updated_at=timezone.now())
    recipients = _role_openids(plan.factory_organization, {"factory_stock"})
    if not recipients:
        raise OrderPlanMaterialFlowError("厂家尚未配置有效成品库管，不能下达发货任务。")
    payload = _shipment_payload(shipment)
    return _new_workitem(
        plan=plan,
        work_type="ORDER_PLAN_V2_STOCK_OUTBOUND",
        stage_key=f"shipment:{shipment.pk}:stock-outbound",
        title=f"{payload['shipment_label']}待确认发货",
        summary=(
            f"{plan.merchant_organization.name}｜{payload['quantity']}台{payload.get('accessory_summary', '')}；"
            "物流经理已确认线下出库，请确认本批发货。"
        ),
        recipients=recipients,
        need_action=True,
        action_mode="ANYONE_DO",
        payload=payload,
        actor_openid=actor_openid,
    )


def _publish_shipment_merchant_receipt_task(*, plan, shipment, actor_openid):
    recipients = _role_openids(
        plan.merchant_organization,
        shared_operation_recipient_role_codes(
            plan.merchant_organization_id, "goods_transfer_receipt"
        ),
    )
    if not recipients:
        raise OrderPlanMaterialFlowError(
            "接收商家尚未配置负责人或库管，不能下达本批入库任务。"
        )
    payload = _shipment_payload(shipment)
    return _new_workitem(
        plan=plan,
        work_type="ORDER_PLAN_V2_MERCHANT_RECEIPT",
        stage_key=f"shipment:{shipment.pk}:merchant-receipt",
        title=f"{payload['shipment_label']}待到货入库",
        summary=(
            f"{plan.factory_organization.name}｜{plan.brand.name}｜"
            f"本批{payload['quantity']}台{payload.get('accessory_summary', '')}已发货，"
            "实际收货后请由具备权限的负责人或库管确认入库。"
        ),
        recipients=recipients,
        need_action=True,
        action_mode="ANYONE_DO",
        payload=payload,
        actor_openid=actor_openid,
    )


def _shipment_planned_quantity(*, plan, order_plan_item, production_subplan_item=None):
    rows = OrderPlanShipmentItem.objects.filter(
        shipment__order_plan=plan,
        shipment__status__in=(
            OrderPlanShipment.Status.PLANNED,
            OrderPlanShipment.Status.AUTHORIZED,
            OrderPlanShipment.Status.OUTBOUNDED,
            OrderPlanShipment.Status.SHIPPED,
            OrderPlanShipment.Status.RECEIVED,
        ),
        order_plan_item=order_plan_item,
    )
    if production_subplan_item is None:
        rows = rows.filter(production_subplan_item__isnull=True)
    else:
        rows = rows.filter(production_subplan_item=production_subplan_item)
    return rows.aggregate(value=Sum("quantity"))["value"] or 0


def _create_v2_shipment(
    *,
    plan,
    client_request_id,
    item_rows,
    production_subplan=None,
    actor_openid="",
    publish_authorization=True,
):
    """Create one logical V2 shipment and its immutable item quantities.

    The sales-only split UI submits this same shape later.  Server-generated
    finished-goods and subplan shipments use stable request identifiers, which
    makes creation safe to retry after an interrupted transaction.
    """

    request_id = str(client_request_id or "").strip()
    if not request_id or len(request_id) > 64:
        raise OrderPlanMaterialFlowError("发货批次请求标识无效。")
    existing = (
        OrderPlanShipment.objects.select_for_update()
        .select_related("production_subplan")
        .filter(order_plan=plan, client_request_id=request_id)
        .first()
    )
    if existing:
        return existing, False
    if not isinstance(item_rows, list) or not item_rows:
        raise OrderPlanMaterialFlowError("发货批次至少需要一项产品数量。")

    normalized = []
    seen = set()
    for index, raw in enumerate(item_rows, start=1):
        if not isinstance(raw, dict):
            raise OrderPlanMaterialFlowError(f"第{index}项发货数据格式不正确。")
        quantity = _positive_integer(raw.get("quantity"), f"第{index}项发货数量")
        if production_subplan is None:
            item_id = _positive_integer(raw.get("order_plan_item_id"), f"第{index}项订单产品")
            item = (
                OrderPlanItem.objects.select_for_update()
                .select_related("product_model")
                .filter(pk=item_id, plan=plan)
                .first()
            )
            if not item:
                raise OrderPlanMaterialFlowError("发货产品不属于当前订单。")
            source_key = (item.pk, None)
            available = item.return_inventory_quantity
            sub_item = None
        else:
            sub_item_id = _positive_integer(
                raw.get("production_subplan_item_id"),
                f"第{index}项生产子计划产品",
            )
            sub_item = (
                OrderPlanProductionSubplanItem.objects.select_for_update()
                .select_related("order_plan_item__product_model")
                .filter(pk=sub_item_id, production_subplan=production_subplan)
                .first()
            )
            if not sub_item:
                raise OrderPlanMaterialFlowError("发货产品不属于当前生产子计划。")
            item = sub_item.order_plan_item
            source_key = (item.pk, sub_item.pk)
            available = sub_item.quantity
        if source_key in seen:
            raise OrderPlanMaterialFlowError("同一产品在一个发货批次中不能重复填写。")
        seen.add(source_key)
        used = _shipment_planned_quantity(
            plan=plan,
            order_plan_item=item,
            production_subplan_item=sub_item,
        )
        if quantity > available - used:
            raise OrderPlanMaterialFlowError(
                f"{item.product_model.name}可安排发货数量不足，请刷新后重试。"
            )
        normalized.append((item, sub_item, quantity))

    shipment = OrderPlanShipment.objects.create(
        order_plan=plan,
        production_subplan=production_subplan,
        sequence_no=_next_shipment_sequence(plan),
        client_request_id=request_id,
    )
    OrderPlanShipmentItem.objects.bulk_create(
        [
            OrderPlanShipmentItem(
                shipment=shipment,
                order_plan_item=item,
                production_subplan_item=sub_item,
                quantity=quantity,
            )
            for item, sub_item, quantity in normalized
        ]
    )
    _attach_accessories_to_first_shipment(plan=plan, shipment=shipment)
    if publish_authorization:
        _publish_shipment_authorization_task(
            plan=plan,
            shipment=shipment,
            actor_openid=actor_openid,
        )
    return shipment, True


def _ensure_finished_inventory_shipment(*, plan, actor_openid):
    rows = list(
        plan.items.select_related("product_model")
        .filter(return_inventory_quantity__gt=0)
        .order_by("id")
    )
    if not rows:
        return None
    shipment, _created = _create_v2_shipment(
        plan=plan,
        client_request_id="system-finished-inventory",
        item_rows=[
            {"order_plan_item_id": row.pk, "quantity": row.return_inventory_quantity}
            for row in rows
        ],
        production_subplan=None,
        actor_openid=actor_openid,
    )
    return shipment


def _ensure_subplan_shipment(*, plan, subplan, actor_openid):
    rows = list(
        subplan.items.select_related("order_plan_item__product_model").order_by("id")
    )
    if not rows:
        raise OrderPlanMaterialFlowError("生产子计划没有产品项，不能安排发货。")
    shipment, _created = _create_v2_shipment(
        plan=plan,
        client_request_id=f"system-production-subplan-{subplan.pk}",
        item_rows=[
            {"production_subplan_item_id": row.pk, "quantity": row.quantity}
            for row in rows
        ],
        production_subplan=subplan,
        actor_openid=actor_openid,
    )
    return shipment


def _delivery_requirements(material_plan):
    return list(
        OrderPlanMaterialRequirement.objects.select_related(
            "material", "material_spec", "supplier",
            "production_subplan_item__production_subplan",
        ).filter(
            production_subplan_item__production_subplan__material_plan=material_plan,
            reserved_milli__lt=F("required_milli"),
        ).order_by(
            "supplier_id", "material_spec_id",
            "production_subplan_item__production_subplan__sequence_no", "id",
        )
    )


def _lot_is_exhausted_by_plan(requirement):
    allocations = requirement.lot_allocations.select_related("material_lot").filter(
        status=OrderPlanMaterialLotAllocation.Status.RESERVED,
    )
    for allocation in allocations:
        lot = allocation.material_lot
        # This compares the state after all existing V2 reservations, so the
        # test represents real available balance, not a stale plan snapshot.
        if lot.on_hand_milli and lot.on_hand_milli == lot.reserved_milli:
            return True
    return False


def _create_or_refresh_delivery_plans(*, material_plan, actor):
    shortages = _delivery_requirements(material_plan)
    grouped = defaultdict(list)
    for requirement in shortages:
        grouped[requirement.supplier_id].append(requirement)
    created_or_updated = []
    scheduled_for = _next_dispatch_window()
    for supplier_id, requirements in grouped.items():
        delivery, created = OrderPlanSupplyDelivery.objects.select_for_update().get_or_create(
            material_plan=material_plan,
            supplier_id=supplier_id,
            defaults={
                "status": OrderPlanSupplyDelivery.Status.QUEUED,
                "scheduled_for": scheduled_for,
                "dispatch_window_key": _dispatch_window_key(scheduled_for),
            },
        )
        if not created and delivery.status in {
            OrderPlanSupplyDelivery.Status.CANCELLED,
            OrderPlanSupplyDelivery.Status.SATISFIED,
        }:
            # A cancelled/satisfied delivery must remain audit-stable; a new
            # shortage is represented by reopening neither history nor the old
            # row.  This should only happen after a re-plan, which V1 does not
            # issue automatically.
            raise OrderPlanMaterialFlowError("已有已结束配送计划，需重新创建订单计划后再安排物料。")
        if delivery.status == OrderPlanSupplyDelivery.Status.QUEUED:
            delivery.scheduled_for = min(delivery.scheduled_for, scheduled_for)
            delivery.dispatch_window_key = _dispatch_window_key(delivery.scheduled_for)
            delivery.save(update_fields=("scheduled_for", "dispatch_window_key", "updated_at"))
        line_requirements = defaultdict(list)
        for requirement in requirements:
            line_requirements[(requirement.material_id, requirement.material_spec_id)].append(requirement)
        for (material_id, spec_id), requirement_rows in line_requirements.items():
            shortfall = sum(row.required_milli - row.reserved_milli for row in requirement_rows)
            policy = FactoryMaterialSupplyPolicy.objects.filter(
                factory_id=material_plan.order_plan.factory_organization_id,
                supplier_id=supplier_id,
                material_spec_id=spec_id,
            ).first()
            normal_delivery = policy.normal_delivery_milli if policy else 0
            has_exhausted_lot = any(_lot_is_exhausted_by_plan(row) for row in requirement_rows)
            requested = max(shortfall, normal_delivery if has_exhausted_lot else 0)
            if requested <= 0:
                continue
            line, _line_created = OrderPlanSupplyDeliveryLine.objects.update_or_create(
                delivery=delivery,
                material_id=material_id,
                material_spec_id=spec_id,
                defaults={"requested_milli": requested},
            )
            # Requirement allocation is immutable in amount for this first V2
            # planning run.  Extra normal delivery becomes general stock after
            # receipt; it is deliberately not pretended to be order-reserved.
            for requirement in requirement_rows:
                planned = requirement.required_milli - requirement.reserved_milli
                OrderPlanSupplyDeliveryRequirement.objects.update_or_create(
                    delivery_line=line,
                    requirement=requirement,
                    defaults={"planned_milli": planned},
                )
        created_or_updated.append(delivery)
    return created_or_updated


@transaction.atomic
def warehouse_review_material_plan(*, user, plan_id):
    identity = _material_keeper_identity(user)
    plan = _v2_plan_for_factory(identity, plan_id, lock=True)
    material_plan = OrderPlanMaterialPlan.objects.select_for_update().filter(order_plan=plan).first()
    if not material_plan:
        raise OrderPlanMaterialFlowError("订单物料计划不存在。")
    if material_plan.status == OrderPlanMaterialPlan.Status.CANCELLED:
        raise OrderPlanMaterialFlowError("订单已经取消，不能核对物料。")
    # The first warehouse review freezes this order's shortage snapshot and
    # queued supplier instructions.  Later stock receipts are intentionally
    # handled by the independent warehouse receipt hook, rather than by
    # replaying this action and silently changing an already queued/pushed
    # delivery commitment.
    if material_plan.warehouse_reviewed_at:
        return get_material_flow_detail(user=user, plan_id=plan.pk)
    if material_plan.status not in {
        OrderPlanMaterialPlan.Status.WAITING_WAREHOUSE_REVIEW,
    }:
        return get_material_flow_detail(user=user, plan_id=plan.pk)

    _reserve_open_requirements(material_plan=material_plan, actor=user)
    deliveries = _create_or_refresh_delivery_plans(material_plan=material_plan, actor=user)
    now = timezone.now()
    material_plan.warehouse_reviewed_by = user
    material_plan.warehouse_reviewed_at = now
    material_plan.status = (
        OrderPlanMaterialPlan.Status.WAITING_SUPPLIER_DISPATCH
        if deliveries
        else OrderPlanMaterialPlan.Status.WAITING_RECEIPT
    )
    material_plan.save(
        update_fields=(
            "warehouse_reviewed_by", "warehouse_reviewed_at", "status", "updated_at"
        )
    )
    review_workitem = WorkItem.objects.select_for_update().filter(
        uniq_key=f"sales:order-plan:{plan.pk}:material-review"
    ).first()
    if review_workitem and review_workitem.status == "OPEN":
        _complete_workitem_anyone(
            review_workitem,
            user,
            actor_title="物料预占与配送缺口已核对",
            peer_title=f"{_display_name(user)}已核对物料",
        )
    if deliveries:
        summary = "；".join(
            f"{delivery.supplier.name}：{delivery.lines.count()}项物料"
            for delivery in deliveries
        )
        # Development tests must verify the real supplier interaction without
        # waiting for a clock window.  This invokes the same idempotent
        # dispatcher used in production, rather than creating a second task
        # shape directly from the warehouse action.
        dispatch_result = dispatch_due_supply_deliveries(now=now)
        if dispatch_result["errors"]:
            dispatch_summary = (
                f"已即时推送{dispatch_result['dispatched']}个供应商；"
                f"{len(dispatch_result['errors'])}个供应商因账号配置未完成而保留待推送。"
            )
        else:
            dispatch_summary = f"已即时推送至{dispatch_result['dispatched']}个供应商。"
        _new_workitem(
            plan=plan,
            work_type="ORDER_PLAN_SUPPLY_QUEUE_NOTICE",
            stage_key="supply-queue-notice",
            title="订单物料配送任务已即时推送",
            summary=f"{summary}；{dispatch_summary}",
            recipients=_role_openids(
                plan.factory_organization,
                {"factory_matching", "factory_sales"},
            ),
            need_action=False,
            payload={
                "material_plan_id": material_plan.pk,
                "delivery_ids": [delivery.pk for delivery in deliveries],
            },
            actor_openid=_actor_openid(user),
            done_immediately=True,
        )
    return get_material_flow_detail(user=user, plan_id=plan.pk)


def _delivery_payload(delivery):
    lines = list(delivery.lines.select_related("material", "material_spec").order_by("id"))
    return {
        "delivery_id": delivery.pk,
        "supplier_id": delivery.supplier_id,
        "supplier_name": delivery.supplier.name,
        "scheduled_for": delivery.scheduled_for.isoformat(),
        "materials": [
            {
                "delivery_line_id": line.pk,
                "material_id": line.material_id,
                "material_name": line.material.name,
                "material_spec_id": line.material_spec_id,
                "material_spec_name": line.material_spec.name,
                "requested_milli": line.requested_milli,
                "requested": _quantity_text(line.requested_milli),
            }
            for line in lines
        ],
    }


def _set_supplier_delivery_task_display(workitem):
    """Store an immutable per-recipient material count on V2 supplier tasks.

    A ``WorkItem`` deliberately owns the complete, auditable delivery list,
    while a supplier sales user's inbox must only claim the lines assigned to
    that user.  The detail endpoint applies the same scope filter.  Updating
    the individual ``UserTask`` subtitle makes the inbox truthful without
    duplicating business work items or putting mutable responsibility lookups
    into a historical task.
    """

    payload = workitem.payload if isinstance(workitem.payload, dict) else {}
    scopes = payload.get("recipient_scopes") or {}
    material_rows = [
        row for row in payload.get("materials", [])
        if isinstance(row, dict) and row.get("material_id") not in (None, "")
    ]
    for task in workitem.user_tasks.select_for_update().all():
        scope = scopes.get(task.to_openid, {}) if isinstance(scopes, dict) else {}
        scoped_ids = {
            int(material_id)
            for material_id in scope.get("material_ids", [])
            if str(material_id).strip().isdigit()
        } if isinstance(scope, dict) else set()
        line_count = sum(
            1
            for row in material_rows
            if int(row["material_id"]) in scoped_ids
        )
        role = scope.get("role") if isinstance(scope, dict) else ""
        if role == "supplier_sales":
            subtitle = (
                f"{payload.get('supplier_name') or '供应商'}｜"
                f"{line_count}项负责物料｜订单{payload.get('plan_code') or ''}"
            )
        else:
            subtitle = (
                f"{payload.get('supplier_name') or '供应商'}｜"
                f"{line_count}项供应物料｜订单{payload.get('plan_code') or ''}"
            )
        task.display_title_snapshot = workitem.title
        task.display_subtitle_snapshot = subtitle[:255]
        task.save(
            update_fields=(
                "display_title_snapshot", "display_subtitle_snapshot", "updated_at"
            )
        )


def dispatch_due_supply_deliveries(*, now=None, force=False, delivery_ids=None):
    """Create supplier action tasks for due, or explicitly selected, deliveries.

    The management command calls this function twice a day.  ``force`` is for
    audited operations/testing and does not alter any delivery quantities.
    """

    now = now or timezone.now()
    selected_ids = None
    if delivery_ids is not None:
        selected_ids = {int(delivery_id) for delivery_id in delivery_ids}
        if not selected_ids:
            return {"dispatched": 0, "skipped": 0, "delivery_ids": [], "errors": []}
    due_filter = Q(status=OrderPlanSupplyDelivery.Status.QUEUED)
    if selected_ids is not None:
        due_filter &= Q(pk__in=selected_ids)
    if not force:
        due_filter &= Q(scheduled_for__lte=now)
    # Do not wrap the whole scheduled batch in one transaction.  One supplier
    # may temporarily have no reachable owner/sales identity; that must leave
    # only *its* delivery queued for the next 10:00/22:00 run, not roll back
    # every other supplier's already valid delivery task.
    due_ids = list(
        OrderPlanSupplyDelivery.objects.filter(due_filter)
        .order_by("scheduled_for", "id")
        .values_list("pk", flat=True)
    )
    result = {"dispatched": 0, "skipped": 0, "delivery_ids": [], "errors": []}
    for delivery_id in due_ids:
        with transaction.atomic():
            delivery = (
                OrderPlanSupplyDelivery.objects.select_for_update()
                .select_related(
                    "material_plan__order_plan__factory_organization",
                    "material_plan__order_plan__merchant_organization",
                    "material_plan__order_plan__brand",
                    "supplier",
                )
                .filter(pk=delivery_id, status=OrderPlanSupplyDelivery.Status.QUEUED)
                .first()
            )
            # A concurrent worker may already have advanced this row.  This
            # retry is harmless and must never duplicate its WorkItem.
            if not delivery:
                result["skipped"] += 1
                continue
            material_plan = delivery.material_plan
            plan = material_plan.order_plan
            if (
                material_plan.status == OrderPlanMaterialPlan.Status.CANCELLED
                or plan.status == OrderPlan.Status.CANCELLED
            ):
                delivery.status = OrderPlanSupplyDelivery.Status.CANCELLED
                delivery.cancelled_at = now
                delivery.save(update_fields=("status", "cancelled_at", "updated_at"))
                result["skipped"] += 1
                continue
            payload = _delivery_payload(delivery)
            recipients = get_supplier_task_recipients(
                supplier=delivery.supplier,
                material_ids={line["material_id"] for line in payload["materials"]},
            )
            if not recipients["openids"]:
                result["skipped"] += 1
                result["errors"].append(
                    {
                        "delivery_id": delivery.pk,
                        "supplier_id": delivery.supplier_id,
                        "reason": (
                            f"供应商{delivery.supplier.name}尚未配置有效负责人或负责销售，"
                            "配送任务保留待下次推送。"
                        ),
                    }
                )
                continue
            workitem = _new_workitem(
                plan=plan,
                work_type="ORDER_PLAN_SUPPLIER_DELIVERY",
                stage_key=f"material-delivery:{delivery.pk}",
                title="订单物料待配送确认",
                summary=(
                    f"{delivery.supplier.name}｜{len(payload['materials'])}项物料"
                    f"｜订单{plan.plan_code}"
                ),
                recipients=recipients["openids"],
                need_action=True,
                action_mode="OWNER_OR_SALES",
                payload={
                    **payload,
                    "material_plan_id": material_plan.pk,
                    "completion_policy": "OWNER_OR_ALL_SALES",
                    "owner_openids": recipients["owner_openids"],
                    "responsible_sales_identity_ids": recipients["sales_identity_ids"],
                    "unassigned_material_ids": recipients["unassigned_material_ids"],
                    "recipient_scopes": recipients["recipient_scopes"],
                    "processed_actors": [],
                },
                actor_openid="system-dispatch",
            )
            _set_supplier_delivery_task_display(workitem)
            delivery.status = OrderPlanSupplyDelivery.Status.DISPATCHED
            delivery.dispatched_at = now
            delivery.save(update_fields=("status", "dispatched_at", "updated_at"))
            _new_workitem(
                plan=plan,
                work_type="ORDER_PLAN_SUPPLY_DISPATCHED_NOTICE",
                stage_key=f"material-delivery:{delivery.pk}:notice",
                title="订单物料配送任务已推送",
                summary=f"已推送至供应商{delivery.supplier.name}，等待线下配送确认。",
                recipients=_role_openids(
                    plan.factory_organization,
                    {"factory_material_stock", "factory_matching", "factory_sales"},
                ),
                need_action=False,
                payload={"delivery_id": delivery.pk, "workitem_id": workitem.pk},
                actor_openid="system-dispatch",
                done_immediately=True,
            )
            result["dispatched"] += 1
            result["delivery_ids"].append(delivery.pk)
    return result


@transaction.atomic
def confirm_supplier_material_delivery(
    *,
    user,
    plan_id,
    supplier_id=None,
    delivery_id=None,
):
    try:
        identity = get_supplier_identity(user)
    except SupplierResponsibilityError as exc:
        raise OrderPlanMaterialFlowError(str(exc)) from exc
    plan = _v2_plan_by_id(plan_id, lock=True)
    if supplier_id not in (None, ""):
        try:
            supplier_id = int(supplier_id)
        except (TypeError, ValueError) as exc:
            raise OrderPlanMaterialFlowError("供应商参数无效。") from exc
    else:
        supplier_id = identity.organization_id
    if supplier_id != identity.organization_id:
        raise OrderPlanMaterialFlowError("只能确认本供应商的配送任务。")
    deliveries = OrderPlanSupplyDelivery.objects.select_for_update().select_related(
        "material_plan", "supplier"
    ).filter(
        material_plan__order_plan=plan,
        supplier_id=supplier_id,
    )
    if delivery_id not in (None, ""):
        try:
            delivery_id = int(delivery_id)
        except (TypeError, ValueError) as exc:
            raise OrderPlanMaterialFlowError("配送计划参数无效。") from exc
        delivery = deliveries.filter(pk=delivery_id).first()
    else:
        # The normal model keeps one delivery per material-plan/supplier.  Be
        # defensive for historical/repair data nevertheless: an action may
        # only advance a delivery whose immutable user task belongs to this
        # caller, never simply the first delivery returned for the supplier.
        actor_openid = _actor_openid(user)
        delivery = None
        for candidate in deliveries.order_by("id"):
            if UserTask.objects.filter(
                workitem__uniq_key=(
                    f"sales:order-plan:{plan.pk}:material-delivery:{candidate.pk}"
                ),
                to_openid=actor_openid,
                need_action=True,
            ).exclude(state="DONE").exists():
                delivery = candidate
                break
        if delivery is None:
            delivery = deliveries.order_by("id").first()
    if not delivery:
        raise OrderPlanMaterialFlowError("当前订单没有分配给本供应商的物料配送。")
    if delivery.status == OrderPlanSupplyDelivery.Status.CANCELLED:
        raise OrderPlanMaterialFlowError("该配送计划已取消。")
    # The first successful supplier confirmation is an auditable fact.  A
    # client retry (or a later page refresh) must return the current detail,
    # never replace the original confirmer/timestamp.
    if delivery.status in {
        OrderPlanSupplyDelivery.Status.SUPPLIER_CONFIRMED,
        OrderPlanSupplyDelivery.Status.RECEIVING,
        OrderPlanSupplyDelivery.Status.SATISFIED,
    }:
        return get_material_flow_detail(user=user, plan_id=plan.pk)
    if delivery.status not in {
        OrderPlanSupplyDelivery.Status.DISPATCHED,
    }:
        raise OrderPlanMaterialFlowError("该配送任务尚未到推送时间。")
    workitem = WorkItem.objects.select_for_update().filter(
        uniq_key=f"sales:order-plan:{plan.pk}:material-delivery:{delivery.pk}",
    ).first()
    if not workitem:
        raise OrderPlanMaterialFlowError("未找到供应配送任务，请等待系统推送。")
    changed, completed = _complete_workitem_all(
        workitem,
        user,
        allow_owner=True,
    )
    if completed and changed:
        now = timezone.now()
        # The supplier's delivery confirmation is a commitment for every
        # requested line.  Persist it at the same time as the collaborative
        # task closes so the detail page never shows an already-confirmed
        # delivery with a misleading zero quantity.
        OrderPlanSupplyDeliveryLine.objects.select_for_update().filter(
            delivery=delivery
        ).update(
            confirmed_milli=F("requested_milli"),
            updated_at=now,
        )
        delivery.status = OrderPlanSupplyDelivery.Status.SUPPLIER_CONFIRMED
        delivery.supplier_confirmed_at = now
        delivery.supplier_confirmed_by = user
        delivery.save(
            update_fields=(
                "status", "supplier_confirmed_at", "supplier_confirmed_by", "updated_at"
            )
        )
        _new_workitem(
            plan=plan,
            work_type="ORDER_PLAN_SUPPLIER_CONFIRMED_NOTICE",
            stage_key=f"material-delivery:{delivery.pk}:confirmed",
            title="供应商已确认物料配送",
            summary=f"{delivery.supplier.name}已确认线下配送；到厂后请在物料库独立入库。",
            recipients=_role_openids(plan.factory_organization, {"factory_material_stock", "factory_matching", "factory_sales"}),
            need_action=False,
            payload={"delivery_id": delivery.pk, "supplier_id": delivery.supplier_id},
            actor_openid=_actor_openid(user),
            done_immediately=True,
        )
    return get_material_flow_detail(user=user, plan_id=plan.pk)


def _delivery_receipt_rows(*, material_plan, lock=False):
    """Rows that the order-delivery receipt page must receive as one unit."""

    deliveries = (
        OrderPlanSupplyDelivery.objects
        .select_related("supplier", "material_plan__order_plan")
        .filter(
            material_plan=material_plan,
            status__in=(
                OrderPlanSupplyDelivery.Status.SUPPLIER_CONFIRMED,
                OrderPlanSupplyDelivery.Status.RECEIVING,
                OrderPlanSupplyDelivery.Status.SATISFIED,
            ),
        )
        .order_by("id")
    )
    if lock:
        deliveries = deliveries.select_for_update()
    rows = []
    for delivery in deliveries:
        # The line quantities are the receipt source of truth.  A historic
        # delivery may have been marked SATISFIED after its requirements were
        # reserved even though one of its confirmed lines is still unreceived.
        # Keep that residual line visible and repair its state on receipt.
        lines = delivery.lines.select_related(
            "material", "material_spec"
        ).filter(received_milli__lt=F("confirmed_milli")).order_by("id")
        if lock:
            lines = lines.select_for_update()
        for line in lines:
            quantity = line.confirmed_milli - line.received_milli
            if quantity > 0:
                rows.append((delivery, line, quantity))
    return rows


def get_order_delivery_receipt_form(*, user, plan_id):
    """Read-only, prefilled receipt form for all arrived deliveries of one order.

    This is intentionally separate from the ordinary material warehouse form:
    the operator neither chooses a stock item nor changes individual quantities.
    """

    identity = _material_keeper_identity(user)
    plan = _v2_plan_for_factory(identity, plan_id)
    material_plan = OrderPlanMaterialPlan.objects.filter(order_plan=plan).first()
    if not material_plan:
        raise OrderPlanMaterialFlowError("订单物料计划不存在。")
    rows = _delivery_receipt_rows(material_plan=material_plan)
    stock_units = {
        (supplier_id, material_spec_id): unit
        for supplier_id, material_spec_id, unit in FactoryMaterialStock.objects.filter(
            factory_id=plan.factory_organization_id,
        ).values_list("supplier_id", "material_spec_id", "unit")
    }
    return {
        "plan_id": plan.pk,
        "plan_code": plan.plan_code,
        "merchant_name": plan.merchant_organization.name,
        "items": [
            {
                "delivery_id": delivery.pk,
                "delivery_line_id": line.pk,
                "supplier_name": delivery.supplier.name,
                "material_name": line.material.name,
                "material_code": line.material.code,
                "spec_name": line.material_spec.name,
                "spec_code": line.material_spec.code,
                # A pre-existing stock item's unit remains fixed, just as on
                # the ordinary receipt page.  New order-delivery receipts use
                # 件 by default and do not expose a unit selector.
                "unit": stock_units.get(
                    (delivery.supplier_id, line.material_spec_id),
                    FactoryMaterialStock.Unit.PIECE,
                ),
                "quantity": _quantity_text(quantity),
            }
            for delivery, line, quantity in rows
        ],
        "item_count": len(rows),
        "can_submit": bool(rows),
    }


@transaction.atomic
def receive_all_order_delivery_materials(*, user, plan_id):
    """Atomically receive every currently arrived delivery line of one order."""

    identity = _material_keeper_identity(user)
    plan = _v2_plan_for_factory(identity, plan_id, lock=True)
    material_plan = OrderPlanMaterialPlan.objects.select_for_update().filter(order_plan=plan).first()
    if not material_plan:
        raise OrderPlanMaterialFlowError("订单物料计划不存在。")
    # Re-read and lock the delivery rows inside this transaction.  A stale
    # receipt page therefore cannot create a duplicate lot after another
    # warehouse user has already completed the all-at-once receipt.
    rows = _delivery_receipt_rows(material_plan=material_plan, lock=True)
    if not rows:
        raise OrderPlanMaterialFlowError("当前订单没有供应商已配送、待入库的物料。")

    received = []
    now = timezone.now()
    for delivery, line, quantity in rows:
        stock, _created = FactoryMaterialStock.objects.select_for_update().get_or_create(
            factory_id=plan.factory_organization_id,
            supplier_id=delivery.supplier_id,
            material_spec_id=line.material_spec_id,
            defaults={"unit": FactoryMaterialStock.Unit.PIECE},
        )
        # Match the normal material-receipt path: advance the common stock
        # revision before allocations are refreshed, so every reservation and
        # subsequent stock operation observes the same write boundary.
        FactoryMaterialStock.objects.filter(pk=stock.pk).update(
            revision=F("revision") + 1
        )
        # This dedicated receipt generates the factory lot automatically.  No
        # supplier batch number, stock selection, or editable quantity exists
        # on the client page.
        lot = FactoryMaterialLot.objects.create(
            stock=stock,
            batch_code=_new_code("ML", line.material_spec.code),
            supplier_batch_code="",
            received_milli=quantity,
            on_hand_milli=quantity,
            received_by=user,
        )
        request_id = uuid4()
        MaterialWarehouseDocument.objects.create(
            factory_id=plan.factory_organization_id,
            stock=stock,
            document_no=_new_code("ODR", line.material_spec.code),
            request_id=request_id,
            request_hash=hashlib.sha256(
                f"order-delivery:{plan.pk}:{delivery.pk}:{line.pk}:{quantity}".encode()
            ).hexdigest(),
            kind=MaterialWarehouseDocument.Kind.RECEIPT,
            quantity_milli=quantity,
            lot=lot,
            created_by=user,
        )
        # The receipt document is physical proof for this exact delivery line.
        # Reservation refresh is allowed to allocate stock across orders, but it
        # must not be the only mechanism that advances this line's receipt
        # progress; otherwise a successful receipt can still display "已入 0".
        if line.received_milli != line.confirmed_milli:
            line.received_milli = line.confirmed_milli
            line.save(update_fields=("received_milli", "updated_at"))
        refreshed = refresh_material_reservations_for_stock(
            stock=stock, actor=user, receipt_lot=lot
        )
        line.refresh_from_db(fields=("received_milli",))
        received.append({
            "delivery_id": delivery.pk,
            "delivery_line_id": line.pk,
            "material_name": line.material.name,
            "spec_name": line.material_spec.name,
            "quantity": _quantity_text(quantity),
            "lot_code": lot.batch_code,
            "reservation": refreshed,
        })
        delivery.refresh_from_db(fields=("status",))

    # Reconcile the parent card from its actual confirmed/received lines.
    # A card cannot remain "待厂家入库" after all its lines have been received.
    received_delivery_ids = {delivery.pk for delivery, _line, _quantity in rows}
    for delivery_id in received_delivery_ids:
        delivery = OrderPlanSupplyDelivery.objects.select_for_update().get(pk=delivery_id)
        has_unreceived_line = delivery.lines.filter(
            confirmed_milli__gt=0,
            received_milli__lt=F("confirmed_milli"),
        ).exists()
        next_status = (
            OrderPlanSupplyDelivery.Status.RECEIVING
            if has_unreceived_line
            else OrderPlanSupplyDelivery.Status.SATISFIED
        )
        if delivery.status != next_status:
            delivery.status = next_status
            delivery.save(update_fields=("status", "updated_at"))

    # The material keeper performed the real receipt action from the order
    # detail, not by opening every historic awareness notification one by one.
    # Close only this actor's notifications for deliveries that are now fully
    # received.  Other recipients and deliveries still awaiting confirmation
    # are intentionally left untouched.
    actor_openid = _actor_openid(user)
    completed_notice_keys = []
    for delivery_id in received_delivery_ids:
        completed_notice_keys.extend((
            f"sales:order-plan:{plan.pk}:material-delivery:{delivery_id}:notice",
            f"sales:order-plan:{plan.pk}:material-delivery:{delivery_id}:confirmed",
        ))
    UserTask.objects.filter(
        to_openid=actor_openid,
        workitem__uniq_key__in=completed_notice_keys,
        state__in=("NEW", "READ"),
    ).update(
        state="DONE",
        need_action=False,
        done_at=now,
        done_reason="SELF_ACTION",
        cursor_time=now,
        updated_at=now,
    )

    # The single dispatch notice is obsolete only after every supplier delivery
    # for this order has settled.  Leaving it open while another supplier is
    # still dispatched preserves the one-card reminder for the remaining work.
    pending_delivery_exists = material_plan.supply_deliveries.filter(
        status__in=(
            OrderPlanSupplyDelivery.Status.DISPATCHED,
            OrderPlanSupplyDelivery.Status.SUPPLIER_CONFIRMED,
            OrderPlanSupplyDelivery.Status.RECEIVING,
        )
    ).exists()
    if not pending_delivery_exists:
        UserTask.objects.filter(
            to_openid=actor_openid,
            workitem__uniq_key=f"sales:order-plan:{plan.pk}:supply-queue-notice",
            state__in=("NEW", "READ"),
        ).update(
            state="DONE",
            need_action=False,
            done_at=now,
            done_reason="SELF_ACTION",
            cursor_time=now,
            updated_at=now,
        )
    return {"plan_id": plan.pk, "plan_code": plan.plan_code, "received": received}


@transaction.atomic
def refresh_material_reservations_for_stock(*, stock, actor, receipt_lot=None):
    """Receipt hook: reserve newly available stock for oldest open V2 demand.

    It intentionally does not create a supplier task or mark a material plan
    as ready.  The material warehouse keeper sees the new live balance in the
    normal order detail and explicitly decides when all materials are ready.

    ``receipt_lot`` is optional for compatibility with a maintenance caller,
    but the normal material-inventory receipt always supplies the newly
    created lot.  Only allocations from that lot are added to
    ``received_milli`` on a delivery line; an older allocation must not be
    retroactively treated as a new supplier receipt.
    """

    if receipt_lot is not None:
        if not getattr(receipt_lot, "pk", None) or receipt_lot.stock_id != stock.pk:
            raise OrderPlanMaterialFlowError("本次入库批次与物料库存不匹配。")

    # The independent warehouse service already took a revision lock on stock.
    # This query only touches material plans which are still before production.
    material_plans = list(
        OrderPlanMaterialPlan.objects.select_for_update()
        .select_related("order_plan")
        .filter(
            order_plan__factory_organization_id=stock.factory_id,
            order_plan__flow_version=OrderPlan.FlowVersion.MATERIAL_STOCK_V2,
            status__in=(
                OrderPlanMaterialPlan.Status.WAITING_WAREHOUSE_REVIEW,
                OrderPlanMaterialPlan.Status.WAITING_SUPPLIER_DISPATCH,
                OrderPlanMaterialPlan.Status.WAITING_RECEIPT,
            ),
        )
        .order_by("order_plan__submitted_at", "id")
    )
    total = {"plans": 0, "requirements": 0, "reserved_milli": 0}
    for material_plan in material_plans:
        amounts = _reserve_open_requirements(
            material_plan=material_plan,
            actor=actor,
            stock=stock,
            receipt_lot=receipt_lot,
        )
        if amounts["reserved_milli"]:
            total["plans"] += 1
            total["requirements"] += amounts["requirements"]
            total["reserved_milli"] += amounts["reserved_milli"]

        # Show accurate receipt progress on existing delivery rows without
        # claiming that an arbitrary receipt belongs to an order.  Only the
        # allocation delta from this exact new lot may increase a line; the
        # requirement's complete historical reserved balance is deliberately
        # not used here.
        receipt_requirement_deltas = amounts["receipt_requirement_deltas"]
        for delivery in material_plan.supply_deliveries.select_for_update().filter(
            supplier_id=stock.supplier_id,
            status__in=(
                OrderPlanSupplyDelivery.Status.SUPPLIER_CONFIRMED,
                OrderPlanSupplyDelivery.Status.RECEIVING,
            ),
        ):
            received_for_delivery = 0
            for line in delivery.lines.select_for_update().filter(
                material_spec_id=stock.material_spec_id
            ):
                line_delta = sum(
                    receipt_requirement_deltas.get(allocation.requirement_id, 0)
                    for allocation in line.requirement_allocations.all()
                )
                if not line_delta:
                    continue
                # A normal-delivery top-up can exceed the order shortage.  The
                # V2 delivery row records only the order-attributable portion,
                # and never more than what the supplier confirmed.
                confirmed_cap = line.confirmed_milli or line.requested_milli
                maximum = min(line.requested_milli, confirmed_cap)
                updated_received = min(maximum, line.received_milli + line_delta)
                received_for_delivery += updated_received - line.received_milli
                if updated_received != line.received_milli:
                    line.received_milli = updated_received
                    line.save(update_fields=("received_milli", "updated_at"))
            # A supplier delivery is complete only when every confirmed line has
            # actually been received.  Requirement reservation can be filled
            # from another lot and must never hide an unreceived delivery line.
            if _delivery_fully_received(delivery):
                if delivery.status != OrderPlanSupplyDelivery.Status.SATISFIED:
                    delivery.status = OrderPlanSupplyDelivery.Status.SATISFIED
                    delivery.save(update_fields=("status", "updated_at"))
            elif received_for_delivery and delivery.status != OrderPlanSupplyDelivery.Status.RECEIVING:
                delivery.status = OrderPlanSupplyDelivery.Status.RECEIVING
                delivery.save(update_fields=("status", "updated_at"))
    return total


def _consume_subplan_materials(subplan, user):
    requirements = list(
        OrderPlanMaterialRequirement.objects.select_for_update().select_related(
            "production_subplan_item__production_subplan",
        ).filter(production_subplan_item__production_subplan=subplan).order_by("id")
    )
    for requirement in requirements:
        needed = requirement.required_milli - requirement.consumed_milli
        if needed <= 0:
            continue
        allocations = list(
            requirement.lot_allocations.select_for_update().select_related("material_lot").filter(
                status=OrderPlanMaterialLotAllocation.Status.RESERVED,
            ).order_by("material_lot__received_at", "id")
        )
        for allocation in allocations:
            if needed <= 0:
                break
            amount = min(needed, _allocation_available(allocation))
            if not amount:
                continue
            changed = FactoryMaterialLot.objects.filter(
                pk=allocation.material_lot_id,
                on_hand_milli__gte=amount,
                reserved_milli__gte=amount,
            ).update(
                on_hand_milli=F("on_hand_milli") - amount,
                reserved_milli=F("reserved_milli") - amount,
            )
            if changed != 1:
                raise OrderPlanMaterialFlowError("物料库存已变化，不能确认生产完成，请刷新后重试。")
            next_consumed_milli = allocation.consumed_milli + amount
            update_fields = ["consumed_milli", "updated_at"]
            if (
                next_consumed_milli + allocation.released_milli >= allocation.reserved_milli
            ):
                allocation.status = OrderPlanMaterialLotAllocation.Status.CONSUMED
                update_fields.append("status")
            allocation.consumed_milli = F("consumed_milli") + amount
            allocation.save(update_fields=tuple(update_fields))
            allocation.refresh_from_db(fields=("consumed_milli",))
            needed -= amount
        if needed:
            raise OrderPlanMaterialFlowError("该型号生产计划物料预占不完整，不能确认生产完成。")
        requirement.consumed_milli = requirement.required_milli
        requirement.save(update_fields=("consumed_milli", "updated_at"))


def _publish_production_task(plan, subplan, *, actor_openid):
    recipients = _role_openids(plan.factory_organization, {"factory_production"})
    if not recipients:
        raise OrderPlanMaterialFlowError("厂家尚未配置有效生产经理，不能下达生产任务。")
    return _new_workitem(
        plan=plan,
        work_type="ORDER_PLAN_V2_PRODUCTION_CONFIRM",
        stage_key=f"production:{subplan.pk}",
        title="订单型号生产计划待确认完成",
        summary=f"{plan.merchant_organization.name}｜本批{subplan.planned_total_quantity}台。",
        recipients=recipients,
        need_action=True,
        action_mode="ANYONE_DO",
        payload={"material_plan_id": subplan.material_plan_id, "subplan_id": subplan.pk, "sequence_no": subplan.sequence_no},
        actor_openid=actor_openid,
    )


@transaction.atomic
def release_material_ready_for_production(*, user, plan_id):
    identity = _material_keeper_identity(user)
    plan = _v2_plan_for_factory(identity, plan_id, lock=True)
    material_plan = OrderPlanMaterialPlan.objects.select_for_update().filter(order_plan=plan).first()
    if not material_plan:
        raise OrderPlanMaterialFlowError("订单物料计划不存在。")
    if material_plan.status == OrderPlanMaterialPlan.Status.CANCELLED:
        raise OrderPlanMaterialFlowError("订单已经取消。")
    if material_plan.status in {
        OrderPlanMaterialPlan.Status.READY_FOR_PRODUCTION,
        OrderPlanMaterialPlan.Status.PRODUCTION_IN_PROGRESS,
        OrderPlanMaterialPlan.Status.PRODUCTION_COMPLETED,
    }:
        return get_material_flow_detail(user=user, plan_id=plan.pk)
    if (
        not material_plan.warehouse_reviewed_at
        or material_plan.status
        not in {
            OrderPlanMaterialPlan.Status.WAITING_SUPPLIER_DISPATCH,
            OrderPlanMaterialPlan.Status.WAITING_RECEIPT,
        }
    ):
        raise OrderPlanMaterialFlowError(
            "请先完成物料库管核对，再确认所需物料全部到位。"
        )
    # A generic warehouse receipt may physically arrive before its supplier
    # opens the scheduled task.  It can reserve stock, but it must not bypass
    # the supplier-delivery confirmation step for an order that has created a
    # delivery commitment.  A no-delivery, all-stock plan remains valid.
    if not _all_delivery_commitments_confirmed(material_plan):
        raise OrderPlanMaterialFlowError(
            "仍有供应商配送尚未确认，不能推送生产。"
        )
    _reserve_open_requirements(material_plan=material_plan, actor=user)
    if not _all_materials_reserved(material_plan):
        raise OrderPlanMaterialFlowError("所需物料尚未全部到位，不能推送生产。")
    first_subplan = material_plan.production_subplans.select_for_update().filter(
        status=OrderPlanProductionSubplan.Status.PLANNED,
    ).order_by("sequence_no").first()
    if not first_subplan:
        raise OrderPlanMaterialFlowError("订单没有待生产计划。")
    now = timezone.now()
    material_plan.material_ready_by = user
    material_plan.material_ready_at = now
    material_plan.status = OrderPlanMaterialPlan.Status.READY_FOR_PRODUCTION
    material_plan.save(
        update_fields=("material_ready_by", "material_ready_at", "status", "updated_at")
    )
    plan.status = OrderPlan.Status.PRODUCTION_PENDING
    plan.save(update_fields=("status", "updated_at"))
    first_subplan.status = OrderPlanProductionSubplan.Status.PRODUCTION_PENDING
    first_subplan.production_released_at = now
    first_subplan.save(update_fields=("status", "production_released_at", "updated_at"))
    _publish_production_task(plan, first_subplan, actor_openid=_actor_openid(user))
    _new_workitem(
        plan=plan,
        work_type="ORDER_PLAN_MATERIAL_READY_NOTICE",
        stage_key="material-ready-notice",
        title="订单所需物料全部到位",
        summary=f"已推送型号生产计划，本批{first_subplan.planned_total_quantity}台。",
        recipients=_role_openids(plan.factory_organization, {"factory_sales", "factory_matching"}),
        need_action=False,
        payload={"material_plan_id": material_plan.pk, "subplan_id": first_subplan.pk},
        actor_openid=_actor_openid(user),
        done_immediately=True,
    )
    return get_material_flow_detail(user=user, plan_id=plan.pk)


@transaction.atomic
def confirm_material_flow_production(*, user, plan_id, subplan_id=None):
    identity = _production_identity(user)
    plan = _v2_plan_for_factory(identity, plan_id, lock=True)
    material_plan = OrderPlanMaterialPlan.objects.select_for_update().filter(order_plan=plan).first()
    if not material_plan:
        raise OrderPlanMaterialFlowError("订单物料计划不存在。")
    cancellation_audit = OrderPlanCancellationAudit.objects.select_for_update().filter(
        order_plan=plan,
        status=OrderPlanCancellationAudit.Status.WAITING_PRODUCTION_COMPLETION,
    ).first()
    try:
        subplan_id = int(subplan_id) if subplan_id not in (None, "") else None
    except (TypeError, ValueError) as exc:
        raise OrderPlanMaterialFlowError("生产计划参数无效。") from exc
    subplans = material_plan.production_subplans.select_for_update().filter(
        status=OrderPlanProductionSubplan.Status.PRODUCTION_PENDING,
    ).order_by("sequence_no")
    subplan = subplans.filter(pk=subplan_id).first() if subplan_id else subplans.first()
    if not subplan:
        raise OrderPlanMaterialFlowError("当前没有待确认生产完成的型号生产计划。")
    workitem = WorkItem.objects.select_for_update().filter(
        uniq_key=f"sales:order-plan:{plan.pk}:production:{subplan.pk}",
    ).first()
    if not workitem:
        raise OrderPlanMaterialFlowError("未找到该型号生产计划任务。")
    _complete_workitem_anyone(
        workitem,
        user,
        actor_title="型号生产计划已确认完成",
        peer_title=f"{_display_name(user)}已确认型号生产计划完成",
    )
    _consume_subplan_materials(subplan, user)
    now = timezone.now()
    subplan.status = OrderPlanProductionSubplan.Status.PRODUCTION_COMPLETED
    subplan.production_completed_by = user
    subplan.production_completed_at = now
    subplan.save(
        update_fields=(
            "status", "production_completed_by", "production_completed_at", "updated_at"
        )
    )
    # A completed subplan is normally independently shippable.  Once sales
    # has recorded that an already-started assembly must be cancelled, however,
    # it must stay off the QR/outbound path until every child is complete and
    # sales converts the factual output into neutral finished inventory.
    if not cancellation_audit:
        _ensure_subplan_shipment(
            plan=plan,
            subplan=subplan,
            actor_openid=_actor_openid(user),
        )
    next_subplan = material_plan.production_subplans.select_for_update().filter(
        status=OrderPlanProductionSubplan.Status.PLANNED,
    ).order_by("sequence_no").first()
    if next_subplan:
        next_subplan.status = OrderPlanProductionSubplan.Status.PRODUCTION_PENDING
        next_subplan.production_released_at = now
        next_subplan.save(update_fields=("status", "production_released_at", "updated_at"))
        material_plan.status = OrderPlanMaterialPlan.Status.PRODUCTION_IN_PROGRESS
        material_plan.save(update_fields=("status", "updated_at"))
        _publish_production_task(plan, next_subplan, actor_openid=_actor_openid(user))
    else:
        material_plan.status = OrderPlanMaterialPlan.Status.PRODUCTION_COMPLETED
        material_plan.save(update_fields=("status", "updated_at"))
        plan.status = OrderPlan.Status.PRODUCTION_COMPLETED
        plan.save(update_fields=("status", "updated_at"))
        plan.items.filter(production_quantity__gt=0).update(
            status=OrderPlan.Status.PRODUCTION_COMPLETED,
            updated_at=now,
        )
        # Keep the parent completion state synchronized. A cancellation requested after
        # offline assembly began is the one exception: it emits a sales
        # cancellation notice, never a QR/ship task.
        if cancellation_audit:
            _queue_cancelled_finished_stock(
                plan=plan,
                material_plan=material_plan,
                audit=cancellation_audit,
                actor_openid=_actor_openid(user),
            )
        else:
            _new_workitem(
                plan=plan,
                work_type="ORDER_PLAN_V2_PRODUCTION_COMPLETED_NOTICE",
                stage_key="production-completed-notice",
                title="订单生产已完成，待打码和发货",
                summary="各型号生产计划均已完成；后续按型号生产计划生成二维码、贴码和发货。",
                recipients=_role_openids(
                    plan.factory_organization,
                    {"factory_sales", "factory_sales_assistant", "factory_stock"},
                ),
                need_action=False,
                payload={"material_plan_id": material_plan.pk},
                actor_openid=_actor_openid(user),
                done_immediately=True,
            )
    return get_material_flow_detail(user=user, plan_id=plan.pk)


def _factory_stock_identity(user):
    return _factory_identity(
        user,
        {"factory_stock"},
        message="只有厂家成品库管可以打码和确认发货。",
    )


def _factory_logistics_identity(user):
    return _factory_identity(
        user,
        {"factory_logistics"},
        message="只有厂家物流经理可以填写物流信息并确认线下出库。",
    )


def _merchant_receipt_identity(user):
    identity = get_active_identity(user) if user and user.is_active else None
    if (
        not identity
        or identity.organization.organization_type.code != "MERCHANT"
        or not identity_can_perform_shared_operation(
            identity, "goods_transfer_receipt"
        )
    ):
        raise OrderPlanMaterialFlowError("当前岗位没有接收商家入库权限。")
    return identity


def _sales_assistant_identity(user):
    return _factory_identity(
        user,
        {"factory_sales_assistant"},
        message="只有厂家销售助理可以确认订单入账。",
    )


def _shipment_for_plan(*, plan, shipment_id, lock=False):
    try:
        shipment_id = int(shipment_id)
    except (TypeError, ValueError) as exc:
        raise OrderPlanMaterialFlowError("发货批次参数无效。") from exc
    queryset = OrderPlanShipment.objects.select_related("production_subplan")
    if lock:
        queryset = queryset.select_for_update()
    shipment = queryset.filter(pk=shipment_id, order_plan=plan).first()
    if not shipment:
        raise OrderPlanMaterialFlowError("发货批次不存在或不属于当前订单。")
    return shipment


def _shipment_expected_quantity(shipment):
    return shipment.items.aggregate(value=Sum("quantity"))["value"] or 0


def _shipment_action_result(*, shipment, changed):
    return {
        "shipment": _shipment_payload(shipment),
        "changed": bool(changed),
        "detail_link": _plan_link(shipment.order_plan),
    }


def _shipment_source_item_key(item):
    return item.production_subplan_item_id or 0, item.order_plan_item_id


def _update_open_shipment_authorization_task(*, plan, shipment):
    """Keep a pending default batch truthful after a sales-side split.

    A V2 subplan initially creates one *planned* batch so sales has a concrete
    task to act on.  Before authorization the sales manager may shrink that
    batch and leave the balance in a newly-created planned batch.  The old
    task must not continue to show the pre-split quantity.
    """

    payload = _shipment_payload(shipment)
    workitem = WorkItem.objects.select_for_update().filter(
        type="ORDER_PLAN_V2_SHIPMENT_AUTHORIZATION",
        uniq_key=f"sales:order-plan:{plan.pk}:shipment:{shipment.pk}:authorization",
        status="OPEN",
    ).first()
    if not workitem:
        return
    workitem.title = f"{payload['shipment_label']}待授权发货"
    workitem.summary = (
        f"{plan.merchant_organization.name}｜{payload['quantity']}台{payload.get('accessory_summary', '')}；"
        "授权后由厂家成品库管打码贴包，物流经理确认出库，再由成品库管确认发货。"
    )
    workitem.payload = _group_payload(plan, **payload)
    workitem.save(update_fields=("title", "summary", "payload", "updated_at"))


def _shipment_source_pending_batch(*, plan, production_subplan):
    queryset = OrderPlanShipment.objects.select_for_update().prefetch_related("items")
    queryset = queryset.filter(
        order_plan=plan,
        production_subplan=production_subplan,
        status=OrderPlanShipment.Status.PLANNED,
    )
    return queryset.order_by("sequence_no", "id").first()


def _validate_shipment_split_rows(*, shipment, raw_items):
    if not isinstance(raw_items, list) or not raw_items:
        raise OrderPlanMaterialFlowError("请至少填写一项本批发货数量。")
    source_rows = list(
        shipment.items.select_related(
            "order_plan_item__product_model", "production_subplan_item"
        ).order_by("id")
    )
    source_by_key = {_shipment_source_item_key(row): row for row in source_rows}
    desired = {}
    for index, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            raise OrderPlanMaterialFlowError(f"第{index}项发货数据格式不正确。")
        quantity = _positive_integer(raw.get("quantity"), f"第{index}项发货数量")
        if shipment.production_subplan_id:
            sub_item_id = _positive_integer(
                raw.get("production_subplan_item_id"),
                f"第{index}项生产子计划产品",
            )
            key = (sub_item_id, 0)
            # ``_shipment_source_item_key`` also carries the plan item as a
            # second component.  Resolve by the unambiguous subplan item.
            candidates = [
                row for row in source_rows if row.production_subplan_item_id == sub_item_id
            ]
            if len(candidates) != 1:
                raise OrderPlanMaterialFlowError("发货产品不属于当前待拆分批次。")
            source = candidates[0]
            key = _shipment_source_item_key(source)
        else:
            item_id = _positive_integer(raw.get("order_plan_item_id"), f"第{index}项订单产品")
            candidates = [
                row for row in source_rows if row.order_plan_item_id == item_id
            ]
            if len(candidates) != 1:
                raise OrderPlanMaterialFlowError("发货产品不属于当前待拆分批次。")
            source = candidates[0]
            key = _shipment_source_item_key(source)
        if key in desired:
            raise OrderPlanMaterialFlowError("同一产品在一个发货批次中不能重复填写。")
        if quantity > source.quantity:
            raise OrderPlanMaterialFlowError(
                f"{source.order_plan_item.product_model.name}可安排发货数量不足，请刷新后重试。"
            )
        desired[key] = quantity
    if not desired:
        raise OrderPlanMaterialFlowError("请至少填写一项本批发货数量。")
    return source_rows, desired


@transaction.atomic
def create_material_flow_shipment(
    *, user, plan_id, client_request_id, shipment_id=None,
    production_subplan_id=None, items=None
):
    """Split one not-yet-authorized V2 shipment when an urgent partial ship is needed.

    The initial full subplan/finished-inventory batch is intentionally only
    ``PLANNED``.  Sales can replace it with a smaller first batch; the exact
    balance remains another planned batch and receives its own sales task.
    This preserves a complete quantity proof while allowing multiple batches
    without inventing a third order or merging child plans.
    """

    identity = _sales_task_identity(user)
    plan = _v2_plan_for_factory(identity, plan_id, lock=True)
    if plan.created_by_id not in _sales_actor_user_ids(identity):
        raise OrderPlanMaterialFlowError("只有创建该订单的厂家销售经理可以拆分发货批次。")
    if plan.status in {OrderPlan.Status.CANCELLED, OrderPlan.Status.RECEIVED}:
        raise OrderPlanMaterialFlowError("当前订单状态不能再拆分发货批次。")
    _assert_shipment_path_open(plan)
    request_id = str(client_request_id or "").strip()
    if not request_id or len(request_id) > 64:
        raise OrderPlanMaterialFlowError("发货批次请求标识无效，请刷新页面后重试。")
    prior = OrderPlanShipment.objects.select_for_update().filter(
        order_plan=plan, client_request_id=request_id
    ).first()
    if prior:
        return _shipment_action_result(shipment=prior, changed=False), False

    material_plan = OrderPlanMaterialPlan.objects.select_for_update().filter(order_plan=plan).first()
    if not material_plan:
        raise OrderPlanMaterialFlowError("订单物料计划不存在。")
    if production_subplan_id in (None, ""):
        source_subplan = None
        if not plan.items.filter(return_inventory_quantity__gt=0).exists():
            raise OrderPlanMaterialFlowError("该订单没有可拆分的成品库存发货批次。")
    else:
        try:
            source_subplan_id = int(production_subplan_id)
        except (TypeError, ValueError) as exc:
            raise OrderPlanMaterialFlowError("生产子计划参数无效。") from exc
        source_subplan = material_plan.production_subplans.select_for_update().filter(
            pk=source_subplan_id,
            status__in={
                OrderPlanProductionSubplan.Status.PRODUCTION_COMPLETED,
                OrderPlanProductionSubplan.Status.QRCODE_COMPLETED,
            },
        ).first()
        if not source_subplan:
            raise OrderPlanMaterialFlowError("只有已完成生产、尚未发货的子计划可以拆分发货。")

    if shipment_id in (None, ""):
        shipment = _shipment_source_pending_batch(
            plan=plan,
            production_subplan=source_subplan,
        )
    else:
        shipment = _shipment_for_plan(
            plan=plan,
            shipment_id=shipment_id,
            lock=True,
        )
        if shipment.production_subplan_id != (
            source_subplan.pk if source_subplan is not None else None
        ):
            raise OrderPlanMaterialFlowError("发货批次与所选生产子计划不匹配。")
        if shipment.status != OrderPlanShipment.Status.PLANNED:
            raise OrderPlanMaterialFlowError("仅待确认发货批次可以拆分。")
    if not shipment:
        raise OrderPlanMaterialFlowError(
            "当前没有可拆分的待确认发货批次；已授权或已出库批次不能修改。"
        )
    source_rows, desired = _validate_shipment_split_rows(shipment=shipment, raw_items=items)
    original = {_shipment_source_item_key(row): row.quantity for row in source_rows}
    if desired == original:
        return _shipment_action_result(shipment=shipment, changed=False), False

    remainder = []
    for row in source_rows:
        key = _shipment_source_item_key(row)
        chosen = desired.get(key, 0)
        left = row.quantity - chosen
        if left:
            remainder.append((row, left))
        if chosen:
            row.quantity = chosen
            row.save(update_fields=("quantity",))
        else:
            row.delete()
    if not shipment.items.exists():
        raise OrderPlanMaterialFlowError("拆分后的首批发货数量不能为空。")
    # The client request identifies the balance record.  Retrying after a
    # completed transaction returns that exact record rather than creating a
    # second residual batch.
    residual = OrderPlanShipment.objects.create(
        order_plan=plan,
        production_subplan=source_subplan,
        sequence_no=_next_shipment_sequence(plan),
        client_request_id=request_id,
    )
    OrderPlanShipmentItem.objects.bulk_create(
        [
            OrderPlanShipmentItem(
                shipment=residual,
                order_plan_item=row.order_plan_item,
                production_subplan_item=row.production_subplan_item,
                quantity=quantity,
            )
            for row, quantity in remainder
        ]
    )
    _update_open_shipment_authorization_task(plan=plan, shipment=shipment)
    _publish_shipment_authorization_task(
        plan=plan,
        shipment=residual,
        actor_openid=_actor_openid(user),
    )
    return {
        "shipment": _shipment_payload(shipment),
        "residual_shipment": _shipment_payload(residual),
        "changed": True,
        "detail_link": _plan_link(plan),
    }, True


def _production_devices_for_subplan_item(*, sub_item, actor):
    selection = OrderPlanSalesBOMSelection.objects.select_related("frozen_bom").filter(
        order_plan_item_id=sub_item.order_plan_item_id
    ).first()
    if not selection:
        raise OrderPlanMaterialFlowError("生产子计划缺少销售冻结BOM，不能生成二维码。")
    plan = sub_item.production_subplan.material_plan.order_plan
    batch, created = ProductionBatch.objects.select_for_update().get_or_create(
        batch_no=sub_item.production_batch_code,
        defaults={
            "product_model": sub_item.order_plan_item.product_model,
            "bom": selection.frozen_bom,
            "manufacturer": plan.factory_organization,
            "production_date": timezone.localdate(),
            "quantity": sub_item.quantity,
        },
    )
    expected = (
        sub_item.order_plan_item.product_model_id,
        selection.frozen_bom_id,
        plan.factory_organization_id,
        sub_item.quantity,
    )
    actual = (batch.product_model_id, batch.bom_id, batch.manufacturer_id, batch.quantity)
    if actual != expected:
        raise OrderPlanMaterialFlowError(
            "已有生产批次与当前子计划的型号、BOM、厂家或数量不一致，请停止操作并核对。"
        )
    devices = list(batch.devices.select_for_update().order_by("id"))
    if len(devices) > sub_item.quantity:
        raise OrderPlanMaterialFlowError("生产批次设备数量超过子计划数量，不能继续打码。")
    while len(devices) < sub_item.quantity:
        devices.append(
            LifecycleService.create_device(
                product_model=sub_item.order_plan_item.product_model,
                operator=actor,
                organization=plan.factory_organization,
                batch=batch,
            )
        )
    now = timezone.now()
    changed = []
    if sub_item.production_batch_id != batch.pk:
        sub_item.production_batch = batch
        changed.append("production_batch")
    if not sub_item.qrcode_generated_at:
        sub_item.qrcode_generated_at = now
        changed.append("qrcode_generated_at")
    if changed:
        sub_item.save(update_fields=tuple(changed + ["updated_at"]))
    return list(batch.devices.select_related("qrcode").order_by("id"))


def _map_shipment_devices(*, plan, shipment, actor):
    """Allocate exact devices before print; this is the V2 proof of partial ship."""

    shipment_items = list(
        shipment.items.select_for_update().select_related(
            "order_plan_item__product_model",
            "production_subplan_item__production_subplan__material_plan__order_plan",
        ).order_by("id")
    )
    if not shipment_items:
        raise OrderPlanMaterialFlowError("发货批次没有产品明细。")
    existing_total = shipment.device_allocations.count()
    expected_total = sum(row.quantity for row in shipment_items)
    if existing_total:
        if existing_total != expected_total:
            raise OrderPlanMaterialFlowError("发货批次设备分配不完整，不能继续打码。")
        return list(
            shipment.device_allocations.select_related(
                "device__qrcode", "return_inventory_allocation__replacement_qr"
            ).order_by("shipment_item_id", "id")
        )

    created = []
    used_device_ids = set(
        OrderPlanShipmentDevice.objects.filter(order_plan=plan).values_list("device_id", flat=True)
    )
    for shipment_item in shipment_items:
        if shipment_item.production_subplan_item_id:
            sub_item = shipment_item.production_subplan_item
            if sub_item.production_subplan_id != shipment.production_subplan_id:
                raise OrderPlanMaterialFlowError("发货批次与生产子计划不一致。")
            devices = _production_devices_for_subplan_item(sub_item=sub_item, actor=actor)
            choices = [device for device in devices if device.pk not in used_device_ids]
            if len(choices) < shipment_item.quantity:
                raise OrderPlanMaterialFlowError("该生产子计划可用于本批发货的设备数量不足。")
            for device in choices[: shipment_item.quantity]:
                created.append(
                    OrderPlanShipmentDevice(
                        shipment=shipment,
                        shipment_item=shipment_item,
                        order_plan=plan,
                        device=device,
                    )
                )
                used_device_ids.add(device.pk)
            continue

        if shipment.production_subplan_id:
            raise OrderPlanMaterialFlowError("生产子计划发货批次不能混入成品库存设备。")
        # Reused finished inventory keeps its original QR trace, while this mapping limits *printing and outbound*
        # to the exact partial shipment selected by sales.
        ensure_reissue_qrcodes(order_plan_item=shipment_item.order_plan_item, actor=actor)
        allocations = list(
            OrderPlanReturnInventoryAllocation.objects.select_for_update()
            .select_related("source_device", "replacement_qr")
            .filter(
                order_plan_item=shipment_item.order_plan_item,
                status__in={
                    OrderPlanReturnInventoryAllocation.Status.RESERVED,
                    OrderPlanReturnInventoryAllocation.Status.READY_TO_SHIP,
                },
            )
            .filter(v2_shipment_device_allocation__isnull=True)
            .order_by("id")
        )
        allocations = [row for row in allocations if row.source_device_id not in used_device_ids]
        if len(allocations) < shipment_item.quantity:
            raise OrderPlanMaterialFlowError("可用于本批发货的成品库存设备数量不足。")
        for allocation in allocations[: shipment_item.quantity]:
            if not allocation.replacement_qr_id:
                raise OrderPlanMaterialFlowError("成品库存设备二维码未生成，不能继续打码。")
            created.append(
                OrderPlanShipmentDevice(
                    shipment=shipment,
                    shipment_item=shipment_item,
                    order_plan=plan,
                    device=allocation.source_device,
                    return_inventory_allocation=allocation,
                )
            )
            used_device_ids.add(allocation.source_device_id)
    OrderPlanShipmentDevice.objects.bulk_create(created)
    return list(
        OrderPlanShipmentDevice.objects.select_related(
            "device__qrcode", "return_inventory_allocation__replacement_qr"
        ).filter(shipment=shipment).order_by("shipment_item_id", "id")
    )


def _mark_selected_return_allocations_ready(*, shipment_device_rows, now):
    allocations = [
        row.return_inventory_allocation
        for row in shipment_device_rows
        if row.return_inventory_allocation_id
    ]
    if not allocations:
        return
    allocation_ids = [row.pk for row in allocations]
    inventory_ids = [row.inventory_item_id for row in allocations if row.inventory_item_id]
    recall_ids = [row.recall_inventory_id for row in allocations if row.recall_inventory_id]
    finished_inventory_ids = [
        row.finished_inventory_item_id
        for row in allocations
        if row.finished_inventory_item_id
    ]
    OrderPlanReturnInventoryAllocation.objects.filter(
        pk__in=allocation_ids,
        status=OrderPlanReturnInventoryAllocation.Status.RESERVED,
    ).update(
        status=OrderPlanReturnInventoryAllocation.Status.READY_TO_SHIP,
        ready_to_ship_at=now,
        updated_at=now,
    )
    if inventory_ids:
        FactoryReturnInventoryItem.objects.filter(pk__in=inventory_ids).update(
            stock_status=FactoryReturnInventoryItem.StockStatus.READY_TO_SHIP,
            updated_at=now,
        )
    if recall_ids:
        QualityRecallReusableInventory.objects.filter(pk__in=recall_ids).update(
            stock_status=QualityRecallReusableInventory.Status.READY_TO_SHIP,
            updated_at=now,
        )
    if finished_inventory_ids:
        FactoryFinishedGoodsInventoryItem.objects.filter(pk__in=finished_inventory_ids).update(
            stock_status=FactoryFinishedGoodsInventoryItem.StockStatus.READY_TO_SHIP,
            updated_at=now,
        )


def _mark_selected_return_allocations_reissued(*, shipment_device_rows, now):
    allocations = [
        row.return_inventory_allocation
        for row in shipment_device_rows
        if row.return_inventory_allocation_id
    ]
    if not allocations:
        return
    allocation_ids = [row.pk for row in allocations]
    inventory_ids = [row.inventory_item_id for row in allocations if row.inventory_item_id]
    recall_ids = [row.recall_inventory_id for row in allocations if row.recall_inventory_id]
    finished_inventory_ids = [
        row.finished_inventory_item_id
        for row in allocations
        if row.finished_inventory_item_id
    ]
    OrderPlanReturnInventoryAllocation.objects.filter(pk__in=allocation_ids).update(
        status=OrderPlanReturnInventoryAllocation.Status.REISSUED,
        reissued_at=now,
        updated_at=now,
    )
    if inventory_ids:
        FactoryReturnInventoryItem.objects.filter(pk__in=inventory_ids).update(
            stock_status=FactoryReturnInventoryItem.StockStatus.REISSUED,
            updated_at=now,
        )
    if recall_ids:
        QualityRecallReusableInventory.objects.filter(pk__in=recall_ids).update(
            stock_status=QualityRecallReusableInventory.Status.REISSUED,
            updated_at=now,
        )
    if finished_inventory_ids:
        FactoryFinishedGoodsInventoryItem.objects.filter(pk__in=finished_inventory_ids).update(
            stock_status=FactoryFinishedGoodsInventoryItem.StockStatus.REISSUED,
            updated_at=now,
        )


def _shipment_workitem(*, plan, shipment, work_type, stage_key):
    workitem = WorkItem.objects.select_for_update().filter(
        type=work_type,
        uniq_key=f"sales:order-plan:{plan.pk}:shipment:{shipment.pk}:{stage_key}",
        status="OPEN",
    ).first()
    if not workitem:
        raise OrderPlanMaterialFlowError("未找到本发货批次的待办任务。")
    return workitem


def _shipment_devices_ready(shipment):
    expected = _shipment_expected_quantity(shipment)
    actual = shipment.device_allocations.count()
    return expected > 0 and expected == actual


def _all_source_quantities_at_status(*, plan, statuses):
    """Return True only when each order item has its full quantity at status."""

    delivered = {
        row["order_plan_item_id"]: row["value"] or 0
        for row in OrderPlanShipmentItem.objects.filter(
            shipment__order_plan=plan,
            shipment__status__in=tuple(statuses),
        ).values("order_plan_item_id").annotate(value=Sum("quantity"))
    }
    for item in plan.items.all():
        if delivered.get(item.pk, 0) < item.quantity:
            return False
    return plan.items.exists()


def _refresh_subplan_progress(*, plan, now):
    """Advance only a fully covered child plan; parent order stays aggregate."""

    subplans = OrderPlanProductionSubplan.objects.select_for_update().filter(
        material_plan__order_plan=plan
    ).order_by("sequence_no")
    for subplan in subplans:
        sub_items = list(subplan.items.select_for_update().all())
        expected = sum(row.quantity for row in sub_items)
        if not expected:
            continue
        base = OrderPlanShipmentDevice.objects.filter(
            shipment__order_plan=plan,
            shipment_item__production_subplan_item__production_subplan=subplan,
        )
        for sub_item in sub_items:
            item_printed = base.filter(
                shipment_item__production_subplan_item=sub_item,
                qrcode_printed_at__isnull=False,
            ).count()
            if item_printed == sub_item.quantity and not sub_item.qrcode_printed_at:
                sub_item.qrcode_printed_at = now
                sub_item.save(update_fields=("qrcode_printed_at", "updated_at"))
        printed = base.filter(qrcode_printed_at__isnull=False).count()
        shipped = base.filter(
            shipment__status__in=(OrderPlanShipment.Status.SHIPPED, OrderPlanShipment.Status.RECEIVED)
        ).count()
        received = base.filter(shipment__status=OrderPlanShipment.Status.RECEIVED).count()
        if received == expected:
            if subplan.status != OrderPlanProductionSubplan.Status.RECEIVED:
                subplan.status = OrderPlanProductionSubplan.Status.RECEIVED
                subplan.save(update_fields=("status", "updated_at"))
        elif shipped == expected:
            if subplan.status != OrderPlanProductionSubplan.Status.SHIPPED:
                subplan.status = OrderPlanProductionSubplan.Status.SHIPPED
                subplan.save(update_fields=("status", "updated_at"))
        elif printed == expected and subplan.status == OrderPlanProductionSubplan.Status.PRODUCTION_COMPLETED:
            subplan.status = OrderPlanProductionSubplan.Status.QRCODE_COMPLETED
            subplan.qrcode_completed_at = now
            subplan.save(update_fields=("status", "qrcode_completed_at", "updated_at"))


@transaction.atomic
def authorize_material_flow_shipment(*, user, plan_id, shipment_id):
    identity = _sales_task_identity(user)
    plan = _v2_plan_for_factory(identity, plan_id, lock=True)
    if plan.created_by_id not in _sales_actor_user_ids(identity):
        raise OrderPlanMaterialFlowError("只有创建该订单的厂家销售经理可以确认发货。")
    _assert_shipment_path_open(plan)
    shipment = _shipment_for_plan(plan=plan, shipment_id=shipment_id, lock=True)
    if shipment.status in {
        OrderPlanShipment.Status.AUTHORIZED,
        OrderPlanShipment.Status.OUTBOUNDED,
        OrderPlanShipment.Status.SHIPPED,
        OrderPlanShipment.Status.RECEIVED,
    }:
        return _shipment_action_result(shipment=shipment, changed=False), False
    if shipment.status != OrderPlanShipment.Status.PLANNED:
        raise OrderPlanMaterialFlowError("当前发货批次不能确认发货。")
    workitem = _shipment_workitem(
        plan=plan,
        shipment=shipment,
        work_type="ORDER_PLAN_V2_SHIPMENT_AUTHORIZATION",
        stage_key="authorization",
    )
    _complete_workitem_anyone(
        workitem,
        user,
        actor_title=f"{_shipment_label(shipment)}发货已授权",
        peer_title=f"{_display_name(user)}已授权{_shipment_label(shipment)}发货",
    )
    now = timezone.now()
    shipment.status = OrderPlanShipment.Status.AUTHORIZED
    shipment.shipment_authorized_by = user
    shipment.shipment_authorized_at = now
    shipment.save(
        update_fields=(
            "status", "shipment_authorized_by", "shipment_authorized_at", "updated_at"
        )
    )
    if not plan.shipment_authorized_at:
        plan.shipment_authorized_at = now
        plan.shipment_authorized_by = user
        plan.save(update_fields=("shipment_authorized_at", "shipment_authorized_by", "updated_at"))
    _publish_shipment_qrcode_task(plan=plan, shipment=shipment, actor_openid=_actor_openid(user))
    return _shipment_action_result(shipment=shipment, changed=True), True


@quality_gate_atomic(OrderPlanMaterialFlowError)
def confirm_material_flow_shipment_qrcode_print(*, user, plan_id, shipment_id):
    identity = _factory_stock_identity(user)
    plan = _v2_plan_for_factory(identity, plan_id, lock=True)
    _assert_shipment_path_open(plan)
    shipment = _shipment_for_plan(plan=plan, shipment_id=shipment_id, lock=True)
    if shipment.qrcode_printed_at:
        return _shipment_action_result(shipment=shipment, changed=False), False
    if shipment.status != OrderPlanShipment.Status.AUTHORIZED:
        raise OrderPlanMaterialFlowError("厂家销售经理尚未授权本批发货，不能打码。")
    workitem = _shipment_workitem(
        plan=plan,
        shipment=shipment,
        work_type="ORDER_PLAN_V2_QRCODE_PRINT",
        stage_key="qrcode-print",
    )
    mappings = _map_shipment_devices(plan=plan, shipment=shipment, actor=user)
    if len(mappings) != _shipment_expected_quantity(shipment):
        raise OrderPlanMaterialFlowError("本批二维码设备数量不完整，不能确认打印。")
    for mapping in mappings:
        enforce_device_operation(
            device_id=mapping.device_id,
            operation="QRCODE_PRINT",
            user=user,
            context={
                "source": "ORDER_PLAN_V2_QRCODE_PRINT",
                "order_plan_id": plan.pk,
                "plan_code": plan.plan_code,
                "shipment_id": shipment.pk,
                "shipment_sequence_no": shipment.sequence_no,
            },
        )
    now = timezone.now()
    _complete_workitem_anyone(
        workitem,
        user,
        actor_title=f"{_shipment_label(shipment)}二维码已打印",
        peer_title=f"{_display_name(user)}已完成{_shipment_label(shipment)}二维码打印",
    )
    OrderPlanShipmentDevice.objects.filter(
        pk__in=[row.pk for row in mappings], qrcode_printed_at__isnull=True
    ).update(qrcode_printed_at=now, updated_at=now)
    _mark_selected_return_allocations_ready(shipment_device_rows=mappings, now=now)
    shipment.qrcode_printed_at = now
    shipment.save(update_fields=("qrcode_printed_at", "updated_at"))
    _refresh_subplan_progress(plan=plan, now=now)
    _publish_shipment_logistics_task(
        plan=plan,
        shipment=shipment,
        actor_openid=_actor_openid(user),
    )
    return _shipment_action_result(shipment=shipment, changed=True), True


@transaction.atomic
def prepare_material_flow_shipment_qrcodes(*, user, plan_id, shipment_id):
    """Lock exact V2 shipment devices for preview without marking them printed."""
    identity = _factory_stock_identity(user)
    plan = _v2_plan_for_factory(identity, plan_id, lock=True)
    _assert_shipment_path_open(plan)
    shipment = _shipment_for_plan(plan=plan, shipment_id=shipment_id, lock=True)
    # A confirmed physical print is an irreversible business fact.  Do not
    # expose the same shipment through a stale page or a handcrafted request.
    if shipment.qrcode_printed_at:
        raise OrderPlanMaterialFlowError("本批二维码已确认打印贴包，不能再次生成打印清单。")
    if shipment.status != OrderPlanShipment.Status.AUTHORIZED:
        raise OrderPlanMaterialFlowError("厂家销售经理尚未授权本批发货，不能生成二维码清单。")
    rows = _map_shipment_devices(plan=plan, shipment=shipment, actor=user)
    return {
        "shipment_id": shipment.pk,
        "plan_id": plan.pk,
        "plan_code": plan.plan_code,
        "shipment_title": _shipment_label(shipment),
        "qrcodes": [
            {"device_sn": row.device.sn, "code": _qrcode_for_shipment_mapping(row)}
            for row in rows
        ],
    }


def _serialize_logistics_document(document):
    if not document:
        return None
    return {
        "id": document.pk,
        "file_url": (
            f"/pldp/api/sales/order-plans/{document.order_plan_id}/material-flow/"
            f"shipment-logistics-documents/{document.pk}/file/"
        ),
        "file_type": document.file_type,
        "content_type": document.content_type,
        "original_name": document.original_name,
        "file_size": document.file_size,
        "uploaded_at": document.uploaded_at.isoformat() if document.uploaded_at else None,
    }


def _positive_id(value, label):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise OrderPlanMaterialFlowError(f"{label}参数无效。")
    value = str(value).strip()
    if len(value) > 19 or not value.isascii() or not value.isdecimal() or not 0 < int(value) <= 9223372036854775807:
        raise OrderPlanMaterialFlowError(f"{label}参数无效。")
    return int(value)


def _validate_logistics_image(document):
    if not document or not 0 < document.size <= 10 * 1024 * 1024:
        raise OrderPlanMaterialFlowError("请上传不超过10MB的物流凭证图片。")
    try:
        with Image.open(document) as image:
            file_format = image.format
            image.verify()
        document.seek(0)
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise OrderPlanMaterialFlowError("物流凭证不是有效图片。") from exc
    formats = {"JPEG": (".jpg", "image/jpeg"), "PNG": (".png", "image/png"), "WEBP": (".webp", "image/webp")}
    if file_format not in formats:
        raise OrderPlanMaterialFlowError("物流凭证只支持JPG、PNG或WEBP图片。")
    return formats[file_format]


@transaction.atomic
def upload_material_flow_logistics_document(*, user, plan_id, client_request_id, document):
    identity = _factory_logistics_identity(user)
    plan = _v2_plan_for_factory(identity, plan_id, lock=True)
    _assert_shipment_path_open(plan)
    request_id = str(client_request_id or "").strip()
    if not request_id or len(request_id) > 64:
        raise OrderPlanMaterialFlowError("物流凭证请求标识无效，请重新进入页面。")
    suffix, content_type = _validate_logistics_image(document)
    existing = OrderPlanLogisticsDocument.objects.filter(order_plan=plan, client_request_id=request_id).first()
    if existing:
        if existing.uploaded_by_id != user.pk:
            raise OrderPlanMaterialFlowError("该物流凭证请求标识已被其他账号使用。")
        incoming_hash = hashlib.sha256()
        for chunk in document.chunks():
            incoming_hash.update(chunk)
        document.seek(0)
        stored_hash = hashlib.sha256()
        try:
            with existing.document_file.open("rb") as stored:
                for chunk in stored.chunks():
                    stored_hash.update(chunk)
        except OSError as exc:
            raise OrderPlanMaterialFlowError("已上传物流凭证文件不可读，请联系管理员核对。") from exc
        if incoming_hash.digest() != stored_hash.digest():
            raise OrderPlanMaterialFlowError("相同请求标识不能替换已上传的物流凭证。")
        return {"document": _serialize_logistics_document(existing), "changed": False}, False
    candidates = OrderPlanShipment.objects.filter(
        order_plan=plan, status=OrderPlanShipment.Status.AUTHORIZED,
        qrcode_printed_at__isnull=False, logistics_confirmed_at__isnull=True,
    )
    if not any(_has_open_shipment_task(
        plan=plan, shipment=shipment, identity=identity,
        work_type="ORDER_PLAN_V2_SHIPMENT_LOGISTICS_CONFIRM", stage_key="logistics-confirm",
    ) for shipment in candidates):
        raise OrderPlanMaterialFlowError("当前账号没有可确认出库的已贴包批次。")
    sequence_no = (plan.logistics_documents.aggregate(value=Max("sequence_no"))["value"] or 0) + 1
    row = OrderPlanLogisticsDocument(
        order_plan=plan, sequence_no=sequence_no, client_request_id=request_id,
        original_name=Path(str(document.name).replace("\\", "/")).name[:255],
        file_type=OrderPlanLogisticsDocument.FileType.IMAGE, content_type=content_type,
        file_size=document.size, uploaded_by=user,
    )
    row.document_file.save(f"shipment-{uuid4().hex}{suffix}", document, save=False)
    try:
        row.save()
    except Exception:
        row.document_file.delete(save=False)
        raise
    return {"document": _serialize_logistics_document(row), "changed": True}, True


def get_material_flow_logistics_document_file(*, user, plan_id, document_id):
    identity = _viewer_identity(user)
    plan = _v2_plan_by_id(plan_id)
    if identity.organization.organization_type.code not in {"OWNER", "MERCHANT"} or not _can_view_plan(identity, plan):
        raise OrderPlanMaterialFlowError("当前岗位无权查看此订单物流凭证。")
    document_id = _positive_id(document_id, "物流凭证")
    document = OrderPlanLogisticsDocument.objects.filter(pk=document_id, order_plan=plan).first()
    if not document:
        raise OrderPlanMaterialFlowError("物流凭证不存在或不属于本订单。")
    if not document.shipment_batches.filter(order_plan=plan).exists() and not (
        identity.organization_id == plan.factory_organization_id
        and identity.role.code == "factory_logistics"
        and document.uploaded_by_id == user.pk
    ):
        raise OrderPlanMaterialFlowError("该物流凭证尚未关联发货批次。")
    return document


def _logistics_document_for_confirmation(*, plan, user, document_id):
    if document_id in (None, ""):
        return None
    document_id = _positive_id(document_id, "物流凭证")
    document = OrderPlanLogisticsDocument.objects.select_for_update().filter(
        pk=document_id, order_plan=plan, uploaded_by=user,
    ).first()
    if not document:
        raise OrderPlanMaterialFlowError("物流凭证不存在、不属于本订单或不是当前账号上传。")
    return document


@quality_gate_atomic(OrderPlanMaterialFlowError)
def confirm_material_flow_shipments_logistics(
    *, user, plan_id, shipment_ids, logistics_company, tracking_no, logistics_document_id=None
):
    """Confirm physical outbound atomically for separately traceable batches.

    One document may apply to several batches, but every shipment owns its
    carrier, number, document reference and confirmation audit. Validate all
    states and assigned tasks before changing any batch; retries may repeat
    the exact immutable facts, never overwrite an earlier confirmation.
    """
    identity = _factory_logistics_identity(user)
    plan = _v2_plan_for_factory(identity, plan_id, lock=True)
    _assert_shipment_path_open(plan)
    if not isinstance(shipment_ids, (list, tuple)) or not 1 <= len(shipment_ids) <= 200:
        raise OrderPlanMaterialFlowError("请选择1至200个发货批次。")
    ids = [_positive_id(value, "发货批次") for value in shipment_ids]
    if len(set(ids)) != len(ids):
        raise OrderPlanMaterialFlowError("发货批次不能重复选择。")
    if not isinstance(logistics_company, str) or not isinstance(tracking_no, str):
        raise OrderPlanMaterialFlowError("请填写物流公司和物流单号。")
    company, number = logistics_company.strip(), tracking_no.strip()
    if not company or not number:
        raise OrderPlanMaterialFlowError("请填写物流公司和物流单号。")
    if len(company) > 128 or len(number) > 128:
        raise OrderPlanMaterialFlowError("物流公司和物流单号均不能超过128个字符。")
    shipments = list(OrderPlanShipment.objects.select_for_update().select_related("production_subplan").filter(
        order_plan=plan, pk__in=ids,
    ).order_by("pk"))
    if len(shipments) != len(ids):
        raise OrderPlanMaterialFlowError("选中的发货批次不存在或不属于本订单。")
    document = _logistics_document_for_confirmation(plan=plan, user=user, document_id=logistics_document_id)
    pending = []
    for shipment in shipments:
        if shipment.logistics_confirmed_at:
            if shipment.status not in {OrderPlanShipment.Status.OUTBOUNDED, OrderPlanShipment.Status.SHIPPED, OrderPlanShipment.Status.RECEIVED}:
                raise OrderPlanMaterialFlowError("本批物流状态不一致，请先完成数据库迁移并核对。")
            if (shipment.logistics_company, shipment.tracking_no, shipment.logistics_document_id) != (company, number, document.pk if document else None):
                raise OrderPlanMaterialFlowError("本批物流信息已确认，不能直接修改。")
            continue
        if shipment.status != OrderPlanShipment.Status.AUTHORIZED or not shipment.qrcode_printed_at:
            raise OrderPlanMaterialFlowError("请先完成全部选中批次的二维码打印贴包，再确认线下出库。")
        workitem = _shipment_workitem(plan=plan, shipment=shipment,
            work_type="ORDER_PLAN_V2_SHIPMENT_LOGISTICS_CONFIRM", stage_key="logistics-confirm")
        if workitem.status != "OPEN" or not _has_open_shipment_task(
            plan=plan, shipment=shipment, identity=identity,
            work_type="ORDER_PLAN_V2_SHIPMENT_LOGISTICS_CONFIRM", stage_key="logistics-confirm",
        ):
            raise OrderPlanMaterialFlowError("当前账号不是选中批次物流任务的处理人。")
        pending.append((shipment, workitem))
    if pending and not _role_openids(plan.factory_organization, {"factory_stock"}):
        raise OrderPlanMaterialFlowError("厂家尚未配置有效成品库管，不能下达发货任务。")
    # Outbound is now a physical handover in its own right. Apply the existing
    # recall/quality gate before recording it, then check again on final ship
    # in case a new quality restriction appeared between these two decisions.
    for shipment, _workitem in pending:
        mappings = list(shipment.device_allocations.select_for_update().order_by("pk"))
        if not mappings or len(mappings) != _shipment_expected_quantity(shipment) or any(not row.qrcode_printed_at for row in mappings):
            raise OrderPlanMaterialFlowError("本批二维码设备数量不完整或尚未贴包，不能确认出库。")
        for mapping in mappings:
            enforce_device_operation(
                device_id=mapping.device_id, operation="STOCK_OUTBOUND", user=user,
                context={"source": "ORDER_PLAN_V2_LOGISTICS_OUTBOUND", "order_plan_id": plan.pk,
                    "plan_code": plan.plan_code, "shipment_id": shipment.pk,
                    "shipment_sequence_no": shipment.sequence_no},
            )
    now = timezone.now()
    for shipment, workitem in pending:
        _complete_workitem_anyone(workitem, user,
            actor_title=f"{_shipment_label(shipment)}已出库待发货",
            peer_title=f"{_display_name(user)}已确认{_shipment_label(shipment)}线下出库")
        shipment.status = OrderPlanShipment.Status.OUTBOUNDED
        shipment.logistics_company, shipment.tracking_no = company, number
        shipment.logistics_document = document
        shipment.logistics_confirmed_by, shipment.logistics_confirmed_at = user, now
        shipment.save(update_fields=("status", "logistics_company", "tracking_no", "logistics_document",
            "logistics_confirmed_by", "logistics_confirmed_at", "updated_at"))
        _publish_shipment_outbound_task(plan=plan, shipment=shipment, actor_openid=_actor_openid(user))
    changed_ids = [shipment.pk for shipment, _ in pending]
    return {
        "shipments": [_shipment_payload(shipment) for shipment in shipments],
        "changed": bool(pending), "changed_shipment_ids": changed_ids, "detail_link": _plan_link(plan),
    }, bool(pending)


def confirm_material_flow_shipment_logistics(
    *, user, plan_id, shipment_id, logistics_company, tracking_no, logistics_document_id=None
):
    """Keep the deployed single-shipment endpoint and result shape compatible."""
    result, changed = confirm_material_flow_shipments_logistics(
        user=user, plan_id=plan_id, shipment_ids=[shipment_id],
        logistics_company=logistics_company, tracking_no=tracking_no,
        logistics_document_id=logistics_document_id,
    )
    return {"shipment": result["shipments"][0], "changed": changed, "detail_link": result["detail_link"]}, changed


def _publish_shipment_shipped_notice(*, plan, shipment, actor_openid):
    recipients = set(_role_openids(plan.merchant_organization, {"merchant_owner", "merchant_sales", "merchant_stock"}))
    sales_openid = _sales_openid_for_plan(plan)
    if sales_openid:
        recipients.add(sales_openid)
    payload = _shipment_payload(shipment)
    return _new_workitem(
        plan=plan, work_type="ORDER_PLAN_V2_SHIPPED_NOTICE",
        stage_key=f"shipment:{shipment.pk}:shipped-notice",
        title=f"{payload['shipment_label']}已发货",
        summary=f"{plan.merchant_organization.name}｜本批{payload['quantity']}台{payload.get('accessory_summary', '')}已发货；商家实际收货后确认入库。",
        recipients=sorted(recipients), need_action=False, done_immediately=True,
        payload=payload, actor_openid=actor_openid,
    )


def _ship_accessory_allocations(*, plan, shipment, user, now):
    shipment_items = list(
        OrderPlanShipmentAccessoryItem.objects.select_for_update()
        .filter(shipment=shipment)
        .select_related("accessory_item__material_spec__material")
        .order_by("id")
    )
    if not shipment_items:
        return 0
    shipped_total = 0
    for shipment_item in shipment_items:
        allocations = list(
            OrderPlanAccessoryLotAllocation.objects.select_for_update()
            .select_related("material_lot__stock")
            .filter(shipment_item=shipment_item)
            .order_by("material_lot__received_at", "id")
        )
        if (
            sum(row.reserved_milli for row in allocations) != shipment_item.quantity_milli
            or any(row.status != OrderPlanAccessoryLotAllocation.Status.RESERVED for row in allocations)
        ):
            raise OrderPlanMaterialFlowError("本批配件批次预占记录不完整，不能确认发货。")
        for allocation in allocations:
            amount = allocation.reserved_milli
            changed = FactoryMaterialLot.objects.filter(
                pk=allocation.material_lot_id,
                on_hand_milli__gte=amount,
                reserved_milli__gte=amount,
            ).update(
                on_hand_milli=F("on_hand_milli") - amount,
                reserved_milli=F("reserved_milli") - amount,
            )
            if changed != 1:
                raise OrderPlanMaterialFlowError("厂家配件库存已变化，不能确认本批发货。")
            request_hash = hashlib.sha256(
                f"order-accessory:{plan.pk}:{shipment.pk}:{allocation.pk}:{amount}".encode("utf-8")
            ).hexdigest()
            MaterialWarehouseDocument.objects.create(
                factory=plan.factory_organization,
                stock_id=allocation.material_lot.stock_id,
                document_no=_new_code("MD"),
                request_id=uuid4(),
                request_hash=request_hash,
                kind=MaterialWarehouseDocument.Kind.ORDER_SHIPMENT,
                quantity_milli=amount,
                lot_id=allocation.material_lot_id,
                created_by=user,
            )
            allocation.status = OrderPlanAccessoryLotAllocation.Status.SHIPPED
            allocation.shipped_by = user
            allocation.shipped_at = now
            allocation.save(update_fields=("status", "shipped_by", "shipped_at", "updated_at"))
            shipped_total += amount
        shipment_item.accessory_item.status = OrderPlanAccessoryItem.Status.SHIPPED
        shipment_item.accessory_item.save(update_fields=("status", "updated_at"))
    return shipped_total


def _receive_accessory_allocations(*, plan, shipment, user, now):
    shipment_items = list(
        OrderPlanShipmentAccessoryItem.objects.select_for_update()
        .filter(shipment=shipment)
        .select_related("accessory_item")
        .order_by("id")
    )
    received_total = 0
    for shipment_item in shipment_items:
        allocations = list(
            OrderPlanAccessoryLotAllocation.objects.select_for_update()
            .select_related("material_lot__stock")
            .filter(shipment_item=shipment_item)
            .order_by("material_lot__received_at", "id")
        )
        if (
            sum(row.reserved_milli for row in allocations) != shipment_item.quantity_milli
            or any(row.status != OrderPlanAccessoryLotAllocation.Status.SHIPPED for row in allocations)
        ):
            raise OrderPlanMaterialFlowError("本批配件发货批次记录不完整，不能确认商家入库。")
        for allocation in allocations:
            source_lot = allocation.material_lot
            stock, _created = OrganizationMaterialStock.objects.get_or_create(
                organization=plan.merchant_organization,
                source_factory=plan.factory_organization,
                supplier=source_lot.stock.supplier,
                material_spec_id=source_lot.stock.material_spec_id,
                defaults={"unit": source_lot.stock.unit},
            )
            if stock.unit != source_lot.stock.unit:
                raise OrderPlanMaterialFlowError("商家该配件库存单位与厂家来源批次不一致。")
            reference = (
                f"sales:order-plan:{plan.pk}:shipment:{shipment.pk}:"
                f"accessory-allocation:{allocation.pk}"
            )
            _org_lot, lot_created = OrganizationMaterialLot.objects.get_or_create(
                source_reference=reference,
                defaults={
                    "stock": stock,
                    "source_factory_lot": source_lot,
                    "source_order_plan_id": plan.pk,
                    "source_shipment_id": shipment.pk,
                    "source_accessory_item_id": allocation.accessory_item_id,
                    "source_batch_code_snapshot": source_lot.batch_code,
                    "received_milli": allocation.reserved_milli,
                    "on_hand_milli": allocation.reserved_milli,
                    "received_by": user,
                    "received_at": now,
                },
            )
            if lot_created:
                OrganizationMaterialStock.objects.filter(pk=stock.pk).update(
                    revision=F("revision") + 1
                )
            allocation.status = OrderPlanAccessoryLotAllocation.Status.RECEIVED
            allocation.received_by = user
            allocation.received_at = now
            allocation.save(update_fields=("status", "received_by", "received_at", "updated_at"))
            received_total += allocation.reserved_milli
        if not OrderPlanAccessoryLotAllocation.objects.filter(
            accessory_item=shipment_item.accessory_item
        ).exclude(status=OrderPlanAccessoryLotAllocation.Status.RECEIVED).exists():
            shipment_item.accessory_item.status = OrderPlanAccessoryItem.Status.RECEIVED
            shipment_item.accessory_item.save(update_fields=("status", "updated_at"))
    return received_total


def _release_accessory_allocations(plan):
    allocations = list(
        OrderPlanAccessoryLotAllocation.objects.select_for_update()
        .filter(
            accessory_item__order_plan=plan,
            status=OrderPlanAccessoryLotAllocation.Status.RESERVED,
        )
        .order_by("id")
    )
    released = 0
    now = timezone.now()
    for allocation in allocations:
        amount = allocation.reserved_milli
        if amount:
            changed = FactoryMaterialLot.objects.filter(
                pk=allocation.material_lot_id,
                reserved_milli__gte=amount,
            ).update(reserved_milli=F("reserved_milli") - amount)
            if changed != 1:
                raise OrderPlanMaterialFlowError("配件库存已变化，不能释放预占。")
        allocation.status = OrderPlanAccessoryLotAllocation.Status.RELEASED
        allocation.save(update_fields=("status", "updated_at"))
        released += amount
    plan.accessory_items.filter(
        status=OrderPlanAccessoryItem.Status.RESERVED
    ).update(status=OrderPlanAccessoryItem.Status.RELEASED, updated_at=now)
    return released


def _shipment_event(*, device, event_type, plan, shipment, user, now):
    # A shipment status lock makes this idempotent in normal use.  The explicit
    # lookup also protects a retry immediately after an interrupted request.
    for event in LifecycleEvent.objects.filter(
        device=device,
        event_type=event_type,
        from_organization=plan.factory_organization,
        to_organization=plan.merchant_organization,
    ).order_by("-id"):
        data = event.data if isinstance(event.data, dict) else {}
        if data.get("order_plan_id") == plan.pk and data.get("shipment_id") == shipment.pk:
            return event
    return LifecycleEvent.objects.create(
        device=device,
        event_type=event_type,
        operator=user,
        organization=(
            plan.factory_organization if event_type == "SHIP" else plan.merchant_organization
        ),
        from_organization=plan.factory_organization,
        to_organization=plan.merchant_organization,
        data={
            "source": "ORDER_PLAN_MATERIAL_STOCK_V2",
            "order_plan_id": plan.pk,
            "plan_code": plan.plan_code,
            "shipment_id": shipment.pk,
            "shipment_sequence_no": shipment.sequence_no,
            "logistics_company": shipment.logistics_company,
            "tracking_no": shipment.tracking_no,
            "logistics_document_id": shipment.logistics_document_id,
            "outbound_at": shipment.logistics_confirmed_at.isoformat() if shipment.logistics_confirmed_at else None,
            "flow_version": OrderPlan.FlowVersion.MATERIAL_STOCK_V2,
            "occurred_at": now.isoformat(),
        },
    )


@quality_gate_atomic(OrderPlanMaterialFlowError)
def confirm_material_flow_shipment_outbound(*, user, plan_id, shipment_id):
    identity = _factory_stock_identity(user)
    plan = _v2_plan_for_factory(identity, plan_id, lock=True)
    _assert_shipment_path_open(plan)
    shipment = _shipment_for_plan(plan=plan, shipment_id=shipment_id, lock=True)
    if shipment.status in {OrderPlanShipment.Status.SHIPPED, OrderPlanShipment.Status.RECEIVED}:
        return _shipment_action_result(shipment=shipment, changed=False), False
    if shipment.status != OrderPlanShipment.Status.OUTBOUNDED or not shipment.qrcode_printed_at:
        raise OrderPlanMaterialFlowError("请先由厂家物流经理确认本批线下出库，才能确认发货。")
    if not (
        shipment.logistics_confirmed_at
        and shipment.logistics_company
        and shipment.tracking_no
    ):
        raise OrderPlanMaterialFlowError("请先由厂家物流经理填写本批物流公司和物流单号。")
    workitem = _shipment_workitem(
        plan=plan,
        shipment=shipment,
        work_type="ORDER_PLAN_V2_STOCK_OUTBOUND",
        stage_key="stock-outbound",
    )
    mappings = list(
        shipment.device_allocations.select_for_update().select_related(
            "device", "return_inventory_allocation"
        ).order_by("shipment_item_id", "id")
    )
    if len(mappings) != _shipment_expected_quantity(shipment) or any(
        not row.qrcode_printed_at for row in mappings
    ):
        raise OrderPlanMaterialFlowError("本批二维码设备数量不完整或尚未打印，不能确认发货。")
    for mapping in mappings:
        enforce_device_operation(
            device_id=mapping.device_id,
            operation="STOCK_OUTBOUND",
            user=user,
            context={
                "source": "ORDER_PLAN_V2_STOCK_OUTBOUND",
                "order_plan_id": plan.pk,
                "plan_code": plan.plan_code,
                "shipment_id": shipment.pk,
                "shipment_sequence_no": shipment.sequence_no,
            },
        )
    now = timezone.now()
    _complete_workitem_anyone(
        workitem,
        user,
        actor_title=f"{_shipment_label(shipment)}已发货",
        peer_title=f"{_display_name(user)}已确认{_shipment_label(shipment)}发货",
    )
    _ship_accessory_allocations(plan=plan, shipment=shipment, user=user, now=now)
    for mapping in mappings:
        event = _shipment_event(
            device=mapping.device,
            event_type="SHIP",
            plan=plan,
            shipment=shipment,
            user=user,
            now=now,
        )
        DeviceState.objects.update_or_create(
            device=mapping.device,
            defaults={
                "current_organization": plan.factory_organization,
                "status": "SHIPPED",
                "last_event": event,
                "last_event_time": now,
            },
        )
        OrganizationDevice.objects.update_or_create(
            device=mapping.device,
            defaults={
                "organization": plan.factory_organization,
                "status": "TRANSFERRED",
                "delivered_at": now,
                "last_event": event,
            },
        )
    shipment.status = OrderPlanShipment.Status.SHIPPED
    shipment.shipped_by = user
    shipment.shipped_at = now
    shipment.save(update_fields=("status", "shipped_by", "shipped_at", "updated_at"))
    _mark_selected_return_allocations_reissued(shipment_device_rows=mappings, now=now)
    _refresh_subplan_progress(plan=plan, now=now)
    if _all_source_quantities_at_status(
        plan=plan,
        statuses={OrderPlanShipment.Status.SHIPPED, OrderPlanShipment.Status.RECEIVED},
    ):
        plan.status = OrderPlan.Status.SHIPPED
        plan.shipped_at = now
        plan.shipped_by = user
        plan.save(update_fields=("status", "shipped_at", "shipped_by", "updated_at"))
        for item in plan.items.all():
            item.status = OrderPlan.Status.SHIPPED
            item.save(update_fields=("status", "updated_at"))
    _publish_shipment_shipped_notice(plan=plan, shipment=shipment, actor_openid=_actor_openid(user))
    _publish_shipment_merchant_receipt_task(
        plan=plan,
        shipment=shipment,
        actor_openid=_actor_openid(user),
    )
    return _shipment_action_result(shipment=shipment, changed=True), True


def _publish_accounting_task(*, plan, actor_openid):
    recipients = _role_openids(plan.factory_organization, {"factory_sales_assistant"})
    if not recipients:
        raise OrderPlanMaterialFlowError("厂家尚未配置有效销售助理，不能下达入账任务。")
    return _new_workitem(
        plan=plan,
        work_type="ORDER_PLAN_V2_ACCOUNTING_CONFIRM",
        stage_key="accounting-confirm",
        title="订单待入账",
        summary=f"{plan.merchant_organization.name}｜共{plan.total_quantity}台已完成入库。",
        recipients=recipients,
        need_action=True,
        action_mode="ANYONE_DO",
        payload={"received_quantity": plan.total_quantity},
        actor_openid=actor_openid,
    )


@transaction.atomic
def confirm_material_flow_merchant_receipt(*, user, plan_id, shipment_id):
    identity = _merchant_receipt_identity(user)
    plan = _v2_plan_by_id(plan_id, lock=True)
    if plan.merchant_organization_id != identity.organization_id:
        raise OrderPlanMaterialFlowError("订单计划不存在或不属于当前商家。")
    _assert_shipment_path_open(plan)
    shipment = _shipment_for_plan(plan=plan, shipment_id=shipment_id, lock=True)
    if shipment.status == OrderPlanShipment.Status.RECEIVED:
        return _shipment_action_result(shipment=shipment, changed=False), False
    if shipment.status != OrderPlanShipment.Status.SHIPPED:
        raise OrderPlanMaterialFlowError("本发货批次尚未发货，不能确认商家入库。")
    workitem = _shipment_workitem(
        plan=plan,
        shipment=shipment,
        work_type="ORDER_PLAN_V2_MERCHANT_RECEIPT",
        stage_key="merchant-receipt",
    )
    mappings = list(
        shipment.device_allocations.select_for_update().select_related("device").order_by(
            "shipment_item_id", "id"
        )
    )
    if len(mappings) != _shipment_expected_quantity(shipment):
        raise OrderPlanMaterialFlowError("本批设备分配不完整，不能确认商家入库。")
    now = timezone.now()
    _complete_workitem_anyone(
        workitem,
        user,
        actor_title=f"{_shipment_label(shipment)}商家已入库",
        peer_title=f"{_display_name(user)}已完成{_shipment_label(shipment)}商家入库",
        allow_no_task=(
            identity.role.code in SENIOR_MANAGER_ROLE_CODES
            and identity_can_perform_shared_operation(
                identity, "goods_transfer_receipt"
            )
        ),
    )
    _receive_accessory_allocations(plan=plan, shipment=shipment, user=user, now=now)
    for mapping in mappings:
        event = _shipment_event(
            device=mapping.device,
            event_type="RECEIVE",
            plan=plan,
            shipment=shipment,
            user=user,
            now=now,
        )
        DeviceState.objects.update_or_create(
            device=mapping.device,
            defaults={
                "current_organization": plan.merchant_organization,
                "status": "IN_STOCK",
                "last_event": event,
                "last_event_time": now,
            },
        )
        OrganizationDevice.objects.update_or_create(
            device=mapping.device,
            defaults={
                "organization": plan.merchant_organization,
                "status": "IN_STOCK",
                "received_at": now,
                "last_event": event,
            },
        )
    shipment.status = OrderPlanShipment.Status.RECEIVED
    shipment.received_by = user
    shipment.received_at = now
    shipment.save(update_fields=("status", "received_by", "received_at", "updated_at"))
    from sales.services.shipment_receipt_notice_service import close_received_shipment_notices
    close_received_shipment_notices(shipment)
    _refresh_subplan_progress(plan=plan, now=now)
    if _all_source_quantities_at_status(
        plan=plan,
        statuses={OrderPlanShipment.Status.RECEIVED},
    ):
        plan.status = OrderPlan.Status.RECEIVED
        plan.received_at = now
        plan.received_by = user
        plan.save(update_fields=("status", "received_at", "received_by", "updated_at"))
        for item in plan.items.all():
            item.status = OrderPlan.Status.RECEIVED
            item.save(update_fields=("status", "updated_at"))
        _publish_accounting_task(plan=plan, actor_openid=_actor_openid(user))
        _new_workitem(
            plan=plan,
            work_type="ORDER_PLAN_V2_RECEIVED_NOTICE",
            stage_key="received-notice:sales",
            title="订单已完成商家入库",
            summary=f"{plan.merchant_organization.name}已完成全部{plan.total_quantity}台入库。",
            recipients=(_sales_openid_for_plan(plan),),
            need_action=False,
            payload={"received_quantity": plan.total_quantity},
            actor_openid=_actor_openid(user),
            done_immediately=True,
        )
    return _shipment_action_result(shipment=shipment, changed=True), True


@transaction.atomic
def confirm_material_flow_accounting(*, user, plan_id, shipment_id=None):
    identity = _sales_assistant_identity(user)
    plan = _v2_plan_for_factory(identity, plan_id, lock=True)
    _assert_shipment_path_open(plan)
    if shipment_id not in (None, ""):
        _shipment_for_plan(plan=plan, shipment_id=shipment_id, lock=True)
    if plan.accounted_at:
        return {
            "accounted_by_name": _display_name(plan.accounted_by) if plan.accounted_by_id else "其他成员",
            "accounted_at": plan.accounted_at.isoformat(),
        }, False
    if plan.status != OrderPlan.Status.RECEIVED or not _all_source_quantities_at_status(
        plan=plan, statuses={OrderPlanShipment.Status.RECEIVED}
    ):
        raise OrderPlanMaterialFlowError("商家尚未完成全部批次入库，不能确认订单入账。")
    workitem = WorkItem.objects.select_for_update().filter(
        type="ORDER_PLAN_V2_ACCOUNTING_CONFIRM",
        uniq_key=f"sales:order-plan:{plan.pk}:accounting-confirm",
    ).first()
    if not workitem:
        raise OrderPlanMaterialFlowError("未找到订单入账任务。")
    _complete_workitem_anyone(
        workitem,
        user,
        actor_title="订单入账已确认",
        peer_title=f"{_display_name(user)}已确认订单入账",
    )
    now = timezone.now()
    plan.accounted_at = now
    plan.accounted_by = user
    plan.save(update_fields=("accounted_at", "accounted_by", "updated_at"))
    return {"accounted_by_name": _display_name(user), "accounted_at": now.isoformat()}, True


def _release_material_allocations(material_plan):
    now = timezone.now()
    allocations = list(
        OrderPlanMaterialLotAllocation.objects.select_for_update().select_related("material_lot")
        .filter(requirement__production_subplan_item__production_subplan__material_plan=material_plan)
        .filter(status=OrderPlanMaterialLotAllocation.Status.RESERVED)
    )
    released = 0
    for allocation in allocations:
        amount = _allocation_available(allocation)
        if amount:
            changed = FactoryMaterialLot.objects.filter(
                pk=allocation.material_lot_id,
                reserved_milli__gte=amount,
            ).update(reserved_milli=F("reserved_milli") - amount)
            if changed != 1:
                raise OrderPlanMaterialFlowError("物料库存已变化，不能释放预占。")
            allocation.released_milli = F("released_milli") + amount
            allocation.save(update_fields=("released_milli", "updated_at"))
            released += amount
        # A completed child plan has already consumed its material.  It is an
        # audit fact and must never be relabelled as a released reservation
        # merely because a later child is cancelled.  One allocation belongs
        # to one subplan requirement, so the remaining balance can be safely
        # released without changing the consumed portion.
        if allocation.consumed_milli:
            allocation.status = OrderPlanMaterialLotAllocation.Status.CONSUMED
        else:
            allocation.status = OrderPlanMaterialLotAllocation.Status.RELEASED
        allocation.save(update_fields=("status", "updated_at"))
    # Keep requirement reservation as its actual consumed amount after release.
    for requirement in OrderPlanMaterialRequirement.objects.select_for_update().filter(
        production_subplan_item__production_subplan__material_plan=material_plan
    ):
        if requirement.reserved_milli != requirement.consumed_milli:
            requirement.reserved_milli = requirement.consumed_milli
            requirement.save(update_fields=("reserved_milli", "updated_at"))
    return released, now


def _release_return_inventory(plan):
    now = timezone.now()
    allocations = list(
        OrderPlanReturnInventoryAllocation.objects.select_for_update().filter(
            order_plan_item__plan=plan,
            status=OrderPlanReturnInventoryAllocation.Status.RESERVED,
        )
    )
    inventory_ids = [row.inventory_item_id for row in allocations if row.inventory_item_id]
    recall_ids = [row.recall_inventory_id for row in allocations if row.recall_inventory_id]
    finished_inventory_ids = [
        row.finished_inventory_item_id
        for row in allocations
        if row.finished_inventory_item_id
    ]
    if inventory_ids:
        FactoryReturnInventoryItem.objects.filter(pk__in=inventory_ids).update(
            stock_status=FactoryReturnInventoryItem.StockStatus.PENDING_REISSUE,
            updated_at=now,
        )
    if recall_ids:
        QualityRecallReusableInventory.objects.filter(pk__in=recall_ids).update(
            stock_status=QualityRecallReusableInventory.Status.PENDING_REISSUE,
            updated_at=now,
        )
    if finished_inventory_ids:
        FactoryFinishedGoodsInventoryItem.objects.filter(pk__in=finished_inventory_ids).update(
            stock_status=FactoryFinishedGoodsInventoryItem.StockStatus.PENDING_REISSUE,
            updated_at=now,
        )
    if allocations:
        OrderPlanReturnInventoryAllocation.objects.filter(pk__in=[row.pk for row in allocations]).update(
            status=OrderPlanReturnInventoryAllocation.Status.RELEASED,
            updated_at=now,
        )
    return len(allocations)


def _shipment_has_physical_progress(plan):
    """A printed QR is a physical commitment, just like outbound/receipt.

    V2 allows a sales manager to cancel only while no part of the order has
    crossed that point.  A merely planned or authorized shipment is still
    cancellable because no package has been labelled or handed over yet.
    """

    return OrderPlanShipment.objects.filter(order_plan=plan).filter(
        Q(qrcode_printed_at__isnull=False)
        | Q(status__in=(OrderPlanShipment.Status.OUTBOUNDED, OrderPlanShipment.Status.SHIPPED, OrderPlanShipment.Status.RECEIVED))
    ).exists()


def _shipment_path_has_pending_cancellation(plan):
    """Return whether an offline-start cancellation has closed the ship path.

    When sales records that assembly has already started, production remains
    allowed only so the factual completed products can later become neutral
    finished-goods inventory.  No API, stale task, or manually crafted client
    request may revive a QR or shipment branch during that waiting period.
    """

    return OrderPlanCancellationAudit.objects.filter(
        order_plan=plan,
        status__in=(
            OrderPlanCancellationAudit.Status.WAITING_PRODUCTION_COMPLETION,
            OrderPlanCancellationAudit.Status.WAITING_FINISHED_STOCK,
        ),
    ).exists()


def _assert_shipment_path_open(plan):
    if _shipment_path_has_pending_cancellation(plan):
        raise OrderPlanMaterialFlowError(
            "订单已登记取消，等待生产完成并转入成品库存，不能生成或推进打码、发货。"
        )


def _cancel_nonphysical_shipments(*, plan, now):
    """Close V2 shipment tasks created before an offline cancellation fact.

    A first child plan can already have generated a *planned* shipment while
    another child remains in production.  It has no physical progress, so it
    must not leak into the normal QR/outbound path while the cancellation is
    waiting for production completion.
    """

    if _shipment_has_physical_progress(plan):
        raise OrderPlanMaterialFlowError("订单已有批次打码或发货，不能取消。")
    OrderPlanShipment.objects.filter(
        order_plan=plan,
        status__in=(OrderPlanShipment.Status.PLANNED, OrderPlanShipment.Status.AUTHORIZED),
    ).update(status=OrderPlanShipment.Status.CANCELLED, updated_at=now)
    shipment_workitems = WorkItem.objects.select_for_update().filter(
        biz_ref=f"sales.OrderPlan:{plan.pk}",
        status="OPEN",
        type__in=(
            "ORDER_PLAN_V2_SHIPMENT_AUTHORIZATION",
            "ORDER_PLAN_V2_QRCODE_PRINT",
            "ORDER_PLAN_V2_SHIPMENT_LOGISTICS_CONFIRM",
            "ORDER_PLAN_V2_STOCK_OUTBOUND",
            "ORDER_PLAN_V2_MERCHANT_RECEIPT",
        ),
    )
    workitem_ids = list(shipment_workitems.values_list("pk", flat=True))
    if not workitem_ids:
        return
    WorkItem.objects.filter(pk__in=workitem_ids).update(
        status="CANCELLED",
        done_at=now,
        last_activity_at=now,
        updated_at=now,
    )
    UserTask.objects.filter(
        workitem_id__in=workitem_ids,
        state__in=("NEW", "READ"),
    ).update(
        state="DONE",
        need_action=False,
        done_at=now,
        done_reason="OTHER_ACTION",
        cursor_time=now,
        updated_at=now,
    )


def _queue_cancelled_finished_stock(*, plan, material_plan, audit, actor_openid):
    """Expose the post-production cancellation fact without blocking sales.

    The business rule gives the sales manager the final cancellation action
    once production is complete.  This notice keeps the finished-goods keeper
    informed, but is deliberately not another required workflow gate.
    """

    recipients = _role_openids(plan.factory_organization, {"factory_sales", "factory_stock"})
    if audit.status != OrderPlanCancellationAudit.Status.WAITING_FINISHED_STOCK:
        audit.status = OrderPlanCancellationAudit.Status.WAITING_FINISHED_STOCK
        audit.save(update_fields=("status", "updated_at"))
    _new_workitem(
        plan=plan,
        work_type="ORDER_PLAN_V2_CANCELLED_FINISHED_STOCK_NOTICE",
        stage_key="cancellation-finished-stock-notice",
        title="取消订单生产已完成，待销售确认取消",
        summary="销售经理确认取消后，已完成成品将自动转为厂家可调用成品库存。",
        recipients=recipients,
        need_action=False,
        payload={
            "material_plan_id": material_plan.pk,
            "cancellation_status": audit.status,
        },
        actor_openid=actor_openid,
        done_immediately=True,
    )


def _finalize_material_flow_cancellation(
    *,
    plan,
    material_plan,
    user,
    assembly_started_offline,
    reason,
    audit=None,
    finished_goods_created=0,
):
    """Finish a cancellation once no unrecorded production remains.

    This is shared by the simple pre-release path and the post-production
    finished-stock confirmation path.  It deliberately preserves consumed
    material allocations and completed production rows as immutable history.
    """

    if _shipment_has_physical_progress(plan):
        raise OrderPlanMaterialFlowError(
            "订单已有批次打码或发货，不能取消；如需减少数量，请取消后重新创建未发货订单。"
        )
    released_milli, now = _release_material_allocations(material_plan)
    released_finished = _release_return_inventory(plan)
    _release_accessory_allocations(plan)
    _cancel_nonphysical_shipments(plan=plan, now=now)
    material_plan.status = OrderPlanMaterialPlan.Status.CANCELLED
    material_plan.save(update_fields=("status", "updated_at"))
    material_plan.production_subplans.exclude(
        status=OrderPlanProductionSubplan.Status.PRODUCTION_COMPLETED
    ).update(
        status=OrderPlanProductionSubplan.Status.CANCELLED,
        updated_at=now,
    )
    # Only instructions that have not become a supplier delivery commitment
    # are cancelled.  A supplier-confirmed / receiving / satisfied delivery
    # represents real offline material movement; any arrived quantity remains
    # ordinary material inventory instead of being rewritten as cancelled
    # order history.
    material_plan.supply_deliveries.filter(
        status__in=(
            OrderPlanSupplyDelivery.Status.QUEUED,
            OrderPlanSupplyDelivery.Status.DISPATCHED,
        )
    ).update(
        status=OrderPlanSupplyDelivery.Status.CANCELLED,
        cancelled_at=now,
        cancelled_by=user,
        updated_at=now,
    )
    WorkItem.objects.select_for_update().filter(
        biz_ref=f"sales.OrderPlan:{plan.pk}", status="OPEN"
    ).update(status="CANCELLED", done_at=now, last_activity_at=now, updated_at=now)
    UserTask.objects.filter(
        workitem__biz_ref=f"sales.OrderPlan:{plan.pk}", state__in=("NEW", "READ")
    ).update(
        state="DONE",
        need_action=False,
        done_at=now,
        done_reason="OTHER_ACTION",
        cursor_time=now,
        updated_at=now,
    )
    from sales.services.replacement_supply_service import release_normal
    release_normal(plan)
    plan.status = OrderPlan.Status.CANCELLED
    plan.save(update_fields=("status", "updated_at"))
    plan.items.update(status=OrderPlan.Status.CANCELLED, updated_at=now)
    if audit is None:
        audit, _created = OrderPlanCancellationAudit.objects.update_or_create(
            order_plan=plan,
            defaults={
                "requested_by": user,
                "status": OrderPlanCancellationAudit.Status.CANCELLED,
                "assembly_started_offline": bool(assembly_started_offline),
                "reason": str(reason or "")[:2000],
                "released_reservations_at": now,
                "resolved_at": now,
            },
        )
    else:
        audit.status = OrderPlanCancellationAudit.Status.CANCELLED
        audit.requested_by = audit.requested_by or user
        audit.assembly_started_offline = bool(assembly_started_offline)
        if reason:
            audit.reason = str(reason)[:2000]
        audit.released_reservations_at = now
        audit.resolved_at = now
        audit.save(
            update_fields=(
                "status",
                "requested_by",
                "assembly_started_offline",
                "reason",
                "released_reservations_at",
                "resolved_at",
                "updated_at",
            )
        )
    # Financial eligibility depends on both the order and cancellation audit.
    # Reconcile only after those facts are final, so an earlier eligible order
    # can resume the pair's payment task without reviving this cancelled card.
    _cancel_material_flow_finance_tasks(plan=plan, user=user)
    return {
        "order_plan_id": plan.pk,
        "plan_code": plan.plan_code,
        "cancellation_status": audit.status,
        "released_material_milli": released_milli,
        "released_finished_inventory_count": released_finished,
        "finished_goods_created": finished_goods_created,
    }


def _cancel_material_flow_finance_tasks(*, plan, user):
    try:
        cancel_order_finance_tasks(plan, user=user)
    except OrderFinanceError as exc:
        raise OrderPlanMaterialFlowError(str(exc)) from exc


@financial_write_errors(OrderPlanMaterialFlowError)
@transaction.atomic
def cancel_material_flow_order_plan(*, user, plan_id, assembly_started_offline=None, reason=""):
    identity = _sales_task_identity(user)
    plan = _v2_plan_for_factory(identity, plan_id)
    if plan.created_by_id not in _sales_actor_user_ids(identity):
        raise OrderPlanMaterialFlowError("只有创建该订单的厂家销售经理可以取消。")
    # Match statement/payment lock ordering: reserve the directed account pair
    # before any order row, including on SQLite where row locks are not enough.
    lock_receivable_account(plan)
    plan = _v2_plan_for_factory(identity, plan_id, lock=True)
    if plan.created_by_id not in _sales_actor_user_ids(identity):
        raise OrderPlanMaterialFlowError("只有创建该订单的厂家销售经理可以取消。")
    material_plan = OrderPlanMaterialPlan.objects.select_for_update().filter(order_plan=plan).first()
    if not material_plan:
        raise OrderPlanMaterialFlowError("订单物料计划不存在。")
    shipment_has_physical_progress = _shipment_has_physical_progress(plan)
    if plan.status in {OrderPlan.Status.SHIPPED, OrderPlan.Status.RECEIVED} or shipment_has_physical_progress:
        OrderPlanCancellationAudit.objects.update_or_create(
            order_plan=plan,
            defaults={
                "requested_by": user,
                "status": OrderPlanCancellationAudit.Status.REJECTED_AFTER_SHIPMENT,
                "assembly_started_offline": bool(assembly_started_offline),
                "reason": str(reason or "")[:2000],
            },
        )
        raise OrderPlanMaterialFlowError("订单已有批次打码或发货，不能取消；如需减少数量，请取消后重新创建未发货订单。")
    if plan.status == OrderPlan.Status.CANCELLED:
        _cancel_material_flow_finance_tasks(plan=plan, user=user)
        return get_material_flow_detail(user=user, plan_id=plan.pk)
    audit = OrderPlanCancellationAudit.objects.select_for_update().filter(order_plan=plan).first()
    if audit and audit.status == OrderPlanCancellationAudit.Status.WAITING_PRODUCTION_COMPLETION:
        _cancel_material_flow_finance_tasks(plan=plan, user=user)
        return {
            "order_plan_id": plan.pk,
            "plan_code": plan.plan_code,
            "cancellation_status": audit.status,
            "message": "该订单已登记为已开始装配，请等待生产经理确认全部子计划完成。",
        }
    if audit and audit.status == OrderPlanCancellationAudit.Status.WAITING_FINISHED_STOCK:
        if material_plan.status != OrderPlanMaterialPlan.Status.PRODUCTION_COMPLETED:
            raise OrderPlanMaterialFlowError("订单生产尚未全部确认完成，不能完成取消。")
        finished_goods_created = _record_cancelled_finished_goods(
            plan=plan,
            material_plan=material_plan,
            actor=user,
        )
        return _finalize_material_flow_cancellation(
            plan=plan,
            material_plan=material_plan,
            user=user,
            assembly_started_offline=True,
            reason=audit.reason or reason,
            audit=audit,
            finished_goods_created=finished_goods_created,
        )

    released_for_production = bool(material_plan.material_ready_at)
    production_completed = (
        material_plan.status == OrderPlanMaterialPlan.Status.PRODUCTION_COMPLETED
        and material_plan.production_subplans.exists()
    )
    if not released_for_production and not production_completed:
        return _finalize_material_flow_cancellation(
            plan=plan,
            material_plan=material_plan,
            user=user,
            assembly_started_offline=False,
            reason=reason,
            audit=audit,
        )

    # Once the material keeper has released production, the sales manager must
    # explicitly record the offline assembly fact.  `None` is deliberately
    # different from False: older clients cannot silently claim that assembly
    # never began.
    if assembly_started_offline is None:
        raise OrderPlanMaterialFlowError(
            "物料库管已确认齐套并推送生产，请先线下核实是否已开始装配后再提交取消。"
        )

    if not assembly_started_offline:
        if production_completed or material_plan.production_subplans.filter(
            status=OrderPlanProductionSubplan.Status.PRODUCTION_COMPLETED
        ).exists():
            raise OrderPlanMaterialFlowError(
                "已有生产完成事实，必须按“已开始装配”登记；成品入库后才能完成取消。"
            )
        return _finalize_material_flow_cancellation(
            plan=plan,
            material_plan=material_plan,
            user=user,
            assembly_started_offline=False,
            reason=reason,
            audit=audit,
        )

    audit, _created = OrderPlanCancellationAudit.objects.update_or_create(
        order_plan=plan,
        defaults={
            "requested_by": user,
            "status": OrderPlanCancellationAudit.Status.WAITING_PRODUCTION_COMPLETION,
            "assembly_started_offline": True,
            "reason": str(reason or "")[:2000],
        },
    )
    now = timezone.now()
    _cancel_nonphysical_shipments(plan=plan, now=now)
    _release_accessory_allocations(plan)
    _cancel_material_flow_finance_tasks(plan=plan, user=user)
    if production_completed:
        finished_goods_created = _record_cancelled_finished_goods(
            plan=plan,
            material_plan=material_plan,
            actor=user,
        )
        return _finalize_material_flow_cancellation(
            plan=plan,
            material_plan=material_plan,
            user=user,
            assembly_started_offline=True,
            reason=reason,
            audit=audit,
            finished_goods_created=finished_goods_created,
        )
    return {
        "order_plan_id": plan.pk,
        "plan_code": plan.plan_code,
        "cancellation_status": audit.status,
        "message": "已记录线下开始装配；请等待生产经理确认完成后，再由销售经理完成取消并转入成品库存。",
    }


def _record_cancelled_finished_goods(*, plan, material_plan, actor):
    """Create neutral reusable inventory for the completed cancelled plan.

    The sales manager is the business actor who finalizes cancellation after
    production; the stored record nevertheless has an immutable product,
    BOM, production-batch and original-plan trace.  It never retains the
    cancelled order's merchant or sales-manager responsibility as inventory
    ownership.
    """

    sub_items = list(
        OrderPlanProductionSubplanItem.objects.select_for_update()
        .select_related(
            "production_subplan__material_plan__order_plan",
            "order_plan_item__product_model",
        )
        .filter(
            production_subplan__material_plan=material_plan,
            production_subplan__status=OrderPlanProductionSubplan.Status.PRODUCTION_COMPLETED,
        )
        .order_by("production_subplan__sequence_no", "id")
    )
    if not sub_items:
        raise OrderPlanMaterialFlowError("订单没有已完成的生产子计划，不能转入成品库存。")
    now = timezone.now()
    created_count = 0
    for sub_item in sub_items:
        selection = OrderPlanSalesBOMSelection.objects.select_related("frozen_bom").filter(
            order_plan_item_id=sub_item.order_plan_item_id
        ).first()
        if not selection:
            raise OrderPlanMaterialFlowError("生产子计划缺少销售冻结BOM，不能转入成品库存。")
        devices = _production_devices_for_subplan_item(sub_item=sub_item, actor=actor)
        if sub_item.production_batch_id is None:
            raise OrderPlanMaterialFlowError("生产子计划没有实际生产批次，不能转入成品库存。")
        for device in devices:
            inventory_item, created = FactoryFinishedGoodsInventoryItem.objects.select_for_update().get_or_create(
                device=device,
                defaults={
                    "factory_organization": plan.factory_organization,
                    "source_order_plan": plan,
                    "source_subplan_item": sub_item,
                    "source_product_model": sub_item.order_plan_item.product_model,
                    "source_production_batch_id": sub_item.production_batch_id,
                    "source_bom": selection.frozen_bom,
                    "stock_status": FactoryFinishedGoodsInventoryItem.StockStatus.PENDING_REISSUE,
                    "stored_by": actor,
                    "stored_at": now,
                },
            )
            if not created:
                expected = (
                    plan.factory_organization_id,
                    plan.pk,
                    sub_item.pk,
                    sub_item.order_plan_item.product_model_id,
                )
                actual = (
                    inventory_item.factory_organization_id,
                    inventory_item.source_order_plan_id,
                    inventory_item.source_subplan_item_id,
                    inventory_item.source_product_model_id,
                )
                if actual != expected:
                    raise OrderPlanMaterialFlowError("成品设备已关联到其他库存记录，不能完成取消。")
            else:
                created_count += 1
    return created_count


def _viewer_identity(user):
    identity = get_active_identity(user) if user and user.is_active else None
    if not identity:
        raise OrderPlanMaterialFlowError("账号没有有效岗位，请重新登录。")
    return identity


def _supplier_sales_material_scope_for_plan(identity, plan):
    """Resolve supplier-sales detail scope without changing task snapshots.

    An open V2 supplier-delivery task is owned by the sales recipients that
    existed when the dispatcher published it.  Live responsibility changes
    are intentionally ignored until that task closes: otherwise a newly
    assigned sales manager could replace the historical recipient.  A former
    recipient also retains its completed task's material scope afterwards so
    the successful confirmation can return the audited V2 detail instead of
    failing permission checks mid-request.
    """

    if (
        identity.organization.organization_type.code != "SUPPLIER"
        or identity.role.code != "supplier_sales"
    ):
        return None
    snapshot_access = get_v2_supplier_delivery_snapshot_access(
        supplier=identity.organization,
        sales_identity=identity,
        plan_id=plan.pk,
    )
    open_plan_ids = snapshot_access["open_plan_ids"]
    if plan.pk in open_plan_ids:
        return {
            "source": "open_task_snapshot",
            "material_ids": set(
                snapshot_access["open_material_ids_by_plan"].get(plan.pk, set())
            ),
        }

    current_material_ids = get_responsible_material_ids(
        sales_identity=identity
    )
    completed_snapshot_material_ids = set(
        snapshot_access["history_material_ids_by_plan"].get(plan.pk, set())
    )
    if current_material_ids or completed_snapshot_material_ids:
        return {
            # Once no V2 delivery action is open, a current responsibility
            # and a completed historical task can coexist.  Keep both
            # read-only scopes available so an original confirmer can receive
            # the successful response even if their live assignment moved to
            # a different material in the meantime.
            "source": "current_and_completed_task_snapshot",
            "material_ids": set(current_material_ids)
            | completed_snapshot_material_ids,
        }
    return {"source": "current_responsibility", "material_ids": set()}


def _can_view_plan(identity, plan, *, supplier_sales_scope=None):
    roles = effective_role_codes(identity)
    org_id = identity.organization_id
    if identity.organization.organization_type.code == "OWNER" and org_id == plan.factory_organization_id:
        if not roles & FACTORY_PLAN_VIEW_ROLES:
            return False
        if "factory_sales" in roles and not (roles & (FACTORY_PLAN_VIEW_ROLES - {"factory_sales"})):
            sales_user_ids = (
                delegated_role_user_ids(identity, "factory_sales")
                if identity.role.code in {"factory_senior_manager", "merchant_senior_manager"}
                else {identity.user_id}
            )
            return plan.created_by_id in sales_user_ids
        return True
    if identity.organization.organization_type.code == "SUPPLIER" and roles & {"supplier_owner", "supplier_sales"}:
        deliveries = OrderPlanSupplyDelivery.objects.filter(
            material_plan__order_plan=plan,
            supplier_id=org_id,
        )
        if "supplier_sales" in roles:
            # A sales manager normally uses the current responsibility map.
            # Once a V2 delivery task is open, however, only its immutable
            # task snapshot can grant access.  That keeps the original task
            # reachable after reassignment and prevents a new manager from
            # taking it over merely because they now own the material.
            supplier_sales_scope = (
                supplier_sales_scope
                if supplier_sales_scope is not None
                else _supplier_sales_material_scope_for_plan(identity, plan)
            )
            material_ids = supplier_sales_scope["material_ids"]
            if not material_ids:
                return False
            deliveries = deliveries.filter(
                lines__material_id__in=material_ids,
            )
        return deliveries.exists()
    if identity.organization.organization_type.code == "MERCHANT" and org_id == plan.merchant_organization_id:
        return bool(roles & {"merchant_owner", "merchant_sales", "merchant_stock"})
    return False


def _serialize_plan_detail(plan, material_plan):
    return {
        "id": plan.pk,
        "plan_code": plan.plan_code,
        "status": plan.status,
        "status_name": material_flow_status_name(plan, material_plan),
        # The material-plan record provides the operational status chip.
        "material_status": material_plan.status,
        "material_status_name": material_plan.get_status_display(),
        "flow_version": plan.flow_version,
        "factory": {"id": plan.factory_organization_id, "name": plan.factory_organization.name},
        "merchant": {"id": plan.merchant_organization_id, "name": plan.merchant_organization.name},
        "brand": {"id": plan.brand_id, "name": plan.brand.name},
        "region": plan.region_snapshot,
        "total_quantity": plan.total_quantity,
        "accessory_items": _serialize_order_accessory_items(plan),
        "remark": plan.remark,
        "submitted_at": plan.submitted_at.isoformat(),
        "sales_manager": {"id": plan.created_by_id, "name": _display_name(plan.created_by)},
    }


def _viewer_timeline_active_codes(*, plan, identity, available_actions):
    """Return timeline nodes that the current viewer may legitimately see lit.

    The order's physical state is global, while task ownership is not.  Do
    not turn a global ``CURRENT`` state into a viewer action here: that was
    the source of every role seeing the same green node.  Instead, combine
    server-authorized actions with the current user's V2 work items for this
    exact order only.
    """

    active_codes = set()
    for action in available_actions or ():
        if not isinstance(action, dict):
            continue
        node = _TIMELINE_NODE_BY_ACTION_CODE.get(action.get("code"))
        if node:
            active_codes.add(node)

    openid = _actor_openid(identity.user)
    if not openid:
        return active_codes

    order_ref = f"sales.OrderPlan:{plan.pk}"
    open_action_types = tuple(_TIMELINE_NODE_BY_OPEN_ACTION_TASK)
    for task_type in (
        UserTask.objects.filter(
            to_openid=openid,
            need_action=True,
            workitem__biz_ref=order_ref,
            workitem__status="OPEN",
            workitem__type__in=open_action_types,
        )
        .exclude(state="DONE")
        .values_list("workitem__type", flat=True)
        .distinct()
    ):
        active_codes.add(_TIMELINE_NODE_BY_OPEN_ACTION_TASK[task_type])

    awareness_types = tuple(_TIMELINE_NODE_BY_AWARENESS_TASK)
    for task_type in (
        UserTask.objects.filter(
            to_openid=openid,
            workitem__biz_ref=order_ref,
            workitem__type__in=awareness_types,
        )
        .values_list("workitem__type", flat=True)
        .distinct()
    ):
        active_codes.add(_TIMELINE_NODE_BY_AWARENESS_TASK[task_type])
    return active_codes


def _timeline(plan, material_plan, *, viewer_active_codes=()):
    status = material_plan.status
    shipment_rows = OrderPlanShipment.objects.filter(order_plan=plan)
    delivery_statuses = list(
        material_plan.supply_deliveries.values_list("status", flat=True)
    )
    # The supplier step is not complete merely because a delivery row has
    # been created or pushed.  It completes only when every required supplier
    # has confirmed the offline delivery (or it is already in factory receipt
    # / fully satisfied).  When the order needed no delivery at all, normal
    # stock reservation is the evidence of completion instead.
    supply_completed = (
        _all_materials_reserved(material_plan)
        if not delivery_statuses
        else all(
            delivery_status
            in {
                OrderPlanSupplyDelivery.Status.SUPPLIER_CONFIRMED,
                OrderPlanSupplyDelivery.Status.RECEIVING,
                OrderPlanSupplyDelivery.Status.SATISFIED,
            }
            for delivery_status in delivery_statuses
        )
    )
    printed_device_count = OrderPlanShipmentDevice.objects.filter(
        shipment__order_plan=plan,
        qrcode_printed_at__isnull=False,
    ).count()
    shipment_qrcode_done = bool(plan.total_quantity) and printed_device_count >= plan.total_quantity
    shipment_done = _all_source_quantities_at_status(
        plan=plan,
        statuses={OrderPlanShipment.Status.SHIPPED, OrderPlanShipment.Status.RECEIVED},
    )
    merchant_received = _all_source_quantities_at_status(
        plan=plan,
        statuses={OrderPlanShipment.Status.RECEIVED},
    )
    no_new_production = not material_plan.production_subplans.exists()
    completed = {
        "CREATED": True,
        "MATERIAL_REVIEW": bool(material_plan.warehouse_reviewed_at),
        "SUPPLY": supply_completed,
        "RECEIPT": _all_materials_reserved(material_plan) or no_new_production,
        "MATERIAL_READY": bool(material_plan.material_ready_at) or no_new_production,
        "PRODUCTION": status == OrderPlanMaterialPlan.Status.PRODUCTION_COMPLETED,
        "QRCODE": shipment_qrcode_done,
        "SHIPMENT": shipment_done,
        "MERCHANT_RECEIPT": merchant_received,
        "ACCOUNTING": bool(plan.accounted_at),
    }
    definitions = [
        ("CREATED", "销售创建订单计划", "销售已选择并冻结BOM版本"),
        ("MATERIAL_REVIEW", "物料库管核对", "核对现库存预占与配送缺口"),
        ("SUPPLY", "供应商配送", "定时推送并线下配送；物料库独立入库"),
        ("RECEIPT", "物料入库与自动预占", "按实际入库批次自动补足订单预占"),
        ("MATERIAL_READY", "物料全部到位", "物料库管确认后推送生产"),
        ("PRODUCTION", "型号生产", "按型号生产计划推进；仅消解尾货时按子计划完成"),
        ("QRCODE", "生产打码", "按型号生产计划生成、打印二维码"),
        ("SHIPMENT", "分批发货", "物流经理确认出库，成品库管确认发货"),
        ("MERCHANT_RECEIPT", "商家入库", "商家确认到货入库"),
        ("ACCOUNTING", "销售助理入账", "全部商家批次入库后由销售助理确认入账"),
    ]
    current_map = {
        OrderPlanMaterialPlan.Status.WAITING_WAREHOUSE_REVIEW: "MATERIAL_REVIEW",
        OrderPlanMaterialPlan.Status.WAITING_SUPPLIER_DISPATCH: "SUPPLY",
        OrderPlanMaterialPlan.Status.WAITING_RECEIPT: "RECEIPT",
        OrderPlanMaterialPlan.Status.READY_FOR_PRODUCTION: "PRODUCTION",
        OrderPlanMaterialPlan.Status.PRODUCTION_IN_PROGRESS: "PRODUCTION",
        OrderPlanMaterialPlan.Status.PRODUCTION_COMPLETED: "QRCODE",
    }
    current = current_map.get(status, "")
    # A reviewed plan stays in its delivery/receipt database state until the
    # material keeper explicitly releases production.  The live timeline can
    # nevertheless show the actual next decision: supplier confirmation plus
    # full reservation means the material-ready action is now the current
    # node; confirmed delivery with an uncovered requirement is still receipt.
    if status in {
        OrderPlanMaterialPlan.Status.WAITING_SUPPLIER_DISPATCH,
        OrderPlanMaterialPlan.Status.WAITING_RECEIPT,
    }:
        if _all_delivery_commitments_confirmed(material_plan) and _all_materials_reserved(material_plan):
            current = "MATERIAL_READY"
        elif supply_completed:
            current = "RECEIPT"
    if status == OrderPlanMaterialPlan.Status.PRODUCTION_COMPLETED:
        if not shipment_rows.exists() or not shipment_qrcode_done:
            current = "QRCODE"
        elif not shipment_done:
            current = "SHIPMENT"
        elif not merchant_received:
            current = "MERCHANT_RECEIPT"
        else:
            current = "" if plan.accounted_at else "ACCOUNTING"
    viewer_active_codes = set(viewer_active_codes or ())
    rows = []
    for code, title, description in definitions:
        state = "DONE" if completed[code] else ("CURRENT" if code == current else "WAITING")
        if plan.status == OrderPlan.Status.CANCELLED:
            state = "CANCELLED"
        # A completed node is already visually green; keeping an old notice
        # task from highlighting it again would make prior actors look like
        # current handlers.  Global ``current`` remains a diagnostic flow
        # state only and is never used as a viewer-specific active signal.
        active = (
            plan.status != OrderPlan.Status.CANCELLED
            and not completed[code]
            and code in viewer_active_codes
        )
        rows.append({
            "code": code,
            "title": title,
            "description": description,
            "state": state,
            "completed": completed[code],
            "current": code == current,
            "active": active,
        })
    return rows


def _serialize_finished_inventory(plan):
    result = []
    for item in plan.items.select_related("product_model").all():
        data = serialize_item_reissue(item)
        result.append(
            {
                "id": item.pk,
                "product_model_id": item.product_model_id,
                "product_model_name": item.product_model.name,
                "quantity": item.quantity,
                "used_quantity": item.return_inventory_quantity,
                "production_quantity": item.production_quantity,
                "source_summary": "成品库存优先调用" if item.return_inventory_quantity else "未调用成品库存",
                **data,
            }
        )
    return result


def _serialize_bom_selections(plan, identity, *, supplier_sales_material_ids=None):
    result = []
    supplier_id = None
    responsible_material_ids = None
    if identity.organization.organization_type.code == "SUPPLIER":
        supplier_id = identity.organization_id
        if identity.role.code == "supplier_sales":
            responsible_material_ids = (
                set(supplier_sales_material_ids)
                if supplier_sales_material_ids is not None
                else get_responsible_material_ids(sales_identity=identity)
            )
    selections = OrderPlanSalesBOMSelection.objects.select_related(
        "order_plan_item__product_model", "source_bom", "frozen_bom"
    ).prefetch_related("frozen_bom__items__material", "frozen_bom__items__material_spec", "frozen_bom__items__supplier").filter(
        order_plan_item__plan=plan
    ).order_by("order_plan_item_id")
    for selection in selections:
        material_rows = list(selection.frozen_bom.items.all())
        if supplier_id is not None:
            material_rows = [
                item
                for item in material_rows
                if item.supplier_id == supplier_id
                and (
                    responsible_material_ids is None
                    or item.material_id in responsible_material_ids
                )
            ]
            # Do not expose a product/BOM selection that has no material line
            # for this supplier viewer.  In particular, a supplier sales
            # manager must not infer another manager's product scope merely
            # from an otherwise empty V2 BOM card.
            if not material_rows:
                continue
        result.append(
            {
                "id": selection.pk,
                "order_plan_item_id": selection.order_plan_item_id,
                "product_model_id": selection.order_plan_item.product_model_id,
                "product_model_name": selection.order_plan_item.product_model.name,
                "bom_id": selection.frozen_bom_id,
                "bom_version": selection.frozen_bom.version,
                "source_bom_id": selection.source_bom_id,
                "source_name": "本订单销售经理创建的新版本" if selection.has_changes else "销售经理选择并冻结现有版本",
                "has_changes": selection.has_changes,
                "materials": [
                    {
                        "bom_item_id": item.pk,
                        "material_id": item.material_id,
                        "material_name": item.material.name,
                        "material_spec_id": item.material_spec_id,
                        "material_spec_name": item.material_spec.name if item.material_spec_id else "",
                        "supplier_id": item.supplier_id,
                        "supplier_name": item.supplier.name if item.supplier_id else "",
                        "quantity_per_device": str(item.quantity),
                        "remark": item.remark or "",
                    }
                    for item in material_rows
                ],
            }
        )
    return result


def _serialize_requirements(
    material_plan,
    identity,
    *,
    supplier_sales_material_ids=None,
):
    query = OrderPlanMaterialRequirement.objects.select_related(
        "material", "material_spec", "supplier",
        "production_subplan_item__production_subplan",
    ).prefetch_related("lot_allocations__material_lot").filter(
        production_subplan_item__production_subplan__material_plan=material_plan,
    )
    if identity.organization.organization_type.code == "SUPPLIER":
        query = query.filter(supplier_id=identity.organization_id)
        if identity.role.code == "supplier_sales":
            allowed = (
                set(supplier_sales_material_ids)
                if supplier_sales_material_ids is not None
                else get_responsible_material_ids(sales_identity=identity)
            )
            query = query.filter(material_id__in=allowed)
    result = []
    for row in query.order_by("supplier_id", "material_spec_id", "id"):
        result.append(
            {
                "id": row.pk,
                "subplan_id": row.production_subplan_item.production_subplan_id,
                "subplan_sequence": row.production_subplan_item.production_subplan.sequence_no,
                "material_id": row.material_id,
                "material_name": row.material.name,
                "material_spec_id": row.material_spec_id,
                "material_spec_name": row.material_spec.name,
                "supplier_id": row.supplier_id,
                "supplier_name": row.supplier.name,
                "unit": row.unit_snapshot,
                "quantity_per_device": _quantity_text(row.quantity_milli_per_device),
                "required": _quantity_text(row.required_milli),
                "reserved": _quantity_text(row.reserved_milli),
                "consumed": _quantity_text(row.consumed_milli),
                "shortfall": _quantity_text(row.required_milli - row.reserved_milli),
                "state": "READY" if row.reserved_milli >= row.required_milli else "WAITING",
                "state_name": "已预占" if row.reserved_milli >= row.required_milli else "待到料",
                "allocations": [
                    {
                        "id": allocation.pk,
                        "lot_code": allocation.material_lot.batch_code,
                        "quantity": _quantity_text(allocation.reserved_milli),
                        "consumed": _quantity_text(allocation.consumed_milli),
                        "state": allocation.status,
                        "state_name": allocation.get_status_display(),
                    }
                    for allocation in row.lot_allocations.all()
                ],
            }
        )
    return result


def _serialize_deliveries(
    material_plan,
    identity,
    *,
    supplier_sales_material_ids=None,
):
    deliveries = OrderPlanSupplyDelivery.objects.select_related("supplier").prefetch_related(
        "lines__material", "lines__material_spec"
    ).filter(material_plan=material_plan)
    responsible_material_ids = None
    if identity.organization.organization_type.code == "SUPPLIER":
        deliveries = deliveries.filter(supplier_id=identity.organization_id)
        if identity.role.code == "supplier_sales":
            responsible_material_ids = (
                set(supplier_sales_material_ids)
                if supplier_sales_material_ids is not None
                else get_responsible_material_ids(sales_identity=identity)
            )
    result = []
    for delivery in deliveries.order_by("scheduled_for", "id"):
        lines = list(delivery.lines.all())
        if responsible_material_ids is not None:
            lines = [
                line for line in lines
                if line.material_id in responsible_material_ids
            ]
            # ``_can_view_plan`` only permits a sales manager with at least
            # one matching line.  Retain that rule here so no empty supplier
            # delivery card leaks the existence of unrelated lines.
            if not lines:
                continue
        result.append(
            {
                "id": delivery.pk,
                "supplier_id": delivery.supplier_id,
                "supplier_name": delivery.supplier.name,
                "status": delivery.status,
                "status_name": delivery.get_status_display(),
                "scheduled_for": delivery.scheduled_for.isoformat(),
                "dispatched_at": delivery.dispatched_at.isoformat() if delivery.dispatched_at else None,
                "supplier_confirmed_at": delivery.supplier_confirmed_at.isoformat() if delivery.supplier_confirmed_at else None,
                "confirmed_by_name": _display_name(delivery.supplier_confirmed_by) if delivery.supplier_confirmed_by_id else "",
                "lines": [
                    {
                        "id": line.pk,
                        "material_id": line.material_id,
                        "material_name": line.material.name,
                        "material_spec_id": line.material_spec_id,
                        "material_spec_name": line.material_spec.name,
                        "requested": _quantity_text(line.requested_milli),
                        "confirmed": _quantity_text(line.confirmed_milli),
                        "received": _quantity_text(line.received_milli),
                    }
                    for line in lines
                ],
            }
        )
    return result


def _has_open_shipment_task(*, plan, shipment, identity, work_type, stage_key):
    openid = _actor_openid(identity.user)
    if not openid:
        return False
    return UserTask.objects.filter(
        workitem__type=work_type,
        workitem__status="OPEN",
        workitem__uniq_key=f"sales:order-plan:{plan.pk}:shipment:{shipment.pk}:{stage_key}",
        to_openid=openid,
        need_action=True,
    ).exclude(state="DONE").exists()


def _shipment_actions(plan, shipment, identity):
    """Return only the current identity's real V2 shipment task buttons."""

    if _shipment_path_has_pending_cancellation(plan):
        return []

    roles = effective_role_codes(identity)
    sales_actor_ids = (
        delegated_role_user_ids(identity, "factory_sales")
        if identity.role.code in {"factory_senior_manager", "merchant_senior_manager"}
        else {identity.user_id} if "factory_sales" in roles else set()
    )
    actions = []
    if (
        "factory_sales" in roles
        and identity.organization_id == plan.factory_organization_id
        and plan.created_by_id in sales_actor_ids
        and shipment.status == OrderPlanShipment.Status.PLANNED
        and _has_open_shipment_task(
            plan=plan,
            shipment=shipment,
            identity=identity,
            work_type="ORDER_PLAN_V2_SHIPMENT_AUTHORIZATION",
            stage_key="authorization",
        )
    ):
        # Splitting is an optional sales decision made *before* authorization.
        # The action carries the exact shipment identity, so a page holding
        # several planned residual batches cannot accidentally split the first
        # one returned by a broad query.
        if _shipment_quantity(shipment) > 1:
            actions.append(
                {
                    "code": "shipment-create",
                    "label": "拆分本批发货",
                    "payload": {
                        "shipment_id": shipment.pk,
                        "production_subplan_id": shipment.production_subplan_id,
                    },
                    "confirm_title": "拆分本批发货",
                    "confirm_text": "可按实际紧急需求拆成首批和剩余批次；未授权批次不会改变订单总数量。",
                }
            )
        actions.append(
            {
                "code": "shipment-authorize",
                "label": "授权本批发货",
                "payload": {"shipment_id": shipment.pk},
                "confirm_title": "授权本批发货",
                "confirm_text": "授权后由成品库管打印贴包，物流经理确认出库，再由成品库管确认实际发货。",
            }
        )
    if "factory_stock" in roles and identity.organization_id == plan.factory_organization_id:
        if (
            shipment.status == OrderPlanShipment.Status.AUTHORIZED
            and not shipment.qrcode_printed_at
            and _has_open_shipment_task(
                plan=plan,
                shipment=shipment,
                identity=identity,
                work_type="ORDER_PLAN_V2_QRCODE_PRINT",
                stage_key="qrcode-print",
            )
        ):
            actions.append(
                {
                    "code": "shipment-qrcode-print",
                    "label": "确认二维码已打印贴包",
                    "payload": {"shipment_id": shipment.pk},
                    "confirm_title": "确认二维码打印贴包",
                    "confirm_text": "请确认本发货批次二维码已生成、已打印并已线下贴到包装。",
                }
            )
        if (
            shipment.status == OrderPlanShipment.Status.OUTBOUNDED
            and shipment.qrcode_printed_at
            and shipment.logistics_confirmed_at
            and _has_open_shipment_task(
                plan=plan,
                shipment=shipment,
                identity=identity,
                work_type="ORDER_PLAN_V2_STOCK_OUTBOUND",
                stage_key="stock-outbound",
            )
        ):
            actions.append(
                {
                    "code": "shipment-outbound",
                    "label": "确认本批发货",
                    "payload": {"shipment_id": shipment.pk},
                    "confirm_title": "确认本批发货",
                    "confirm_text": "请确认本批设备已实际发往直接商家；发货后将通知商家收货，实际到货入库后才转移库存。",
                }
            )
    if (
        "factory_logistics" in roles
        and identity.organization_id == plan.factory_organization_id
        and shipment.status == OrderPlanShipment.Status.AUTHORIZED
        and shipment.qrcode_printed_at
        and not shipment.logistics_confirmed_at
        and _has_open_shipment_task(
            plan=plan,
            shipment=shipment,
            identity=identity,
            work_type="ORDER_PLAN_V2_SHIPMENT_LOGISTICS_CONFIRM",
            stage_key="logistics-confirm",
        )
    ):
        actions.append(
            {
                "code": "shipment-logistics-confirm",
                "label": "确认本批线下出库",
                "payload": {"shipment_id": shipment.pk},
                "input_fields": [
                    {
                        "name": "logistics_company",
                        "label": "物流公司",
                        "placeholder": "请输入物流公司",
                        "required": True,
                        "max_length": 128,
                    },
                    {
                        "name": "tracking_no",
                        "label": "物流单号",
                        "placeholder": "请输入物流单号",
                        "required": True,
                        "max_length": 128,
                    },
                ],
                "confirm_title": "确认线下出库",
                "confirm_text": "物流信息及可选凭证将分别保存在选中批次，确认已线下出库后由成品库管确认发货。",
            }
        )
    merchant_receipt_manager = bool(
        identity.role.code in {"merchant_senior_manager", "factory_senior_manager"}
        and identity_can_perform_shared_operation(
            identity, "goods_transfer_receipt"
        )
    )
    if (
        identity.organization.organization_type.code == "MERCHANT"
        and identity.organization_id == plan.merchant_organization_id
        and identity_can_perform_shared_operation(
            identity, "goods_transfer_receipt"
        )
        and shipment.status == OrderPlanShipment.Status.SHIPPED
        and (
            merchant_receipt_manager
            or _has_open_shipment_task(
                plan=plan,
                shipment=shipment,
                identity=identity,
                work_type="ORDER_PLAN_V2_MERCHANT_RECEIPT",
                stage_key="merchant-receipt",
            )
        )
    ):
        actions.append(
            {
                "code": "shipment-receipt",
                "label": "确认本批到货入库",
                "payload": {"shipment_id": shipment.pk},
                "confirm_title": "确认本批到货入库",
                "confirm_text": "请确认本批设备已线下收货并入库。",
            }
        )
    return actions


def _qrcode_for_shipment_mapping(mapping):
    allocation = mapping.return_inventory_allocation
    if allocation and allocation.replacement_qr_id:
        return allocation.replacement_qr.code
    try:
        return mapping.device.qrcode.code
    except Exception:
        return ""


def _serialize_shipments(plan, identity):
    shipments = OrderPlanShipment.objects.select_related(
        "production_subplan",
        "shipment_authorized_by",
        "logistics_confirmed_by",
        "logistics_document",
        "shipped_by",
        "received_by",
    ).prefetch_related(
        "items__order_plan_item__product_model",
        "items__production_subplan_item",
        "device_allocations__device__qrcode",
        "device_allocations__return_inventory_allocation__replacement_qr",
        "accessory_items__accessory_item__category",
        "accessory_items__accessory_item__product_model",
        "accessory_items__accessory_item__material_spec__material",
    ).filter(order_plan=plan).order_by("sequence_no", "id")
    shipment_rows = []
    qrcode_batches = []
    for shipment in shipments:
        mappings = list(shipment.device_allocations.all())
        qrcode_rows = [
            {
                "id": mapping.pk,
                "device_sn": mapping.device.sn,
                "code": _qrcode_for_shipment_mapping(mapping),
                "status": "PRINTED" if mapping.qrcode_printed_at else "GENERATED",
                "status_name": "已打印贴包" if mapping.qrcode_printed_at else "已生成待打印",
            }
            for mapping in mappings
        ]
        label = _shipment_label(shipment)
        actions = _shipment_actions(plan, shipment, identity)
        accessory_rows = _shipment_accessory_rows(shipment)
        item_rows = [
            {
                "id": item.pk,
                "order_plan_item_id": item.order_plan_item_id,
                "production_subplan_item_id": item.production_subplan_item_id,
                "product_model_id": item.order_plan_item.product_model_id,
                "product_model_name": item.order_plan_item.product_model.name,
                "quantity": item.quantity,
                "status": shipment.status,
                "status_name": shipment.get_status_display(),
            }
            for item in shipment.items.all()
        ]
        shipment_rows.append(
            {
                "id": shipment.pk,
                "shipment_id": shipment.pk,
                "sequence_no": shipment.sequence_no,
                "title": label,
                "subplan_code": (
                    f"子计划{shipment.production_subplan.sequence_no}"
                    if shipment.production_subplan_id
                    else "成品库存调用"
                ),
                "production_subplan_id": shipment.production_subplan_id,
                "quantity": sum(row["quantity"] for row in item_rows),
                "status": shipment.status,
                "status_name": shipment.get_status_display(),
                "shipment_authorized_at": (
                    shipment.shipment_authorized_at.isoformat()
                    if shipment.shipment_authorized_at
                    else None
                ),
                "shipment_authorized_by_name": (
                    _display_name(shipment.shipment_authorized_by)
                    if shipment.shipment_authorized_by_id
                    else ""
                ),
                "qrcode_printed_at": (
                    shipment.qrcode_printed_at.isoformat() if shipment.qrcode_printed_at else None
                ),
                "logistics_company": shipment.logistics_company,
                "tracking_no": shipment.tracking_no,
                "logistics_document": (
                    _serialize_logistics_document(shipment.logistics_document)
                    if identity.organization.organization_type.code in {"OWNER", "MERCHANT"}
                    else None
                ),
                "outbound_at": shipment.logistics_confirmed_at.isoformat() if shipment.logistics_confirmed_at else None,
                # ``logistics_no`` remains an alias for the existing mobile
                # detail renderer while the canonical API name stays aligned
                # with the other logistics flows in the system.
                "logistics_no": shipment.tracking_no,
                "logistics_confirmed_at": (
                    shipment.logistics_confirmed_at.isoformat()
                    if shipment.logistics_confirmed_at
                    else None
                ),
                "logistics_confirmed_by_name": (
                    _display_name(shipment.logistics_confirmed_by)
                    if shipment.logistics_confirmed_by_id
                    else ""
                ),
                "shipped_at": shipment.shipped_at.isoformat() if shipment.shipped_at else None,
                "shipped_by_name": _display_name(shipment.shipped_by) if shipment.shipped_by_id else "",
                "received_at": shipment.received_at.isoformat() if shipment.received_at else None,
                "received_by_name": _display_name(shipment.received_by) if shipment.received_by_id else "",
                "items": item_rows,
                "accessory_items": accessory_rows,
                "accessory_summary": _shipment_accessory_summary(accessory_rows),
                "device_count": len(qrcode_rows),
                "available_actions": actions,
            }
        )
        # The print centre is a pending-work list. Completed package labels
        # must not be emitted as a printable QR list through a stale detail.
        if (
            shipment.qrcode_printed_at
            or shipment.status != OrderPlanShipment.Status.AUTHORIZED
            or not any(action["code"] == "shipment-qrcode-print" for action in actions)
        ):
            continue
        qrcode_batches.append(
            {
                "id": shipment.pk,
                "title": f"{label}二维码",
                "subplan_code": (
                    f"子计划{shipment.production_subplan.sequence_no}"
                    if shipment.production_subplan_id
                    else "成品库存调用"
                ),
                "quantity": sum(row["quantity"] for row in item_rows),
                "generated_at": (
                    min(
                        (
                            item.production_subplan_item.qrcode_generated_at
                            for item in shipment.items.all()
                            if item.production_subplan_item_id
                            and item.production_subplan_item.qrcode_generated_at
                        ),
                        default=None,
                    ).isoformat()
                    if any(
                        item.production_subplan_item_id
                        and item.production_subplan_item.qrcode_generated_at
                        for item in shipment.items.all()
                    )
                    else None
                ),
                "qrcode_printed_at": (
                    shipment.qrcode_printed_at.isoformat() if shipment.qrcode_printed_at else None
                ),
                "status": (
                    "PRINTED"
                    if shipment.qrcode_printed_at
                    else "AUTHORIZED"
                    if shipment.status == OrderPlanShipment.Status.AUTHORIZED
                    else shipment.status
                ),
                "status_name": (
                    "已打印贴包"
                    if shipment.qrcode_printed_at
                    else "待打印"
                    if shipment.status == OrderPlanShipment.Status.AUTHORIZED
                    else shipment.get_status_display()
                ),
                "codes": qrcode_rows,
            }
        )
    return shipment_rows, qrcode_batches


def _serialize_subplans(material_plan):
    """Serialize production execution records with the correct business terms.

    The historical database table is named ``OrderPlanProductionSubplan``.
    It also stores the normal one-record-per-model execution plan, so exposing
    every record as a "子计划" is misleading.  Only two records belonging to
    the same order item represent a real tail-stock split.
    """

    subplans = list(
        material_plan.production_subplans.prefetch_related(
            "items__order_plan_item__product_model"
        ).all()
    )
    item_counts = Counter(
        item.order_plan_item_id
        for subplan in subplans
        for item in subplan.items.all()
    )
    split_positions = defaultdict(int)
    result = []
    for subplan in subplans:
        items = list(subplan.items.all())
        split_item_ids = {
            item.order_plan_item_id
            for item in items
            if item_counts[item.order_plan_item_id] > 1
        }
        is_tail_split = len(items) == 1 and bool(split_item_ids)
        model_name = items[0].order_plan_item.product_model.name if len(items) == 1 else "多个产品型号"
        split_position = None
        if is_tail_split:
            order_item_id = items[0].order_plan_item_id
            split_positions[order_item_id] += 1
            split_position = split_positions[order_item_id]
            plan_label = "尾货子计划" if split_position == 1 else "主生产子计划"
            plan_title = f"{model_name} · {plan_label}{split_position}"
        elif len(items) == 1:
            plan_label = "型号生产计划"
            plan_title = f"{model_name}生产计划"
        else:
            # Compatibility for legacy records created before V2 separated
            # product models.  It is a production plan, never a tail split.
            plan_label = "联合型号生产计划"
            plan_title = f"联合型号生产计划{subplan.sequence_no}"
        result.append(
            {
                "id": subplan.pk,
                "sequence_no": subplan.sequence_no,
                "subplan_code": f"子计划{subplan.sequence_no}" if is_tail_split else "",
                "plan_label": plan_label,
                "plan_title": plan_title,
                "is_tail_split": is_tail_split,
                "split_position": split_position,
                "quantity": subplan.planned_total_quantity,
                "status": subplan.status,
                "status_name": subplan.get_status_display(),
                "production_released_at": subplan.production_released_at.isoformat() if subplan.production_released_at else None,
                "production_completed_at": subplan.production_completed_at.isoformat() if subplan.production_completed_at else None,
                "qrcode_completed_at": subplan.qrcode_completed_at.isoformat() if subplan.qrcode_completed_at else None,
                "items": [
                    {
                        "id": item.pk,
                        "order_plan_item_id": item.order_plan_item_id,
                        "product_model_id": item.order_plan_item.product_model_id,
                        "product_model_name": item.order_plan_item.product_model.name,
                        "quantity": item.quantity,
                        "production_batch_code": item.production_batch_code,
                        "production_batch_id": item.production_batch_id,
                        "qrcode_generated_at": item.qrcode_generated_at.isoformat() if item.qrcode_generated_at else None,
                        "qrcode_printed_at": item.qrcode_printed_at.isoformat() if item.qrcode_printed_at else None,
                    }
                    for item in items
                ],
            }
        )
    return result


def _available_actions(plan, material_plan, identity):
    roles = effective_role_codes(identity)
    sales_actor_ids = (
        delegated_role_user_ids(identity, "factory_sales")
        if identity.role.code in {"factory_senior_manager", "merchant_senior_manager"}
        else {identity.user_id} if "factory_sales" in roles else set()
    )
    actions = []
    if "factory_material_stock" in roles and identity.organization_id == plan.factory_organization_id:
        if material_plan.status == OrderPlanMaterialPlan.Status.WAITING_WAREHOUSE_REVIEW:
            actions.append({
                "code": "warehouse-review", "label": "核对物料与配送计划",
                "confirm_title": "确认核对物料", "confirm_text": "将按当前库存预占并排入供应配送推送队列。",
            })
        if _delivery_receipt_rows(material_plan=material_plan):
            actions.append({
                "code": "order-delivery-receipt",
                "label": "一键入库",
                "confirm_title": "订单配送物料入库",
                "confirm_text": "进入后将自动带入本订单所有已完成线下配送、尚未入库的物料。",
                "url": f"/pages/material_inventory/order_delivery_receipt/order_delivery_receipt?plan_id={plan.pk}",
            })
        if material_plan.status in {
            OrderPlanMaterialPlan.Status.WAITING_SUPPLIER_DISPATCH,
            OrderPlanMaterialPlan.Status.WAITING_RECEIPT,
        } and _all_materials_reserved(material_plan) and _all_delivery_commitments_confirmed(material_plan):
            actions.append({
                "code": "material-ready", "label": "所需物料全部到位，推送生产",
                "confirm_title": "确认物料全部到位", "confirm_text": "请确认线下物料已核对齐套，随后将推送生产经理。",
            })
    if roles & {"supplier_owner", "supplier_sales"} and identity.organization.organization_type.code == "SUPPLIER":
        deliveries = OrderPlanSupplyDelivery.objects.filter(
            material_plan=material_plan,
            supplier_id=identity.organization_id,
            status=OrderPlanSupplyDelivery.Status.DISPATCHED,
        ).order_by("id")
        # Select the exact delivery that owns this user's pending task.  The
        # normal V2 schema keeps one supplier delivery per material plan, but
        # this guard also keeps repaired/historical multi-delivery data from
        # pointing a snapshot recipient at an unrelated first row.
        for delivery in deliveries:
            task = UserTask.objects.filter(
                workitem__uniq_key=f"sales:order-plan:{plan.pk}:material-delivery:{delivery.pk}",
                to_openid=_actor_openid(identity.user),
                need_action=True,
            ).exclude(state="DONE").exists()
            if task:
                actions.append({
                    "code": "supplier-confirm", "label": "确认物料已线下配送",
                    "payload": {
                        "supplier_id": delivery.supplier_id,
                        "delivery_id": delivery.pk,
                    },
                    "confirm_title": "确认线下配送", "confirm_text": "确认后等待厂家物料库独立入库。",
                })
                break
    if "factory_production" in roles and identity.organization_id == plan.factory_organization_id:
        subplan = material_plan.production_subplans.filter(
            status=OrderPlanProductionSubplan.Status.PRODUCTION_PENDING,
        ).order_by("sequence_no").first()
        if subplan:
            task = UserTask.objects.filter(
                workitem__uniq_key=f"sales:order-plan:{plan.pk}:production:{subplan.pk}",
                to_openid=_actor_openid(identity.user), need_action=True,
            ).exclude(state="DONE").exists()
            if task:
                actions.append({
                    "code": "production-confirm", "label": "确认本型号生产计划完成",
                    "payload": {"subplan_id": subplan.pk},
                    "confirm_title": "确认生产完成", "confirm_text": "请确认本型号生产计划已在线下完成装配。",
                })
    if (
        "factory_sales_assistant" in roles
        and identity.organization_id == plan.factory_organization_id
        and plan.status == OrderPlan.Status.RECEIVED
        and not plan.accounted_at
    ):
        task = UserTask.objects.filter(
            workitem__type="ORDER_PLAN_V2_ACCOUNTING_CONFIRM",
            workitem__uniq_key=f"sales:order-plan:{plan.pk}:accounting-confirm",
            to_openid=_actor_openid(identity.user),
            need_action=True,
        ).exclude(state="DONE").exists()
        if task:
            actions.append({
                "code": "accounting-confirm", "label": "确认订单入账",
                "confirm_title": "确认订单入账", "confirm_text": "请确认本订单已按商家入库结果完成入账。",
            })
    has_started_shipment = _shipment_has_physical_progress(plan)
    production_released = bool(material_plan.material_ready_at)
    cancellation_audit = OrderPlanCancellationAudit.objects.filter(order_plan=plan).first()
    if (
        "factory_sales" in roles
        and plan.created_by_id in sales_actor_ids
        and not has_started_shipment
        and plan.status not in {
            OrderPlan.Status.CANCELLED,
            OrderPlan.Status.SHIPPED,
            OrderPlan.Status.RECEIVED,
        }
    ):
        if cancellation_audit and cancellation_audit.status == OrderPlanCancellationAudit.Status.WAITING_FINISHED_STOCK:
            actions.append({
                "code": "cancel",
                "label": "确认生产完成并取消订单",
                "payload": {"assembly_started_offline": True},
                "danger": True,
                "confirm_title": "确认取消已完成生产订单",
                "confirm_text": "已完成产品将转入厂家可调用成品库存；订单及未消耗预占将正式取消。",
            })
        elif not cancellation_audit or cancellation_audit.status != OrderPlanCancellationAudit.Status.WAITING_PRODUCTION_COMPLETION:
            if not production_released or not material_plan.production_subplans.exists():
                actions.append({
                    "code": "cancel",
                    "label": "取消订单计划",
                    "danger": True,
                    "confirm_title": "取消订单计划",
                    "confirm_text": "未入库物料配送将取消，已入库物料保留为正常库存。",
                })
            elif material_plan.status == OrderPlanMaterialPlan.Status.PRODUCTION_COMPLETED:
                actions.append({
                    "code": "cancel",
                    "label": "按已开始装配取消订单",
                    "payload": {"assembly_started_offline": True},
                    "danger": True,
                    "confirm_title": "确认生产完成后取消订单",
                    "confirm_text": "已完成产品将转入厂家可调用成品库存；不会创建任何发货批次。",
                })
            else:
                actions.extend([
                    {
                        "code": "cancel",
                        "label": "确认未开始装配并取消",
                        "payload": {"assembly_started_offline": False},
                        "danger": True,
                        "confirm_title": "确认未开始装配",
                        "confirm_text": "将取消订单并释放未消耗物料预占。请先完成线下核实。",
                    },
                    {
                        "code": "cancel",
                        "label": "已开始装配，登记取消等待",
                        "payload": {"assembly_started_offline": True},
                        "danger": True,
                        "confirm_title": "确认已开始线下装配",
                        "confirm_text": "订单将等待生产经理完成全部子计划；期间不会再创建打码或发货任务。",
                    },
                ])
    return actions


def get_material_flow_detail(*, user, plan_id):
    identity = _viewer_identity(user)
    plan = _v2_plan_by_id(plan_id)
    supplier_sales_scope = _supplier_sales_material_scope_for_plan(identity, plan)
    if not _can_view_plan(
        identity,
        plan,
        supplier_sales_scope=supplier_sales_scope,
    ):
        raise OrderPlanMaterialFlowError("当前账号无权查看该订单计划。")
    material_plan = OrderPlanMaterialPlan.objects.select_related(
        "material_ready_by", "warehouse_reviewed_by"
    ).filter(order_plan=plan).first()
    if not material_plan:
        raise OrderPlanMaterialFlowError("订单物料计划不存在。")
    shipments, qrcode_batches = _serialize_shipments(plan, identity)
    production_plans = _serialize_subplans(material_plan)
    available_actions = _available_actions(plan, material_plan, identity)
    timeline_actions = list(available_actions)
    for shipment in shipments:
        timeline_actions.extend(shipment.get("available_actions") or ())
    viewer_active_codes = _viewer_timeline_active_codes(
        plan=plan,
        identity=identity,
        available_actions=timeline_actions,
    )
    from lifecycle.services.replacement_flow_service import pending_for_plan
    # Kept outside sales shipment quantity, pricing, finance and receivable calculations.
    replacement_demands = pending_for_plan(plan) if identity.organization_id in {plan.factory_organization_id,plan.merchant_organization_id} else []
    return {
        "pending_replacement_demands": replacement_demands,
        "plan": _serialize_plan_detail(plan, material_plan),
        "viewer": {
            "role": identity.role.code,
            "role_name": identity.role.name,
            "organization_id": identity.organization_id,
            "organization_name": identity.organization.name,
        },
        "timeline": _timeline(
            plan,
            material_plan,
            viewer_active_codes=viewer_active_codes,
        ),
        "finished_inventory": _serialize_finished_inventory(plan),
        "bom_selections": _serialize_bom_selections(
            plan,
            identity,
            supplier_sales_material_ids=(
                supplier_sales_scope["material_ids"]
                if supplier_sales_scope is not None
                else None
            ),
        ),
        "material_requirements": _serialize_requirements(
            material_plan,
            identity,
            supplier_sales_material_ids=(
                supplier_sales_scope["material_ids"]
                if supplier_sales_scope is not None
                else None
            ),
        ),
        "supply_deliveries": _serialize_deliveries(
            material_plan,
            identity,
            supplier_sales_material_ids=(
                supplier_sales_scope["material_ids"]
                if supplier_sales_scope is not None
                else None
            ),
        ),
        # ``production_plans`` is the business-facing V2 field.  Keep the
        # old key for clients that have not yet received this frontend patch.
        "production_plans": production_plans,
        "production_subplans": production_plans,
        "qrcode_batches": qrcode_batches,
        "shipments": shipments,
        # Payment starts at creation; final accounting still waits for every receipt.
        "finance": serialize_order_finance(user=user, plan=plan),
        "available_actions": available_actions,
    }
