import re
import uuid

from django.db import transaction
from django.utils import timezone

from account.models import (
    InvitationRecord,
    OrganizationRelation,
    OrganizationRelationType,
    UserOrganization,
)
from channel.models import MerchantChannelAssignment
from lifecycle.utils.id_generator import gen_life_id
from product.models import BOM, Brand, ProductModel
from sales.models import OrderPlan, OrderPlanItem
from sales.services.order_finance_service import initialize_order_finance
from sales.services.order_plan_reissue_service import (
    get_return_inventory_options,
    mark_return_inventory_ready_to_ship,
    reserve_return_inventory,
    serialize_item_reissue,
)
from sales.services.order_qrcode_print_service import (
    publish_stock_outbound_preparation_task,
)
from workflow.services.workflow_service import publish_workitem


class OrderPlanError(Exception):
    """订单计划输入或业务身份不满足要求。"""


def _get_sales_identity(user):
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
    if not identity or identity.role.code != "factory_sales":
        raise OrderPlanError("只有厂家销售经理可以创建订单计划")
    if identity.organization.organization_type.code != "OWNER":
        raise OrderPlanError("当前岗位没有有效的厂家组织身份")
    return identity


def _direct_merchant_relations(user, factory_organization):
    """只返回当前销售经理本人邀请建立的直接经销关系。"""

    return (
        OrganizationRelation.objects.select_related(
            "target_organization",
            "target_organization__organization_type",
            "created_by_invitation",
        )
        .filter(
            source_organization=factory_organization,
            target_organization__is_active=True,
            target_organization__organization_type__code="MERCHANT",
            target_organization__organization_type__is_active=True,
            relation_type=OrganizationRelationType.DISTRIBUTION,
            is_active=True,
            created_by_invitation__inviter=user,
            created_by_invitation__status=InvitationRecord.Status.ACCEPTED,
            created_by_invitation__target_role__code="merchant_owner",
        )
        .order_by("target_organization__name", "id")
    )


def _get_direct_relation(user, identity, merchant_id):
    try:
        merchant_id = int(merchant_id)
    except (TypeError, ValueError) as exc:
        raise OrderPlanError("请选择有效的直接商家") from exc

    relation = _direct_merchant_relations(
        user,
        identity.organization,
    ).filter(target_organization_id=merchant_id).first()
    if not relation:
        raise OrderPlanError("所选商家不是您本人邀请的直接商家")
    return relation


def _serialize_product_catalog():
    """订单目录独立于品牌，只包含已经建立有效 v1 BOM 的产品型号。"""

    categories = {}
    valid_models = (
        ProductModel.objects.select_related("category")
        .filter(
            boms__version="v1",
            boms__is_active=True,
        )
        .distinct()
        .order_by("category__code", "code", "id")
    )
    for product_model in valid_models:
        category = product_model.category
        item = categories.setdefault(
            category.pk,
            {
                "id": category.pk,
                "code": category.code,
                "name": category.name,
                "models": [],
            },
        )
        item["models"].append(
            {
                "id": product_model.pk,
                "code": product_model.code,
                "name": product_model.name,
            }
        )
    return list(categories.values())


def _serialize_brands(product_catalog):
    """品牌只用于渠道授权；categories 暂为旧版小程序保留兼容。"""

    return [
        {
            "id": brand.pk,
            "code": brand.code,
            "name": brand.name,
            "categories": product_catalog,
        }
        for brand in Brand.objects.order_by("code", "id")
    ]


def get_order_plan_options(user):
    identity = _get_sales_identity(user)
    product_catalog = _serialize_product_catalog()
    relations = list(_direct_merchant_relations(user, identity.organization))
    assignments = {
        assignment.relation_id: assignment
        for assignment in MerchantChannelAssignment.objects.select_related("brand")
        .filter(relation_id__in=[relation.pk for relation in relations])
    }

    merchants = []
    for relation in relations:
        assignment = assignments.get(relation.pk)
        merchant = relation.target_organization
        merchants.append(
            {
                "id": merchant.pk,
                "code": merchant.code,
                "name": merchant.name,
                "relation_id": relation.pk,
                "configured": bool(assignment and assignment.is_active),
                "region": (
                    assignment.region
                    if assignment
                    else merchant.region or ""
                ),
                "brand": (
                    {
                        "id": assignment.brand_id,
                        "code": assignment.brand.code,
                        "name": assignment.brand.name,
                    }
                    if assignment
                    else None
                ),
            }
        )

    return {
        "factory": {
            "id": identity.organization_id,
            "code": identity.organization.code,
            "name": identity.organization.name,
        },
        "direct_merchants": merchants,
        "brands": _serialize_brands(product_catalog),
        "categories": product_catalog,
    }


