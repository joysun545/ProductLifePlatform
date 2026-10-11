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
    OrganizationRole,
    OrganizationType,
    PlatformFactoryInvitation,
    RoleInvitationRule,
    SeniorManagerProfile,
    UserOrganization,
)
from account.services.auth_service import build_user_payload, get_active_identity
from account.services.role_delegation_service import (
    SENIOR_MANAGER_ROLE_CODES,
    available_managed_role_options,
    delegated_role_codes,
    manager_may_invite_role,
)


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


def _get_platform_factory_invitation(token, *, for_update=False):
    queryset = PlatformFactoryInvitation.objects.select_related(
        "inviter", "target_role", "target_role__organization_type", "invitee"
    )
    if for_update:
        queryset = queryset.select_for_update()
    try:
        return queryset.get(token=token)
    except (PlatformFactoryInvitation.DoesNotExist, ValueError, TypeError) as exc:
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

    manager_grants = delegated_role_codes(identity)
    items = []
    managed_role_options = None
    for rule in rules:
        if identity.role.code in SENIOR_MANAGER_ROLE_CODES:
            if (
                rule.scope != InvitationScope.SAME_ORGANIZATION
                or rule.target_role.code not in manager_grants
            ):
                continue
        item = {
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
        if rule.target_role.code in SENIOR_MANAGER_ROLE_CODES:
            if managed_role_options is None:
                managed_role_options = available_managed_role_options(identity)
            item["senior_manager_setup"] = {
                "job_title_optional": True,
                "managed_role_options": managed_role_options,
            }
        items.append(item)
    return items


@transaction.atomic
def create_staff_invitation(
    *,
    inviter,
    rule_id,
    senior_manager_job_title="",
    senior_manager_managed_role_codes=None,
):
    """校验邀请方身份并创建不可由前端修改的邀请记录。"""

    identity = _get_inviter_identity(inviter)
    if not identity.organization.get_factory_root():
        raise InvitationError("当前组织尚未明确所属厂家，请先核对组织链路")

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
        identity.role.code in SENIOR_MANAGER_ROLE_CODES
        and rule.scope != InvitationScope.SAME_ORGANIZATION
    ):
        raise InvitationError("高级经理只能邀请负责人授权的本组织系统岗位")

    manager_job_title = str(senior_manager_job_title or "").strip()
    if len(manager_job_title) > 64:
        raise InvitationError("分管职位名称最多64个字符")
    manager_role_codes = senior_manager_managed_role_codes
    if manager_role_codes is None:
        manager_role_codes = []
    if not isinstance(manager_role_codes, list):
        raise InvitationError("分管系统岗位格式无效")
    normalized_manager_role_codes = []
    for role_code in manager_role_codes:
        if not isinstance(role_code, str) or not role_code.strip():
            raise InvitationError("分管系统岗位格式无效")
        normalized_manager_role_codes.append(role_code.strip())
    if len(set(normalized_manager_role_codes)) != len(normalized_manager_role_codes):
        raise InvitationError("分管系统岗位不能重复")

    target_is_senior_manager = rule.target_role.code in SENIOR_MANAGER_ROLE_CODES
    if target_is_senior_manager:
        if not identity.role.is_owner_role:
            raise InvitationError("只有本组织负责人可以邀请并配置高级经理")
        if rule.scope != InvitationScope.SAME_ORGANIZATION:
            raise InvitationError("高级经理必须加入邀请方组织")
        allowed_codes = {
            item["code"] for item in available_managed_role_options(identity)
        }
        if not normalized_manager_role_codes:
            raise InvitationError("请至少选择一个高级经理分管的系统岗位")
        if set(normalized_manager_role_codes) - allowed_codes:
            raise InvitationError("分管岗位必须来自本组织负责人可邀请的系统岗位")
    elif manager_job_title or normalized_manager_role_codes:
        raise InvitationError("只有邀请高级经理时才能填写分管信息")

    if rule.scope == InvitationScope.SAME_ORGANIZATION:
        if not identity.role.is_owner_role and not (
            identity.role.code in SENIOR_MANAGER_ROLE_CODES
            and manager_may_invite_role(identity, rule.target_role.code)
        ):
            raise InvitationError("当前岗位未获授权邀请该组织成员")

    target_organization = (
        identity.organization
        if rule.scope == InvitationScope.SAME_ORGANIZATION
        else None
    )

    if (
        target_organization
        and target_organization.get_factory_root()
        and target_organization.get_factory_root().pk
        != identity.organization.get_factory_root().pk
    ):
        raise InvitationError("目标组织属于其他厂家链路，不能跨链路邀请")

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
        senior_manager_job_title=(
            manager_job_title if target_is_senior_manager else ""
        ),
        senior_manager_managed_role_codes=(
            normalized_manager_role_codes if target_is_senior_manager else []
        ),
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
    try:
        invitation = _get_invitation(token)
    except InvitationError:
        platform_invitation = _get_platform_factory_invitation(token)
        return _platform_factory_invitation_detail(platform_invitation, viewer)
    _ensure_pending(invitation)

    viewer_has_identity = bool(viewer and get_active_identity(viewer))
    target_type = invitation.target_role.organization_type

    detail = {
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
    if invitation.target_role.code in SENIOR_MANAGER_ROLE_CODES:
        role_rows = OrganizationRole.objects.filter(
            organization_type_id=target_type.pk,
            code__in=invitation.senior_manager_managed_role_codes or [],
            is_active=True,
        ).order_by("id")
        detail["senior_manager"] = {
            "job_title": invitation.senior_manager_job_title or "",
            "managed_roles": [
                {"code": role.code, "name": role.name}
                for role in role_rows
            ],
        }
    return detail


def _platform_factory_invitation_detail(invitation, viewer):
    now = timezone.now()
    if invitation.status == PlatformFactoryInvitation.Status.PENDING and invitation.expires_at <= now:
        invitation.status = PlatformFactoryInvitation.Status.EXPIRED
        invitation.save(update_fields=("status", "updated_at"))
    if invitation.status != PlatformFactoryInvitation.Status.PENDING:
        raise InvitationError("该邀请已失效或已被接受")

    target_type = invitation.target_role.organization_type
    viewer_already_has_identity = bool(
        viewer and UserOrganization.objects.filter(user=viewer).exists()
    )
    return {
        "token": str(invitation.token),
        "invitation_kind": "platform_factory_owner",
        "status": invitation.status,
        "scope": "platform_factory_root",
        "relation_type": "",
        "expires_at": invitation.expires_at.isoformat(),
        "requires_organization_profile": False,
        "can_accept": not viewer_already_has_identity,
        "inviter": {
            "nickname": invitation.inviter.wx_nickname or invitation.inviter.username or "项目管理员",
            "avatar": invitation.inviter.wx_avatar or "",
            "role": "platform_admin",
            "role_name": "项目管理员",
        },
        "source_organization": {"id": None, "name": "项目管理员邀请", "region": ""},
        "target_role": {
            "code": invitation.target_role.code,
            "name": invitation.target_role.name,
        },
        "target_organization_type": {
            "code": target_type.code,
            "name": target_type.name,
        },
        "target_organization": {
            "id": None,
            "name": invitation.organization_name,
            "region": invitation.region,
        },
    }


@transaction.atomic
def create_platform_factory_invitation(
    *, inviter, organization_name, region="", address=""
):
    """Let only the project superuser issue a shareable factory-root invite."""
    if not inviter or not inviter.is_active or not inviter.is_superuser:
        raise InvitationError("只有项目管理员可以邀请厂家负责人")

    organization_name = str(organization_name or "").strip()
    region = str(region or "").strip()
    address = str(address or "").strip()
    if not organization_name:
        raise InvitationError("请填写厂家组织名称")
    if len(organization_name) > 128 or len(region) > 255 or len(address) > 255:
        raise InvitationError("厂家组织信息长度超出限制")

    target_role = OrganizationRole.objects.filter(
        organization_type__code="OWNER",
        code="factory_admin",
        is_owner_role=True,
        is_active=True,
        organization_type__is_active=True,
    ).first()
    if not target_role:
        raise InvitationError("系统尚未配置有效的厂家负责人岗位")

    ttl_seconds = getattr(
        settings, "STAFF_INVITATION_TTL_SECONDS", DEFAULT_STAFF_INVITATION_TTL_SECONDS
    )
    for _attempt in range(10):
        token = uuid.uuid4()
        if not InvitationRecord.objects.filter(token=token).exists() and not PlatformFactoryInvitation.objects.filter(token=token).exists():
            break
    else:
        raise InvitationError("邀请链接生成失败，请重试")

    invitation = PlatformFactoryInvitation(
        token=token,
        inviter=inviter,
        target_role=target_role,
        organization_name=organization_name,
        region=region,
        address=address,
        expires_at=timezone.now() + timedelta(seconds=ttl_seconds),
    )
    invitation.full_clean()
    invitation.save()
    return {
        "token": str(invitation.token),
        "share_path": f"/pages/home/invite/accept/accept?token={invitation.token}",
        "target_role": target_role.code,
        "target_role_name": target_role.name,
        "organization_name": organization_name,
        "expires_at": invitation.expires_at.isoformat(),
    }


@transaction.atomic
def _accept_platform_factory_invitation(*, invitee, token, nickname="", avatar="", phone=""):
    invitation = _get_platform_factory_invitation(token, for_update=True)
    if invitation.status == PlatformFactoryInvitation.Status.ACCEPTED:
        if invitation.invitee_id == invitee.pk:
            return build_user_payload(invitee)
        raise InvitationError("该邀请已被其他账号接受")
    if invitation.status == PlatformFactoryInvitation.Status.PENDING and invitation.expires_at <= timezone.now():
        invitation.status = PlatformFactoryInvitation.Status.EXPIRED
        invitation.save(update_fields=("status", "updated_at"))
    if invitation.status != PlatformFactoryInvitation.Status.PENDING:
        raise InvitationError("该邀请已失效")
    if not invitation.inviter.is_active or not invitation.inviter.is_superuser:
        raise InvitationError("项目管理员权限已失效，该邀请不能再使用")
    if invitation.inviter_id == invitee.pk:
        raise InvitationError("邀请人不能接受自己发出的邀请")
    if UserOrganization.objects.filter(user=invitee).exists():
        raise InvitationError("当前微信账号已经拥有或保留过组织身份，不能再次加入")

    owner_type = OrganizationType.objects.get(code="OWNER", is_active=True)
    organization = Organization.objects.create(
        name=invitation.organization_name,
        code=_generate_organization_code(owner_type.code),
        organization_type=owner_type,
        region=invitation.region,
        address=invitation.address,
    )
    if nickname:
        invitee.wx_nickname = str(nickname).strip()[:128]
    if phone:
        invitee.phone = str(phone).strip()[:32]
    avatar = str(avatar or "").strip()
    if avatar.startswith(("https://", "http://")):
        invitee.wx_avatar = avatar[:512]
    update_fields = [field for field in ("wx_nickname", "phone", "wx_avatar") if getattr(invitee, field)]
    if update_fields:
        invitee.save(update_fields=tuple(update_fields))

    identity = UserOrganization(
        user=invitee,
        organization=organization,
        role=invitation.target_role,
        inviter=invitation.inviter,
    )
    identity.full_clean()
    identity.save()
    invitation.invitee = invitee
    invitation.status = PlatformFactoryInvitation.Status.ACCEPTED
    invitation.accepted_at = timezone.now()
    invitation.save(update_fields=("invitee", "status", "accepted_at", "updated_at"))
    payload = build_user_payload(invitee)
    payload["invitation"] = {
        "token": str(invitation.token),
        "scope": "platform_factory_root",
        "source_organization": "项目管理员邀请",
    }
    return payload


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

    if (
        invitation.scope == InvitationScope.SAME_ORGANIZATION
        and invitation.inviter_role.code in SENIOR_MANAGER_ROLE_CODES
        and not manager_may_invite_role(inviter_identity, invitation.target_role.code)
    ):
        raise InvitationError("高级经理已不再拥有该岗位的邀请授权")

    if invitation.target_role.code in SENIOR_MANAGER_ROLE_CODES:
        selected_codes = set(invitation.senior_manager_managed_role_codes or [])
        valid_codes = set(
            OrganizationRole.objects.filter(
                organization_type_id=invitation.target_role.organization_type_id,
                code__in=selected_codes,
                is_active=True,
                is_owner_role=False,
            )
            .exclude(code__in=SENIOR_MANAGER_ROLE_CODES)
            .values_list("code", flat=True)
        )
        if not selected_codes or valid_codes != selected_codes:
            raise InvitationError("高级经理分管岗位已失效，请重新邀请")


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
    try:
        invitation = _get_invitation(token, for_update=True)
    except InvitationError:
        return _accept_platform_factory_invitation(
            invitee=invitee,
            token=token,
            nickname=nickname,
            avatar=avatar,
            phone=phone,
        )

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
                factory_root=invitation.source_organization.get_factory_root(),
                region=region,
                address=(address or "").strip(),
                provides_after_sales=False,
            )

        if organization.organization_type_id != target_role.organization_type_id:
            raise InvitationError("目标组织类型与指定岗位不一致")
        source_root = invitation.source_organization.get_factory_root()
        target_root = organization.get_factory_root()
        if not source_root or not target_root or source_root.pk != target_root.pk:
            raise InvitationError("目标组织不能加入其他厂家根组织的链路")

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

    if target_role.code in SENIOR_MANAGER_ROLE_CODES:
        manager_profile = SeniorManagerProfile.objects.create(
            identity=identity,
            job_title=invitation.senior_manager_job_title or "",
        )
        manager_profile.managed_roles.set(
            OrganizationRole.objects.filter(
                organization_type_id=target_role.organization_type_id,
                code__in=invitation.senior_manager_managed_role_codes or [],
                is_active=True,
                is_owner_role=False,
            ).exclude(code__in=SENIOR_MANAGER_ROLE_CODES)
        )

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
