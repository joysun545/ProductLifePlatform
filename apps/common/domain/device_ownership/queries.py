from account.models import (
    Organization,
    OrganizationRelation,
    OrganizationRelationType,
)
from django.db.models import Q

from lifecycle.models import Device, InstallationInvitation


def descendant_organization_ids(organization, *, include_self=True):
    """返回经销链路下全部组织ID，供统计查询和任务路由复用。"""

    discovered = {organization.pk} if include_self else set()
    frontier = {organization.pk}

    while frontier:
        child_ids = set(
            OrganizationRelation.objects.filter(
                source_organization_id__in=frontier,
                relation_type=OrganizationRelationType.DISTRIBUTION,
                is_active=True,
                target_organization__is_active=True,
            ).values_list("target_organization_id", flat=True)
        )
        child_ids -= discovered
        if not child_ids:
            break
        discovered.update(child_ids)
        frontier = child_ids

    if not include_self:
        discovered.discard(organization.pk)
    return discovered


def devices_owned_directly_by(organization):
    return Device.objects.filter(
        organization_record__organization=organization
    ).distinct()


def devices_under_organization(organization):
    organization_ids = descendant_organization_ids(organization)
    return Device.objects.filter(
        organization_record__organization_id__in=organization_ids
    ).distinct()


def devices_under_account(identity):
    direct_child_ids = set(
        OrganizationRelation.objects.filter(
            created_by_invitation__inviter_id=identity.user_id,
            relation_type=OrganizationRelationType.DISTRIBUTION,
            is_active=True,
        ).values_list("target_organization_id", flat=True)
    )

    organization_ids = set()
    for child_id in direct_child_ids:
        child = Organization.objects.filter(pk=child_id).first()
        if child:
            organization_ids.update(descendant_organization_ids(child))

    # 安装邀请按具体设备建立用户链路。同一用户组织可能拥有多台由不同
    # 安装账号交接的设备，因此这里不能把整个用户组织都算到某个账号下。
    installation_device_ids = InstallationInvitation.objects.filter(
        inviter_id=identity.user_id,
        source_organization_id=identity.organization_id,
        status=InstallationInvitation.Status.ACCEPTED,
        target_organization__is_active=True,
    ).values_list("device_id", flat=True)

    return Device.objects.filter(
        Q(organization_record__organization_id__in=organization_ids)
        | Q(id__in=installation_device_ids)
    ).distinct()


def devices_under_region(regional_organization):
    return devices_under_organization(regional_organization)
