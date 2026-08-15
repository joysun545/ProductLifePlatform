from django.db.models import Prefetch

from account.models import UserOrganization
from product.models import BOMItem
from sales.models import OrderPlan, OrderPlanItem
from supply_chain.services.supplier_responsibility_service import (
    get_responsible_material_ids,
)
from workflow.models import WorkItem


class OrderPlanListError(Exception):
    pass


FACTORY_ROLE_CODES = {
    "factory_sales",
    "factory_sales_assistant",
    "factory_matching",
    "factory_production",
    "factory_stock",
}
MERCHANT_ROLE_CODES = {
    "merchant_owner",
    "merchant_senior_manager",
    "merchant_sales",
    "merchant_stock",
}
SUPPLIER_ROLE_CODES = {
    "supplier_owner",
    "supplier_sales",
}
ALLOWED_ROLE_CODES = FACTORY_ROLE_CODES | SUPPLIER_ROLE_CODES | MERCHANT_ROLE_CODES
SUPPLIER_STATUS_PENDING = "SUPPLIER_PENDING"
SUPPLIER_STATUS_PROCESSING = "SUPPLIER_PROCESSING"
SUPPLIER_STATUS_CONFIRMED = "SUPPLIER_CONFIRMED"


def _get_identity(user):
    identity = (
        UserOrganization.objects.select_related(
            "organization",
            "organization__organization_type",
            "role",
        )
        .filter(
            user=user,
            is_active=True,
            organization__is_active=True,
            role__is_active=True,
        )
        .first()
    )
    if not identity or identity.role.code not in ALLOWED_ROLE_CODES:
        raise OrderPlanListError("当前账号没有查看订单计划列表的岗位权限。")

    organization_type = identity.organization.organization_type.code
    if identity.role.code in FACTORY_ROLE_CODES and organization_type != "OWNER":
        raise OrderPlanListError("当前厂家岗位与所属组织类型不一致。")
    if identity.role.code in SUPPLIER_ROLE_CODES and organization_type != "SUPPLIER":
        raise OrderPlanListError("当前供应商岗位与所属组织类型不一致。")
    if identity.role.code in MERCHANT_ROLE_CODES and organization_type != "MERCHANT":
        raise OrderPlanListError("当前商家岗位与所属组织类型不一致。")
    return identity


def _display_quantity(value):
    number = float(value)
    return int(number) if number.is_integer() else round(number, 4)


def _confirmation_data(order_item):
    confirmation = getattr(order_item, "bom_confirmation", None)
    if not confirmation:
        return None
    return {
        "bom_id": confirmation.confirmed_bom_id,
        "version": confirmation.confirmed_bom.version,
        "version_no": confirmation.confirmed_bom.version_no,
        "has_changes": confirmation.has_changes,
        "confirmed_at": confirmation.confirmed_at.isoformat(),
    }


def _base_plan_data(plan):
    return {
        "id": plan.pk,
        "plan_code": plan.plan_code,
        "status": plan.status,
        "status_name": plan.get_status_display(),
        "factory": {
            "id": plan.factory_organization_id,
            "name": plan.factory_organization.name,
        },
        "merchant": {
            "id": plan.merchant_organization_id,
            "name": plan.merchant_organization.name,
        },
        "brand": {
            "id": plan.brand_id,
            "name": plan.brand.name,
        },
        "region": plan.region_snapshot,
        "sales_manager": {
            "id": plan.created_by_id,
            "name": (
                plan.created_by.wx_nickname
                or plan.created_by.get_full_name()
                or plan.created_by.username
            ),
        },
        "remark": plan.remark,
        "submitted_at": plan.submitted_at.isoformat(),
    }


def _supplier_workitems(plan_ids):
    by_plan = {}
    for workitem in (
        WorkItem.objects.filter(
            type="ORDER_PLAN_SUPPLIER_PREPARE",
            biz_ref__in=[f"sales.OrderPlan:{plan_id}" for plan_id in plan_ids],
        )
        .prefetch_related("user_tasks")
        .order_by("id")
    ):
        payload = workitem.payload if isinstance(workitem.payload, dict) else {}
        try:
            plan_id = int(payload.get("order_plan_id"))
            supplier_id = int(payload.get("supplier_id"))
        except (TypeError, ValueError):
            continue
        by_plan.setdefault(plan_id, {})[supplier_id] = workitem
    return by_plan


