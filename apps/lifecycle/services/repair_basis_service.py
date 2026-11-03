"""Quantity-backed, read-only settlement evidence; no payment status is inferred."""
import copy
from django.db.models import Q
from lifecycle.services.aftersales_access import visible_cases


def actual_quantity(value):
    # BOM quantities and boolean values must never become replacement quantities.
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    if not text.isascii() or not text.isdigit():
        return None
    number = int(text)
    return number if 1 <= number <= 100000 else None


def normalize_replacements(service, application, raw):
    items = copy.deepcopy(service._normalize_materials(application, raw, key_only=False))
    quantities = {}
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("bom_item_id") or entry.get("material_id") or "")
        if key in quantities:
            raise ValueError("实际换件物料不可重复提交")
        if "replacement_quantity" in entry:
            quantity = actual_quantity(entry["replacement_quantity"])
            if quantity is None:
                raise ValueError("实际更换数量须为1至100000的整数件数")
            quantities[key] = quantity
    for item in items:
        key = str(item.get("bom_item_id") or item.get("material_id") or "")
        item["replacement_quantity"] = quantities.get(key)
    return items


def make_basis(application):
    if (application.escalation or {}).get("decision") == "REPLACEMENT":
        return {"version": 1, "application_id": application.pk, "case_no": application.case_no,
                "status": "NOT_APPLICABLE", "repair_visits": 0, "replacement_pieces": 0,
                "materials": [], "issues": [], "service_type": "DEVICE_REPLACEMENT"}
    items = copy.deepcopy(application.repair_materials or [])
    errors = []
    if not application.repair_completed_at or not application.repair_completed_by_id:
        errors.append("缺少维修完成事实")
    if not application.applicant_confirmed_at:
        errors.append("尚未确认维修结果")
    if application.status != "CLOSED" or not application.closed_at or not application.closed_by_id:
        errors.append("厂家尚未审核结案")
    if application.repair_material_replaced is None:
        errors.append("未确认是否换件")
    if application.repair_material_replaced and not items:
        errors.append("缺少实际换件清单")
    count = 0
    seen = set()
    if application.repair_material_replaced:
        for item in items:
            if not isinstance(item, dict):
                errors.append("换件记录格式待核对")
                continue
            key = str(item.get("bom_item_id") or item.get("material_id") or "")
            qty = actual_quantity(item.get("replacement_quantity"))
            if not key or key in seen or qty is None:
                errors.append("实际换件数量缺失或物料记录重复，不能使用BOM数量推算")
            else:
                count += qty
            seen.add(key)
    elif items:
        errors.append("换件标记与物料清单不一致")
    return {
        "version": 1, "basis_no": "ASB-%s" % application.pk,
        "application_id": application.pk, "case_no": application.case_no,
        "device_id": application.device_id,
        "organization_id": application.assigned_organization_id,
        "executor_id": application.repair_completed_by_id,
        "route": application.dispatch_route,
        "repair_completed_at": application.repair_completed_at.isoformat() if application.repair_completed_at else "",
        "confirmed_at": application.applicant_confirmed_at.isoformat() if application.applicant_confirmed_at else "",
        "confirmation_mode": application.applicant_confirm_mode,
        "closed_at": application.closed_at.isoformat() if application.closed_at else "",
        "closed_by_id": application.closed_by_id,
        "materials": items,
        "status": "READY" if not errors else "REVIEW_REQUIRED",
        "repair_visits": 1 if not errors else 0,
        "replacement_pieces": count if not errors else 0,
        "issues": list(dict.fromkeys(errors)),
    }


def basis_for(application):
    frozen = application.repair_settlement_basis
    if frozen:
        return copy.deepcopy(frozen)
    result = make_basis(application)
    if result["status"] == "NOT_APPLICABLE":
        return result
    # Existing closed records have not been frozen/reviewed under this release.
    if application.status == "CLOSED":
        result.update(status="LEGACY_REVIEW", repair_visits=0, replacement_pieces=0)
        result["issues"].append("历史工单未形成冻结结算依据，保留原记录待核对")
    else:
        result["status"] = "WAITING_CONFIRMATION"
    return result


