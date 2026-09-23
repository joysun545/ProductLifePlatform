from django.db import transaction
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
        .prefetch_related("product_categories")
        .distinct()
        .order_by("code", "id")
    )


def _responsibility_assignments(supplier):
    return SupplierSalesResponsibility.objects.select_related(
        "sales_identity",
        "sales_identity__user",
        "material",
    ).filter(
        supplier=supplier,
        is_active=True,
        sales_identity__is_active=True,
        sales_identity__role__code="supplier_sales",
        sales_identity__role__is_active=True,
        sales_identity__user__is_active=True,
    )


def _supplier_sales_identities(supplier):
    return list(
        _active_identity_queryset()
        .filter(
            organization=supplier,
            role__code="supplier_sales",
        )
        .order_by("user__wx_nickname", "user__username", "id")
    )


def get_responsibility_options(*, user):
    owner_identity = get_supplier_identity(user, ("supplier_owner",))
    supplier = owner_identity.organization
    sales_identities = _supplier_sales_identities(supplier)
    materials = list(_supplied_materials(supplier))
    assignment_by_material = {
        item.material_id: item
        for item in _responsibility_assignments(supplier)
        if item.scope_type == SupplierResponsibilityScope.MATERIAL
    }

    if not sales_identities:
        assignment_mode = "OWNER_ONLY"
        rule_message = "当前组织仅有供应商负责人账号，负责人负责全部物料。"
    elif len(sales_identities) == 1:
        assignment_mode = "SINGLE_SALES_DEFAULT_ALL"
        rule_message = "当前组织只有一名供应商销售经理，默认负责全部物料，无需逐项分配。"
    else:
        assignment_mode = "MULTI_SALES_BY_MATERIAL"
        rule_message = "当前组织有多名供应商销售经理，请按物料类型指定唯一负责销售经理。"

    managers = []
    for identity in sales_identities:
        own_assignments = [
            item for item in assignment_by_material.values()
            if item.sales_identity_id == identity.pk
        ]
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
                "default_all_materials": (
                    assignment_mode == "SINGLE_SALES_DEFAULT_ALL"
                ),
                "target_ids": [item.material_id for item in own_assignments],
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

    def category_data(material):
        categories = list(material.product_categories.all())
        return {
            "category_ids": [category.pk for category in categories],
            "category_names": [category.name for category in categories],
        }

    return {
        "supplier": {
            "id": supplier.pk,
            "name": supplier.name,
        },
        "assignment_mode": assignment_mode,
        "rule_message": rule_message,
        "sales_managers": managers,
        "materials": [
            {
                "id": material.pk,
                "code": material.code,
                "name": material.name,
                "category_id": (category_data(material)["category_ids"] or [None])[0],
                "category_name": "、".join(category_data(material)["category_names"]) or "未分类",
                **category_data(material),
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
    if scope_type != SupplierResponsibilityScope.MATERIAL:
        raise SupplierResponsibilityError("供应商销售责任只能按物料类型分配。")
    if len(_supplier_sales_identities(supplier)) < 2:
        raise SupplierResponsibilityError("仅有一名供应商销售经理时默认负责全部物料，无需手工分配。")
    target_ids = _normalize_target_ids(target_ids)

    supplied_materials = list(_supplied_materials(supplier))
    supplied_material_ids = {item.pk for item in supplied_materials}
    if not set(target_ids).issubset(supplied_material_ids):
        raise SupplierResponsibilityError("只能分配当前组织实际供应的物料。")

    SupplierSalesResponsibility.objects.select_for_update().filter(
        supplier=supplier,
        sales_identity=sales_identity,
    ).delete()

    if target_ids:
        SupplierSalesResponsibility.objects.select_for_update().filter(
            supplier=supplier,
            scope_type=SupplierResponsibilityScope.MATERIAL,
            material_id__in=target_ids,
        ).delete()

    rows = []
    for target_id in target_ids:
        row = SupplierSalesResponsibility(
            supplier=supplier,
            sales_identity=sales_identity,
            scope_type=SupplierResponsibilityScope.MATERIAL,
            assigned_by=user,
            material_id=target_id,
        )
        row.full_clean()
        rows.append(row)
    SupplierSalesResponsibility.objects.bulk_create(rows)
    result = get_responsibility_options(user=user)
    result["open_task_sync"] = _sync_open_supplier_tasks(supplier)
    return result


def responsibility_map_for_materials(*, supplier, materials):
    """按组织销售人数解析物料责任人。

    无销售经理：负责人独自办理；一名销售经理：默认负责全部物料；
    多名销售经理：只认负责人按物料类型作出的明确分配。
    """

    material_rows = list(materials)
    sales_identities = _supplier_sales_identities(supplier)
    if len(sales_identities) == 1:
        return {material.pk: sales_identities[0] for material in material_rows}
    if not sales_identities:
        return {material.pk: None for material in material_rows}

    material_ids = {item.pk for item in material_rows}
    assignments = list(
        _responsibility_assignments(supplier).filter(
            scope_type=SupplierResponsibilityScope.MATERIAL,
            material_id__in=material_ids,
        )
    )
    direct = {
        item.material_id: item.sales_identity
        for item in assignments
        if item.scope_type == SupplierResponsibilityScope.MATERIAL
    }
    return {
        material.pk: direct.get(material.pk)
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


def get_v2_supplier_delivery_snapshot_access(
    *,
    supplier,
    sales_identity,
    plan_id=None,
):
    """Return a supplier-sales user's immutable V2 delivery-task scope.

    Responsibility assignment is deliberately a *current* configuration, but
    an ``ORDER_PLAN_SUPPLIER_DELIVERY`` task is an audited operational fact.
    Once the 10:00/22:00 dispatcher has created that task, changing a material
    assignment must neither remove the original sales recipient nor let a new
    sales recipient take over the old task.  The task's ``UserTask`` recipient
    and the ``recipient_scopes`` stored in that work item are therefore read
    as a snapshot here; this helper never updates either of them.

    ``open_plan_ids`` is intentionally returned for *all* open V2 delivery
    tasks of the supplier, even when the current sales identity was not a
    recipient.  Callers use it to prevent a newly assigned sales manager from
    seeing an open historical task merely through the live responsibility
    table.  ``*_material_ids_by_plan`` only contains exact supplier-sales
    task recipients and is suitable for scoped detail/list projections.
    """

    empty = {
        "open_plan_ids": set(),
        "open_material_ids_by_plan": {},
        "history_material_ids_by_plan": {},
    }
    if not supplier or not sales_identity:
        return empty
    if getattr(getattr(sales_identity, "role", None), "code", "") != "supplier_sales":
        return empty
    if sales_identity.organization_id != supplier.pk:
        return empty
    openid = (sales_identity.user.wx_openid or "").strip()
    if not openid:
        return empty

    workitem_filters = {
        "type": "ORDER_PLAN_SUPPLIER_DELIVERY",
        "status__in": ("OPEN", "DONE"),
    }
    if plan_id not in (None, ""):
        try:
            workitem_filters["biz_ref"] = f"sales.OrderPlan:{int(plan_id)}"
        except (TypeError, ValueError):
            return empty

    workitems = list(WorkItem.objects.filter(**workitem_filters))
    if not workitems:
        return empty
    recipient_workitem_ids = set(
        UserTask.objects.filter(
            workitem_id__in=[item.pk for item in workitems],
            to_openid=openid,
            to_role_snapshot="supplier_sales",
        ).values_list("workitem_id", flat=True)
    )

    def _payload_int(payload, key):
        try:
            return int(payload.get(key))
        except (AttributeError, TypeError, ValueError):
            return None

    def _scope_material_ids(scope):
        if not isinstance(scope, dict):
            return set()
        values = set()
        for raw in scope.get("material_ids", []) or []:
            try:
                material_id = int(raw)
            except (TypeError, ValueError):
                continue
            if material_id > 0:
                values.add(material_id)
        return values

    for workitem in workitems:
        payload = workitem.payload if isinstance(workitem.payload, dict) else {}
        if _payload_int(payload, "supplier_id") != supplier.pk:
            continue
        payload_plan_id = _payload_int(payload, "order_plan_id")
        if not payload_plan_id:
            continue
        if workitem.status == "OPEN":
            empty["open_plan_ids"].add(payload_plan_id)
        if workitem.pk not in recipient_workitem_ids:
            continue
        scopes = payload.get("recipient_scopes") or {}
        scope = scopes.get(openid) if isinstance(scopes, dict) else None
        # Both snapshots must say supplier_sales.  Do not infer permission
        # from a current identity or from a recipient of another role.
        if not isinstance(scope, dict) or scope.get("role") != "supplier_sales":
            continue
        material_ids = _scope_material_ids(scope)
        if not material_ids:
            continue
        empty["history_material_ids_by_plan"].setdefault(
            payload_plan_id, set()
        ).update(material_ids)
        if workitem.status == "OPEN":
            empty["open_material_ids_by_plan"].setdefault(
                payload_plan_id, set()
            ).update(material_ids)
    return empty


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
        # A supplier owner may always close a delivery.  Sales can close it
        # collectively only when every material line has an explicit,
        # task-reachable responsible sales identity.  A configured identity
        # without a live WeChat openid cannot receive this immutable task, so
        # it must be treated like an owner-required line rather than allowing
        # some other sales account to complete the full delivery alone.
        # Keep the set in the task snapshot so a later responsibility
        # reconfiguration cannot silently change the completion rule for an
        # already-dispatched delivery.
        "unassigned_material_ids": sorted(
            material_id
            for material_id, identity in mapping.items()
            if identity is None or not (identity.user.wx_openid or "").strip()
        ),
        "recipient_scopes": recipient_scopes,
    }
