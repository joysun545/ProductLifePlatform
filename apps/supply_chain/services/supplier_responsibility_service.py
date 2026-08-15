from collections import defaultdict

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from account.models import UserOrganization
from supply_chain.models import (
    Material,
    SupplierResponsibilityScope,
    SupplierSalesResponsibility,
)
from workflow.models import UserTask, WorkItem
from workflow.services.workflow_service import get_role_snapshot


class SupplierResponsibilityError(Exception):
    """当前账号或提交数据不能用于供应商责任配置。"""


def _identity_name(identity):
    user = identity.user
    return user.wx_nickname or user.get_full_name() or user.username


def _scope_line_count(payload, scope):
    material_ids = {int(value) for value in scope.get("material_ids", [])}
    return sum(
        1
        for item in payload.get("materials", [])
        if isinstance(item, dict)
        and item.get("material_id")
        and int(item["material_id"]) in material_ids
    )


def _task_subtitle(payload, line_count):
    merchant_name = payload.get("merchant_name") or "订单商家"
    brand_name = payload.get("brand_name") or "未指定品牌"
    return f"{merchant_name}｜{brand_name}｜{line_count}项供应物料"


def _active_identity_queryset():
    return UserOrganization.objects.select_related(
        "user",
        "organization",
        "organization__organization_type",
        "role",
    ).filter(
        user__is_active=True,
        is_active=True,
        organization__is_active=True,
        organization__organization_type__is_active=True,
        role__is_active=True,
    )


def get_supplier_identity(user, allowed_roles=("supplier_owner", "supplier_sales")):
    identity = _active_identity_queryset().filter(user=user).first()
    if not identity or identity.role.code not in set(allowed_roles):
        raise SupplierResponsibilityError("当前账号没有供应商责任范围权限。")
    if identity.organization.organization_type.code != "SUPPLIER":
        raise SupplierResponsibilityError("当前岗位没有有效的供应商组织身份。")
    return identity


def _supplied_materials(supplier):
    return (
        Material.objects.filter(
            specs__suppliers__supplier=supplier,
        )
        .select_related("product_category")
        .distinct()
        .order_by("product_category__code", "code", "id")
    )


def _responsibility_assignments(supplier):
    return SupplierSalesResponsibility.objects.select_related(
        "sales_identity",
        "sales_identity__user",
        "product_category",
        "material",
    ).filter(
        supplier=supplier,
        is_active=True,
        sales_identity__is_active=True,
        sales_identity__role__code="supplier_sales",
        sales_identity__role__is_active=True,
        sales_identity__user__is_active=True,
    )


def get_responsibility_options(*, user):
    owner_identity = get_supplier_identity(user, ("supplier_owner",))
    supplier = owner_identity.organization
    sales_identities = list(
        _active_identity_queryset()
        .filter(
            organization=supplier,
            role__code="supplier_sales",
        )
        .order_by("user__wx_nickname", "user__username", "id")
    )
    materials = list(_supplied_materials(supplier))
    categories = {}
    for material in materials:
        if material.product_category_id:
            categories[material.product_category_id] = material.product_category

    assignments = list(_responsibility_assignments(supplier))
    assignment_by_category = {
        item.product_category_id: item
        for item in assignments
        if item.scope_type == SupplierResponsibilityScope.CATEGORY
    }
    assignment_by_material = {
        item.material_id: item
        for item in assignments
        if item.scope_type == SupplierResponsibilityScope.MATERIAL
    }
    manager_assignments = defaultdict(list)
    for item in assignments:
        manager_assignments[item.sales_identity_id].append(item)

    managers = []
    for identity in sales_identities:
        own_assignments = manager_assignments.get(identity.pk, [])
        scope_types = {item.scope_type for item in own_assignments}
        scope_type = next(iter(scope_types)) if len(scope_types) == 1 else ""
        managers.append(
            {
                "identity_id": identity.pk,
                "user_id": identity.user_id,
                "name": (
                    identity.user.wx_nickname
                    or identity.user.get_full_name()
                    or identity.user.username
                ),
                "phone": identity.user.phone or "",
                "scope_type": scope_type,
                "target_ids": [
                    (
                        item.product_category_id
                        if item.scope_type == SupplierResponsibilityScope.CATEGORY
                        else item.material_id
                    )
                    for item in own_assignments
                ],
                "assignment_count": len(own_assignments),
            }
        )

    def assigned_manager(item):
        if not item:
            return None
        return {
            "identity_id": item.sales_identity_id,
            "name": (
                item.sales_identity.user.wx_nickname
                or item.sales_identity.user.get_full_name()
                or item.sales_identity.user.username
            ),
        }

    return {
        "supplier": {
            "id": supplier.pk,
            "name": supplier.name,
        },
        "precedence_rule": "具体物料责任优先于品类责任",
        "sales_managers": managers,
        "categories": [
            {
                "id": category.pk,
                "code": category.code,
                "name": category.name,
                "assigned_manager": assigned_manager(
                    assignment_by_category.get(category.pk)
                ),
            }
            for category in sorted(categories.values(), key=lambda value: value.code)
        ],
        "materials": [
            {
                "id": material.pk,
                "code": material.code,
                "name": material.name,
                "category_id": material.product_category_id,
                "category_name": (
                    material.product_category.name
                    if material.product_category_id
                    else "未分类"
                ),
                "assigned_manager": assigned_manager(
                    assignment_by_material.get(material.pk)
                ),
            }
            for material in materials
        ],
    }


