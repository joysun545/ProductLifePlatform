"""Scope-checked permissions for factory and merchant senior managers."""

from django.db import transaction
from django.db.models.functions import Trim

from account.models import Organization, OrganizationRole, SeniorManagerProfile, UserOrganization
from account.services.retired_roles import RETIRED_ROLE_CODES


SENIOR_MANAGER_ROLE_CODES = frozenset({
    "factory_senior_manager",
    "merchant_senior_manager",
})

# Explicit merchant operations whose shared owner authority can be taken over
# by a senior manager. ``any`` means one of the listed roles is enough;
# ``all`` means the same manager must be assigned every listed role.
MERCHANT_OPERATION_ROLE_RULES = {
    "goods_transfer_coordination": (
        "any",
        frozenset({"merchant_stock"}),
    ),
    "goods_transfer_routing": (
        "any",
        frozenset({"merchant_sales"}),
    ),
    "goods_transfer_receipt": (
        "any",
        frozenset({"merchant_stock"}),
    ),
    "install_device": (
        "any",
        frozenset({"merchant_electrician"}),
    ),
}


def delegated_role_codes(identity):
    """Return active roles explicitly delegated to this manager in this org."""

    if not identity or identity.role.code not in SENIOR_MANAGER_ROLE_CODES:
        return frozenset()
    profile = (
        SeniorManagerProfile.objects.filter(
            identity=identity,
            identity__is_active=True,
            identity__organization=identity.organization,
            identity__organization__is_active=True,
            identity__role=identity.role,
        )
        .prefetch_related("managed_roles")
        .first()
    )
    if not profile:
        return frozenset()
    return frozenset(
        profile.managed_roles.filter(
            is_active=True,
            organization_type_id=identity.role.organization_type_id,
            organization_type__is_active=True,
            is_owner_role=False,
        ).exclude(code__in=SENIOR_MANAGER_ROLE_CODES | RETIRED_ROLE_CODES).values_list("code", flat=True)
    )


def delegated_role_user_ids(identity, role_code):
    """IDs of active role holders in the organization managed by this manager."""

    if not identity or role_code not in delegated_role_codes(identity):
        return frozenset()

    return frozenset(
        UserOrganization.objects.filter(
            organization_id=identity.organization_id,
            role__code=role_code,
            role__is_active=True,
            is_active=True,
            user__is_active=True,
            organization__is_active=True,
        ).values_list("user_id", flat=True)
    )


def effective_role_codes(identity):
    """Return the roles whose business permissions this identity may use.

    A senior-manager identity is an authorization container, not an implicit
    owner or staff role. Its effective permissions come only from the active
    role grants selected by its organization owner.
    """

    if not identity:
        return frozenset()
    if identity.role.code in SENIOR_MANAGER_ROLE_CODES:
        return delegated_role_codes(identity)
    return frozenset({identity.role.code}) | delegated_role_codes(identity)


def identity_has_role(identity, role_codes):
    """Check a role gate against the identity and explicitly delegated roles."""

    if not identity:
        return False
    if isinstance(role_codes, str):
        role_codes = {role_codes}
    else:
        role_codes = set(role_codes or ())
    if (
        identity.role.code not in SENIOR_MANAGER_ROLE_CODES
        and identity.role.code in role_codes
    ):
        return True
    return bool(delegated_role_codes(identity) & role_codes)


class VacantRoleRecipient(str):
    """An OpenID carrying the role scopes used to grant its fallback task."""

    def __new__(cls, openid, organization_id, role_ids):
        value = super().__new__(cls, openid)
        value.organization_id = organization_id
        value.role_ids = frozenset(role_ids)
        return value


def vacant_role_manager_openids(organization, role_codes):
    """Find authorized managers for unstaffed action roles in this organization."""

    requested = {role_codes} if isinstance(role_codes, str) else set(role_codes or ())
    requested -= RETIRED_ROLE_CODES
    if not organization or not requested:
        return []

    reachable_identities = UserOrganization.objects.filter(
        organization_id=organization.pk,
        organization__is_active=True,
        organization__organization_type__is_active=True,
        organization__factory_root__is_active=True,
        role__organization_type_id=organization.organization_type_id,
        role__is_active=True,
        is_active=True,
        user__is_active=True,
        user__wx_openid__isnull=False,
    ).exclude(user__wx_openid="").order_by()
    staffed = {
        role_code
        for role_code, openid in reachable_identities.filter(
            role__code__in=requested,
        ).values_list("role__code", "user__wx_openid")
        if openid.strip()
    }
    vacant = requested - staffed - SENIOR_MANAGER_ROLE_CODES
    if not vacant:
        return []

    openids = reachable_identities.filter(
        role__code__in=SENIOR_MANAGER_ROLE_CODES,
        senior_manager_profile__managed_roles__code__in=vacant,
        senior_manager_profile__managed_roles__is_active=True,
        senior_manager_profile__managed_roles__is_owner_role=False,
        senior_manager_profile__managed_roles__organization_type_id=organization.organization_type_id,
    ).values_list("user__wx_openid", "senior_manager_profile__managed_roles")
    roles_by_openid = {}
    for openid, role_id in openids:
        if openid.strip():
            roles_by_openid.setdefault(openid.strip(), set()).add(role_id)
    return [
        VacantRoleRecipient(openid, organization.pk, roles)
        for openid, roles in sorted(roles_by_openid.items())
    ]


