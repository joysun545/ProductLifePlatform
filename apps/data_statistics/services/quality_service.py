from django.db.models import Count

from quality_trace.models import QualityRecallCampaign, QualityRecallDevice

from .query_scope import device_scope


def build_quality_statistics(identity):
    org = identity.organization
    if identity.organization_type == "OWNER":
        campaigns = QualityRecallCampaign.objects.filter(factory_organization=org)
    elif identity.organization_type == "SUPPLIER":
        campaigns = QualityRecallCampaign.objects.filter(
            quality_case__supplier_material_batch__supplier=org,
        ).distinct()
    else:
        campaigns = QualityRecallCampaign.objects.filter(
            devices__device_id__in=device_scope(identity).values("id"),
        ).distinct()
    active = campaigns.filter(status=QualityRecallCampaign.Status.ACTIVE)
    devices = QualityRecallDevice.objects.filter(campaign__in=campaigns)
    active_devices = devices.filter(campaign__status=QualityRecallCampaign.Status.ACTIVE).exclude(
        status__in=(QualityRecallDevice.Status.DISPOSED, QualityRecallDevice.Status.EXEMPTED),
    ).count()
    return {
        "domain": "QUALITY",
        "metrics": {
            "active_recall_campaign_count": active.count(),
            "active_recall_device_count": active_devices,
            "completed_recall_campaign_count": campaigns.filter(status=QualityRecallCampaign.Status.COMPLETED).count(),
        },
        "charts": {
            "recall_status_distribution": list(
                devices.values("status").annotate(value=Count("id")).order_by("status")
            ),
        },
    }