def _normalize_target_ids(target_ids):
    if target_ids in (None, ""):
        return []
    if not isinstance(target_ids, list):
        raise SupplierResponsibilityError("责任范围数据格式无效。")
    normalized = []
    seen = set()
    for raw in target_ids:
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise SupplierResponsibilityError("责任范围数据包含无效选项。") from exc
        if value <= 0 or value in seen:
            if value in seen:
                continue
            raise SupplierResponsibilityError("责任范围数据包含无效选项。")
        seen.add(value)
        normalized.append(value)
    return normalized


def _sync_open_supplier_tasks(supplier):
    """
    责任范围调整后同步尚未完成的供应商共享任务。

    负责人账号始终保留任务；已不再负责相关物料的销售经理从未完成任务中
    移除；新责任销售经理加入同一个 WorkItem，不另建重复业务任务。
    """

    synced_workitems = 0
    added_tasks = 0
    removed_tasks = 0
    now = timezone.now()
    workitems = WorkItem.objects.select_for_update().filter(
        type="ORDER_PLAN_SUPPLIER_PREPARE",
        status="OPEN",
    )
    for workitem in workitems:
        payload = workitem.payload if isinstance(workitem.payload, dict) else {}
        try:
            payload_supplier_id = int(payload.get("supplier_id"))
        except (TypeError, ValueError):
            continue
        if payload_supplier_id != supplier.pk:
            continue

        material_ids = {
            item.get("material_id")
            for item in payload.get("materials", [])
            if isinstance(item, dict) and item.get("material_id")
        }
        recipient_data = get_supplier_task_recipients(
            supplier=supplier,
            material_ids=material_ids,
        )
        desired_openids = set(recipient_data["openids"])
        recipient_scopes = recipient_data["recipient_scopes"]
        existing_tasks = {
            item.to_openid: item
            for item in workitem.user_tasks.select_for_update().all()
        }

        stale_ids = [
            item.pk
            for openid, item in existing_tasks.items()
            if openid not in desired_openids and item.state != "DONE"
        ]
        if stale_ids:
            removed_tasks += UserTask.objects.filter(pk__in=stale_ids).delete()[0]

        for openid in desired_openids:
            scope = recipient_scopes[openid]
            subtitle = _task_subtitle(
                payload,
                _scope_line_count(payload, scope),
            )
            task = existing_tasks.get(openid)
            if task:
                task.to_role_snapshot = scope["role"]
                task.display_title_snapshot = workitem.title
                task.display_subtitle_snapshot = subtitle
                task.need_action = True
                task.save(
                    update_fields=(
                        "to_role_snapshot",
                        "display_title_snapshot",
                        "display_subtitle_snapshot",
                        "need_action",
                        "updated_at",
                    )
                )
            else:
                UserTask.objects.create(
                    workitem=workitem,
                    to_openid=openid,
                    to_role_snapshot=scope["role"],
                    need_action=True,
                    state="NEW",
                    cursor_time=now,
                    badge_group="MY",
                    display_title_snapshot=workitem.title,
                    display_subtitle_snapshot=subtitle,
                )
                added_tasks += 1

        payload["responsible_sales_identity_ids"] = recipient_data[
            "sales_identity_ids"
        ]
        payload["owner_openids"] = recipient_data["owner_openids"]
        payload["recipient_scopes"] = recipient_scopes
        workitem.payload = payload
        workitem.last_activity_at = now
        workitem.save(
            update_fields=("payload", "last_activity_at", "updated_at")
        )
        synced_workitems += 1

    return {
        "workitems": synced_workitems,
        "tasks_added": added_tasks,
        "tasks_removed": removed_tasks,
    }