def senior_manager_can_proactively_perform(identity, operation):
    """Whether this manager has an explicit proactive shared-operation grant.

    These grants are deliberately operation-specific. A selected role does
    not grant every capability belonging to that role.
    """

    if not identity or identity.role.code not in SENIOR_MANAGER_ROLE_CODES:
        return False
    rule = MERCHANT_OPERATION_ROLE_RULES.get(str(operation or ""))
    if not rule:
        return False
    mode, required_roles = rule
    granted_roles = delegated_role_codes(identity)
    if mode == "all":
        return required_roles <= granted_roles
    return bool(required_roles & granted_roles)


def organization_has_operation_manager(organization_id, operation):
    """Whether an active manager in this organization replaces owner authority.

    For an ``all`` rule, one profile must carry all required roles; separate
    managers with partial grants do not collectively replace the owner.
    """

    rule = MERCHANT_OPERATION_ROLE_RULES.get(str(operation or ""))
    if not organization_id or not rule:
        return False
    mode, required_roles = rule
    profiles = SeniorManagerProfile.objects.filter(
        identity__organization_id=organization_id,
        identity__organization__organization_type__code="MERCHANT",
        identity__is_active=True,
        identity__user__is_active=True,
        identity__user__wx_openid__isnull=False,
        identity__organization__is_active=True,
        identity__role__is_active=True,
        identity__role__code__in=SENIOR_MANAGER_ROLE_CODES,
        managed_roles__is_active=True,
        managed_roles__organization_type__code="MERCHANT",
        managed_roles__code__in=required_roles,
    ).annotate(manager_openid=Trim("identity__user__wx_openid")).exclude(manager_openid="")
    if mode == "all":
        from django.db.models import Count, Q

        return profiles.annotate(
            operation_role_count=Count(
                "managed_roles",
                filter=Q(
                    managed_roles__is_active=True,
                    managed_roles__code__in=required_roles,
                ),
                distinct=True,
            )
        ).filter(operation_role_count=len(required_roles)).exists()
    return profiles.exists()


def shared_operation_recipient_role_codes(organization_id, operation):
    """Return merchant roles that receive actionable operation copies.

    The owner remains in the role cohort until a reachable, authorized
    manager takes over that operation. Publishers resolve managers for any
    vacant roles in the resulting cohort.
    """

    rule = MERCHANT_OPERATION_ROLE_RULES.get(str(operation or ""))
    if not organization_id or not rule:
        return frozenset()
    _mode, collaborator_roles = rule
    recipients = set(collaborator_roles)
    if not organization_has_operation_manager(organization_id, operation):
        recipients.add("merchant_owner")
    return frozenset(recipients)


def identity_can_perform_shared_operation(identity, operation):
    """Apply owner replacement and manager takeover for a named operation."""

    if not identity:
        return False
    rule = MERCHANT_OPERATION_ROLE_RULES.get(str(operation or ""))
    if not rule:
        return False
    _, delegated_roles_for_operation = rule
    if identity.role.code in SENIOR_MANAGER_ROLE_CODES:
        return senior_manager_can_proactively_perform(identity, operation)
    if identity.role.code in delegated_roles_for_operation:
        return True
    if identity.role.code == "merchant_owner":
        return not organization_has_operation_manager(
            identity.organization_id,
            operation,
        )
    return False


def has_open_action_task_copy(identity, uniq_keys=None, *, workitem_ids=None):
    """Require a manager's own actionable task copy for delegated work.

    Role grants decide which roles a senior manager may invite and collaborate
    with. They do not turn the manager into a general-purpose occupant of each
    granted role. Domain mutation services should call this with the exact
    task key for the operation they are about to perform.
    """

    if not identity:
        return False
    if identity.role.code not in SENIOR_MANAGER_ROLE_CODES:
        return True
    keys = {str(value).strip() for value in (uniq_keys or ()) if str(value).strip()}
    ids = {int(value) for value in (workitem_ids or ()) if str(value).isdigit()}
    openid = (identity.user.wx_openid or "").strip()
    if not (keys or ids) or not openid or not delegated_role_codes(identity):
        return False
    from django.db.models import Q
    from workflow.models import UserTask

    tasks = UserTask.objects.filter(
        to_openid=openid,
        workitem__status="OPEN",
        need_action=True,
        state__in=("NEW", "READ"),
    )
    task_scope = []
    if keys:
        task_scope.append(Q(workitem__uniq_key__in=keys))
    if ids:
        task_scope.append(Q(workitem_id__in=ids))
    combined_scope = Q()
    for condition in task_scope:
        combined_scope |= condition
    return bool(task_scope and tasks.filter(combined_scope).exists())


