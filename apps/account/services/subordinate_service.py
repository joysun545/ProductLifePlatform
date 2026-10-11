from django.db import transaction
from django.db.models import Q

from account.models import (
    InvitationRecord,
    InvitationScope,
    Organization,
    OrganizationRelation,
    OrganizationRelationType,
    RoleInvitationRule,
    UserOrganization,
)
from account.services.auth_service import get_active_identity
from channel.models import MerchantProductCategoryScope


class SubordinateManagementError(Exception):
    """当前账号不能读取或管理直接关联的下级身份。"""


def _get_management_identity(user):
    identity = get_active_identity(user)
    if not identity:
        raise SubordinateManagementError("游客没有下级账号管理权限")

    can_invite = RoleInvitationRule.objects.filter(
        inviter_role=identity.role,
        is_active=True,
        target_role__is_active=True,
        target_role__organization_type__is_active=True,
    ).exists()
    if not can_invite:
        raise SubordinateManagementError("当前岗位没有邀请或管理下级账号的权限")

    return identity


def _management_mode(identity, invitation):
    """Return only editing capabilities explicitly supported by the role chain."""

    if (
        identity.role.code == "factory_sales"
        and invitation.target_role.code == "merchant_owner"
        and invitation.scope == InvitationScope.CROSS_ORGANIZATION
        and invitation.relation_type == OrganizationRelationType.DISTRIBUTION
    ):
        return "merchant_channel"
    if (
        identity.role.code == "merchant_owner"
        and invitation.target_role.code == "merchant_owner"
        and invitation.scope == InvitationScope.CROSS_ORGANIZATION
        and invitation.relation_type == OrganizationRelationType.DISTRIBUTION
    ):
        return "merchant_catalog"
    return "view_only"


def _created_relation(invitation):
    try:
        relation = invitation.created_organization_relation
    except OrganizationRelation.DoesNotExist:
        return None
    if not relation.is_active:
        return None
    return relation


