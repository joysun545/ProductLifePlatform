from django.db.models import Count

from lifecycle.models import ActivationRecord, AfterSalesApplication

from .query_scope import device_scope


OPEN_STATUSES = (
    AfterSalesApplication.Status.DRAFT,
    AfterSalesApplication.Status.SUBMITTED,
    AfterSalesApplication.Status.ROUTING,
    AfterSalesApplication.Status.PROCESSING,
    AfterSalesApplication.Status.WAITING_CUSTOMER_CONFIRM,
    AfterSalesApplication.Status.PENDING_FACTORY_CLOSE,
)


def _case_scope(identity):
    qs = AfterSalesApplication.objects.all()
    org = identity.organization
    if identity.organization_type == "OWNER":
        return qs.filter(factory_organization=org)
    if identity.organization_type == "MERCHANT":
        return qs.filter(device_id__in=device_scope(identity).values("id"))
    if identity.organization_type == "SERVICE":
        return qs.filter(assigned_organization=org)
    if identity.organization_type == "SUPPLIER":
        return qs.filter(device__material_fault_records__supplier=org).distinct()
    return qs.none()


def build_aftersales_statistics(identity):
    cases = _case_scope(identity)
    device_ids = device_scope(identity).values("id")
    case_count = cases.exclude(status=AfterSalesApplication.Status.CANCELLED).count()
    fault_device_count = cases.exclude(
        status__in=(AfterSalesApplication.Status.DRAFT, AfterSalesApplication.Status.CANCELLED),
    ).values("device_id").distinct().count()
    open_count = cases.filter(status__in=OPEN_STATUSES).count()
    activated = ActivationRecord.objects.filter(
        device_id__in=device_ids, status="ACTIVATED",
    ).values("device_id").distinct().count()
    fault_rate = round(fault_device_count / activated * 100, 2) if activated else 0
    status_rows = list(cases.values("status").annotate(value=Count("id")).order_by("status"))
    fault_rows = list(
        cases.exclude(final_fault_type="").values("final_fault_type")
        .annotate(value=Count("id")).order_by("-value", "final_fault_type")
    )
    model_rows = list(
        cases.values("device__product_model_id", "device__product_model__code", "device__product_model__name")
        .annotate(case_count=Count("id"), device_count=Count("device_id", distinct=True))
        .order_by("-device_count", "device__product_model_id")[:10]
    )
    return {
        "domain": "AFTERSALES",
        "metrics": {
            "aftersales_case_count": case_count,
            "aftersales_fault_device_count": fault_device_count,
            "aftersales_open_count": open_count,
            "activated_exposure_device_count": activated,
            "aftersales_fault_rate": fault_rate,
        },
        "metric_note": "故障率分母为当前统计范围内的有效已激活设备，分子为已提交且未取消售后的去重设备。",
        "charts": {
            "status_distribution": status_rows,
            "final_fault_distribution": fault_rows,
            "product_model_distribution": model_rows,
        },
    }
