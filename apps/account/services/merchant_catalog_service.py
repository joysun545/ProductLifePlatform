from collections import defaultdict

from django.db import transaction
from django.db.models import Prefetch

from account.models import (
    Organization,
    OrganizationRelation,
    OrganizationRelationType,
)
from account.services.auth_service import get_active_identity
from account.services.subordinate_service import (
    SubordinateManagementError,
    get_direct_subordinates,
)
from channel.models import (
    MerchantProductCategoryScope,
    MerchantProductModelScope,
)
from product.models import Brand, ProductCategory, ProductModel


class MerchantCatalogError(Exception):
    """商家经营范围编辑错误。"""


def _positive_id(value, label):
    if isinstance(value, bool):
        raise MerchantCatalogError(f"{label}无效")
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise MerchantCatalogError(f"{label}无效")
    if value <= 0:
        raise MerchantCatalogError(f"{label}无效")
    return value


def _managed_merchant_relation(*, user, organization_id):
    organization_id = _positive_id(organization_id, "商家组织ID")
    identity = get_active_identity(user)
    if not identity:
        raise MerchantCatalogError("游客没有下级商家管理权限")

    try:
        subordinate_data = get_direct_subordinates(user)
    except SubordinateManagementError as exc:
        raise MerchantCatalogError(str(exc)) from exc

    subordinate = next(
        (
            item
            for item in subordinate_data["items"]
            if item["organization"]["id"] == organization_id
            and item["organization"]["type_code"] == "MERCHANT"
            and item["role"]["code"] == "merchant_owner"
            and item["management_mode"]
            in {"merchant_channel", "merchant_catalog"}
            and item.get("relation_id")
        ),
        None,
    )
    if not subordinate:
        raise MerchantCatalogError("只能编辑管理范围内的直接下级商家负责人")

    relation = (
        OrganizationRelation.objects.select_related(
            "source_organization",
            "target_organization",
            "source_organization__organization_type",
            "target_organization__organization_type",
        )
        .filter(
            pk=subordinate["relation_id"],
            source_organization=identity.organization,
            target_organization_id=organization_id,
            relation_type=OrganizationRelationType.DISTRIBUTION,
            is_active=True,
            source_organization__is_active=True,
            target_organization__is_active=True,
            source_organization__factory_root_id=identity.organization.factory_root_id,
            target_organization__factory_root_id=identity.organization.factory_root_id,
            target_organization__organization_type__code="MERCHANT",
        )
        .first()
    )
    if not relation:
        raise MerchantCatalogError("经销关系无效或不属于当前厂家链路")

    if identity.role.code == "factory_sales":
        if identity.organization.organization_type.code != "OWNER":
            raise MerchantCatalogError("厂家销售经理必须属于厂家负责人组织")
        mode = "factory"
    elif identity.role.code == "merchant_owner":
        if identity.organization.organization_type.code != "MERCHANT":
            raise MerchantCatalogError("商家负责人身份无效")
        mode = "parent_merchant"
    else:
        raise MerchantCatalogError("当前岗位不能编辑商家经营范围")

    return identity, relation, mode


def _relation_catalog_map(relation):
    scopes = (
        MerchantProductCategoryScope.objects.filter(relation=relation)
        .select_related("brand", "category")
        .prefetch_related(
            Prefetch(
                "model_scopes",
                queryset=MerchantProductModelScope.objects.select_related(
                    "product_model"
                ),
            )
        )
        .order_by("brand__code", "category__code", "id")
    )
    result = {}
    for scope in scopes:
        result[(scope.brand_id, scope.category_id)] = {
            "brand": scope.brand,
            "category": scope.category,
            "model_ids": {
                grant.product_model_id for grant in scope.model_scopes.all()
            },
        }
    return result


def _active_distribution_path(organization):
    root = organization.get_factory_root()
    if not root or root.pk != organization.factory_root_id:
        raise MerchantCatalogError("商家未归属有效厂家根链路")
    if organization.pk == root.pk:
        return root, []

    reverse_path = []
    current = organization
    visited = {current.pk}
    while current.pk != root.pk:
        parents = list(
            OrganizationRelation.objects.select_related(
                "source_organization",
                "target_organization",
            ).filter(
                target_organization=current,
                relation_type=OrganizationRelationType.DISTRIBUTION,
                is_active=True,
                source_organization__is_active=True,
                source_organization__factory_root_id=root.pk,
                target_organization__factory_root_id=root.pk,
            )[:2]
        )
        if len(parents) != 1:
            raise MerchantCatalogError("商家的上级经销链路无法唯一确认")
        relation = parents[0]
        reverse_path.append(relation)
        current = relation.source_organization
        if current.pk in visited:
            raise MerchantCatalogError("商家经销链路存在循环")
        visited.add(current.pk)
        if len(reverse_path) > 32:
            raise MerchantCatalogError("商家经销链路层级异常")

    return root, list(reversed(reverse_path))


