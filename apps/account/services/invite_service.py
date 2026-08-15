import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from account.models import (
    InvitationRecord,
    InvitationScope,
    Organization,
    OrganizationRelation,
    RoleInvitationRule,
    UserOrganization,
)
from account.services.auth_service import build_user_payload, get_active_identity


DEFAULT_STAFF_INVITATION_TTL_SECONDS = 7 * 24 * 60 * 60


class InvitationError(Exception):
    """普通组织邀请流程错误。"""


def _get_inviter_identity(user):
    identity = get_active_identity(user)
    if not identity:
        raise InvitationError("游客没有组织邀请资格")
    return identity


def _get_invitation_queryset(for_update=False):
    queryset = InvitationRecord.objects.select_related(
        "inviter",
        "source_organization",
        "source_organization__organization_type",
        "inviter_role",
        "rule",
        "target_role",
        "target_role__organization_type",
        "target_organization",
        "invitee",
    )
    if for_update:
        queryset = queryset.select_for_update()
    return queryset


def _get_invitation(token, *, for_update=False):
    try:
        return _get_invitation_queryset(for_update=for_update).get(token=token)
    except (InvitationRecord.DoesNotExist, ValueError, TypeError) as exc:
        raise InvitationError("邀请链接不存在或已失效") from exc


def _ensure_pending(invitation):
    now = timezone.now()

    if (
        invitation.status == InvitationRecord.Status.PENDING
        and invitation.expires_at <= now
    ):
        invitation.status = InvitationRecord.Status.EXPIRED
        invitation.save(update_fields=("status", "updated_at"))

    if invitation.status == InvitationRecord.Status.ACCEPTED:
        raise InvitationError("该邀请已被接受")
    if invitation.status == InvitationRecord.Status.CANCELLED:
        raise InvitationError("该邀请已被取消")
    if invitation.status == InvitationRecord.Status.EXPIRED:
        raise InvitationError("该邀请已过期")
    if invitation.status != InvitationRecord.Status.PENDING:
        raise InvitationError("邀请状态无效")


def get_available_invitation_roles(user):
    """从数据库返回当前岗位真实可发起的邀请规则。"""

    identity = _get_inviter_identity(user)
    rules = (
        RoleInvitationRule.objects.select_related(
            "target_role",
            "target_role__organization_type",
        )
        .filter(
            inviter_role=identity.role,
            is_active=True,
            target_role__is_active=True,
            target_role__organization_type__is_active=True,
        )
        .order_by("scope", "target_role__name", "id")
    )

    return [
        {
            "rule_id": rule.pk,
            "role": rule.target_role.code,
            "name": rule.target_role.name,
            "scope": rule.scope,
            "relation_type": rule.relation_type,
            "organization_type": rule.target_role.organization_type.code,
            "organization_type_name": rule.target_role.organization_type.name,
            "requires_organization_profile": (
                rule.scope == InvitationScope.CROSS_ORGANIZATION
            ),
        }
        for rule in rules
    ]


@transaction.atomic
def create_staff_invitation(*, inviter, rule_id):
    """校验邀请方身份并创建不可由前端修改的邀请记录。"""

    identity = _get_inviter_identity(inviter)

    rule = (
        RoleInvitationRule.objects.select_for_update()
        .select_related("target_role", "target_role__organization_type")
        .filter(
            pk=rule_id,
            inviter_role=identity.role,
            is_active=True,
            target_role__is_active=True,
            target_role__organization_type__is_active=True,
        )
        .first()
    )
    if not rule:
        raise InvitationError("当前岗位不能发起该邀请")

    if (
        rule.scope == InvitationScope.SAME_ORGANIZATION
        and not identity.role.is_owner_role
    ):
        raise InvitationError("同组织邀请只能由负责人账号发起")

    target_organization = (
        identity.organization
        if rule.scope == InvitationScope.SAME_ORGANIZATION
        else None
    )

    ttl_seconds = getattr(
        settings,
        "STAFF_INVITATION_TTL_SECONDS",
        DEFAULT_STAFF_INVITATION_TTL_SECONDS,
    )

    invitation = InvitationRecord(
        inviter=inviter,
        source_organization=identity.organization,
        inviter_role=identity.role,
        rule=rule,
        target_role=rule.target_role,
        target_organization=target_organization,
        scope=rule.scope,
        relation_type=rule.relation_type,
        expires_at=timezone.now() + timedelta(seconds=ttl_seconds),
    )
    invitation.full_clean()
    invitation.save()

    return {
        "token": str(invitation.token),
        "share_path": (
            "/pages/home/invite/accept/accept"
            f"?token={invitation.token}"
        ),
        "target_role": invitation.target_role.code,
        "target_role_name": invitation.target_role.name,
        "scope": invitation.scope,
        "expires_at": invitation.expires_at.isoformat(),
    }


