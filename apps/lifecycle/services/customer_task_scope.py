"""Customer inbox collaboration follows the currently shared vehicle devices."""

from django.db.models import Q

from account.models import UserOrganization
from lifecycle.models import CustomerDeviceBinding, DriverVehicleBinding

AFTERSALES_CUSTOMER_NOTICE = "AFTERSALES_CUSTOMER_COLLABORATION_NOTICE"


def device_customer_openids(organization_id, device_ids):
    bindings = CustomerDeviceBinding.objects.filter(
        customer_organization_id=organization_id, device_id__in=device_ids,
        status=CustomerDeviceBinding.Status.ACTIVE,
    )
    if not bindings.exists():
        return []
    drivers = DriverVehicleBinding.objects.filter(
        vehicle_id__in=bindings.values("vehicle_id"),
        vehicle__customer_organization_id=organization_id, is_active=True,
    ).values("driver_id")
    return list(UserOrganization.objects.filter(
        Q(role__code="customer_owner") | Q(role__code="driver", user_id__in=drivers),
        organization_id=organization_id, organization__is_active=True,
        organization__organization_type__is_active=True, role__is_active=True,
        user__is_active=True, is_active=True,
    ).exclude(user__wx_openid__isnull=True).exclude(user__wx_openid="").values_list(
        "user__wx_openid", flat=True,
    ).distinct())


def _visible_application_ids(identity, application_ids):
    if not identity or not application_ids:
        return set()
    devices = CustomerDeviceBinding.objects.filter(
        customer_organization_id=identity.organization_id,
        status=CustomerDeviceBinding.Status.ACTIVE,
    )
    if identity.role.code == "driver":
        vehicles = DriverVehicleBinding.objects.filter(
            driver_id=identity.user_id, is_active=True,
            vehicle__customer_organization_id=identity.organization_id,
        ).values("vehicle_id")
        devices = devices.filter(vehicle_id__in=vehicles)
    device_ids = devices.values("device_id")
    from lifecycle.services.aftersales_access import visible_cases

    return set(visible_cases(identity.user, identity).filter(
        Q(device_id__in=device_ids) | Q(replacement_device_id__in=device_ids),
        pk__in=application_ids,
    ).values_list("pk", flat=True))


def filter_aftersales_customer_tasks(tasks, openid):
    case_tasks = [
        task for task in tasks
        if task.workitem.domain == "AFTERSALE"
        and task.workitem.biz_ref.startswith("AfterSalesApplication:")
    ]
    if not case_tasks:
        return tasks
    identity = UserOrganization.objects.filter(
        user__wx_openid=openid, user__is_active=True, is_active=True,
        role__code__in=("customer_owner", "driver"), role__is_active=True,
        organization__is_active=True, organization__organization_type__is_active=True,
    ).select_related("role", "user", "organization__factory_root", "organization__organization_type").first()
    driver = bool(identity and identity.role.code == "driver")
    scoped = {}
    for task in case_tasks:
        if driver or task.workitem.type == AFTERSALES_CUSTOMER_NOTICE:
            ref = task.workitem.biz_ref.removeprefix("AfterSalesApplication:")
            scoped[task.pk] = int(ref) if ref.isascii() and ref.isdecimal() and len(ref) < 19 else None
    if not scoped:
        return tasks
    visible = _visible_application_ids(identity, {value for value in scoped.values() if value is not None})
    return [task for task in tasks if task.pk not in scoped or scoped[task.pk] in visible]
