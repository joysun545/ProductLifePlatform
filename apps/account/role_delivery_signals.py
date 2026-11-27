"""Reconcile pending work when organization role coverage changes."""

from django.db.models.signals import m2m_changed, post_save, pre_save
from django.dispatch import receiver

from account.models import OrganizationRole, SeniorManagerProfile, User, UserOrganization
from account.services.retired_roles import RETIRED_ROLE_CODES
from account.services.role_delegation_service import (
    ManagedRoleConflict,
    check_managed_role_conflicts,
    lock_managed_roles,
)
from workflow.services.role_delivery import has_role_routed_work, reconcile_organization_role_delivery


@receiver(m2m_changed, sender=SeniorManagerProfile.managed_roles.through)
def managed_roles_changed(sender, instance, action, reverse, pk_set, **kwargs):
    if reverse:
        if action.startswith("pre_"):
            raise ManagedRoleConflict("请从高级经理的分管设置修改岗位")
        return
    organization_id = instance.identity.organization_id
    if action.startswith("pre_"):
        lock_managed_roles(organization_id)
    if action == "pre_add":
        if OrganizationRole.objects.filter(pk__in=pk_set, code__in=RETIRED_ROLE_CODES).exists():
            raise ManagedRoleConflict("已退出的岗位不能新增分管关系")
        check_managed_role_conflicts(organization_id, pk_set, profile_id=instance.pk)
    if action.startswith("post_"):
        reconcile_organization_role_delivery(organization_id)


@receiver(pre_save, sender=UserOrganization)
def capture_membership_before_save(sender, instance, raw, **kwargs):
    if not raw and instance.pk:
        instance._old_coverage = sender.objects.filter(pk=instance.pk).values_list(
            "organization_id", "role_id", "is_active", "user__wx_openid", "role__code",
        ).first()
        before = instance._old_coverage
        if before and before[:2] != (instance.organization_id, instance.role_id):
            if SeniorManagerProfile.objects.filter(identity=instance, managed_roles__isnull=False).exists():
                raise ManagedRoleConflict("请先解除高级经理的分管关系，再调岗或更换组织")
        if before and not before[2] and instance.is_active:
            _check_reactivated_profile(instance)


@receiver(post_save, sender=UserOrganization)
def membership_changed(sender, instance, raw, **kwargs):
    current = (instance.organization_id, instance.role_id, instance.is_active)
    before = getattr(instance, "_old_coverage", None)
    if raw or (before and before[:3] == current):
        return
    organization_ids = {current[0]} | ({before[0]} if before else set())
    organizations = SeniorManagerProfile.objects.filter(
        identity__organization_id__in=organization_ids,
    ).values_list("identity__organization_id", flat=True).distinct()
    with_managers = set(organizations)
    for organization_id in sorted(organization_ids):
        if organization_id not in with_managers and not has_role_routed_work(organization_id):
            continue
        former = (before[3], before[1], before[4]) if before and organization_id == before[0] else None
        reconcile_organization_role_delivery(organization_id, former_member=former)


def _check_reactivated_profile(identity):
    profile = SeniorManagerProfile.objects.filter(identity=identity).first()
    if profile:
        check_managed_role_conflicts(
            identity.organization_id, profile.managed_roles.all(), profile_id=profile.pk,
        )


@receiver(pre_save, sender=User)
def capture_user_before_save(sender, instance, raw, **kwargs):
    if not raw and instance.pk:
        instance._old_coverage = sender.objects.filter(pk=instance.pk).values_list(
            "is_active", "wx_openid",
        ).first()
        if instance._old_coverage and not instance._old_coverage[0] and instance.is_active:
            identity = UserOrganization.objects.filter(user=instance, is_active=True).first()
            if identity:
                _check_reactivated_profile(identity)


@receiver(post_save, sender=User)
def user_coverage_changed(sender, instance, raw, created, **kwargs):
    if raw or created or getattr(instance, "_old_coverage", None) == (instance.is_active, instance.wx_openid):
        return
    organization_id = UserOrganization.objects.filter(user=instance).values_list("organization_id", flat=True).first()
    if organization_id and (
        SeniorManagerProfile.objects.filter(identity__organization_id=organization_id).exists()
        or has_role_routed_work(organization_id)
    ):
        reconcile_organization_role_delivery(organization_id)
