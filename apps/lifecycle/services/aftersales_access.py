"""Read scopes shared by after-sales lists, details and settlement evidence."""
from django.db.models import F, Q
from account.models import UserOrganization
from account.services.auth_service import get_active_identity
from account.services.role_delegation_service import (
    SENIOR_MANAGER_ROLE_CODES, delegated_role_codes, delegated_role_user_ids, identity_has_role,
)
from lifecycle.models import (
    AfterSalesApplication, AfterSalesServiceStationLiaison,
    CustomerDeviceBinding, DriverVehicleBinding, Device,
)

INSTALL_ROLES = {"merchant_electrician", "service_electrician"}
FACTORY_READ_ROLES = {"factory_admin", "factory_owner", "factory_manager", "factory_aftersales", "factory_sales"}


def station_links(identity):
    root = identity.organization.get_factory_root()
    if not root:
        return AfterSalesServiceStationLiaison.objects.none()
    return AfterSalesServiceStationLiaison.objects.filter(
        identity=identity, identity__is_active=True,
        identity__user__is_active=True, identity__role__is_active=True,
        application__status="APPROVED", application__organization=identity.organization,
        application__organization__is_active=True,
        application__organization__factory_root_id=root.pk,
    )


def customer_device_ids(identity):
    records = Device.objects.filter(organization_record__organization=identity.organization)
    if identity.role.code == "driver":
        vehicles = DriverVehicleBinding.objects.filter(
            driver=identity.user, is_active=True,
            vehicle__customer_organization=identity.organization,
        ).values("vehicle_id")
        bindings = CustomerDeviceBinding.objects.filter(
            customer_organization=identity.organization, status="ACTIVE",
            vehicle_id__in=vehicles,
        ).values("device_id")
        records = records.filter(pk__in=bindings)
    return records.values("pk")