@transaction.atomic
def assign_supplier_sales_responsibilities(
    *,
    user,
    sales_identity_id,
    scope_type,
    target_ids,
):
    owner_identity = get_supplier_identity(user, ("supplier_owner",))
    supplier = owner_identity.organization
    try:
        sales_identity_id = int(sales_identity_id)
    except (TypeError, ValueError) as exc:
        raise SupplierResponsibilityError("请选择供应商销售经理。") from exc

    sales_identity = (
        _active_identity_queryset()
        .select_for_update()
        .filter(
            pk=sales_identity_id,
            organization=supplier,
            role__code="supplier_sales",
        )
        .first()
    )
    if not sales_identity:
        raise SupplierResponsibilityError("所选销售经理不属于当前供应商组织。")

    scope_type = str(scope_type or "").strip().upper()
    if scope_type not in {
        SupplierResponsibilityScope.CATEGORY,
        SupplierResponsibilityScope.MATERIAL,
    }:
        raise SupplierResponsibilityError("请选择按品类或按物料分配。")
    target_ids = _normalize_target_ids(target_ids)

    supplied_materials = list(_supplied_materials(supplier))
    supplied_material_ids = {item.pk for item in supplied_materials}
    supplied_category_ids = {
        item.product_category_id
        for item in supplied_materials
        if item.product_category_id
    }
    allowed_ids = (
        supplied_category_ids
        if scope_type == SupplierResponsibilityScope.CATEGORY
        else supplied_material_ids
    )
    if not set(target_ids).issubset(allowed_ids):
        raise SupplierResponsibilityError("只能分配当前组织实际供应的品类或物料。")

    SupplierSalesResponsibility.objects.select_for_update().filter(
        supplier=supplier,
        sales_identity=sales_identity,
    ).delete()

    target_filter = {
        (
            "product_category_id__in"
            if scope_type == SupplierResponsibilityScope.CATEGORY
            else "material_id__in"
        ): target_ids
    }
    if target_ids:
        SupplierSalesResponsibility.objects.select_for_update().filter(
            supplier=supplier,
            scope_type=scope_type,
            **target_filter,
        ).delete()

    rows = []
    for target_id in target_ids:
        row = SupplierSalesResponsibility(
            supplier=supplier,
            sales_identity=sales_identity,
            scope_type=scope_type,
            assigned_by=user,
            product_category_id=(
                target_id
                if scope_type == SupplierResponsibilityScope.CATEGORY
                else None
            ),
            material_id=(
                target_id
                if scope_type == SupplierResponsibilityScope.MATERIAL
                else None
            ),
        )
        row.full_clean()
        rows.append(row)
    SupplierSalesResponsibility.objects.bulk_create(rows)
    result = get_responsibility_options(user=user)
    result["open_task_sync"] = _sync_open_supplier_tasks(supplier)
    return result


def responsibility_map_for_materials(*, supplier, materials):
    """按“具体物料优先、品类兜底”解析每个物料的责任销售身份。"""

    material_rows = list(materials)
    material_ids = {item.pk for item in material_rows}
    category_ids = {
        item.product_category_id
        for item in material_rows
        if item.product_category_id
    }
    assignments = list(
        _responsibility_assignments(supplier).filter(
            (
                Q(
                    scope_type=SupplierResponsibilityScope.MATERIAL,
                    material_id__in=material_ids,
                )
                | Q(
                    scope_type=SupplierResponsibilityScope.CATEGORY,
                    product_category_id__in=category_ids,
                )
            )
        )
    )
    direct = {
        item.material_id: item.sales_identity
        for item in assignments
        if item.scope_type == SupplierResponsibilityScope.MATERIAL
    }
    by_category = {
        item.product_category_id: item.sales_identity
        for item in assignments
        if item.scope_type == SupplierResponsibilityScope.CATEGORY
    }
    return {
        material.pk: (
            direct.get(material.pk)
            or by_category.get(material.product_category_id)
        )
        for material in material_rows
    }


def get_responsible_material_ids(*, sales_identity):
    supplier = sales_identity.organization
    materials = list(_supplied_materials(supplier))
    mapping = responsibility_map_for_materials(
        supplier=supplier,
        materials=materials,
    )
    return {
        material_id
        for material_id, identity in mapping.items()
        if identity and identity.pk == sales_identity.pk
    }


def get_supplier_task_recipients(*, supplier, material_ids):
    materials = list(
        _supplied_materials(supplier).filter(pk__in=set(material_ids or ()))
    )
    mapping = responsibility_map_for_materials(
        supplier=supplier,
        materials=materials,
    )
    sales_identities = {
        identity.pk: identity
        for identity in mapping.values()
        if identity and identity.user.wx_openid
    }
    owner_identities = list(
        _active_identity_queryset()
        .filter(
            organization=supplier,
            role__code="supplier_owner",
            user__wx_openid__isnull=False,
        )
        .exclude(user__wx_openid="")
    )
    owner_openids = [identity.user.wx_openid for identity in owner_identities]
    sales_openids = [
        identity.user.wx_openid
        for identity in sales_identities.values()
        if identity.user.wx_openid
    ]
    all_material_ids = sorted(material.pk for material in materials)
    recipient_scopes = {}
    for identity in owner_identities:
        recipient_scopes[identity.user.wx_openid] = {
            "role": "supplier_owner",
            "identity_id": identity.pk,
            "name": _identity_name(identity),
            "material_ids": all_material_ids,
        }
    for identity in sales_identities.values():
        recipient_scopes[identity.user.wx_openid] = {
            "role": "supplier_sales",
            "identity_id": identity.pk,
            "name": _identity_name(identity),
            "material_ids": sorted(
                material_id
                for material_id, responsible_identity in mapping.items()
                if responsible_identity
                and responsible_identity.pk == identity.pk
            ),
        }
    return {
        "openids": list(dict.fromkeys(owner_openids + sales_openids)),
        "owner_openids": owner_openids,
        "sales_openids": sales_openids,
        "sales_identity_ids": list(sales_identities),
        "recipient_scopes": recipient_scopes,
    }
