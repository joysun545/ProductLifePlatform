"""Shared V2 order-plan catalogue and identity helpers.

This module intentionally contains no retired whole-plan creation or
fulfilment logic.  The active creation path is material-flow V2.
"""

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
from product.models import BOM, Brand, ProductModel
from sales.services.order_plan_reissue_service import get_return_inventory_options, serialize_item_reissue


class OrderPlanError(Exception):
    pass


def _get_sales_identity(user):
    identity = UserOrganization.objects.select_related(
        "organization", "organization__organization_type", "role"
    ).filter(
        user=user,
        is_active=True,
        organization__is_active=True,
        organization__organization_type__is_active=True,
        role__is_active=True,
    ).first()
    if not identity or identity.role.code != "factory_sales":
        raise OrderPlanError("只有厂家销售经理可以创建订单计划")
    if identity.organization.organization_type.code != "OWNER":
        raise OrderPlanError("当前岗位没有有效的厂家组织身份")
    return identity


def _direct_merchant_relations(user, factory_organization):
    return OrganizationRelation.objects.select_related(
        "target_organization", "target_organization__organization_type", "created_by_invitation"
    ).filter(
        source_organization=factory_organization,
        target_organization__is_active=True,
        target_organization__organization_type__code="MERCHANT",
        target_organization__organization_type__is_active=True,
        relation_type=OrganizationRelationType.DISTRIBUTION,
        is_active=True,
        created_by_invitation__inviter=user,
        created_by_invitation__status=InvitationRecord.Status.ACCEPTED,
        created_by_invitation__target_role__code="merchant_owner",
    ).order_by("target_organization__name", "id")


def _get_direct_relation(user, identity, merchant_id):
    try:
        merchant_id = int(merchant_id)
    except (TypeError, ValueError) as exc:
        raise OrderPlanError("请选择有效的直接商家") from exc
    relation = _direct_merchant_relations(user, identity.organization).filter(
        target_organization_id=merchant_id
    ).first()
    if not relation:
        raise OrderPlanError("所选商家不是您本人邀请的直接商家")
    return relation


def _serialize_product_catalog():
    categories = {}
    for product_model in ProductModel.objects.select_related("category").filter(
        boms__is_active=True
    ).distinct().order_by("category__code", "code", "id"):
        category = product_model.category
        categories.setdefault(category.pk, {
            "id": category.pk, "code": category.code, "name": category.name, "models": []
        })["models"].append({
            "id": product_model.pk, "code": product_model.code, "name": product_model.name
        })
    return list(categories.values())


def get_order_plan_options(user):
    identity = _get_sales_identity(user)
    catalog = _serialize_product_catalog()
    relations = list(_direct_merchant_relations(user, identity.organization))
    assignments = {
        row.relation_id: row for row in MerchantChannelAssignment.objects.select_related("brand").filter(
            relation_id__in=[row.pk for row in relations]
        )
    }
    merchants = []
    for relation in relations:
        assignment = assignments.get(relation.pk)
        merchant = relation.target_organization
        merchants.append({
            "id": merchant.pk,
            "code": merchant.code,
            "name": merchant.name,
            "relation_id": relation.pk,
            "configured": bool(assignment and assignment.is_active),
            "region": assignment.region if assignment else merchant.region or "",
            "brand": ({"id": assignment.brand_id, "code": assignment.brand.code, "name": assignment.brand.name}
                      if assignment else None),
        })
    return {
        "factory": {"id": identity.organization_id, "code": identity.organization.code, "name": identity.organization.name},
        "direct_merchants": merchants,
        "brands": [{"id": brand.pk, "code": brand.code, "name": brand.name, "categories": catalog}
                   for brand in Brand.objects.order_by("code", "id")],
        "categories": catalog,
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
    if not region or len(region) > 255:
        raise OrderPlanError("请填写不超过255个字符的负责区域")
    try:
        brand_id = int(brand_id)
    except (TypeError, ValueError) as exc:
        raise OrderPlanError("请选择有效品牌") from exc
    brand = Brand.objects.filter(pk=brand_id).first()
    if not brand:
        raise OrderPlanError("品牌不存在")
    assignment, _ = MerchantChannelAssignment.objects.update_or_create(
        relation=relation,
        defaults={"brand": brand, "region": region, "is_active": True, "assigned_by": user},
    )
    return {
        "merchant_id": relation.target_organization_id,
        "relation_id": relation.pk,
        "configured": True,
        "region": assignment.region,
        "brand": {"id": brand.pk, "code": brand.code, "name": brand.name},
    }


def _new_code(prefix, model_code=""):
    timestamp = timezone.now().strftime("%Y%m%d%H%M%S")
    normalized = re.sub(r"[^A-Za-z0-9]+", "-", model_code).strip("-").upper()
    return f"{prefix}-{timestamp}{('-' + normalized[:18]) if normalized else ''}-{uuid.uuid4().hex[:6].upper()}"


def _serialize_plan(plan):
    return {
        "id": plan.pk,
        "plan_code": plan.plan_code,
        "status": plan.status,
        "status_name": plan.get_status_display(),
        "factory": {"id": plan.factory_organization_id, "name": plan.factory_organization.name},
        "merchant": {"id": plan.merchant_organization_id, "name": plan.merchant_organization.name},
        "brand": {"id": plan.brand_id, "code": plan.brand.code, "name": plan.brand.name},
        "region": plan.region_snapshot,
        "total_quantity": plan.total_quantity,
        "remark": plan.remark,
        "submitted_at": plan.submitted_at.isoformat(),
        "items": [dict({
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
        }, **serialize_item_reissue(item)) for item in plan.items.select_related("category", "product_model")],
    }
