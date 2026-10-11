"""V2-only order-plan list projection.

The active application has one stock-aware order workflow. This module never
serializes or exposes rows from the retired whole-plan flow.
"""

from django.db.models import Q

from sales.models import OrderPlan, OrderPlanMaterialPlan
from sales.services.order_plan_flow_guard import material_flow_status_name
from account.services.role_delegation_service import effective_role_codes
from account.services.role_delegation_service import delegated_role_user_ids
from sales.services.order_plan_material_flow_service import _viewer_identity
from supply_chain.services.supplier_responsibility_service import (
    get_responsible_material_ids,
    get_v2_supplier_delivery_snapshot_access,
)


class OrderPlanListError(Exception):
    pass


FACTORY_ROLES = {
    "factory_admin", "factory_manager", "factory_sales", "factory_matching",
    "factory_material_stock", "factory_production", "factory_stock",
    "factory_sales_assistant", "factory_logistics", "factory_chief_engineer",
}
MERCHANT_ROLES = {
    "merchant_owner", "merchant_senior_manager", "merchant_sales", "merchant_stock",
}
SUPPLIER_ROLES = {"supplier_owner", "supplier_sales"}
ALLOWED_ROLES = FACTORY_ROLES | MERCHANT_ROLES | SUPPLIER_ROLES


def _identity(user):
    try:
        identity = _viewer_identity(user)
    except Exception as exc:
        raise OrderPlanListError(str(exc)) from exc
    if not (effective_role_codes(identity) & ALLOWED_ROLES):
        raise OrderPlanListError("当前账号没有查看订单计划列表的岗位权限。")
    return identity


def _scope(identity):
    roles = effective_role_codes(identity)
    kind = identity.organization.organization_type.code
    if roles & FACTORY_ROLES and kind == "OWNER":
        return "factory"
    if roles & MERCHANT_ROLES and kind == "MERCHANT":
        return "merchant"
    if roles & SUPPLIER_ROLES and kind == "SUPPLIER":
        return "supplier"
    raise OrderPlanListError("当前岗位与所属组织类型不一致。")


def _serialize(plan):
    material_plan = plan.material_plan
    return {
        "id": plan.pk,
        "plan_code": plan.plan_code,
        "status": material_plan.status,
        "status_name": material_flow_status_name(plan, material_plan),
        "material_status": material_plan.status,
        "material_status_name": material_plan.get_status_display(),
        "flow_version": 2,
        "material_flow_version": 2,
        "is_material_flow": True,
        "total_quantity": plan.total_quantity,
        "factory": {"id": plan.factory_organization_id, "name": plan.factory_organization.name},
        "merchant": {"id": plan.merchant_organization_id, "name": plan.merchant_organization.name},
        "brand": {"id": plan.brand_id, "name": plan.brand.name},
        "region": plan.region_snapshot,
        "sales_manager": {
            "id": plan.created_by_id,
            "name": plan.created_by.wx_nickname or plan.created_by.get_full_name() or plan.created_by.username,
        },
        "remark": plan.remark,
        "submitted_at": plan.submitted_at.isoformat(),
        "detail_link": "/pages/sales/order_plan_detail/order_plan_detail?order_plan_id=%s" % plan.pk,
    }


def _supplier_sales_query(identity):
    snapshot = get_v2_supplier_delivery_snapshot_access(
        supplier=identity.organization,
        sales_identity=identity,
    )
    open_plan_ids = set(snapshot["open_plan_ids"])
    history_plan_ids = set(snapshot["history_material_ids_by_plan"])
    material_ids = get_responsible_material_ids(sales_identity=identity)
    current = Q(
        material_plan__supply_deliveries__supplier=identity.organization,
        material_plan__supply_deliveries__lines__material_id__in=material_ids,
    )
    if open_plan_ids:
        current &= ~Q(pk__in=open_plan_ids)
    return current | Q(pk__in=history_plan_ids)


def plan_status_choices():
    return list(OrderPlanMaterialPlan.Status.choices) + [("CANCELLED", "已取消")]


def get_order_plan_list(user, status="", limit=50):
    identity = _identity(user)
    role = identity.role.code
    scope = _scope(identity)
    try:
        limit = min(max(int(limit or 50), 1), 100)
    except (TypeError, ValueError) as exc:
        raise OrderPlanListError("列表数量参数无效。") from exc

    plans = OrderPlan.objects.select_related(
        "factory_organization", "merchant_organization", "brand", "created_by", "material_plan"
    ).filter(flow_version=OrderPlan.FlowVersion.MATERIAL_STOCK_V2)
    if scope == "factory":
        plans = plans.filter(factory_organization=identity.organization)
        if roles := effective_role_codes(identity):
            # A senior manager assigned only the sales role sees the plans
            # created by factory-sales users they directly invited. A second
            # delegated factory role keeps its normal organization-wide scope.
            other_factory_roles = FACTORY_ROLES - {"factory_sales"}
            if "factory_sales" in roles and not (roles & other_factory_roles):
                creator_ids = (
                    delegated_role_user_ids(identity, "factory_sales")
                    if identity.role.code in {"factory_senior_manager", "merchant_senior_manager"}
                    else {identity.user_id}
                )
                plans = plans.filter(created_by_id__in=creator_ids)
        visibility = "factory_v2"
    elif scope == "merchant":
        plans = plans.filter(merchant_organization=identity.organization)
        visibility = "merchant_v2"
    elif role == "supplier_owner":
        plans = plans.filter(material_plan__supply_deliveries__supplier=identity.organization).distinct()
        visibility = "supplier_v2"
    else:
        plans = plans.filter(_supplier_sales_query(identity)).distinct()
        visibility = "supplier_responsibility_v2"

    status = str(status or "").strip()
    if status:
        accepted = {value for value, _ in plan_status_choices()}
        if status not in accepted:
            raise OrderPlanListError("订单计划状态筛选值无效。")
        plans = plans.filter(Q(material_plan__status=status) | Q(status=status))

    items = [_serialize(plan) for plan in plans.order_by("-submitted_at", "-id")[:limit]]
    return {
        "viewer": {
            "role": role,
            "role_name": identity.role.name,
            "organization_id": identity.organization_id,
            "organization_name": identity.organization.name,
        },
        "visibility": visibility,
        "count": len(items),
        "status_choices": [{"value": value, "name": name} for value, name in plan_status_choices()],
        "items": items,
    }