def workload_scope(user):
    from account.services.auth_service import get_active_identity
    from account.services.role_delegation_service import delegated_role_codes, delegated_role_user_ids
    identity = get_active_identity(user)
    if not identity:
        return {"code":"NONE", "title":"无维修统计权限", "executor_ids":[]}
    role = identity.role.code
    kind = identity.organization.organization_type.code
    if kind == "CUSTOMER":
        return {"code":"DEVICE_SERVICE", "title":"授权设备接受的维修服务", "executor_ids":None}
    if kind == "OWNER" and (identity.role.is_owner_role or role in {"factory_admin","factory_owner","factory_manager","factory_aftersales"}
            or (role == "factory_senior_manager" and "factory_aftersales" in delegated_role_codes(identity))):
        return {"code":"FACTORY_SERVICE", "title":"本厂家维修服务与结算依据", "executor_ids":None}
    if identity.role.is_owner_role and kind in {"MERCHANT","SERVICE"}:
        return {"code":"ORGANIZATION_WORKLOAD", "title":"本组织实际完成的维修工作量", "executor_ids":None}
    if role == "merchant_senior_manager":
        managed = {user.pk}
        for code in delegated_role_codes(identity):
            managed.update(delegated_role_user_ids(identity,code))
        return {"code":"MANAGED_WORKLOAD", "title":"本人及分管人员实际完成的维修工作量", "executor_ids":sorted(managed)}
    return {"code":"PERSONAL_WORKLOAD", "title":"本人实际完成的维修工作量", "executor_ids":[user.pk]}


def repair_cases(user):
    from account.services.auth_service import get_active_identity
    qs = visible_cases(user).filter(repair_completed_at__isnull=False).exclude(status="CANCELLED")
    scope = workload_scope(user)
    if scope["executor_ids"] is not None:
        qs = qs.filter(repair_completed_by_id__in=scope["executor_ids"])
    if scope["code"] in {"ORGANIZATION_WORKLOAD","MANAGED_WORKLOAD"}:
        identity = get_active_identity(user)
        # Keep the repair organization recorded on the order, not current device location.
        qs = qs.filter(assigned_organization_id=identity.organization_id)
    return qs


def summary(user):
    result = {"repair_completed_visits": 0, "repair_waiting_visits": 0,
              "repair_basis_visits": 0, "replacement_basis_pieces": 0,
              "repair_basis_review_visits": 0}
    fields = ["id", "case_no", "device_id", "assigned_organization_id", "repair_completed_by_id",
              "dispatch_route", "repair_completed_at", "applicant_confirmed_at", "applicant_confirm_mode",
              "closed_at", "closed_by_id", "status", "repair_materials", "repair_material_replaced", "repair_settlement_basis", "escalation"]
    for app in repair_cases(user).only(*fields).iterator(chunk_size=500):
        result["repair_completed_visits"] += 1
        basis = basis_for(app)
        if basis["status"] == "READY":
            result["repair_basis_visits"] += 1
            result["replacement_basis_pieces"] += basis["replacement_pieces"]
        elif app.status != "CLOSED":
            result["repair_waiting_visits"] += 1
        else:
            result["repair_basis_review_visits"] += 1
    return result


def catalog(user, cursor=None):
    qs = repair_cases(user).select_related("device", "assigned_organization", "repair_completed_by")
    if cursor not in (None, ""):
        try:
            cursor = int(cursor)
        except (TypeError, ValueError):
            raise ValueError("依据分页游标无效")
        if cursor <= 0:
            raise ValueError("依据分页游标无效")
        qs = qs.filter(pk__lt=cursor)
    records = list(qs.order_by("-pk")[:51])
    rows = []
    labels = {"READY": "结算依据已具备", "REVIEW_REQUIRED": "依据待核对",
              "LEGACY_REVIEW": "历史依据待核对", "WAITING_CONFIRMATION": "已完工待确认／结案"}
    for app in records[:50]:
        row = basis_for(app)
        row.update(sn=app.device.sn,
                   organization_name=app.assigned_organization.name if app.assigned_organization else "",
                   executor_name=(app.repair_completed_by.wx_nickname or app.repair_completed_by.username) if app.repair_completed_by else "",
                   status_name=labels.get(row["status"], row["status"]),
                   note="仅提供结算事实依据，不代表已定价、已付款或已结算")
        rows.append(row)
    return {"items": rows, "next_cursor": records[49].pk if len(records) > 50 else None,
            "metrics": summary(user), "scope": {key:value for key,value in workload_scope(user).items() if key != "executor_ids"}, "read_only": True}