def get_order_plan_reissue_options(*, user, merchant_id, product_model_id):
    identity = _get_sales_identity(user)
    relation = _get_direct_relation(user, identity, merchant_id)
    try:
        product_model_id = int(product_model_id)
    except (TypeError, ValueError) as exc:
        raise OrderPlanError("请选择有效的产品型号") from exc
    product_model = ProductModel.objects.filter(pk=product_model_id).first()
    if not product_model:
        raise OrderPlanError("产品型号不存在")
    return get_return_inventory_options(
        factory_organization=identity.organization,
        merchant_organization=relation.target_organization,
        product_model=product_model,
    )


@transaction.atomic
def configure_direct_merchant(*, user, merchant_id, brand_id, region):
    identity = _get_sales_identity(user)
    relation = _get_direct_relation(user, identity, merchant_id)

    region = str(region or "").strip()
    if not region:
        raise OrderPlanError("请填写商家的负责区域")
    if len(region) > 255:
        raise OrderPlanError("负责区域不能超过255个字符")

    try:
        brand_id = int(brand_id)
    except (TypeError, ValueError) as exc:
        raise OrderPlanError("请选择有效品牌") from exc

    brand = Brand.objects.filter(pk=brand_id).first()
    if not brand:
        raise OrderPlanError("请选择有效品牌")

    assignment, _created = MerchantChannelAssignment.objects.update_or_create(
        relation=relation,
        defaults={
            "brand": brand,
            "region": region,
            "assigned_by": user,
            "is_active": True,
        },
    )
    assignment.full_clean()
    assignment.save()

    merchant = relation.target_organization
    if merchant.region != region:
        merchant.region = region
        merchant.save(update_fields=("region", "updated_at"))

    return {
        "merchant_id": merchant.pk,
        "relation_id": relation.pk,
        "configured": True,
        "region": assignment.region,
        "brand": {
            "id": brand.pk,
            "code": brand.code,
            "name": brand.name,
        },
    }


def _positive_integer(value, label):
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise OrderPlanError(f"{label}必须是正整数") from exc
    if value <= 0:
        raise OrderPlanError(f"{label}必须大于0")
    return value


def _validated_items(raw_items):
    if not isinstance(raw_items, list) or not raw_items:
        raise OrderPlanError("请至少添加一个产品型号")
    if len(raw_items) > 50:
        raise OrderPlanError("单个订单计划最多包含50个产品型号")

    parsed = []
    model_ids = set()
    for index, raw_item in enumerate(raw_items, start=1):
        if not isinstance(raw_item, dict):
            raise OrderPlanError(f"第{index}项产品数据格式不正确")
        category_id = _positive_integer(raw_item.get("category_id"), f"第{index}项品类")
        model_id = _positive_integer(raw_item.get("product_model_id"), f"第{index}项型号")
        quantity = _positive_integer(raw_item.get("quantity"), f"第{index}项数量")
        if model_id in model_ids:
            raise OrderPlanError("同一个产品型号不能重复添加")
        model_ids.add(model_id)
        use_return_inventory = raw_item.get("use_return_inventory", False)
        if not isinstance(use_return_inventory, bool):
            raise OrderPlanError(f"第{index}项退货库存选择格式不正确")
        parsed.append(
            (index, category_id, model_id, quantity, use_return_inventory)
        )

    product_models = {
        product_model.pk: product_model
        for product_model in ProductModel.objects.select_related("category")
        .filter(pk__in=model_ids)
    }

    result = []
    for index, category_id, model_id, quantity, use_return_inventory in parsed:
        product_model = product_models.get(model_id)
        if not product_model:
            raise OrderPlanError(f"第{index}项产品型号不存在")
        if product_model.category_id != category_id:
            raise OrderPlanError(f"第{index}项产品型号与品类不匹配")
        if not BOM.objects.filter(
            product_model=product_model,
            version="v1",
            is_active=True,
        ).exists():
            raise OrderPlanError(f"{product_model.name}尚未建立有效的v1版BOM")
        result.append(
            (product_model.category, product_model, quantity, use_return_inventory)
        )
    return result


