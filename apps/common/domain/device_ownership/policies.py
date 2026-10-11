from django.core.exceptions import ObjectDoesNotExist

from account.services.role_delegation_service import (
    identity_can_perform_shared_operation,
)

from .types import DeviceAccessDecision


INSTALLER_ROLE_CODES = {
    "merchant_owner",
    "merchant_electrician",
}


def _related_object(instance, name):
    try:
        return getattr(instance, name)
    except ObjectDoesNotExist:
        return None


def evaluate_device_access(*, device, identity, ownership):
    """基于岗位、设备关系和当前状态生成统一能力集合。"""

    capabilities = ["view_public_detail"]

    if not identity:
        return DeviceAccessDecision(
            view_mode="public",
            capabilities=tuple(capabilities),
            ownership=ownership,
        )

    capabilities.append("view_region_owner")
    if not ownership.is_chain_related:
        return DeviceAccessDecision(
            view_mode="registered",
            capabilities=tuple(capabilities),
            ownership=ownership,
        )

    capabilities.extend(
        (
            "view_device_detail",
            "view_ownership",
            "view_traceability",
        )
    )

    state = _related_object(device, "state")
    organization_record = _related_object(device, "organization_record")
    installation = _related_object(device, "installation")
    role_code = identity.role.code

    has_installer_role = identity_can_perform_shared_operation(
        identity, "install_device"
    )
    is_direct_inventory = bool(
        ownership.is_direct_owner
        and organization_record
        and organization_record.organization_id == identity.organization_id
        and organization_record.status == "IN_STOCK"
    )
    can_install = bool(
        has_installer_role
        and is_direct_inventory
        and state
        and state.status == "IN_STOCK"
        and state.install_status == "NOT_INSTALLED"
        and state.activation_status == "NOT_ACTIVATED"
        and not installation
    )
    can_create_invite = bool(
        has_installer_role
        and ownership.is_direct_owner
        and state
        and state.install_status == "INSTALLED"
        and state.activation_status == "NOT_ACTIVATED"
        and installation
        and installation.status == "INSTALLED"
        and installation.organization_id == identity.organization_id
    )
    can_activate = bool(
        role_code == "customer_owner"
        and ownership.is_direct_owner
        and state
        and state.install_status == "INSTALLED"
        and state.activation_status == "NOT_ACTIVATED"
        and installation
        and installation.status == "INSTALLED"
    )

    if can_install:
        capabilities.append("install_device")
    if can_create_invite:
        capabilities.append("create_customer_invitation")
    if can_activate:
        capabilities.append("activate_device")

    return DeviceAccessDecision(
        view_mode="chain",
        capabilities=tuple(capabilities),
        ownership=ownership,
    )