def _production_workitems(plan_ids):
    by_plan = {}
    refs = [f"sales.OrderPlan:{plan_id}" for plan_id in plan_ids]
    for workitem in (
        WorkItem.objects.filter(
            type="ORDER_PLAN_PRODUCTION_CONFIRM",
            biz_ref__in=refs,
        ).prefetch_related("user_tasks").order_by("id")
    ):
        payload = workitem.payload if isinstance(workitem.payload, dict) else {}
        try:
            plan_id = int(payload.get("order_plan_id"))
        except (TypeError, ValueError):
            continue
        by_plan[plan_id] = workitem
    return by_plan


def _stock_workitems(plan_ids):
    by_plan = {}
    refs = [f"sales.OrderPlan:{plan_id}" for plan_id in plan_ids]
    for workitem in (
        WorkItem.objects.filter(
            type="ORDER_PLAN_STOCK_OUTBOUND",
            biz_ref__in=refs,
        ).prefetch_related("user_tasks").order_by("id")
    ):
        payload = workitem.payload if isinstance(workitem.payload, dict) else {}
        try:
            plan_id = int(payload.get("order_plan_id"))
        except (TypeError, ValueError):
            continue
        by_plan[plan_id] = workitem
    return by_plan


def _role_workitems(plan_ids, work_type):
    by_plan = {}
    refs = [f"sales.OrderPlan:{plan_id}" for plan_id in plan_ids]
    for workitem in (
        WorkItem.objects.filter(type=work_type, biz_ref__in=refs)
        .prefetch_related("user_tasks")
        .order_by("id")
    ):
        payload = workitem.payload if isinstance(workitem.payload, dict) else {}
        try:
            plan_id = int(payload.get("order_plan_id"))
        except (TypeError, ValueError):
            continue
        by_plan[plan_id] = workitem
    return by_plan


def _stage_data(plan):
    order = [
        OrderPlan.Status.MATCHING_PENDING,
        OrderPlan.Status.MATCHING_COMPLETED,
        OrderPlan.Status.PRODUCTION_PENDING,
        OrderPlan.Status.PRODUCTION_COMPLETED,
        OrderPlan.Status.SHIPPED,
        OrderPlan.Status.RECEIVED,
    ]
    current_index = order.index(plan.status) if plan.status in order else -1
    definitions = (
        ("CREATED", "已创建", 0),
        ("MATCHING", "配套", 1),
        ("PRODUCTION", "生产", 3),
        ("SHIPPING", "发货", 4),
        ("RECEIPT", "商家入库", 5),
    )
    stages = []
    for code, name, completed_at in definitions:
        completed = current_index >= completed_at
        current = not completed and (
            (code == "MATCHING" and current_index == 0)
            or (code == "PRODUCTION" and current_index in {1, 2})
            or (code == "SHIPPING" and current_index == 3)
            or (code == "RECEIPT" and current_index == 4)
        )
        stages.append({
            "code": code,
            "name": name,
            "state": "DONE" if completed else ("CURRENT" if current else "WAITING"),
        })
    if plan.status == OrderPlan.Status.CANCELLED:
        for stage in stages:
            stage["state"] = "CANCELLED"
    return stages


def _stock_confirmation_data(workitem, openid):
    if not workitem:
        return None
    task = next(
        (row for row in workitem.user_tasks.all() if row.to_openid == openid),
        None,
    )
    done = workitem.status == "DONE"
    return {
        "workitem_id": workitem.pk,
        "task_id": task.pk if task else None,
        "state": "COMPLETED" if done else "PENDING",
        "state_name": "已发货" if done else "待发货",
        "can_confirm": bool(
            task and not done and task.state != "DONE" and task.need_action
        ),
    }