def _matching_openids(factory_organization):
    return list(
        UserOrganization.objects.filter(
            organization=factory_organization,
            role__code="factory_matching",
            role__is_active=True,
            is_active=True,
            user__is_active=True,
            user__wx_openid__isnull=False,
        )
        .exclude(user__wx_openid="")
        .values_list("user__wx_openid", flat=True)
    )


def _production_openids(factory_organization):
    return list(
        UserOrganization.objects.filter(
            organization=factory_organization,
            role__code="factory_production",
            role__is_active=True,
            is_active=True,
            user__is_active=True,
            user__wx_openid__isnull=False,
        )
        .exclude(user__wx_openid="")
        .values_list("user__wx_openid", flat=True)
    )


def _new_code(prefix, model_code=""):
    timestamp = timezone.now().strftime("%Y%m%d%H%M%S")
    normalized = re.sub(r"[^A-Za-z0-9]+", "-", model_code).strip("-").upper()
    middle = f"-{normalized[:18]}" if normalized else ""
    return f"{prefix}-{timestamp}{middle}-{uuid.uuid4().hex[:6].upper()}"


def _serialize_plan(plan):
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
            "code": plan.brand.code,
            "name": plan.brand.name,
        },
        "region": plan.region_snapshot,
        "total_quantity": plan.total_quantity,
        "remark": plan.remark,
        "submitted_at": plan.submitted_at.isoformat(),
        "items": [
            dict({
                "id": item.pk,
                "category_id": item.category_id,
                "category_name": item.category.name,
                "product_model_id": item.product_model_id,
                "product_model_code": item.product_model.code,
                "product_model_name": item.product_model.name,
                "quantity": item.quantity,
                "order_batch_code": item.order_batch_code,
                "lifecycle_code": item.lifecycle_code,
                "status": item.status,
            }, **serialize_item_reissue(item))
            for item in plan.items.select_related("category", "product_model").all()
        ],
    }


