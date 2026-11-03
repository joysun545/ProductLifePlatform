from django.db.models import Count, Q

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
    from lifecycle.services.aftersales_access import visible_cases, capabilities
    from lifecycle.services.repair_basis_service import summary, workload_scope
    participant = capabilities(identity.user)["can_view_repair_basis"]
    cases = visible_cases(identity.user) if participant else _case_scope(identity)
    device_ids = (cases.values("device_id") if participant else device_scope(identity).values("id"))
    case_count = cases.exclude(status=AfterSalesApplication.Status.CANCELLED).count()
    fault_device_count = cases.exclude(
        status__in=(AfterSalesApplication.Status.DRAFT, AfterSalesApplication.Status.CANCELLED),
    ).values("device_id").distinct().count()
    open_count = cases.filter(status__in=OPEN_STATUSES).count()
    activated = ActivationRecord.objects.filter(
        Q(device_id__in=device_ids) | Q(device__replacement_after_sales__id__in=cases.values("pk")),
    ).filter(
        Q(status="ACTIVATED") | Q(status="CANCELLED", device__after_sales_applications__id__in=cases.values("pk"),
            device__after_sales_applications__replacement_flow__swapped_at__isnull=False),
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
    from lifecycle.services.replacement_flow_service import statistics as replacement_statistics
    return {
        "replacement_statistics": replacement_statistics(identity.user),
        "domain": "AFTERSALES",
        "metrics": {
            **replacement_statistics(identity.user),
            "aftersales_case_count": case_count,
            "aftersales_fault_device_count": fault_device_count,
            "aftersales_open_count": open_count,
            "activated_exposure_device_count": activated,
            "aftersales_fault_rate": fault_rate,
            **(summary(identity.user) if participant else {}),
        },
        "can_view_repair_basis": participant,
        "repair_basis_scope": ({key:value for key,value in workload_scope(identity.user).items() if key != "executor_ids"} if participant else None),
        "metric_note": "售后参与方按授权工单范围统计；故障率分母为同范围历史有效激活设备，包含换新退役旧机及已承接激活的新机。售后可见记录与维修工作量分别统计：操作人员只计本人实际维修，商家负责人只计本组织维修，高级经理只计分管人员维修；用户方按授权设备接受服务统计。维修台次按独立维修工单计数；结算依据台次须有完工、结果确认和厂家结案；换件件次仅汇总明确填写的实际更换数量。BOM配套数量不作换件数量。历史缺失记录待核对，不推算；结算依据不代表已定价或已付款。",
        "charts": {
            "status_distribution": status_rows,
            "final_fault_distribution": fault_rows,
            "product_model_distribution": model_rows,
        },
    }