def visible_cases(user, identity=None):
    identity = identity or get_active_identity(user)
    empty = AfterSalesApplication.objects.none()
    if not identity:
        return empty
    org = identity.organization
    root = org.get_factory_root()
    if not root:
        return empty
    role = identity.role.code
    qs = AfterSalesApplication.objects.filter(factory_organization=root)
    org_type = org.organization_type.code
    if org_type == "CUSTOMER":
        if role not in {"customer_owner", "driver"}:
            return empty
        owned = Q(device_id__in=customer_device_ids(identity))
        incoming = Q(replacement_device_id__in=customer_device_ids(identity))
        historical = Q(replacement_flow__isnull=False,device__customer_binding__customer_organization=org)
        if role=='driver':
            vehicles=DriverVehicleBinding.objects.filter(driver=user,is_active=True,vehicle__customer_organization=org).values('vehicle_id')
            historical &= Q(device__customer_binding__vehicle_id__in=vehicles)
        return qs.filter(customer_organization=org).filter(owned|incoming|historical).distinct()
    if org_type == "OWNER":
        if role in FACTORY_READ_ROLES or identity.role.is_owner_role:
            return qs
        if role == "factory_senior_manager" and "factory_aftersales" in delegated_role_codes(identity):
            return qs
    # Individual assignments stay readable, including when station qualification expires.
    scope = Q(applicant_id=user.pk) | Q(assigned_user_id=user.pk)
    scope |= Q(escalation__source_user_id=user.pk, escalation__source_org_id=org.pk)
    # Only channel operators in an actual recorded leg receive channel visibility.
    from lifecycle.services.replacement_flow_service import _org_user
    if _org_user(user,org.pk):
        scope |= Q(replacement_flow__legs__from_organization=org)|Q(replacement_flow__legs__to_organization=org)
        for n in range(23):scope |= Q(**{f'escalation__factory_transport__participants__{n}':org.pk})
    if org_type == 'OWNER' and role in SENIOR_MANAGER_ROLE_CODES and identity_has_role(identity, 'factory_stock'):
        from sales.models import ReplacementSupplyPlanItem

        supply_cases = ReplacementSupplyPlanItem.objects.filter(
            status__in=('RESERVED', 'SENT'), plan__status='OPEN', plan__factory=org,
            plan__normal_order__factory_organization=org,
            plan__normal_order__merchant_organization_id=F('plan__merchant_id'),
            leg__direction='SUPPLY', leg__sequence=1, leg__from_organization=org,
            leg__to_organization_id=F('plan__merchant_id'), leg__flow__mode='LOCAL',
            leg__flow__swapped_at__isnull=False,
        ).values('leg__flow__application_id')
        scope |= Q(pk__in=supply_cases)
    if org_type=='OWNER' and identity_has_role(identity,'factory_logistics'):
        scope |= Q(factory_organization=org,escalation__requires_replacement_flow_v6=True,escalation__decision='FACTORY_REPAIR')
    links = station_links(identity)
    station_ids = list(links.values_list("application_id", flat=True))
    if station_ids:
        scope |= Q(escalation__source_org_id=org.pk, escalation__source_station_id__in=station_ids)
        scope |= Q(dispatch_route="SERVICE_STATION", assigned_organization=org,
                   assignee_snapshot__service_station_application_id__in=station_ids)
    if role in INSTALL_ROLES:
        scope |= Q(device__installation__operator_id=user.pk, device__installation__status="INSTALLED")
    if org_type in {"MERCHANT", "SERVICE"} and identity.role.is_owner_role:
        scope |= Q(assigned_organization=org)
        scope |= Q(escalation__source_org_id=org.pk)
        scope |= Q(device__installation__organization=org, device__installation__status="INSTALLED")
    elif role == "merchant_senior_manager":
        # Managed roles alone do not authorize unrelated employees with the same title.
        managed = set()
        for code in delegated_role_codes(identity):
            managed.update(delegated_role_user_ids(identity, code))
        if managed:
            scope |= Q(assigned_organization=org, assigned_user_id__in=managed)
            scope |= Q(escalation__source_org_id=org.pk, escalation__source_user_id__in=managed)
            installers = UserOrganization.objects.filter(
                user_id__in=managed, organization=org, role__code__in=INSTALL_ROLES,
                is_active=True,
            ).values("user_id")
            scope |= Q(device__installation__organization=org,
                       device__installation__operator_id__in=installers,
                       device__installation__status="INSTALLED")
            managed_stations = AfterSalesServiceStationLiaison.objects.filter(
                identity__user_id__in=managed, identity__organization=org,
                identity__is_active=True, identity__user__is_active=True,
                identity__role__is_active=True, application__status="APPROVED",
                application__organization=org,
            ).values("application_id")
            scope |= Q(escalation__source_org_id=org.pk, escalation__source_station_id__in=managed_stations)
            scope |= Q(dispatch_route="SERVICE_STATION", assigned_organization=org,
                       assignee_snapshot__service_station_application_id__in=managed_stations)
    return qs.filter(scope).distinct()


def capabilities(user):
    identity = get_active_identity(user)
    if not identity:
        return {"can_view_aftersales": False, "can_view_repair_basis": False}
    role = identity.role.code
    codes = delegated_role_codes(identity)
    manager_station = False
    if role == "merchant_senior_manager" and codes:
        manager_station = AfterSalesServiceStationLiaison.objects.filter(
            identity__organization=identity.organization,
            identity__role__code__in=codes, identity__role__is_active=True,
            identity__is_active=True, identity__user__is_active=True,
            application__organization=identity.organization, application__status="APPROVED",
        ).exists()
    allowed = (
        role in FACTORY_READ_ROLES | INSTALL_ROLES | {"customer_owner", "driver", "merchant_owner", "service_owner"}
        or identity.role.is_owner_role
        or (role == "factory_senior_manager" and "factory_aftersales" in codes)
        or (role == "merchant_senior_manager" and "merchant_electrician" in codes)
        or manager_station
        or station_links(identity).exists()
        or visible_cases(user, identity).exists()
    )
    return {"can_view_aftersales": bool(allowed), "can_view_repair_basis": bool(allowed)}
