from django.db.models import Q

from lifecycle.models import Device


def device_scope(identity):
    """返回角色可统计的设备集合；始终按设备去重。"""
    org = identity.organization
    org_type = identity.organization_type
    if org_type == "OWNER":
        return Device.objects.filter(batch__manufacturer=org)
    if org_type == "MERCHANT":
        return Device.objects.filter(
            Q(organization_record__organization=org)
            | Q(lifecycle_events__organization=org)
            | Q(lifecycle_events__from_organization=org)
            | Q(lifecycle_events__to_organization=org)
            | Q(installation__organization=org)
            | Q(activation__organization=org)
        ).distinct()
    if org_type == "SERVICE":
        return Device.objects.filter(
            Q(after_sales_applications__assigned_organization=org)
            | Q(installation__organization=org)
        ).distinct()
    if org_type == "SUPPLIER":
        return Device.objects.filter(material_fault_records__supplier=org).distinct()
    return Device.objects.none()