def _shared_action_data(workitem, openid, *, pending_name, completed_name):
    if not workitem:
        return None
    payload = workitem.payload if isinstance(workitem.payload, dict) else {}
    task = next(
        (row for row in workitem.user_tasks.all() if row.to_openid == openid),
        None,
    )
    done = workitem.status == "DONE"
    completed_by_name = payload.get("completed_by_name", "")
    return {
        "workitem_id": workitem.pk,
        "task_id": task.pk if task else None,
        "state": "COMPLETED" if done else "PENDING",
        "state_name": (
            f"已由{completed_by_name}完成" if done and completed_by_name
            else (completed_name if done else pending_name)
        ),
        "completed_by_name": completed_by_name,
        "can_confirm": bool(
            task and not done and task.state != "DONE" and task.need_action
        ),
    }


def _production_confirmation_data(workitem, openid):
    if not workitem:
        return None
    task = next(
        (row for row in workitem.user_tasks.all() if row.to_openid == openid),
        None,
    )
    done = workitem.status == "DONE"
    return {
        "workitem_id": workitem.pk,
        "task_id": task.pk if task else None,
        "state": "COMPLETED" if done else "PENDING",
        "state_name": "生产已完成" if done else "待确认生产完成",
        "can_confirm": bool(
            task and not done and task.state != "DONE" and task.need_action
        ),
    }


def _processed_actors(payload):
    actors = payload.get("processed_actors", [])
    return actors if isinstance(actors, list) else []


def _supplier_confirmation_data(workitem, *, openid=""):
    if not workitem:
        return {
            "state": "NOT_DISPATCHED",
            "state_name": "尚未发起供应确认",
            "can_confirm": False,
            "processed_actors": [],
            "processed_by_text": "",
            "scope_material_ids": [],
            "material_count": 0,
        }

    payload = workitem.payload if isinstance(workitem.payload, dict) else {}
    scopes = payload.get("recipient_scopes", {})
    scope = scopes.get(openid, {}) if openid and isinstance(scopes, dict) else {}
    if openid:
        material_ids = sorted(
            {int(value) for value in scope.get("material_ids", [])}
        )
    else:
        material_ids = sorted(
            {
                int(row["material_id"])
                for row in payload.get("materials", [])
                if isinstance(row, dict) and row.get("material_id")
            }
        )
    task = next(
        (
            item
            for item in workitem.user_tasks.all()
            if openid and item.to_openid == openid
        ),
        None,
    )
    if workitem.status == "DONE":
        state = "CONFIRMED"
        state_name = "配送完成"
    elif task and task.state == "DONE":
        state = "PROCESSED"
        state_name = "本人已确认配送，等待协同账号"
    else:
        state = "PENDING"
        state_name = "待确认配送"
    actors = _processed_actors(payload)
    return {
        "workitem_id": workitem.pk,
        "task_id": task.pk if task else None,
        "state": state,
        "state_name": state_name,
        "can_confirm": bool(
            task
            and workitem.status == "OPEN"
            and task.state != "DONE"
            and task.need_action
        ),
        "completion_policy": payload.get(
            "completion_policy",
            "OWNER_OR_ALL_SALES",
        ),
        "processed_actors": actors,
        "processed_by_text": "、".join(
            f"{actor.get('name', '协同账号')}已处理" for actor in actors
        ),
        "waiting_for_owner": bool(payload.get("waiting_for_owner")),
        "scope_material_ids": material_ids,
        "material_count": sum(
            1
            for row in payload.get("materials", [])
            if isinstance(row, dict)
            and row.get("material_id")
            and int(row["material_id"]) in set(material_ids)
        ),
        "supplier": {
            "id": payload.get("supplier_id"),
            "name": payload.get("supplier_name", ""),
        },
    }


