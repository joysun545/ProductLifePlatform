"""设备属主关系的唯一推算入口。"""

from .policies import INSTALLER_ROLE_CODES, evaluate_device_access
from .queries import (
    descendant_organization_ids,
    devices_owned_directly_by,
    devices_under_account,
    devices_under_organization,
    devices_under_region,
)
from .resolver import resolve_device_ownership
from .types import DeviceAccessDecision, DeviceOwnershipContext, ViewerRelation

__all__ = [
    "DeviceAccessDecision",
    "DeviceOwnershipContext",
    "INSTALLER_ROLE_CODES",
    "ViewerRelation",
    "descendant_organization_ids",
    "devices_owned_directly_by",
    "devices_under_account",
    "devices_under_organization",
    "devices_under_region",
    "evaluate_device_access",
    "resolve_device_ownership",
]