def get_effective_merchant_catalog(organization):
    """Return catalog rights after intersecting every edge from the factory root.

    Categories are intersected independently of model IDs. That distinction is
    intentional: downstream sales authorization may be model-specific, while
    after-sales eligibility is category-wide.
    """

    root, path = _active_distribution_path(organization)
    if not path:
        return {
            "root": root,
            "scopes": _factory_catalog_map(),
            "category_ids": set(ProductCategory.objects.values_list("id", flat=True)),
        }

    effective = None
    for relation in path:
        edge = _relation_catalog_map(relation)
        if effective is None:
            effective = edge
            continue
        effective = {
            key: {
                **effective[key],
                "model_ids": effective[key]["model_ids"] & edge[key]["model_ids"],
            }
            for key in effective.keys() & edge.keys()
        }

    effective = effective or {}
    return {
        "root": root,
        "scopes": effective,
        "category_ids": {category_id for _brand_id, category_id in effective},
    }


def _factory_catalog_map():
    brands = list(Brand.objects.order_by("code", "id"))
    categories = list(ProductCategory.objects.order_by("code", "id"))
    models_by_category = defaultdict(set)
    for category_id, product_model_id in ProductModel.objects.order_by(
        "category_id", "code", "id"
    ).values_list("category_id", "id"):
        models_by_category[category_id].add(product_model_id)

    result = {}
    for brand in brands:
        for category in categories:
            result[(brand.pk, category.pk)] = {
                "brand": brand,
                "category": category,
                "model_ids": set(models_by_category[category.pk]),
            }
    return result


def _serialized_catalog(scopes):
    brand_map = {}
    model_rows = {
        row.pk: row
        for row in ProductModel.objects.filter(
            pk__in={mid for data in scopes.values() for mid in data["model_ids"]}
        ).order_by("code", "id")
    }
    for data in sorted(
        scopes.values(),
        key=lambda row: (row["brand"].code, row["category"].code),
    ):
        brand = data["brand"]
        category = data["category"]
        brand_row = brand_map.setdefault(
            brand.pk,
            {"id": brand.pk, "code": brand.code, "name": brand.name, "categories": []},
        )
        brand_row["categories"].append(
            {
                "id": category.pk,
                "code": category.code,
                "name": category.name,
                "models": [
                    {
                        "id": model_rows[mid].pk,
                        "code": model_rows[mid].code,
                        "name": model_rows[mid].name,
                    }
                    for mid in sorted(
                        data["model_ids"],
                        key=lambda model_id: (
                            model_rows[model_id].code,
                            model_rows[model_id].pk,
                        ),
                    )
                    if mid in model_rows
                ],
            }
        )
    return list(brand_map.values())


def _serialized_selected_scopes(relation, allowed_scopes):
    selected = _relation_catalog_map(relation)
    filtered = {
        key: {
            **row,
            "model_ids": row["model_ids"] & allowed_scopes[key]["model_ids"],
        }
        for key, row in selected.items()
        if key in allowed_scopes
    }
    return _serialized_catalog(filtered)


def get_merchant_catalog_options(*, user, organization_id):
    identity, relation, mode = _managed_merchant_relation(
        user=user,
        organization_id=organization_id,
    )
    if mode == "factory":
        catalog = _factory_catalog_map()
        source = "厂家产品目录"
    else:
        catalog = get_effective_merchant_catalog(identity.organization)["scopes"]
        source = "上级商家已授权范围"

    effective_target = get_effective_merchant_catalog(relation.target_organization)
    return {
        "organization": {
            "id": relation.target_organization_id,
            "name": relation.target_organization.name,
        },
        "relation_id": relation.pk,
        "catalog_source": source,
        "brands": _serialized_catalog(catalog),
        "selected": _serialized_selected_scopes(relation, catalog),
        "provides_after_sales": relation.target_organization.provides_after_sales,
        "after_sales_category_ids": sorted(effective_target["category_ids"]),
    }