def _serialize_factory_plan(
    plan, role_code, supplier_workitems, production_workitem=None,
    stock_workitem=None, receipt_workitem=None, accounting_workitem=None,
    openid=""
):
    data = _base_plan_data(plan)
    confirmations = [
        _supplier_confirmation_data(workitem)
        for workitem in supplier_workitems.values()
    ]
    data.update(
        {
            "scope": "factory",
            "total_quantity": plan.total_quantity,
            "items": [
                {
                    "id": item.pk,
                    "category": {
                        "id": item.category_id,
                        "name": item.category.name,
                    },
                    "product_model": {
                        "id": item.product_model_id,
                        "code": item.product_model.code,
                        "name": item.product_model.name,
                    },
                    "quantity": item.quantity,
                    "order_batch_code": item.order_batch_code,
                    "lifecycle_code": item.lifecycle_code,
                    "bom_confirmation": _confirmation_data(item),
                }
                for item in plan.visible_items
            ],
            "detail_link": (
                "/pages/sales/matching_confirm/matching_confirm"
                f"?order_plan_id={plan.pk}"
                if role_code == "factory_matching"
                else ""
            ),
            "supplier_confirmations": confirmations,
            "supplier_confirmation_summary": {
                "total": len(confirmations),
                "confirmed": sum(
                    1
                    for item in confirmations
                    if item["state"] == "CONFIRMED"
                ),
            },
            "production_confirmation": (
                _production_confirmation_data(production_workitem, openid)
                if role_code == "factory_production"
                else None
            ),
            "fulfillment_stages": _stage_data(plan),
            "stock_confirmation": (
                _stock_confirmation_data(stock_workitem, openid)
                if role_code == "factory_stock"
                else None
            ),
            "accounting_confirmation": (
                _shared_action_data(
                    accounting_workitem, openid,
                    pending_name="待确认入账", completed_name="已入账",
                )
                if role_code == "factory_sales_assistant" else None
            ),
            "shipped_by_name": (
                plan.shipped_by.wx_nickname or plan.shipped_by.username
                if plan.shipped_by_id else ""
            ),
            "received_by_name": (
                plan.received_by.wx_nickname or plan.received_by.username
                if plan.received_by_id else ""
            ),
            "accounted_by_name": (
                (plan.accounted_by.wx_nickname or plan.accounted_by.username)
                if role_code == "factory_sales_assistant" and plan.accounted_by_id
                else ""
            ),
            "accounted_at": (
                plan.accounted_at.isoformat()
                if role_code == "factory_sales_assistant" and plan.accounted_at
                else None
            ),
        }
    )
    return data


def _serialize_merchant_plan(plan, identity, receipt_workitem):
    data = _serialize_factory_plan(
        plan, identity.role.code, {}, receipt_workitem=receipt_workitem,
        openid=identity.user.wx_openid or "",
    )
    data["scope"] = "merchant"
    data["receipt_confirmation"] = _shared_action_data(
        receipt_workitem,
        identity.user.wx_openid or "",
        pending_name="待确认入库",
        completed_name="已入库",
    )
    return data


