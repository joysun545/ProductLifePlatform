from collections import defaultdict

from django.db import transaction
from django.db.models import Max, Prefetch, Q
from django.utils import timezone

from account.models import Organization, UserOrganization
from product.models import BOM, BOMItem, ProductModel
from sales.models import (
    OrderPlan,
    OrderPlanItem,
    OrderPlanItemBOMConfirmation,
)
from supply_chain.models import Material, MaterialSpec, MaterialSpecSupplier
from supply_chain.services.supplier_responsibility_service import (
    get_supplier_task_recipients,
)
from workflow.models import UserTask, WorkItem
from workflow.services.workflow_service import publish_workitem


class OrderPlanMatchingError(Exception):
    """订单配套页面输入或当前身份不满足要求。"""


def _get_matching_identity(user):
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
            organization__organization_type__is_active=True,
            role__is_active=True,
        )
        .first()
    )
    if not identity or identity.role.code != "factory_matching":
        raise OrderPlanMatchingError("只有厂家配套经理可以确认订单BOM")
    if identity.organization.organization_type.code != "OWNER":
        raise OrderPlanMatchingError("当前岗位没有有效的厂家组织身份")
    return identity


def _get_plan_for_identity(identity, plan_id, *, for_update=False):
    try:
        plan_id = int(plan_id)
    except (TypeError, ValueError) as exc:
        raise OrderPlanMatchingError("订单计划参数无效") from exc

    queryset = OrderPlan.objects.select_related(
        "factory_organization",
        "merchant_organization",
        "brand",
        "created_by",
    )
    if for_update:
        queryset = queryset.select_for_update()
    plan = queryset.filter(
        pk=plan_id,
        factory_organization=identity.organization,
    ).first()
    if not plan:
        raise OrderPlanMatchingError("订单计划不存在或不属于当前厂家")
    return plan


def _default_bom_for_item(item):
    previous = (
        OrderPlanItemBOMConfirmation.objects.select_related("confirmed_bom")
        .filter(
            order_plan_item__plan__merchant_organization_id=(
                item.plan.merchant_organization_id
            ),
            order_plan_item__product_model_id=item.product_model_id,
            order_plan_item__plan__status=OrderPlan.Status.MATCHING_COMPLETED,
        )
        .exclude(order_plan_item__plan_id=item.plan_id)
        .order_by("-confirmed_at", "-id")
        .first()
    )
    if previous and previous.confirmed_bom.is_active:
        return previous.confirmed_bom, "previous_order"

    bom = (
        BOM.objects.filter(
            product_model_id=item.product_model_id,
            is_active=True,
        )
        .filter(Q(version_no=1) | Q(version__iexact="v1"))
        .order_by("version_no", "id")
        .first()
    )
    if not bom:
        raise OrderPlanMatchingError(
            f"{item.product_model.name}尚未建立有效的v1版BOM"
        )
    return bom, "v1"


def _serialize_bom_option(bom):
    return {
        "id": bom.pk,
        "version": bom.version,
        "version_no": bom.version_no,
        "remark": bom.remark or "",
        "created_at": bom.created_at.isoformat(),
    }


def _available_boms_for_item(item):
    return list(
        BOM.objects.filter(
            product_model_id=item.product_model_id,
            is_active=True,
        ).order_by("-version_no", "-id")
    )


def _material_queryset(item, bom):
    bom_material_ids = bom.items.values_list("material_id", flat=True)
    supplier_queryset = MaterialSpecSupplier.objects.select_related(
        "supplier",
        "supplier__organization_type",
    ).filter(
        supplier__is_active=True,
        supplier__organization_type__is_active=True,
        supplier__organization_type__code="SUPPLIER",
    ).order_by("supplier__name", "supplier_id")
    spec_queryset = MaterialSpec.objects.prefetch_related(
        Prefetch("suppliers", queryset=supplier_queryset)
    ).order_by("code", "id")
    return (
        Material.objects.filter(
            Q(product_category_id=item.category_id) | Q(pk__in=bom_material_ids)
        )
        .distinct()
        .prefetch_related(Prefetch("specs", queryset=spec_queryset))
        .order_by("code", "id")
    )


