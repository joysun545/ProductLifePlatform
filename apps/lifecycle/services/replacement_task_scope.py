"""Read-only scopes for factory handoffs and local service scan delivery.

An open physical handoff is not automatically an action for every observer.
Only complete, mutually consistent business references are interpreted here;
unknown/contradictory tasks keep their existing inbox behavior.
"""

import re
from collections import defaultdict

from account.services.auth_service import get_active_identity
from account.services.role_delegation_service import identity_has_role, identity_can_perform_shared_operation
from lifecycle.models import (
    AfterSalesApplication, AfterSalesServiceStationLiaison, ReplacementFlow, ReplacementLeg,
)


_KEY = re.compile(
    r"^aftersales:([1-9][0-9]*):replacement-(?:([1-9][0-9]*)-(send|receive)|(factory-pickup|scan))"
    r"(-notice)?:v([0-9]+)$"
)
_NOTIFICATION_KEY = re.compile(r"^([1-9][0-9]*)-(send|receive)$")


def _positive_id(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 < value < 2**63 else None
    if isinstance(value, str) and len(value) <= 19 and re.fullmatch(r"[1-9][0-9]*", value):
        number = int(value)
        return number if number < 2**63 else None
    return None


def _matches(value, expected):
    return value in (None, "") or _positive_id(value) == expected


def _root_is_consistent(application):
    root = application.factory_organization
    return bool(
        root.organization_type.code == "OWNER"
        and root.factory_root_id == root.pk
    )


def _leg_role(application, leg, action):
    """Return a role only for the deliberately limited factory node scope."""
    root_id = application.factory_organization_id
    if leg.flow.application_id != application.pk:
        return None
    if (
        action == "receive" and leg.direction == "OLD"
        and leg.to_organization_id == root_id
        and leg.from_organization_id != root_id
        and leg.from_organization.factory_root_id == root_id
    ):
        return "factory_logistics"
    if (
        action == "send" and leg.direction == "SUPPLY" and leg.sequence == 1
        and leg.from_organization_id == root_id
        and leg.to_organization_id != root_id
        and leg.to_organization.factory_root_id == root_id
    ):
        return "factory_stock"
    return None


def _responsible(identity, root_id, role):
    # Same role gate as logistics()/stock(); no stock/allocation readiness gate.
    return bool(
        identity and identity.user.is_active
        and identity.organization_id == root_id
        and identity_has_role(identity, role)
    )


def _local_scan_flow(application, flow):
    """Only the recorded local execution organization belongs to this scope."""
    return bool(
        flow and flow.mode == "LOCAL"
        and flow.execution_organization_id == application.assigned_organization_id
        and flow.execution_organization_id != application.factory_organization_id
        and flow.execution_organization.factory_root_id == application.factory_organization_id
        and flow.execution_organization.is_active
        and flow.execution_organization.organization_type.is_active
    )


def _scan_organization_gate(identity):
    if not identity:
        return False
    from lifecycle.services.replacement_flow_service import ORG_ROLES
    return bool(
        identity.role.is_owner_role or identity.role.code in ORG_ROLES
        or identity_can_perform_shared_operation(identity, "goods_transfer_receipt")
        or identity_has_role(identity, "factory_aftersales")
    )


def _scan_responsible(application, flow, identity, station_ids, organization_gate=None):
    """Mirror existing assignee + organization gates, not owner collaboration.

    The service mode has not necessarily been confirmed when this task is
    published. Readiness must not remove the actual operator's responsibility.
    """
    if not identity or not identity.user.is_active or identity.organization_id != flow.execution_organization_id:
        return False
    if organization_gate is None:
        organization_gate = _scan_organization_gate(identity)
    if not organization_gate:
        return False
    if application.assigned_user_id == identity.user_id:
        return True
    station_id = _positive_id((application.assignee_snapshot or {}).get("service_station_application_id"))
    return bool(
        application.dispatch_route == "SERVICE_STATION"
        and station_id and station_id in station_ids
    )


def split_scan_recipients(application, flow, recipients):
    """Retain the original observers; give action only to existing operators."""
    from account.models import UserOrganization

    recipients = list(dict.fromkeys(str(value or "").strip() for value in recipients if str(value or "").strip()))
    identities = UserOrganization.objects.select_related(
        "user", "role", "organization__organization_type", "organization__factory_root",
    ).filter(
        organization_id=flow.execution_organization_id, user__is_active=True, is_active=True,
        organization__is_active=True, organization__organization_type__is_active=True,
        organization__factory_root__is_active=True, role__is_active=True,
        role__organization_type_id=flow.execution_organization.organization_type_id,
        user__wx_openid__isnull=False,
    ).exclude(user__wx_openid="").order_by("pk")
    responsible = []
    for candidate in identities:
        identity = get_active_identity(candidate.user)
        if not identity or identity.pk != candidate.pk:
            continue
        station_ids = set(AfterSalesServiceStationLiaison.objects.filter(
            identity=identity, application__status="APPROVED",
        ).values_list("application_id", flat=True))
        if _scan_responsible(application, flow, identity, station_ids):
            openid = candidate.user.wx_openid.strip()
            if openid:
                responsible.append(openid)
    actions = list(dict.fromkeys(responsible))
    return actions, [openid for openid in recipients if openid not in actions]


def notification_scope(application, organization_id, key, leg=None):
    """Classify a trusted publisher's node without changing any facts."""
    if not _root_is_consistent(application):
        return None
    if key == "scan":
        flow = ReplacementFlow.objects.select_related("execution_organization__organization_type").filter(application=application).first()
        if not _local_scan_flow(application, flow) or organization_id != flow.execution_organization_id:
            return None
        return {"role": "replacement_scan", "leg": None, "action": "scan", "flow": flow}
    if organization_id != application.factory_organization_id:
        return None
    match = _NOTIFICATION_KEY.fullmatch(str(key))
    if match:
        leg_id, action = _positive_id(match[1]), match[2]
        if leg_id is None:
            return None
        if leg is not None and leg.pk != leg_id:
            return None
        if leg is None:
            leg = ReplacementLeg.objects.select_related(
                "flow__application", "from_organization", "to_organization",
            ).filter(pk=leg_id, flow__application_id=application.pk).first()
        if not leg:
            return None
        role = _leg_role(application, leg, action)
        return {"role": role, "leg": leg, "action": action} if role else None
    if key != "factory-pickup":
        return None
    flow = ReplacementFlow.objects.filter(application=application).first()
    if not flow or flow.mode != "FACTORY" or flow.protocol_version < 6 or flow.legs.filter(direction="OLD").exists():
        return None
    return {"role": "factory_logistics", "leg": None, "action": "pickup"}


def split_recipients(application, role, recipients):
    """Preserve all original recipients, distinguishing action from knowledge."""
    from account.models import UserOrganization

    recipients = list(dict.fromkeys(str(value or "").strip() for value in recipients if str(value or "").strip()))
    identities = UserOrganization.objects.select_related(
        "user", "role", "organization__organization_type", "organization__factory_root",
    ).filter(
        user__wx_openid__in=recipients, user__is_active=True, is_active=True,
        organization=application.factory_organization, organization__is_active=True,
        organization__organization_type__is_active=True,
        organization__factory_root__is_active=True, role__is_active=True,
        role__organization_type_id=application.factory_organization.organization_type_id,
    )
    responsible = {
        identity.user.wx_openid for identity in identities
        if _responsible(identity, application.factory_organization_id, role)
    }
    return (
        [openid for openid in recipients if openid in responsible],
        [openid for openid in recipients if openid not in responsible],
    )


def resolve_scopes(workitems, user):
    """Batch-load trusted case/leg references for one inbox's current reader.

    ``responsible`` describes role responsibility, ``can_act_now`` readiness.
    Main-case CLOSED never completes an independent physical handoff here.
    Conflicting references are deliberately absent from the returned mapping.
    """
    parsed = []
    for item in workitems:
        if item.domain != "AFTERSALE" or item.type != "AFTERSALES_REPLACEMENT_CHANNEL":
            continue
        match = _KEY.fullmatch(item.uniq_key or "")
        if not match:
            continue
        app_id = _positive_id(match[1])
        leg_id = _positive_id(match[2]) if match[2] else None
        if app_id is None or (match[2] and leg_id is None):
            continue
        payload = item.payload if isinstance(item.payload, dict) else {}
        if not _matches(item.biz_id, app_id) or not _matches(payload.get("application_id"), app_id):
            continue
        if item.biz_ref and item.biz_ref != f"AfterSalesApplication:{app_id}":
            continue
        key_version = int(match[6])
        if payload.get("dispatch_version") not in (None, ""):
            raw_version = payload["dispatch_version"]
            if isinstance(raw_version, bool) or str(raw_version) != str(key_version):
                continue
        if leg_id is not None and not _matches(payload.get("replacement_leg_id"), leg_id):
            continue
        if leg_id is None and payload.get("replacement_leg_id") not in (None, ""):
            continue
        action = match[3] or ("scan" if match[4] == "scan" else "pickup")
        parsed.append((item, payload, app_id, leg_id, action, bool(match[5]), key_version))
    if not parsed:
        return {}

    app_ids = {row[2] for row in parsed}
    applications = {
        app.pk: app for app in AfterSalesApplication.objects.select_related(
            "factory_organization__organization_type", "factory_organization__factory_root",
        ).filter(pk__in=app_ids)
    }
    flows = {
        flow.application_id: flow for flow in ReplacementFlow.objects.select_related(
            "execution_organization__organization_type", "execution_organization__factory_root",
        ).filter(application_id__in=app_ids)
    }
    by_flow = defaultdict(list)
    legs = {}
    for leg in ReplacementLeg.objects.select_related(
        "from_organization", "to_organization__organization_type", "to_organization__factory_root",
    ).filter(flow_id__in=[flow.pk for flow in flows.values()]):
        legs[leg.pk] = leg
        by_flow[leg.flow_id].append(leg)
    for app_id, flow in flows.items():
        if app_id in applications:
            flow.application = applications[app_id]
        for leg in by_flow[flow.pk]:
            leg.flow = flow

    from sales.models import ReplacementSupplyPlanItem
    allocated_legs = set(ReplacementSupplyPlanItem.objects.filter(
        leg_id__in=legs, status="RESERVED", plan__status="OPEN",
    ).values_list("leg_id", flat=True))
    identity = get_active_identity(user) if user and user.is_active else None
    roles = {
        role: bool(identity and identity_has_role(identity, role))
        for role in ("factory_logistics", "factory_stock")
    }
    from lifecycle.services.aftersales_access import visible_cases
    visible_ids = set(visible_cases(user, identity).filter(pk__in=app_ids).values_list("pk", flat=True)) if identity else set()
    scan_station_ids = set(AfterSalesServiceStationLiaison.objects.filter(
        identity=identity, application__status="APPROVED",
    ).values_list("application_id", flat=True)) if identity and any(row[4] == "scan" for row in parsed) else set()
    scan_organization_gate = _scan_organization_gate(identity) if any(row[4] == "scan" for row in parsed) else False
    contexts = {}
    for item, payload, app_id, leg_id, action, notice, version in parsed:
        application, flow = applications.get(app_id), flows.get(app_id)
        if not application or not flow or not _root_is_consistent(application):
            continue
        root_id = application.factory_organization_id
        hints = [payload.get("root_organization_id"), payload.get("factory_root_id"), payload.get("factory_organization_id")]
        if isinstance(payload.get("root_organization"), dict):
            hints.append(payload["root_organization"].get("id"))
        elif payload.get("root_organization") not in (None, ""):
            continue
        if any(not _matches(value, root_id) for value in hints):
            continue
        if payload.get("case_no") not in (None, "", application.case_no):
            continue
        if payload.get("inbox_group_key") not in (None, "", f"aftersales:{application.case_no}"):
            continue
        if any(not _matches(payload.get(key), flow.pk) for key in ("replacement_flow_id", "flow_id")):
            continue
        flow_legs = by_flow[flow.pk]
        leg = legs.get(leg_id) if leg_id else None
        scope_revision = None
        if action == "scan":
            if not _local_scan_flow(application, flow) or not _matches(
                payload.get("execution_organization_id"), flow.execution_organization_id,
            ):
                continue
            role = "replacement_scan"
            unfinished = not flow.swapped_at and not application.replacement_device_id
            current = unfinished
            responsible = _scan_responsible(application, flow, identity, scan_station_ids, scan_organization_gate)
            can_act = responsible and current and application.status != "CANCELLED" and bool(flow.service_confirmed_at)
            station_id = _positive_id((application.assignee_snapshot or {}).get("service_station_application_id"))
            scope_revision = [
                application.assigned_user_id, application.assigned_organization_id,
                application.dispatch_route, station_id,
                bool(station_id and station_id in scan_station_ids),
                identity.pk if identity else None, identity.organization_id if identity else None,
                identity.role_id if identity else None,
            ]
        elif action == "pickup":
            if flow.mode != "FACTORY" or flow.protocol_version < 6 or any(row.direction == "OLD" for row in flow_legs):
                continue
            role = "factory_logistics"
            current = (flow.recovery or {}).get("state") == "WAIT_PICKUP"
            unfinished = (flow.recovery or {}).get("state") != "CLOSED"
        else:
            if not leg or leg.flow_id != flow.pk:
                continue
            role = _leg_role(application, leg, action)
            if not role:
                continue
            current = leg.status == ("SENT" if action == "receive" else "PENDING")
            unfinished = leg.status != "RECEIVED"
        if action != "scan":
            responsible = bool(identity and identity.organization_id == root_id and roles[role])
            can_act = responsible and current and application.status != "CANCELLED"
        if action == "send":
            old_legs = [row for row in flow_legs if row.direction == "OLD"]
            last_old = max(old_legs, key=lambda row: row.sequence, default=None)
            target = leg.to_organization
            target_root = target.pk if target.organization_type.code == "OWNER" else target.factory_root_id
            can_act = bool(can_act and flow.mode == "LOCAL" and flow.swapped_at
                and last_old and last_old.status == "RECEIVED"
                and (flow.recovery or {}).get("picked_up_at")
                and leg.pk in allocated_legs and target.is_active and target_root == root_id)
        if (application.escalation or {}).get("factory_transport"):
            can_act = False  # This case uses its explicit factory-transport controls.
        contexts[item.pk] = {
            "application": application, "flow": flow, "leg": leg,
            "action": action, "role": role, "root_id": root_id,
            "responsible": responsible, "observer": app_id in visible_ids,
            "can_act_now": bool(can_act), "is_current_stage": bool(current),
            "unfinished": bool(unfinished), "notification_only": notice,
            "version_matches": version == application.dispatch_version,
        }
        if scope_revision is not None:
            contexts[item.pk]["scope_revision"] = scope_revision
    return contexts