def _serialize_supplier_plan(plan, identity, supplier_workitems):
    confirmation_data = _supplier_confirmation_data(
        supplier_workitems.get(identity.organization_id),
        openid=identity.user.wx_openid,
    )
    scoped_material_ids = set(confirmation_data["scope_material_ids"])
    visible_items = []
    visible_total_quantity = 0
    for order_item in plan.visible_items:
        confirmation = getattr(order_item, "bom_confirmation", None)
        if not confirmation:
            continue
        supplier_bom_items = getattr(
            confirmation.confirmed_bom,
            "viewer_supplier_items",
            [],
        )
        if not supplier_bom_items:
            continue

        visible_total_quantity += order_item.quantity
        visible_items.append(
            {
                "id": order_item.pk,
                "category": {
                    "id": order_item.category_id,
                    "name": order_item.category.name,
                },
                "product_model": {
                    "id": order_item.product_model_id,
                    "code": order_item.product_model.code,
                    "name": order_item.product_model.name,
                },
                "quantity": order_item.quantity,
                "order_batch_code": order_item.order_batch_code,
                "bom_confirmation": _confirmation_data(order_item),
                "materials": [
                    {
                        "material_id": bom_item.material_id,
                        "material_code": bom_item.material.code,
                        "material_name": bom_item.material.name,
                        "material_spec_id": bom_item.material_spec_id,
                        "material_spec_code": (
                            bom_item.material_spec.code
                            if bom_item.material_spec_id
                            else ""
                        ),
                        "material_spec_name": (
                            bom_item.material_spec.name
                            if bom_item.material_spec_id
                            else "暂未指定规格"
                        ),
                        "quantity_per_device": _display_quantity(
                            bom_item.quantity
                        ),
                        "required_quantity": _display_quantity(
                            bom_item.quantity * order_item.quantity
                        ),
                        "remark": bom_item.remark,
                        "supplier_confirmation_state": (
                            "CONFIRMED"
                            if confirmation_data["state"] == "CONFIRMED"
                            else (
                                "PROCESSED"
                                if bom_item.material_id in scoped_material_ids
                                and confirmation_data["state"] == "PROCESSED"
                                else "PENDING"
                            )
                        ),
                    }
                    for bom_item in supplier_bom_items
                ],
            }
        )

    data = _base_plan_data(plan)
    confirmation_state = confirmation_data["state"]
    if plan.status == OrderPlan.Status.CANCELLED:
        display_status = "CANCELLED"
        display_status_name = "已取消"
    elif confirmation_state == "CONFIRMED":
        display_status = SUPPLIER_STATUS_CONFIRMED
        display_status_name = "配送完成"
    elif confirmation_state == "PROCESSED":
        display_status = SUPPLIER_STATUS_PROCESSING
        display_status_name = "配送确认中"
    elif confirmation_state == "PENDING":
        display_status = SUPPLIER_STATUS_PENDING
        display_status_name = "待配送"
    else:
        display_status = plan.status
        display_status_name = plan.get_status_display()
    data.update(
        {
            "scope": "supplier",
            "order_status": plan.status,
            "order_status_name": plan.get_status_display(),
            "status": display_status,
            "status_name": display_status_name,
            "total_quantity": visible_total_quantity,
            "items": visible_items,
            "detail_link": "",
            "supplier_confirmation": confirmation_data,
        }
    )
    return data