def manager_may_invite_role(identity, target_role_code):
    """A senior manager can invite only a role selected by the owner."""

    return target_role_code in delegated_role_codes(identity)


def available_managed_role_options(identity):
    """Roles an owner may assign to a new senior manager in this organization."""

    if not identity or not identity.role.is_owner_role:
        return []
    from account.models import InvitationScope, RoleInvitationRule

    rows = (
        RoleInvitationRule.objects.filter(
            inviter_role=identity.role,
            scope=InvitationScope.SAME_ORGANIZATION,
            is_active=True,
            target_role__is_active=True,
            target_role__organization_type_id=identity.role.organization_type_id,
            target_role__organization_type__is_active=True,
            target_role__is_owner_role=False,
        )
        .exclude(target_role__code__in=SENIOR_MANAGER_ROLE_CODES | RETIRED_ROLE_CODES)
        .select_related("target_role")
        .order_by("target_role__id")
    )
    return [
        {"code": row.target_role.code, "name": row.target_role.name}
        for row in rows
    ]


class ManagedRoleConflict(ValueError):
    """A role is already allocated to another manager in this organization."""


def lock_managed_roles(organization_id):
    Organization.objects.select_for_update().get(pk=organization_id)


def check_managed_role_conflicts(organization_id, role_ids, *, profile_id=None):
    conflicts = SeniorManagerProfile.objects.filter(
        identity__organization_id=organization_id, managed_roles__in=role_ids,
        identity__is_active=True, identity__user__is_active=True,
        identity__role__is_active=True, identity__role__code__in=SENIOR_MANAGER_ROLE_CODES,
    ).exclude(pk=profile_id)
    if conflicts.exists():
        raise ManagedRoleConflict("本组织已有高级经理分管所选岗位，请先解除原分管关系")


def managed_role_settings(owner, manager_id):
    from account.services.auth_service import get_active_identity

    identity = get_active_identity(owner)
    if not identity or identity.role.code not in {"factory_admin", "merchant_owner"}:
        raise ManagedRoleConflict("只有本组织负责人可以设置高级经理分管岗位")
    if type(manager_id) is not int or not 0 < manager_id < 2**63:
        raise ManagedRoleConflict("高级经理账号无效")
    profile = SeniorManagerProfile.objects.filter(
        identity__user_id=manager_id, identity__organization_id=identity.organization_id,
        identity__is_active=True, identity__user__is_active=True,
        identity__role__is_active=True, identity__role__code__in=SENIOR_MANAGER_ROLE_CODES,
    ).first()
    if profile is None:
        raise ManagedRoleConflict("未找到本组织有效的高级经理")
    return identity, profile


def read_managed_roles(owner, manager_id):
    identity, profile = managed_role_settings(owner, manager_id)
    reserved = set(SeniorManagerProfile.objects.filter(
        identity__organization=identity.organization,
        identity__is_active=True, identity__user__is_active=True,
        identity__role__is_active=True, identity__role__code__in=SENIOR_MANAGER_ROLE_CODES,
    ).exclude(pk=profile.pk).values_list("managed_roles__code", flat=True))
    selected = delegated_role_codes(profile.identity)
    return [
        {**row, "selected": row["code"] in selected, "reserved": row["code"] in reserved}
        for row in available_managed_role_options(identity)
    ]


@transaction.atomic
def update_managed_roles(owner, manager_id, codes):
    identity, profile = managed_role_settings(owner, manager_id)
    if not isinstance(codes, list) or any(not isinstance(code, str) for code in codes):
        raise ManagedRoleConflict("分管岗位格式无效")
    lock_managed_roles(identity.organization_id)
    options = {row["code"] for row in available_managed_role_options(identity)}
    if len(codes) != len(set(codes)) or not set(codes) <= options:
        raise ManagedRoleConflict("请选择本组织有效且不重复的分管岗位")
    roles = OrganizationRole.objects.filter(
        organization_type_id=identity.role.organization_type_id, code__in=codes,
    )
    check_managed_role_conflicts(identity.organization_id, roles, profile_id=profile.pk)
    profile.managed_roles.set(roles)
    return sorted(codes)