@transaction.atomic
def create_order_plan(
    *,
    user,
    merchant_id,
    client_request_id,
    items,
    remark="",
):
    identity = _get_sales_identity(user)
    relation = _get_direct_relation(user, identity, merchant_id)

    client_request_id = str(client_request_id or "").strip()
    if not client_request_id:
        raise OrderPlanError("客户端请求标识缺失，请刷新页面后重试")
    if len(client_request_id) > 64:
        raise OrderPlanError("客户端请求标识长度不正确")

    existing = (
        OrderPlan.objects.select_related(
            "factory_organization",
            "merchant_organization",
            "brand",
        )
        .filter(created_by=user, client_request_id=client_request_id)
        .first()
    )
    if existing:
        return _serialize_plan(existing), False

    assignment = (
        MerchantChannelAssignment.objects.select_for_update()
        .select_related("brand")
        .filter(
            relation=relation,
            assigned_by=user,
            is_active=True,
        )
        .first()
    )
    if not assignment:
        raise OrderPlanError("请先为该直接商家指定负责区域和品牌")

    validated_items = _validated_items(items)
    total_quantity = sum(item[2] for item in validated_items)
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
        total_quantity=total_quantity,
        remark=str(remark or "").strip()[:2000],
    )

    created_items = []
    for category, product_model, quantity, use_return_inventory in validated_items:
        order_item = OrderPlanItem.objects.create(
            plan=plan,
            category=category,
            product_model=product_model,
            quantity=quantity,
            return_inventory_quantity=0,
            production_quantity=quantity,
            order_batch_code=_new_code("OB", product_model.code),
            lifecycle_code=gen_life_id(),
        )
        allocations = reserve_return_inventory(
            order_plan_item=order_item,
            requested_quantity=quantity if use_return_inventory else 0,
            actor=user,
        )
        order_item.return_inventory_quantity = len(allocations)
        order_item.production_quantity = quantity - len(allocations)
        order_item.save(
            update_fields=(
                "return_inventory_quantity",
                "production_quantity",
                "updated_at",
            )
        )
        created_items.append(order_item)

    production_total = sum(item.production_quantity for item in created_items)
    return_inventory_total = sum(
        item.return_inventory_quantity for item in created_items
    )

    model_summary = "、".join(
        (
            f"{item.product_model.name}×{item.quantity}"
            f"（退货库存{item.return_inventory_quantity}，"
            f"新生产{item.production_quantity}）"
        )
        for item in created_items
    )
    if production_total:
        production_openids = _production_openids(identity.organization)
        if not production_openids:
            raise OrderPlanError("厂家尚未配置有效生产经理，暂时无法提交订单")
        matching_openids = _matching_openids(identity.organization)
        if not matching_openids:
            raise OrderPlanError("厂家尚未配置有效的配套经理账号，暂时无法提交")
        for item in created_items:
            if item.production_quantity == 0:
                item.status = OrderPlan.Status.MATCHING_COMPLETED
                item.save(update_fields=("status", "updated_at"))
        publish_workitem(
            domain="SALES",
            type="ORDER_PLAN_MATCHING",
            biz_id=plan.plan_code,
            biz_ref=f"sales.OrderPlan:{plan.pk}",
            uniq_key=f"sales:order-plan:{plan.pk}:matching",
            title="新订单计划待配套",
            summary=(
                f"{relation.target_organization.name}｜{assignment.region}｜"
                f"{assignment.brand.name}｜{model_summary}"
            ),
            link=(
                "/pages/sales/matching_confirm/matching_confirm"
                f"?order_plan_id={plan.pk}"
            ),
            recipients_openids=matching_openids,
            need_action=True,
            action_mode="ANYONE_DO",
            payload={
                "order_plan_id": plan.pk,
                "plan_code": plan.plan_code,
                "merchant_id": relation.target_organization_id,
                "merchant_name": relation.target_organization.name,
                "brand_id": assignment.brand_id,
                "brand_name": assignment.brand.name,
                "region": assignment.region,
                "total_quantity": total_quantity,
                "return_inventory_quantity": return_inventory_total,
                "production_quantity": production_total,
            },
            actor_openid=user.wx_openid or "",
            mark_workitem_done_immediately=False,
        )
    else:
        # 整单全部由已完成退货处置的待流转库存满足时，不存在
        # 新的配套和生产事实。创建订单的销售经理已经明确了本次
        # 重新流转，因此在同一事务内完成发货确认，直接进入新
        # 二维码打印与库房发货阶段。
        now = timezone.now()
        plan.status = OrderPlan.Status.PRODUCTION_COMPLETED
        plan.shipment_authorized_at = now
        plan.shipment_authorized_by = user
        plan.save(
            update_fields=(
                "status",
                "shipment_authorized_at",
                "shipment_authorized_by",
                "updated_at",
            )
        )
        plan.items.update(status=OrderPlan.Status.PRODUCTION_COMPLETED)
        mark_return_inventory_ready_to_ship(plan=plan)
        publish_stock_outbound_preparation_task(plan=plan)

    creator_name = (
        user.wx_nickname
        or user.get_full_name()
        or user.username
    )
    if production_total:
        publish_workitem(
            domain="SALES",
            type="ORDER_PLAN_CREATED_PRODUCTION_NOTICE",
            biz_id=plan.plan_code,
            biz_ref=f"sales.OrderPlan:{plan.pk}",
            uniq_key=f"sales:order-plan:{plan.pk}:production-created-notice",
            title=f"{creator_name}创建订单计划",
            summary=(
                f"{relation.target_organization.name}｜{assignment.region}｜"
                f"{assignment.brand.name}｜{model_summary}"
            ),
            link="/pages/sales/order_plan_list/order_plan_list",
            recipients_openids=production_openids,
            need_action=False,
            action_mode="EACH_ACK",
            payload={
                "order_plan_id": plan.pk,
                "plan_code": plan.plan_code,
                "creator_name": creator_name,
                "merchant_name": relation.target_organization.name,
                "brand_name": assignment.brand.name,
                "region": assignment.region,
                "total_quantity": total_quantity,
                "return_inventory_quantity": return_inventory_total,
                "production_quantity": production_total,
            },
            actor_openid=user.wx_openid or "",
            mark_workitem_done_immediately=True,
        )

    # The receivable/payment ledger runs in parallel with the physical order
    # chain. It must not change OrderPlan.status or the existing accounting
    # confirmation that controls shipment statistics.
    initialize_order_finance(plan=plan, actor=user)

    return _serialize_plan(plan), True