def _parse_requested_scopes(raw_catalog):
    if not isinstance(raw_catalog, list):
        raise MerchantCatalogError("经营范围格式无效")
    if len(raw_catalog) > 100:
        raise MerchantCatalogError("一次最多配置100个品牌")

    requested = {}
    brand_ids = set()
    for brand_entry in raw_catalog:
        if not isinstance(brand_entry, dict):
            raise MerchantCatalogError("品牌范围格式无效")
        brand_id = _positive_id(brand_entry.get("brand_id"), "品牌ID")
        if brand_id in brand_ids:
            raise MerchantCatalogError("同一品牌不能重复提交")
        brand_ids.add(brand_id)
        category_rows = brand_entry.get("categories")
        if not isinstance(category_rows, list):
            raise MerchantCatalogError("品类范围格式无效")
        if len(category_rows) > 300:
            raise MerchantCatalogError("一次最多配置300个品牌品类")
        for category_entry in category_rows:
            if not isinstance(category_entry, dict):
                raise MerchantCatalogError("品类范围格式无效")
            category_id = _positive_id(
                category_entry.get("category_id"), "品类ID"
            )
            key = (brand_id, category_id)
            if key in requested:
                raise MerchantCatalogError("同一品牌下的品类不能重复提交")
            raw_model_ids = category_entry.get("product_model_ids", [])
            if not isinstance(raw_model_ids, list):
                raise MerchantCatalogError("产品型号列表格式无效")
            model_ids = [
                _positive_id(model_id, "产品型号ID")
                for model_id in raw_model_ids
            ]
            if len(model_ids) != len(set(model_ids)):
                raise MerchantCatalogError("同一品类下的产品型号不能重复")
            requested[key] = set(model_ids)
    return requested


@transaction.atomic
def save_merchant_catalog_scope(*, user, organization_id, catalog):
    identity, relation, mode = _managed_merchant_relation(
        user=user,
        organization_id=organization_id,
    )
    try:
        relation = (
            OrganizationRelation.objects.select_for_update()
            .select_related("source_organization", "target_organization")
            .get(pk=relation.pk, is_active=True)
        )
    except OrganizationRelation.DoesNotExist as exc:
        raise MerchantCatalogError("经销关系已失效，请刷新后重试") from exc
    requested = _parse_requested_scopes(catalog)

    organization = Organization.objects.select_for_update().get(
        pk=relation.target_organization_id
    )
    if mode == "factory":
        allowed = _factory_catalog_map()
    else:
        allowed = get_effective_merchant_catalog(identity.organization)["scopes"]

    all_model_rows = {
        model.pk: model
        for model in ProductModel.objects.filter(
            pk__in={mid for mids in requested.values() for mid in mids}
        ).select_related("category")
    }
    for (brand_id, category_id), model_ids in requested.items():
        if (brand_id, category_id) not in allowed:
            raise MerchantCatalogError("所选品牌或品类超出上级已授权范围")
        allowed_model_ids = allowed[(brand_id, category_id)]["model_ids"]
        if not set(model_ids).issubset(allowed_model_ids):
            raise MerchantCatalogError("所选产品型号超出上级已授权范围")
        for model_id in model_ids:
            model = all_model_rows.get(model_id)
            if not model or model.category_id != category_id:
                raise MerchantCatalogError("产品型号与所选品类不匹配")

    MerchantProductCategoryScope.objects.filter(relation=relation).delete()
    brand_rows = {
        row.pk: row for row in Brand.objects.filter(pk__in=brand_ids_from(requested))
    }
    category_rows = {
        row.pk: row
        for row in ProductCategory.objects.filter(
            pk__in={category_id for _brand_id, category_id in requested}
        )
    }
    for (brand_id, category_id), model_ids in requested.items():
        scope = MerchantProductCategoryScope.objects.create(
            relation=relation,
            brand=brand_rows[brand_id],
            category=category_rows[category_id],
            assigned_by=user,
        )
        MerchantProductModelScope.objects.bulk_create(
            [
                MerchantProductModelScope(
                    category_scope=scope,
                    product_model_id=model_id,
                )
                for model_id in sorted(model_ids)
            ]
        )

    effective = get_effective_merchant_catalog(organization)
    _clear_unqualified_merchants_without_categories(
        relation.source_organization.get_factory_root()
    )
    organization.refresh_from_db(fields=("provides_after_sales",))

    return {
        "organization_id": organization.pk,
        "selected": _serialized_selected_scopes(relation, allowed),
        "category_ids": sorted(effective["category_ids"]),
        "provides_after_sales": organization.provides_after_sales,
    }


def _clear_unqualified_merchants_without_categories(factory_root):
    """Clear stale qualification if an ancestor removed the last effective category."""

    candidates = Organization.objects.select_for_update().filter(
        factory_root=factory_root,
        organization_type__code="MERCHANT",
        is_active=True,
        provides_after_sales=True,
    )
    for organization in candidates.iterator():
        try:
            has_categories = bool(
                get_effective_merchant_catalog(organization)["category_ids"]
            )
        except MerchantCatalogError:
            # Fail closed: malformed or ambiguous chain cannot retain service eligibility.
            has_categories = False
        if not has_categories:
            Organization.objects.filter(pk=organization.pk).update(
                provides_after_sales=False
            )


def brand_ids_from(requested):
    return {brand_id for brand_id, _category_id in requested}


def managed_relation_for_qualification(*, user, organization_id):
    _identity, relation, _mode = _managed_merchant_relation(
        user=user,
        organization_id=organization_id,
    )
    return relation
