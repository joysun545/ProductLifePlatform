from account.models import (
    InvitationRecord,
    InvitationScope,
    OrganizationRelation,
    OrganizationRelationType,
    RoleInvitationRule,
)
from account.services.auth_service import get_active_identity


class SubordinateManagementError(Exception):
    """当前账号不能读取或管理直接邀请的下级账号。"""


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
    """只开放已有明确业务规则的编辑能力，其余链路先保持只读。"""

    if (
        identity.role.code == "factory_sales"
        and invitation.target_role.code == "merchant_owner"
        and invitation.scope == InvitationScope.CROSS_ORGANIZATION
        and invitation.relation_type == OrganizationRelationType.DISTRIBUTION
    ):
        return "merchant_channel"
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
    """返回当前身份亲自邀请并且仍保持有效身份的直接下级账号。"""

    identity = _get_management_identity(user)
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
            inviter_role=identity.role,
            status=InvitationRecord.Status.ACCEPTED,
            invitee__isnull=False,
        )
        .order_by("-accepted_at", "-id")
    )

    items = []
    for invitation in invitations:
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
        relation = _created_relation(invitation)
        items.append(
            {
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
                },
                "role": {
                    "code": role.code,
                    "name": role.name,
                },
                "management_mode": _management_mode(identity, invitation),
                "relation_id": relation.pk if relation else None,
            }
        )

    return {
        "manager": {
            "role": identity.role.code,
            "role_name": identity.role.name,
            "organization_id": identity.organization_id,
            "organization_name": identity.organization.name,
        },
        "items": items,
    }