def _serialize_materials(item, bom):
    bom_items = {
        bom_item.material_id: bom_item
        for bom_item in bom.items.select_related(
            "material",
            "material_spec",
            "supplier",
        ).all()
    }
    materials = []
    for material in _material_queryset(item, bom):
        bom_item = bom_items.get(material.pk)
        specs = []
        for spec in material.specs.all():
            specs.append(
                {
                    "id": spec.pk,
                    "code": spec.code,
                    "name": spec.name,
                    "suppliers": [
                        {
                            "id": relation.supplier_id,
                            "code": relation.supplier.code,
                            "name": relation.supplier.name,
                        }
                        for relation in spec.suppliers.all()
                    ],
                }
            )
        materials.append(
            {
                "id": material.pk,
                "code": material.code,
                "name": material.name,
                "is_key_part": material.is_key_part,
                "deleted": bom_item is None,
                "selected_spec_id": (
                    bom_item.material_spec_id if bom_item else None
                ),
                "selected_spec_name": (
                    bom_item.material_spec.name
                    if bom_item and bom_item.material_spec
                    else ""
                ),
                "selected_supplier_id": (
                    bom_item.supplier_id if bom_item else None
                ),
                "selected_supplier_name": (
                    bom_item.supplier.name
                    if bom_item and bom_item.supplier
                    else ""
                ),
                "quantity": bom_item.quantity if bom_item else 1,
                "remark": bom_item.remark if bom_item else "",
                "specs": specs,
            }
        )
    return materials


