from django.core.exceptions import ObjectDoesNotExist

from account.models import (
    OrganizationRelation,
    OrganizationRelationType,
    UserOrganization,
)

from .types import DeviceOwnershipContext, OwnershipLink, ViewerRelation


MAX_OWNERSHIP_DEPTH = 32


def _related_object(instance, name):
    try:
        return getattr(instance, name)
    except ObjectDoesNotExist:
        return None


def _direct_owner(device):
    # 当前归属是库存、统计和任务路由的事实来源。这里主动从数据库读取，
    # 避免调用方曾经使用 update()/update_or_create() 后，Device 实例上的
    # 反向 OneToOne 缓存仍然保留旧组织。
    from lifecycle.models import DeviceState, OrganizationDevice

    organization_record = (
        OrganizationDevice.objects.select_related(
            "organization__organization_type"
        )
        .filter(device_id=device.pk)
        .first()
    )
    if organization_record:
        return organization_record.organization

    state = (
        DeviceState.objects.select_related(
            "current_organization__organization_type"
        )
        .filter(device_id=device.pk)
        .first()
    )
    if state:
        return state.current_organization
    return None


def _parent_relation(organization_id, factory_root_id=None):
    """
    返回当前有效的直接经销上级。

    正常业务数据中一个商家只能有一条有效直接经销上级；若历史数据存在
    多条，采用最后建立的一条，保证扫码、统计和任务推送得到同一结果。
    """

    relations = OrganizationRelation.objects.select_related(
            "source_organization__organization_type",
            "target_organization__organization_type",
            "created_by_invitation__inviter",
        ).filter(
            target_organization_id=organization_id,
            relation_type=OrganizationRelationType.DISTRIBUTION,
            is_active=True,
            source_organization__is_active=True,
        )
    if factory_root_id is not None:
        relations = relations.filter(
            source_organization__factory_root_id=factory_root_id,
            target_organization__factory_root_id=factory_root_id,
        )
    return relations.order_by("-created_at", "-id").first()


def _upstream_identity(relation, device=None):
    invitation = _related_object(relation, "created_by_invitation")
    inviter_id = invitation.inviter_id if invitation else None

    # 商家装车后邀请用户负责人不经过普通 InvitationRecord。设备进入用户
    # 组织后，使用该设备已经接受的安装邀请还原具体的上级邀请账号。
    if not inviter_id and device is not None:
        from lifecycle.models import InstallationInvitation

        installation_invitation = (
            InstallationInvitation.objects.filter(
                device_id=device.pk,
                source_organization_id=relation.source_organization_id,
                target_organization_id=relation.target_organization_id,
                status=InstallationInvitation.Status.ACCEPTED,
            )
            .order_by("-accepted_at", "-id")
            .first()
        )
        if installation_invitation:
            inviter_id = installation_invitation.inviter_id

    if not inviter_id:
        return None

    return (
        UserOrganization.objects.select_related(
            "user",
            "organization__organization_type",
            "role",
        )
        .filter(
            user_id=inviter_id,
            organization_id=relation.source_organization_id,
            is_active=True,
        )
        .first()
    )


def _build_links(direct_owner, device=None):
    if not direct_owner:
        return ()

    factory_root = direct_owner.get_factory_root()
    if device is not None:
        manufacturer = device.batch.manufacturer
        device_root = manufacturer.get_factory_root()
        if not device_root or (factory_root and device_root.pk != factory_root.pk):
            return ()
        factory_root = device_root
    if not factory_root:
        return ()

    links = []
    visited = {direct_owner.pk}
    current_id = direct_owner.pk

    for _depth in range(MAX_OWNERSHIP_DEPTH):
        relation = _parent_relation(current_id, factory_root.pk)
        if not relation:
            break
        if relation.source_organization_id in visited:
            break

        visited.add(relation.source_organization_id)
        links.append(
            OwnershipLink(
                relation=relation,
                upstream_account_identity=_upstream_identity(
                    relation,
                    device=device,
                ),
            )
        )
        current_id = relation.source_organization_id

    return tuple(links)


def _regional_owner(direct_owner, links):
    """区域属主定义为经销链路中层级最高的有效商家组织。"""

    path = [link.relation.source_organization for link in reversed(links)]
    if direct_owner:
        path.append(direct_owner)

    for organization in path:
        if organization.organization_type.code == "MERCHANT":
            return organization
    return None


def resolve_device_ownership(*, device, viewer_identity=None):
    """推算设备完整属主链路以及当前账号在链路中的位置。"""

    direct_owner = _direct_owner(device)
    links = _build_links(direct_owner, device=device)
    regional_owner = _regional_owner(direct_owner, links)

    viewer_user_id = viewer_identity.user_id if viewer_identity else None
    viewer_org_id = viewer_identity.organization_id if viewer_identity else None
    upstream_org_ids = {
        link.relation.source_organization_id for link in links
    }
    upstream_account_user_ids = {
        link.upstream_account_identity.user_id
        for link in links
        if link.upstream_account_identity
    }

    is_direct_owner = bool(
        viewer_org_id and direct_owner and viewer_org_id == direct_owner.pk
    )
    is_upstream_account_owner = bool(
        viewer_user_id and viewer_user_id in upstream_account_user_ids
    )
    is_upstream_organization = bool(
        viewer_org_id and viewer_org_id in upstream_org_ids
    )
    is_upstream_organization_owner = bool(
        is_upstream_organization
        and viewer_identity
        and viewer_identity.role.is_owner_role
    )
    is_regional_owner = bool(
        viewer_org_id
        and regional_owner
        and viewer_org_id == regional_owner.pk
    )

    if not viewer_identity:
        viewer_relation = ViewerRelation.PUBLIC
    elif is_direct_owner:
        viewer_relation = ViewerRelation.DIRECT_OWNER
    elif is_upstream_account_owner:
        viewer_relation = ViewerRelation.UPSTREAM_ACCOUNT_OWNER
    elif is_upstream_organization_owner:
        viewer_relation = ViewerRelation.UPSTREAM_ORGANIZATION_OWNER
    elif is_regional_owner:
        viewer_relation = ViewerRelation.REGIONAL_OWNER
    elif is_upstream_organization:
        viewer_relation = ViewerRelation.UPSTREAM_ORGANIZATION_MEMBER
    else:
        viewer_relation = ViewerRelation.UNRELATED

    return DeviceOwnershipContext(
        viewer_identity=viewer_identity,
        direct_owner=direct_owner,
        links=links,
        regional_owner=regional_owner,
        viewer_relation=viewer_relation,
        is_direct_owner=is_direct_owner,
        is_upstream_account_owner=is_upstream_account_owner,
        is_upstream_organization=is_upstream_organization,
        is_upstream_organization_owner=is_upstream_organization_owner,
        is_regional_owner=is_regional_owner,
    )