def get_invitation_detail(*, token, viewer=None):
    invitation = _get_invitation(token)
    _ensure_pending(invitation)

    viewer_has_identity = bool(viewer and get_active_identity(viewer))
    target_type = invitation.target_role.organization_type

    return {
        "token": str(invitation.token),
        "status": invitation.status,
        "scope": invitation.scope,
        "relation_type": invitation.relation_type,
        "expires_at": invitation.expires_at.isoformat(),
        "requires_organization_profile": (
            invitation.scope == InvitationScope.CROSS_ORGANIZATION
            and invitation.target_organization_id is None
        ),
        "can_accept": not viewer_has_identity,
        "inviter": {
            "nickname": invitation.inviter.wx_nickname
            or invitation.inviter.username
            or "邀请人",
            "avatar": invitation.inviter.wx_avatar or "",
            "role": invitation.inviter_role.code,
            "role_name": invitation.inviter_role.name,
        },
        "source_organization": {
            "id": invitation.source_organization_id,
            "name": invitation.source_organization.name,
            "region": invitation.source_organization.region or "",
        },
        "target_role": {
            "code": invitation.target_role.code,
            "name": invitation.target_role.name,
        },
        "target_organization_type": {
            "code": target_type.code,
            "name": target_type.name,
        },
        "target_organization": (
            {
                "id": invitation.target_organization_id,
                "name": invitation.target_organization.name,
                "region": invitation.target_organization.region or "",
            }
            if invitation.target_organization_id
            else None
        ),
    }


def _generate_organization_code(organization_type_code):
    prefix = (organization_type_code or "ORG").upper()
    for _attempt in range(10):
        code = f"{prefix}-{uuid.uuid4().hex[:12].upper()}"
        if not Organization.objects.filter(code=code).exists():
            return code
    raise InvitationError("组织编码生成失败，请重试")


def _validate_invitation_still_authorized(invitation):
    inviter_identity = (
        UserOrganization.objects.select_related("organization", "role")
        .filter(
            user=invitation.inviter,
            organization=invitation.source_organization,
            role=invitation.inviter_role,
            is_active=True,
            organization__is_active=True,
            role__is_active=True,
        )
        .first()
    )
    if not inviter_identity:
        raise InvitationError("邀请人的组织身份已经失效")

    rule = invitation.rule
    if (
        not rule.is_active
        or rule.inviter_role_id != invitation.inviter_role_id
        or rule.target_role_id != invitation.target_role_id
        or rule.scope != invitation.scope
        or rule.relation_type != invitation.relation_type
    ):
        raise InvitationError("该邀请规则已经失效")


@transaction.atomic
def accept_staff_invitation(
    *,
    invitee,
    token,
    nickname="",
    avatar="",
    phone="",
    organization_name="",
    region="",
    address="",
):
    """接受邀请并一次性建立账号、组织和组织关系链路。"""

    invitation = _get_invitation(token, for_update=True)

    if invitation.status == InvitationRecord.Status.ACCEPTED:
        if invitation.invitee_id == invitee.pk:
            return build_user_payload(invitee)
        raise InvitationError("该邀请已被其他账号接受")

    _ensure_pending(invitation)
    _validate_invitation_still_authorized(invitation)

    if invitation.inviter_id == invitee.pk:
        raise InvitationError("不能接受自己发出的邀请")

    if UserOrganization.objects.filter(user=invitee).exists():
        raise InvitationError("当前微信账号已经拥有组织身份，不能再次加入")

    target_role = invitation.target_role

    if invitation.scope == InvitationScope.SAME_ORGANIZATION:
        organization = invitation.source_organization
    else:
        organization = invitation.target_organization

        if organization is None:
            organization_name = (organization_name or "").strip()
            region = (region or "").strip()

            if not organization_name:
                raise InvitationError("请填写组织名称")
            if not region:
                raise InvitationError("请填写所在或负责区域")

            organization = Organization.objects.create(
                name=organization_name,
                code=_generate_organization_code(
                    target_role.organization_type.code
                ),
                organization_type=target_role.organization_type,
                region=region,
                address=(address or "").strip(),
            )

        if organization.organization_type_id != target_role.organization_type_id:
            raise InvitationError("目标组织类型与指定岗位不一致")

    nickname = (nickname or "").strip()
    phone = (phone or "").strip()
    avatar = (avatar or "").strip()

    update_fields = []
    if nickname:
        invitee.wx_nickname = nickname
        update_fields.append("wx_nickname")
    if phone:
        invitee.phone = phone
        update_fields.append("phone")
    if avatar.startswith(("https://", "http://")):
        invitee.wx_avatar = avatar
        update_fields.append("wx_avatar")
    if update_fields:
        invitee.save(update_fields=tuple(update_fields))

    identity = UserOrganization(
        user=invitee,
        organization=organization,
        role=target_role,
        inviter=invitation.inviter,
    )
    identity.full_clean()
    identity.save()

    invitation.target_organization = organization
    invitation.invitee = invitee
    invitation.status = InvitationRecord.Status.ACCEPTED
    invitation.accepted_at = timezone.now()
    invitation.full_clean()
    invitation.save(
        update_fields=(
            "target_organization",
            "invitee",
            "status",
            "accepted_at",
            "updated_at",
        )
    )

    if invitation.scope == InvitationScope.CROSS_ORGANIZATION:
        relation = OrganizationRelation(
            source_organization=invitation.source_organization,
            target_organization=organization,
            relation_type=invitation.relation_type,
            created_by_invitation=invitation,
        )
        relation.full_clean()
        relation.save()

    payload = build_user_payload(invitee)
    payload["invitation"] = {
        "token": str(invitation.token),
        "scope": invitation.scope,
        "source_organization": invitation.source_organization.name,
    }
    return payload
