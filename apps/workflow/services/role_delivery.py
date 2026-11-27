"""Route manager copies by current organization and role allocations."""

from django.db import transaction
from django.utils import timezone

from account.models import Organization, OrganizationRole, UserOrganization
from account.services.retired_roles import RETIRED_ROLE_CODES
from account.services.role_delegation_service import SENIOR_MANAGER_ROLE_CODES
from workflow.models import UserTask, WorkItem


# These order tasks are published to the whole business role. Supplier delivery
# and order-creator authorization may instead have an immutable named roster.
ROLE_ROUTED_ORDER_TYPES = {
    "ORDER_PLAN_MATERIAL_REVIEW",
    "ORDER_PLAN_V2_PRODUCTION_CONFIRM",
    "ORDER_PLAN_V2_QRCODE_PRINT",
    "ORDER_PLAN_V2_SHIPMENT_LOGISTICS_CONFIRM",
    "ORDER_PLAN_V2_STOCK_OUTBOUND",
    "ORDER_PLAN_V2_MERCHANT_RECEIPT",
    "ORDER_PLAN_V2_ACCOUNTING_CONFIRM",
}


def has_role_routed_work(organization_id):
    return WorkItem.objects.filter(
        status="OPEN", domain="SALES", type__in=ROLE_ROUTED_ORDER_TYPES,
        role_delivery__scopes__has_key=str(organization_id),
    ).exists()


def _members():
    return UserOrganization.objects.filter(
        is_active=True, user__is_active=True, organization__is_active=True,
        organization__organization_type__is_active=True,
        organization__factory_root__is_active=True, role__is_active=True,
    ).exclude(user__wx_openid__isnull=True).exclude(user__wx_openid="")


def record_role_delivery(workitem, openids, need_action, fallback_recipients=(), recovered=()):
    by_org = {}
    for recipient in fallback_recipients:
        for role in recipient.role_ids:
            by_org.setdefault(str(recipient.organization_id), {}).setdefault(str(role), [])
    for org, role, openid in recovered:
        by_org.setdefault(str(org), {}).setdefault(str(role), []).append(openid)
    members = _members().filter(user__wx_openid__in=openids, role__is_owner_role=False)
    for organization, role, openid in members.exclude(
        role__code__in=SENIOR_MANAGER_ROLE_CODES | RETIRED_ROLE_CODES,
    ).values_list("organization_id", "role_id", "user__wx_openid"):
        by_org.setdefault(str(organization), {}).setdefault(str(role), []).append(openid)

    workitem.role_delivery = {
        "scopes": by_org,
        "action": need_action,
        "direct": openids,
        "managed": workitem.role_delivery.get("managed", []),
    }
    workitem.save(update_fields=["role_delivery"])


