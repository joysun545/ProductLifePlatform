from django.db import transaction

from account.services.auth_service import get_active_identity
from lifecycle.models import DeviceState, OrganizationDevice
from lifecycle.services.event_service import EventService


class DeviceStateMachine:
    """只修改 DeviceState，并同步设备的组织归属记录。"""

    @staticmethod
    def _operator_organization(operator):
        if not operator:
            return None
        identity = get_active_identity(operator)
        return identity.organization if identity else None

    @staticmethod
    @transaction.atomic
    def ship(device, operator=None):
        state = DeviceState.objects.select_for_update().get(device=device)
        if state.status != "IN_STOCK":
            raise ValueError(f"设备当前状态 {state.status} 不能执行发货")

        organization = DeviceStateMachine._operator_organization(operator)
        from_organization = state.current_organization or organization
        event = EventService.create_event(
            device=device,
            event_type="SHIP",
            operator=operator,
            organization=organization,
            from_organization=from_organization,
            data={"action": "ship"},
        )

        state.status = "SHIPPED"
        state.last_event = event
        state.last_event_time = event.created_at
        state.save(
            update_fields=("status", "last_event", "last_event_time", "updated_at")
        )

        OrganizationDevice.objects.filter(device=device).update(
            status="TRANSFERRED",
            last_event=event,
        )

    @staticmethod
    @transaction.atomic
    def install(device, operator=None):
        state = DeviceState.objects.select_for_update().get(device=device)
        if state.status not in {"IN_STOCK", "SHIPPED"}:
            raise ValueError(f"设备当前状态 {state.status} 不能执行安装")

        organization = DeviceStateMachine._operator_organization(operator)
        event = EventService.create_event(
            device=device,
            event_type="INSTALL",
            operator=operator,
            organization=organization,
            data={"action": "install"},
        )

        state.status = "INSTALLED"
        state.install_status = "INSTALLED"
        if organization:
            state.current_organization = organization
        state.last_event = event
        state.last_event_time = event.created_at
        state.save(
            update_fields=(
                "status",
                "install_status",
                "current_organization",
                "last_event",
                "last_event_time",
                "updated_at",
            )
        )

        if organization:
            OrganizationDevice.objects.update_or_create(
                device=device,
                defaults={
                    "organization": organization,
                    "status": "INSTALLED",
                    "last_event": event,
                },
            )

    @staticmethod
    @transaction.atomic
    def activate(device, operator=None):
        state = DeviceState.objects.select_for_update().get(device=device)
        if state.status != "INSTALLED":
            raise ValueError(f"设备当前状态 {state.status} 不能执行激活")

        organization = DeviceStateMachine._operator_organization(operator)
        event = EventService.create_event(
            device=device,
            event_type="ACTIVATE",
            operator=operator,
            organization=organization,
            data={"action": "activate"},
        )

        state.status = "ACTIVATED"
        state.activation_status = "ACTIVATED"
        state.last_event = event
        state.last_event_time = event.created_at
        state.save(
            update_fields=(
                "status",
                "activation_status",
                "last_event",
                "last_event_time",
                "updated_at",
            )
        )
