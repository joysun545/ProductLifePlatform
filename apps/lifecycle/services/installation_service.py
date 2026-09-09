from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from account.models import OrganizationRole
from account.services.auth_service import get_active_identity
from common.domain.device_ownership import (
    evaluate_device_access,
    resolve_device_ownership,
)
from lifecycle.models import (
    Device,
    InstallationInvitation,
    InstallationRecord,
)
from lifecycle.services.state_machine import DeviceStateMachine


DEFAULT_INVITATION_LIFETIME_MINUTES = 10


class InstallationError(Exception):
    """安装链路不能继续。"""


class InstallationPermissionError(InstallationError):
    """当前账号不能对该设备执行操作。"""


class InstallationStateError(InstallationError):
    """设备当前状态不允许执行操作。"""


class InstallationService:
    @staticmethod
    def _locked_device(device_id):
        return (
            Device.objects.select_for_update()
            .select_related(
                "state",
                "organization_record__organization__organization_type",
                "installation__organization__organization_type",
                "installation__operator",
                "batch__manufacturer__organization_type",
                "batch__bom",
                "product_model__category",
                "product_model__brand",
            )
            .filter(pk=device_id)
            .first()
        )

    @staticmethod
    def _target_customer_owner_role():
        role = (
            OrganizationRole.objects.select_related("organization_type")
            .filter(
                code="customer_owner",
                organization_type__code="CUSTOMER",
                organization_type__is_active=True,
                is_active=True,
            )
            .first()
        )
        if not role:
            raise InstallationStateError("系统尚未配置用户负责人岗位")
        return role

    @classmethod
    def _create_invitation(cls, *, installation, identity):
        now = timezone.now()
        InstallationInvitation.objects.filter(
            device=installation.device,
            status=InstallationInvitation.Status.PENDING,
            expires_at__lte=now,
        ).update(status=InstallationInvitation.Status.EXPIRED)
        InstallationInvitation.objects.filter(
            device=installation.device,
            status=InstallationInvitation.Status.PENDING,
        ).update(status=InstallationInvitation.Status.CANCELLED)

        return InstallationInvitation.objects.create(
            installation=installation,
            device=installation.device,
            inviter=identity.user,
            source_organization=identity.organization,
            target_role=cls._target_customer_owner_role(),
            expires_at=now + timedelta(
                minutes=DEFAULT_INVITATION_LIFETIME_MINUTES
            ),
        )

    @classmethod
    @transaction.atomic
    def install(cls, *, device_id, operator, location=""):
        identity = get_active_identity(operator)
        if not identity:
            raise InstallationPermissionError("游客不能执行装车操作")

        device = cls._locked_device(device_id)
        if not device:
            raise InstallationStateError("设备不存在")

        # 先读取目标岗位，任何基础数据缺失都会在改变设备状态前终止事务。
        cls._target_customer_owner_role()
        ownership = resolve_device_ownership(
            device=device,
            viewer_identity=identity,
        )
        access = evaluate_device_access(
            device=device,
            identity=identity,
            ownership=ownership,
        )
        if not access.allows("install_device"):
            if not ownership.is_direct_owner:
                raise InstallationPermissionError("该设备不是本组织的库存设备")
            raise InstallationStateError("设备当前状态不能执行装车操作")

        if InstallationRecord.objects.filter(device=device).exists():
            raise InstallationStateError("该设备已经存在安装记录")

        DeviceStateMachine.install(device, operator=operator)
        device.state.refresh_from_db()

        installation = InstallationRecord.objects.create(
            device=device,
            organization=identity.organization,
            operator=operator,
            lifecycle_event=device.state.last_event,
            installed_at=device.state.last_event_time or timezone.now(),
            location=(location or "").strip(),
        )
        invitation = cls._create_invitation(
            installation=installation,
            identity=identity,
        )
        return installation, invitation

    @classmethod
    @transaction.atomic
    def refresh_customer_invitation(cls, *, device_id, operator):
        identity = get_active_identity(operator)
        if not identity:
            raise InstallationPermissionError("游客不能生成用户邀请")

        device = cls._locked_device(device_id)
        if not device:
            raise InstallationStateError("设备不存在")

        ownership = resolve_device_ownership(
            device=device,
            viewer_identity=identity,
        )
        access = evaluate_device_access(
            device=device,
            identity=identity,
            ownership=ownership,
        )
        if not access.allows("create_customer_invitation"):
            if not ownership.is_direct_owner:
                raise InstallationPermissionError("该设备不属于本组织")
            raise InstallationStateError("设备当前状态不能生成用户邀请")

        installation = (
            InstallationRecord.objects.select_for_update()
            .filter(
                device=device,
                organization=identity.organization,
                status=InstallationRecord.Status.INSTALLED,
            )
            .first()
        )
        if not installation:
            raise InstallationStateError("没有找到有效安装记录")

        invitation = cls._create_invitation(
            installation=installation,
            identity=identity,
        )
        return installation, invitation