def get_direct_subordinates(user):
    """返回当前账号建立且仍有效的直接组织身份和组织链路。"""

    identity = _get_management_identity(user)
    factory_root = identity.organization.get_factory_root()
    if not factory_root:
        raise SubordinateManagementError("当前组织尚未明确所属厂家链路")
    factory_root_id = factory_root.pk
    invitations = (
        InvitationRecord.objects.select_related(
            "invitee",
            "invitee__organization_identity",
            "invitee__organization_identity__organization",
            "invitee__organization_identity__organization__organization_type",
            "invitee__organization_identity__role",
            "source_organization",
            "target_organization",
            "target_organization__organization_type",
            "target_role",
            "target_role__organization_type",
            "created_organization_relation",
        )
        .filter(
            inviter=user,
            source_organization=identity.organization,
            source_organization__factory_root_id=factory_root_id,
            target_organization__factory_root_id=factory_root_id,
            inviter_role=identity.role,
            status=InvitationRecord.Status.ACCEPTED,
            invitee__isnull=False,
        )
        .order_by("-accepted_at", "-id")
    )

    invitation_rows = list(invitations)
    relation_by_invitation = {
        invitation.pk: _created_relation(invitation)
        for invitation in invitation_rows
    }
    relation_ids = {
        relation.pk
        for relation in relation_by_invitation.values()
        if relation
    }
    catalog_brands_by_relation = {}
    if relation_ids:
        catalog_brand_rows = (
            MerchantProductCategoryScope.objects.filter(
                relation_id__in=relation_ids
            )
            .order_by("relation_id", "brand__code", "brand_id")
            .values_list("relation_id", "brand_id", "brand__name")
            .distinct()
        )
        for relation_id, brand_id, brand_name in catalog_brand_rows:
            summary = catalog_brands_by_relation.setdefault(
                relation_id,
                {"ids": [], "names": []},
            )
            if brand_id not in summary["ids"]:
                summary["ids"].append(brand_id)
                summary["names"].append(brand_name)

    items = []
    seen_user_ids = set()
    for invitation in invitation_rows:
        subordinate_identity = getattr(
            invitation.invitee,
            "organization_identity",
            None,
        )
        if not subordinate_identity or not subordinate_identity.is_active:
            continue
        if (
            subordinate_identity.organization_id
            != invitation.target_organization_id
            or subordinate_identity.role_id != invitation.target_role_id
        ):
            continue
        if (
            not subordinate_identity.organization.is_active
            or not subordinate_identity.role.is_active
        ):
            continue

        organization = subordinate_identity.organization
        role = subordinate_identity.role
        relation = relation_by_invitation.get(invitation.pk)
        catalog_brand_summary = catalog_brands_by_relation.get(
            relation.pk if relation else None,
            {"ids": [], "names": []},
        )
        items.append(
            {
                "item_key": f"invitation-{invitation.pk}",
                "invitation_id": invitation.pk,
                "scope": invitation.scope,
                "scope_name": invitation.get_scope_display(),
                "relation_type": invitation.relation_type,
                "relation_type_name": (
                    invitation.get_relation_type_display()
                    if invitation.relation_type
                    else "同组织成员"
                ),
                "accepted_at": (
                    invitation.accepted_at.isoformat()
                    if invitation.accepted_at
                    else ""
                ),
                "user": {
                    "id": invitation.invitee_id,
                    "nickname": (
                        invitation.invitee.wx_nickname
                        or invitation.invitee.username
                        or "微信用户"
                    ),
                    "phone": invitation.invitee.phone or "",
                    "avatar_url": invitation.invitee.wx_avatar or "",
                },
                "organization": {
                    "id": organization.pk,
                    "code": organization.code,
                    "name": organization.name,
                    "region": organization.region or "",
                    "type_code": organization.organization_type.code,
                    "type_name": organization.organization_type.name,
                    "provides_after_sales": organization.provides_after_sales,
                },
                "role": {"code": role.code, "name": role.name},
                "management_mode": _management_mode(identity, invitation),
                "relation_id": relation.pk if relation else None,
                "catalog_brand_ids": catalog_brand_summary["ids"],
                "catalog_brand_names": catalog_brand_summary["names"],
                "catalog_brand_label": (
                    "、".join(catalog_brand_summary["names"])
                    if catalog_brand_summary["names"]
                    else "未配置"
                ),
            }
        )
        seen_user_ids.add(invitation.invitee_id)

    # Memberships created by earlier data imports and role-slot handovers retain
    # their inviter on UserOrganization but may predate InvitationRecord. Include
    # those direct links so valid same-organization staff are not silently hidden.
    membership_scope = Q(inviter=user)
    if identity.role.is_owner_role:
        # Owners manage all active staff identities in their own organization,
        # including legacy slots whose invitation event was never recorded.
        membership_scope |= Q(organization=identity.organization)

    memberships = (
        UserOrganization.objects.select_related(
            "user",
            "organization",
            "organization__organization_type",
            "role",
        )
        .filter(
            membership_scope,
            is_active=True,
            user__is_active=True,
            organization__is_active=True,
            organization__factory_root_id=factory_root_id,
            role__is_active=True,
        )
        .exclude(user=user)
        .order_by("organization__name", "role__code", "id")
    )
    for membership in memberships:
        if membership.user_id in seen_user_ids:
            continue

        relation = None
        scope = InvitationScope.SAME_ORGANIZATION
        relation_type = ""
        relation_name = (
            "同组织邀请"
            if membership.organization_id == identity.organization_id
            and membership.inviter_id == user.pk
            else "本组织岗位"
            if membership.organization_id == identity.organization_id
            else ""
        )
        if membership.organization_id != identity.organization_id:
            # A cross-organization identity is included only when there is a
            # direct active business link from the manager's organization.
            relation = OrganizationRelation.objects.filter(
                source_organization=identity.organization,
                target_organization=membership.organization,
                source_organization__factory_root_id=factory_root_id,
                target_organization__factory_root_id=factory_root_id,
                is_active=True,
            ).first()
            if not relation or not membership.role.is_owner_role:
                continue
            scope = InvitationScope.CROSS_ORGANIZATION
            relation_type = relation.relation_type
            relation_name = dict(OrganizationRelationType.choices).get(
                relation_type, relation_type
            )

        items.append({
            "item_key": f"membership-{membership.pk}",
            "invitation_id": None,
            "scope": scope,
            "scope_name": dict(InvitationScope.choices).get(scope, scope),
            "relation_type": relation_type,
            "relation_type_name": relation_name,
            "accepted_at": membership.created_at.isoformat(),
            "user": {
                "id": membership.user_id,
                "nickname": (
                    membership.user.wx_nickname
                    or membership.user.username
                    or "微信用户"
                ),
                "phone": membership.user.phone or "",
                "avatar_url": membership.user.wx_avatar or "",
            },
            "organization": {
                "id": membership.organization_id,
                "code": membership.organization.code,
                "name": membership.organization.name,
                "region": membership.organization.region or "",
                "type_code": membership.organization.organization_type.code,
                "type_name": membership.organization.organization_type.name,
                "provides_after_sales": membership.organization.provides_after_sales,
            },
            "role": {"code": membership.role.code, "name": membership.role.name},
            "management_mode": "view_only",
            "relation_id": relation.pk if relation else None,
        })
        seen_user_ids.add(membership.user_id)

    manageable_items = [
        item
        for item in items
        if item["relation_id"]
        and item["management_mode"] in {"merchant_channel", "merchant_catalog"}
        and item["role"]["code"] == "merchant_owner"
        and item["organization"]["type_code"] == "MERCHANT"
    ]
    if manageable_items:
        from account.services.merchant_catalog_service import (
            MerchantCatalogError,
            get_effective_merchant_catalog,
        )

        organizations = Organization.objects.in_bulk(
            item["organization"]["id"] for item in manageable_items
        )
        for item in manageable_items:
            organization = organizations.get(item["organization"]["id"])
            try:
                category_ids = (
                    get_effective_merchant_catalog(organization)["category_ids"]
                    if organization
                    else set()
                )
            except MerchantCatalogError:
                category_ids = set()
            item["has_operating_categories"] = bool(category_ids)
            item["operating_category_count"] = len(category_ids)

    return {
        "manager": {
            "role": identity.role.code,
            "role_name": identity.role.name,
            "is_owner_role": identity.role.is_owner_role,
            "organization_id": identity.organization_id,
            "organization_name": identity.organization.name,
        },
        "items": items,
    }


