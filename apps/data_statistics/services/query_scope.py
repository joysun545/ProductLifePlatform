from django.db.models import Q

from lifecycle.models import Device


def device_scope(identity):
    """返回角色可统计的设备集合；始终按设备去重。"""
    org = identity.organization
    org_type = identity.organization_type
    factory_root = org.get_factory_root()
    if not factory_root:
        return Device.objects.none()
    if org_type == "OWNER":
        return Device.objects.filter(batch__manufacturer=org)
    tenant_devices = Device.objects.filter(
        batch__manufacturer__factory_root_id=factory_root.pk
    )
    if org_type == "MERCHANT":
        return tenant_devices.filter(
            Q(organization_record__organization=org)
            | Q(lifecycle_events__organization=org)
            | Q(lifecycle_events__from_organization=org)
            | Q(lifecycle_events__to_organization=org)
            | Q(installation__organization=org)
            | Q(activation__organization=org)
        ).distinct()
    if org_type == "SERVICE":
        return tenant_devices.filter(
            Q(after_sales_applications__assigned_organization=org)
            | Q(installation__organization=org)
        ).distinct()
    if org_type == "SUPPLIER":
        return tenant_devices.filter(material_fault_records__supplier=org).distinct()
    return Device.objects.none()