def refresh_role_delivery(workitem, *, reopen_completed_actions=False):
    delivery = workitem.role_delivery
    scopes = delivery.get("scopes", {})
    role_ids = {int(role) for roles in scopes.values() for role in roles}
    if role_ids:
        retired_ids = set(OrganizationRole.objects.filter(
            pk__in=role_ids, code__in=RETIRED_ROLE_CODES,
        ).values_list("pk", flat=True))
        if retired_ids:
            role_ids -= retired_ids
            scopes = {
                org: {role: recipients for role, recipients in roles.items() if int(role) not in retired_ids}
                for org, roles in scopes.items()
            }
    if not role_ids and not delivery.get("managed"):
        return
    members = _members().filter(organization_id__in=scopes)
    staffed = {}
    for org, role, openid, code in members.filter(role_id__in=role_ids).values_list(
        "organization_id", "role_id", "user__wx_openid", "role__code",
    ):
        staffed.setdefault((org, role), []).append((openid, code))
    managers = members.filter(
        role__code__in=SENIOR_MANAGER_ROLE_CODES,
        senior_manager_profile__managed_roles__in=role_ids,
    ).values_list(
        "organization_id", "senior_manager_profile__managed_roles",
        "user__wx_openid", "role__code",
    )
    wanted = {}
    for org, role, openid, code in managers:
        if str(role) not in scopes.get(str(org), {}):
            continue
        action = bool(delivery["action"] and ((org, role) not in staffed or workitem.action_mode != "ALL_DO"))
        wanted[openid] = (code, action or wanted.get(openid, ("", False))[1])

    role_routed = workitem.domain == "SALES" and workitem.type in ROLE_ROUTED_ORDER_TYPES
    # Preserve immutable, named recipients unless the task routes by role.
    for org, roles in scopes.items():
        for role, originals in roles.items():
            if (role_routed or not originals) and delivery["action"]:
                for openid, code in staffed.get((int(org), int(role)), ()):
                    wanted[openid] = (code, True)

    direct = set(delivery.get("direct", []))
    wanted = {openid: entry for openid, entry in wanted.items() if openid not in direct}
    now = timezone.now()
    owned = set(delivery.get("managed", [])) - direct
    if delivery["action"]:
        invalid, valid = set(), set()
        for org, roles in scopes.items():
            for role, originals in roles.items():
                current = {openid for openid, _ in staffed.get((int(org), int(role)), ())}
                invalid.update(set(originals) - current)
                valid.update(set(originals) & current & direct)
        workitem.user_tasks.filter(
            to_openid__in=invalid - valid, need_action=True, state__in=("NEW", "READ"),
        ).update(state="DONE", need_action=False, done_at=now, done_reason="ROLE_REASSIGNED", updated_at=now)
        workitem.user_tasks.filter(
            to_openid__in=valid, state="DONE", done_reason="ROLE_REASSIGNED",
        ).update(state="NEW", need_action=True, read_at=None, done_at=None, done_reason="", cursor_time=now, updated_at=now)
    workitem.user_tasks.filter(
        to_openid__in=owned - wanted.keys(), state__in=("NEW", "READ"),
    ).update(state="DONE", need_action=False, done_at=now, done_reason="ROLE_REASSIGNED", updated_at=now)
    for openid, (code, action) in wanted.items():
        task, created = UserTask.objects.get_or_create(
            workitem=workitem, to_openid=openid,
            defaults={
                "to_role_snapshot": code, "need_action": action,
                "cursor_time": now, "display_title_snapshot": workitem.title,
                "display_subtitle_snapshot": workitem.summary,
            },
        )
        if created:
            owned.add(openid)
        elif openid in owned:
            reopening = task.done_reason == "ROLE_REASSIGNED" or (
                task.state == "DONE" and action and workitem.status == "OPEN"
                and (reopen_completed_actions or not task.need_action)
            )
            if task.state == "DONE" and not reopening:
                continue
            if reopening:
                task.state, task.read_at, task.done_at, task.done_reason = "NEW", None, None, ""
            if (
                not reopening and task.need_action == action
                and task.display_title_snapshot == workitem.title
                and task.display_subtitle_snapshot == workitem.summary
            ):
                continue
            task.need_action = action
            task.cursor_time = now
            task.display_title_snapshot = workitem.title
            task.display_subtitle_snapshot = workitem.summary
            task.save(update_fields=[
                "state", "read_at", "done_at", "done_reason", "need_action",
                "cursor_time", "display_title_snapshot", "display_subtitle_snapshot", "updated_at",
            ])

    managed = sorted(owned)
    if managed != delivery.get("managed", []):
        delivery["managed"] = managed
        workitem.save(update_fields=["role_delivery"])


@transaction.atomic
def reconcile_organization_role_delivery(organization_id, *, former_member=None):
    scope_key = str(organization_id)
    workitems = WorkItem.objects.select_for_update().filter(status="OPEN")
    for workitem in workitems.filter(role_delivery__scopes__has_key=scope_key).order_by("pk"):
        refresh_role_delivery(workitem)

    # Existing records can be recovered only when an original actionable
    # recipient still matches the recorded role and organization.
    originals = {openid: (code, role_id) for openid, code, role_id in (
        UserOrganization.objects.filter(organization_id=organization_id, role__is_owner_role=False)
        .exclude(role__code__in=SENIOR_MANAGER_ROLE_CODES)
        .values_list("user__wx_openid", "role__code", "role_id")
    ) if openid}
    if former_member:
        openid, role_id, role_code = former_member
        if openid:
            originals[openid] = (role_code, role_id)
    legacy = workitems.filter(
        role_delivery={}, user_tasks__to_openid__in=originals,
        user_tasks__need_action=True, user_tasks__state__in=("NEW", "READ"),
    ).values_list("pk", flat=True)
    from workflow.services.task_root import task_root_organizations

    candidates = list(workitems.filter(pk__in=legacy).order_by("pk"))
    roots = task_root_organizations(candidates)
    organization_root = Organization.objects.filter(pk=organization_id).values_list("factory_root_id", flat=True).first()
    for workitem in candidates:
        root = roots.get(workitem.pk)
        if root and root["id"] != organization_root:
            continue
        anchors = list(workitem.user_tasks.filter(
            to_openid__in=originals, need_action=True, state__in=("NEW", "READ"),
        ).values_list("to_openid", "to_role_snapshot"))
        recovered = [
            (organization_id, originals[openid][1], openid)
            for openid, code in anchors if originals[openid][0] == code
        ]
        if recovered:
            direct = list(workitem.user_tasks.exclude(
                to_role_snapshot__in=SENIOR_MANAGER_ROLE_CODES,
            ).values_list("to_openid", flat=True))
            record_role_delivery(workitem, direct, True, recovered=recovered)
            refresh_role_delivery(workitem)