def get_order_plan_list(user, status="", limit=50):
    identity = _get_identity(user)
    role_code = identity.role.code

    try:
        limit = min(max(int(limit or 50), 1), 100)
    except (TypeError, ValueError):
        raise OrderPlanListError("列表数量参数无效。")

    status = str(status or "").strip()
    valid_statuses = {choice for choice, _ in OrderPlan.Status.choices}
    supplier_status = (
        status
        if status in {
            SUPPLIER_STATUS_PENDING,
            SUPPLIER_STATUS_PROCESSING,
            SUPPLIER_STATUS_CONFIRMED,
        }
        else ""
    )
    if status and status not in valid_statuses:
        if not (role_code in SUPPLIER_ROLE_CODES and supplier_status):
            raise OrderPlanListError("订单计划状态筛选值无效。")

    plans = OrderPlan.objects.select_related(
        "factory_organization",
        "merchant_organization",
        "brand",
        "created_by",
        "shipped_by",
        "received_by",
        "accounted_by",
    )

    if role_code == "factory_sales":
        visibility = "created_by_me"
        plans = plans.filter(
            factory_organization=identity.organization,
            created_by=user,
        )
    elif role_code in {
        "factory_matching", "factory_production", "factory_stock",
        "factory_sales_assistant",
    }:
        visibility = "factory_all"
        plans = plans.filter(factory_organization=identity.organization)
    elif role_code in MERCHANT_ROLE_CODES:
        visibility = "merchant_receiving"
        plans = plans.filter(merchant_organization=identity.organization)
    elif role_code == "supplier_owner":
        visibility = "supplier_participating"
        plans = plans.filter(
            items__bom_confirmation__confirmed_bom__items__supplier=(
                identity.organization
            )
        ).distinct()
    else:
        visibility = "supplier_responsibility"
        responsible_material_ids = get_responsible_material_ids(
            sales_identity=identity,
        )
        plans = plans.filter(
            items__bom_confirmation__confirmed_bom__items__supplier=(
                identity.organization
            ),
            items__bom_confirmation__confirmed_bom__items__material_id__in=(
                responsible_material_ids
            ),
        ).distinct()

    if status and not supplier_status:
        plans = plans.filter(status=status)

    if role_code in SUPPLIER_ROLE_CODES:
        supplier_bom_items = BOMItem.objects.filter(
            supplier=identity.organization,
        ).select_related("material", "material_spec")
        if role_code == "supplier_sales":
            supplier_bom_items = supplier_bom_items.filter(
                material_id__in=responsible_material_ids,
            )
        visible_order_items = (
            OrderPlanItem.objects.filter(
                bom_confirmation__confirmed_bom__items__supplier=(
                    identity.organization
                )
            )
            .select_related(
                "category",
                "product_model",
                "bom_confirmation",
                "bom_confirmation__confirmed_bom",
            )
            .prefetch_related(
                Prefetch(
                    "bom_confirmation__confirmed_bom__items",
                    queryset=supplier_bom_items,
                    to_attr="viewer_supplier_items",
                )
            )
            .distinct()
        )
        if role_code == "supplier_sales":
            visible_order_items = visible_order_items.filter(
                bom_confirmation__confirmed_bom__items__material_id__in=(
                    responsible_material_ids
                )
            ).distinct()
    else:
        visible_order_items = OrderPlanItem.objects.select_related(
            "category",
            "product_model",
            "bom_confirmation",
            "bom_confirmation__confirmed_bom",
        )

    plans = list(plans.prefetch_related(
        Prefetch("items", queryset=visible_order_items, to_attr="visible_items")
    )[:limit])
    workitems_by_plan = _supplier_workitems([plan.pk for plan in plans])
    production_by_plan = _production_workitems([plan.pk for plan in plans])
    stock_by_plan = _stock_workitems([plan.pk for plan in plans])
    receipt_by_plan = _role_workitems(
        [plan.pk for plan in plans], "ORDER_PLAN_MERCHANT_RECEIPT"
    )
    accounting_by_plan = _role_workitems(
        [plan.pk for plan in plans], "ORDER_PLAN_ACCOUNTING_CONFIRM"
    )

    if role_code in SUPPLIER_ROLE_CODES:
        items = [
            _serialize_supplier_plan(
                plan,
                identity,
                workitems_by_plan.get(plan.pk, {}),
            )
            for plan in plans
        ]
        if supplier_status:
            expected_state = (
                "CONFIRMED"
                if supplier_status == SUPPLIER_STATUS_CONFIRMED
                else (
                    "PROCESSED"
                    if supplier_status == SUPPLIER_STATUS_PROCESSING
                    else "PENDING"
                )
            )
            items = [
                item
                for item in items
                if (
                    item["supplier_confirmation"]["state"] == expected_state
                )
            ]
    elif role_code in MERCHANT_ROLE_CODES:
        items = [
            _serialize_merchant_plan(
                plan, identity, receipt_by_plan.get(plan.pk)
            )
            for plan in plans
        ]
    else:
        items = [
            _serialize_factory_plan(
                plan,
                role_code,
                workitems_by_plan.get(plan.pk, {}),
                production_by_plan.get(plan.pk),
                stock_by_plan.get(plan.pk),
                receipt_by_plan.get(plan.pk),
                accounting_by_plan.get(plan.pk),
                user.wx_openid or "",
            )
            for plan in plans
        ]

    return {
        "viewer": {
            "role": role_code,
            "role_name": identity.role.name,
            "organization_id": identity.organization_id,
            "organization_name": identity.organization.name,
        },
        "visibility": visibility,
        "responsibility_rule": (
            "供应商负责人可见本组织全部物料"
            if role_code == "supplier_owner"
            else (
                "销售经理仅可见负责人分配的物料；具体物料配置优先于品类配置"
                if role_code == "supplier_sales"
                else ""
            )
        ),
        "count": len(items),
        "supplier_status_choices": [
            {"value": SUPPLIER_STATUS_PENDING, "name": "待配送"},
            {"value": SUPPLIER_STATUS_PROCESSING, "name": "配送确认中"},
            {"value": SUPPLIER_STATUS_CONFIRMED, "name": "配送完成"},
        ],
        "items": items,
    }