@transaction.atomic
def update_direct_subordinate_after_sales_qualification(
    *, user, organization_id, provides_after_sales
):
    """Update the qualification only for a managed merchant-owner organization."""

    if type(provides_after_sales) is not bool:
        raise SubordinateManagementError("售后服务资格必须是布尔值")
    if isinstance(organization_id, bool):
        raise SubordinateManagementError("商家组织ID无效")
    try:
        organization_id = int(organization_id)
    except (TypeError, ValueError):
        raise SubordinateManagementError("商家组织ID无效")

    from account.services.merchant_catalog_service import (
        MerchantCatalogError,
        get_effective_merchant_catalog,
        managed_relation_for_qualification,
    )

    try:
        relation = managed_relation_for_qualification(
            user=user,
            organization_id=organization_id,
        )
    except MerchantCatalogError as exc:
        raise SubordinateManagementError(str(exc)) from exc

    organization = (
        Organization.objects.select_for_update()
        .select_related("organization_type")
        .filter(
            pk=organization_id,
            is_active=True,
            organization_type__code="MERCHANT",
        )
        .first()
    )
    if not organization:
        raise SubordinateManagementError("商家组织不存在或已停用")

    if provides_after_sales:
        try:
            categories = get_effective_merchant_catalog(organization)["category_ids"]
        except MerchantCatalogError as exc:
            raise SubordinateManagementError(str(exc)) from exc
        if not categories:
            raise SubordinateManagementError("请先编辑并确认该商家的经营品类")

    organization.provides_after_sales = provides_after_sales
    organization.save(update_fields=("provides_after_sales", "updated_at"))
    return {
        "organization_id": organization.pk,
        "provides_after_sales": organization.provides_after_sales,
    }