def _serialize_matching_plan(plan):
    confirmations = {
        confirmation.order_plan_item_id: confirmation
        for confirmation in OrderPlanItemBOMConfirmation.objects.select_related(
            "source_bom",
            "confirmed_bom",
            "confirmed_by",
        ).filter(order_plan_item__plan=plan)
    }
    serialized_items = []
    for item in plan.items.select_related("category", "product_model").all():
        confirmation = confirmations.get(item.pk)
        if confirmation:
            default_bom = confirmation.confirmed_bom
            source_kind = "confirmed"
        else:
            default_bom, source_kind = _default_bom_for_item(item)
        available_boms = _available_boms_for_item(item)
        if not any(bom.pk == default_bom.pk for bom in available_boms):
            available_boms.append(default_bom)
        serialized_items.append(
            {
                "id": item.pk,
                "category": {
                    "id": item.category_id,
                    "code": item.category.code,
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
                "default_bom": {
                    "id": default_bom.pk,
                    "version": default_bom.version,
                    "version_no": default_bom.version_no,
                    "source": source_kind,
                    "source_name": {
                        "previous_order": "上一次订单版本",
                        "confirmed": "本订单已确认版本",
                    }.get(source_kind, "v1版"),
                },
                "available_boms": [
                    _serialize_bom_option(bom) for bom in available_boms
                ],
                "confirmation": (
                    {
                        "id": confirmation.pk,
                        "source_bom_id": confirmation.source_bom_id,
                        "confirmed_bom_id": confirmation.confirmed_bom_id,
                        "confirmed_version": confirmation.confirmed_bom.version,
                        "has_changes": confirmation.has_changes,
                        "confirmed_by": (
                            confirmation.confirmed_by.wx_nickname
                            or confirmation.confirmed_by.username
                        ),
                        "confirmed_at": confirmation.confirmed_at.isoformat(),
                    }
                    if confirmation
                    else None
                ),
                "materials": _serialize_materials(item, default_bom),
            }
        )

    return {
        "id": plan.pk,
        "plan_code": plan.plan_code,
        "status": plan.status,
        "status_name": plan.get_status_display(),
        "read_only": plan.status != OrderPlan.Status.MATCHING_PENDING,
        "sales_manager": {
            "id": plan.created_by_id,
            "name": plan.created_by.wx_nickname or plan.created_by.username,
            "phone": plan.created_by.phone or "",
        },
        "merchant": {
            "id": plan.merchant_organization_id,
            "code": plan.merchant_organization.code,
            "name": plan.merchant_organization.name,
        },
        "brand": {
            "id": plan.brand_id,
            "code": plan.brand.code,
            "name": plan.brand.name,
        },
        "region": plan.region_snapshot,
        "total_quantity": plan.total_quantity,
        "remark": plan.remark,
        "submitted_at": plan.submitted_at.isoformat(),
        "items": serialized_items,
    }


def get_order_plan_matching_detail(*, user, plan_id):
    identity = _get_matching_identity(user)
    plan = _get_plan_for_identity(identity, plan_id)
    return _serialize_matching_plan(plan)


def _is_deleted(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _optional_positive_integer(value, label):
    if value in (None, ""):
        return None
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise OrderPlanMatchingError(f"{label}无效") from exc
    if value <= 0:
        raise OrderPlanMatchingError(f"{label}无效")
    return value


def _get_selectable_source_bom(item, bom_id):
    bom_id = _optional_positive_integer(bom_id, "BOM版本")
    if not bom_id:
        raise OrderPlanMatchingError(
            f"请选择{item.product_model.name}的BOM版本"
        )
    bom = BOM.objects.filter(
        pk=bom_id,
        product_model_id=item.product_model_id,
        is_active=True,
    ).first()
    if not bom:
        raise OrderPlanMatchingError(
            f"所选BOM版本不属于{item.product_model.name}或已停用"
        )
    return bom


def get_order_plan_item_bom_detail(*, user, plan_id, item_id, bom_id):
    identity = _get_matching_identity(user)
    plan = _get_plan_for_identity(identity, plan_id)
    item_id = _optional_positive_integer(item_id, "订单产品")
    item = (
        OrderPlanItem.objects.select_related("plan", "category", "product_model")
        .filter(pk=item_id, plan=plan)
        .first()
    )
    if not item:
        raise OrderPlanMatchingError("订单产品不存在或不属于当前订单")
    bom = _get_selectable_source_bom(item, bom_id)
    return {
        "order_plan_item_id": item.pk,
        "bom": _serialize_bom_option(bom),
        "materials": _serialize_materials(item, bom),
    }


def _normalize_item_submission(item, source_bom, raw_materials):
    if not isinstance(raw_materials, list):
        raise OrderPlanMatchingError(f"{item.product_model.name}的物料数据格式无效")

    materials = list(_material_queryset(item, source_bom))
    material_by_id = {material.pk: material for material in materials}
    raw_by_id = {}
    for raw in raw_materials:
        if not isinstance(raw, dict):
            raise OrderPlanMatchingError(f"{item.product_model.name}的物料数据格式无效")
        material_id = _optional_positive_integer(raw.get("material_id"), "物料")
        if material_id in raw_by_id:
            raise OrderPlanMatchingError("同一物料不能重复提交")
        raw_by_id[material_id] = raw

    if set(raw_by_id) != set(material_by_id):
        raise OrderPlanMatchingError(
            f"{item.product_model.name}的物料清单已变化，请刷新页面后重试"
        )

    source_items = {
        bom_item.material_id: bom_item
        for bom_item in source_bom.items.select_related(
            "material_spec",
            "supplier",
        ).all()
    }
    normalized = []
    for material_id, material in material_by_id.items():
        raw = raw_by_id[material_id]
        if _is_deleted(raw.get("deleted")):
            continue

        source_item = source_items.get(material_id)
        spec_id = _optional_positive_integer(
            raw.get("material_spec_id"),
            f"{material.name}的物料规格",
        )
        supplier_id = _optional_positive_integer(
            raw.get("supplier_id"),
            f"{material.name}的供应商",
        )
        source_spec_id = source_item.material_spec_id if source_item else None
        source_supplier_id = source_item.supplier_id if source_item else None
        selection_changed = (
            source_item is None
            or spec_id != source_spec_id
            or supplier_id != source_supplier_id
        )

        if selection_changed:
            if not spec_id or not supplier_id:
                raise OrderPlanMatchingError(
                    f"请为{material.name}选择物料规格和供应商"
                )
            if not MaterialSpec.objects.filter(
                pk=spec_id,
                material_id=material_id,
            ).exists():
                raise OrderPlanMatchingError(f"{material.name}的物料规格不匹配")
            if not MaterialSpecSupplier.objects.filter(
                material_spec_id=spec_id,
                supplier_id=supplier_id,
                supplier__is_active=True,
                supplier__organization_type__code="SUPPLIER",
                supplier__organization_type__is_active=True,
            ).exists():
                raise OrderPlanMatchingError(
                    f"{material.name}所选供应商没有供应该规格的有效关系"
                )

        normalized.append(
            {
                "material_id": material_id,
                "material_spec_id": spec_id,
                "supplier_id": supplier_id,
                "quantity": source_item.quantity if source_item else 1,
                "remark": str(raw.get("remark") or ""),
            }
        )

    if not normalized:
        raise OrderPlanMatchingError(f"{item.product_model.name}不能删除全部物料")

    def signature(rows):
        return sorted(
            (
                row["material_id"],
                row["material_spec_id"],
                row["supplier_id"],
                float(row["quantity"]),
                row["remark"],
            )
            for row in rows
        )

    source_rows = [
        {
            "material_id": source_item.material_id,
            "material_spec_id": source_item.material_spec_id,
            "supplier_id": source_item.supplier_id,
            "quantity": source_item.quantity,
            "remark": source_item.remark or "",
        }
        for source_item in source_items.values()
    ]
    return normalized, signature(normalized) != signature(source_rows)


def _create_bom_version(*, item, source_bom, rows, user):
    ProductModel.objects.select_for_update().get(pk=item.product_model_id)
    maximum = (
        BOM.objects.filter(product_model_id=item.product_model_id)
        .aggregate(value=Max("version_no"))
        .get("value")
        or 0
    )
    version_no = maximum + 1
    bom = BOM.objects.create(
        product_model_id=item.product_model_id,
        version_no=version_no,
        version=f"v{version_no}",
        remark=(
            f"订单计划{item.plan.plan_code}配套确认；"
            f"继承自{source_bom.version}"
        ),
        is_active=True,
        created_by=user,
    )
    BOMItem.objects.bulk_create(
        [
            BOMItem(
                bom=bom,
                material_id=row["material_id"],
                material_spec_id=row["material_spec_id"],
                supplier_id=row["supplier_id"],
                quantity=row["quantity"],
                remark=row["remark"],
            )
            for row in rows
        ]
    )
    return bom


def _role_openids(organization, role_codes):
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
    )


def _complete_matching_workitem(plan, actor_openid):
    now = timezone.now()
    workitem = (
        WorkItem.objects.select_for_update()
        .filter(uniq_key=f"sales:order-plan:{plan.pk}:matching")
        .first()
    )
    if not workitem:
        return
    workitem.status = "DONE"
    workitem.done_by_openid = actor_openid
    workitem.done_at = now
    workitem.last_activity_at = now
    workitem.save(
        update_fields=(
            "status",
            "done_by_openid",
            "done_at",
            "last_activity_at",
            "updated_at",
        )
    )
    workitem.user_tasks.filter(to_openid=actor_openid).update(
        state="DONE",
        done_at=now,
        done_reason="SELF_ACTION",
        cursor_time=now,
        updated_at=now,
    )
    workitem.user_tasks.exclude(to_openid=actor_openid).update(
        state="DONE",
        done_at=now,
        done_reason="OTHER_ACTION",
        cursor_time=now,
        updated_at=now,
    )


def _publish_followup_tasks(plan, confirmed_pairs, actor_openid):
    supplier_materials = defaultdict(list)
    for item, bom in confirmed_pairs:
        for bom_item in bom.items.select_related(
            "material",
            "material_spec",
            "supplier",
        ).exclude(supplier__isnull=True):
            supplier_materials[bom_item.supplier_id].append(
                {
                    "order_plan_item_id": item.pk,
                    "order_batch_code": item.order_batch_code,
                    "product_model_id": item.product_model_id,
                    "product_model_name": item.product_model.name,
                    "bom_id": bom.pk,
                    "bom_version": bom.version,
                    "material_id": bom_item.material_id,
                    "material_name": bom_item.material.name,
                    "material_spec_id": bom_item.material_spec_id,
                    "material_spec_name": (
                        bom_item.material_spec.name if bom_item.material_spec else ""
                    ),
                    "quantity_per_device": bom_item.quantity,
                    "order_quantity": item.quantity,
                    "required_quantity": bom_item.quantity * item.quantity,
                }
            )

    dispatched = {
        "supplier_organizations": 0,
        "supplier_accounts": 0,
        "supplier_owner_accounts": 0,
        "responsible_supplier_sales_accounts": 0,
        "factory_production_accounts": 0,
        "sales_manager_accounts": 0,
    }
    suppliers = {
        supplier.pk: supplier
        for supplier in Organization.objects.filter(pk__in=supplier_materials)
    }
    for supplier_id, materials in supplier_materials.items():
        supplier = suppliers[supplier_id]
        recipient_data = get_supplier_task_recipients(
            supplier=supplier,
            material_ids={item["material_id"] for item in materials},
        )
        recipients = recipient_data["openids"]
        if not recipients:
            continue
        recipient_scopes = recipient_data["recipient_scopes"]
        workitem = publish_workitem(
            domain="SALES",
            type="ORDER_PLAN_SUPPLIER_PREPARE",
            biz_id=f"{plan.plan_code}:{supplier_id}",
            biz_ref=f"sales.OrderPlan:{plan.pk}",
            uniq_key=f"sales:order-plan:{plan.pk}:supplier:{supplier_id}",
            title="新订单物料待供应确认",
            summary=(
                f"{plan.merchant_organization.name}｜{plan.brand.name}｜"
                f"{len(materials)}项供应物料"
            ),
            link="/pages/sales/order_plan_list/order_plan_list",
            recipients_openids=recipients,
            need_action=True,
            action_mode="OWNER_OR_SALES",
            payload={
                "order_plan_id": plan.pk,
                "plan_code": plan.plan_code,
                "supplier_id": supplier_id,
                "supplier_name": supplier.name,
                "merchant_name": plan.merchant_organization.name,
                "brand_name": plan.brand.name,
                "completion_policy": "OWNER_OR_ALL_SALES",
                "owner_openids": recipient_data["owner_openids"],
                "responsible_sales_identity_ids": recipient_data[
                    "sales_identity_ids"
                ],
                "recipient_scopes": recipient_scopes,
                "processed_actors": [],
                "materials": materials,
            },
            actor_openid=actor_openid,
            mark_workitem_done_immediately=False,
        )
        for task in UserTask.objects.filter(workitem=workitem):
            scope = recipient_scopes.get(task.to_openid, {})
            scope_material_ids = {
                int(value) for value in scope.get("material_ids", [])
            }
            line_count = sum(
                1
                for item in materials
                if int(item["material_id"]) in scope_material_ids
            )
            task.to_role_snapshot = scope.get(
                "role",
                task.to_role_snapshot,
            )
            task.display_subtitle_snapshot = (
                f"{plan.merchant_organization.name}｜{plan.brand.name}｜"
                f"{line_count}项供应物料"
            )
            task.save(
                update_fields=(
                    "to_role_snapshot",
                    "display_subtitle_snapshot",
                    "updated_at",
                )
            )
        dispatched["supplier_organizations"] += 1
        dispatched["supplier_accounts"] += len(set(recipients))
        dispatched["supplier_owner_accounts"] += len(
            set(recipient_data["owner_openids"])
        )
        dispatched["responsible_supplier_sales_accounts"] += len(
            set(recipient_data["sales_openids"])
        )

    sales_openid = plan.created_by.wx_openid or ""
    if sales_openid:
        publish_workitem(
            domain="SALES",
            type="ORDER_PLAN_MATCHING_COMPLETED",
            biz_id=plan.plan_code,
            biz_ref=f"sales.OrderPlan:{plan.pk}",
            uniq_key=f"sales:order-plan:{plan.pk}:sales-notice",
            title="订单计划物料配套已确认",
            summary=(
                f"{plan.merchant_organization.name}｜{plan.brand.name}｜"
                f"共{plan.total_quantity}台"
            ),
            link="",
            recipients_openids=(sales_openid,),
            need_action=False,
            action_mode="EACH_ACK",
            payload={
                "order_plan_id": plan.pk,
                "plan_code": plan.plan_code,
            },
            actor_openid=actor_openid,
            mark_workitem_done_immediately=True,
        )
        dispatched["sales_manager_accounts"] = 1
    return dispatched


@transaction.atomic
def confirm_order_plan_matching(*, user, plan_id, items):
    identity = _get_matching_identity(user)
    plan = _get_plan_for_identity(identity, plan_id, for_update=True)
    if plan.status == OrderPlan.Status.CANCELLED:
        raise OrderPlanMatchingError("该订单计划已取消")
    if plan.status == OrderPlan.Status.MATCHING_COMPLETED:
        return _serialize_matching_plan(plan), False, {}
    if not isinstance(items, list) or not items:
        raise OrderPlanMatchingError("请提交全部产品型号的BOM清单")

    plan_items = list(
        OrderPlanItem.objects.select_for_update()
        .select_related("plan", "category", "product_model")
        .filter(plan=plan)
        .order_by("id")
    )
    raw_by_id = {}
    for raw in items:
        if not isinstance(raw, dict):
            raise OrderPlanMatchingError("订单产品BOM数据格式无效")
        item_id = _optional_positive_integer(
            raw.get("order_plan_item_id"),
            "订单产品",
        )
        if item_id in raw_by_id:
            raise OrderPlanMatchingError("同一订单产品不能重复提交")
        raw_by_id[item_id] = raw
    if set(raw_by_id) != {item.pk for item in plan_items}:
        raise OrderPlanMatchingError("必须一次确认该订单的全部产品型号")
    if OrderPlanItemBOMConfirmation.objects.filter(
        order_plan_item__plan=plan
    ).exists():
        raise OrderPlanMatchingError("订单配套数据状态异常，请联系管理员")

    normalized_items = []
    for item in plan_items:
        submitted_bom_id = raw_by_id[item.pk].get("source_bom_id")
        if submitted_bom_id in (None, ""):
            source_bom, _source_kind = _default_bom_for_item(item)
        else:
            source_bom = _get_selectable_source_bom(item, submitted_bom_id)
        rows, changed = _normalize_item_submission(
            item,
            source_bom,
            raw_by_id[item.pk].get("materials"),
        )
        normalized_items.append((item, source_bom, rows, changed))

    confirmed_pairs = []
    for item, source_bom, rows, changed in normalized_items:
        confirmed_bom = (
            _create_bom_version(
                item=item,
                source_bom=source_bom,
                rows=rows,
                user=user,
            )
            if changed
            else source_bom
        )
        confirmation = OrderPlanItemBOMConfirmation(
            order_plan_item=item,
            source_bom=source_bom,
            confirmed_bom=confirmed_bom,
            confirmed_by=user,
            has_changes=changed,
        )
        confirmation.full_clean()
        confirmation.save()
        item.status = OrderPlan.Status.MATCHING_COMPLETED
        item.save(update_fields=("status", "updated_at"))
        confirmed_pairs.append((item, confirmed_bom))

    plan.status = OrderPlan.Status.MATCHING_COMPLETED
    plan.save(update_fields=("status", "updated_at"))
    actor_openid = user.wx_openid or ""
    _complete_matching_workitem(plan, actor_openid)
    dispatched = _publish_followup_tasks(plan, confirmed_pairs, actor_openid)
    return _serialize_matching_plan(plan), True, dispatched
