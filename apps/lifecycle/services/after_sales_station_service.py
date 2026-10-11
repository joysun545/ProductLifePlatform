import re
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from account.models import (
    Organization,
    OrganizationRelation,
    OrganizationRole,
    SeniorManagerProfile,
    UserOrganization,
)
from account.services.auth_service import get_active_identity
from lifecycle.models import (
    AfterSalesServiceStationApplication,
    AfterSalesServiceStationLiaison,
)
from workflow.models import UserTask, WorkItem
from workflow.services.workflow_service import get_role_snapshot, publish_workitem


NOTICE_VERSION = "2026-09-29-v1"
LOCATION_CAPTURE_MAX_AGE_SECONDS = 15 * 60
LOCATION_FUTURE_TOLERANCE_SECONDS = 5 * 60
LIAISON_ROLE_PRIORITY = (
    "merchant_electrician",
    "merchant_sales",
    "merchant_owner",
)
LIAISON_ROLE_CODES = LIAISON_ROLE_PRIORITY
STATION_REVIEW_TYPE = "AFTER_SALES_STATION_REVIEW"
STATION_RESULT_TYPE = "AFTER_SALES_STATION_RESULT"
STATION_REVIEW_PAGE = "/pages/aftersale/service_station/review/review"


class AfterSalesServiceStationError(Exception):
    """Errors raised while reading or submitting a merchant station application."""


class AfterSalesServiceStationPermissionError(AfterSalesServiceStationError):
    """Raised when an account is outside the application task's audience."""


def _station_review_key(application_id):
    return f"after-sales-station:{application_id}:review"


def _station_review_link(application_id):
    return f"{STATION_REVIEW_PAGE}?application_id={application_id}"


def _usable_openid(user):
    if not user or not user.is_active:
        return ""
    return str(user.wx_openid or "").strip()


def _active_openids(identities):
    values = []
    seen = set()
    for identity in identities:
        openid = _usable_openid(identity.user)
        if openid and openid not in seen:
            seen.add(openid)
            values.append(openid)
    return values


def _factory_sales_review_audience(organization):
    root = organization.get_factory_root()
    if not root or not root.is_active:
        raise AfterSalesServiceStationError("申请商家所属厂家根组织无效，无法提交审核")

    sales_role = OrganizationRole.objects.filter(
        organization_type_id=root.organization_type_id,
        code="factory_sales",
        is_active=True,
    ).first()
    if not sales_role:
        raise AfterSalesServiceStationError("所属厂家尚未启用厂家销售经理岗位，暂不能提交申请")

    sales_identities = list(
        UserOrganization.objects.select_related("user", "role", "organization")
        .filter(
            organization=root,
            role=sales_role,
            is_active=True,
            user__is_active=True,
            organization__is_active=True,
        )
        .order_by("id")
    )
    sales_openids = _active_openids(sales_identities)
    if not sales_openids:
        raise AfterSalesServiceStationError("所属厂家尚未设置有效的厂家销售经理，暂不能提交申请")

    # A delegated factory senior manager shares this exact actionable review
    # with factory sales. The manager's copy is a task copy, not a separate
    # review step; the first reviewer to decide closes every copy.
    manager_identities = list(
        UserOrganization.objects.select_related("user", "role", "organization")
        .filter(
            organization=root,
            role__code="factory_senior_manager",
            role__is_active=True,
            is_active=True,
            user__is_active=True,
            organization__is_active=True,
            senior_manager_profile__managed_roles=sales_role,
        )
        .order_by("id")
    )
    return root, sales_role, sales_identities, _active_openids(
        [*sales_identities, *manager_identities]
    )


def _publish_station_review_task(application, *, actor_openid):
    root, _sales_role, _sales_identities, recipients = _factory_sales_review_audience(
        application.organization
    )
    organization = application.organization
    publish_workitem(
        domain="AFTERSALE",
        type=STATION_REVIEW_TYPE,
        biz_id=str(application.pk),
        biz_ref=f"售后服务站申请:{application.pk}",
        uniq_key=_station_review_key(application.pk),
        title="售后服务站申请待审核",
        summary=f"{organization.name}已提交售后服务站申请，请核实后确认。",
        link=_station_review_link(application.pk),
        recipients_openids=recipients,
        need_action=True,
        action_mode="ANYONE_DO",
        payload={
            "station_application_id": application.pk,
            "organization_id": organization.pk,
            "organization_name": organization.name,
            "factory_root_id": root.pk,
        },
        actor_openid=actor_openid,
        mark_workitem_done_immediately=False,
    )


def _parent_organization_for(application, applicant_identity):
    inviter_identity = None
    if applicant_identity and applicant_identity.inviter_id:
        inviter_identity = (
            UserOrganization.objects.select_related("organization", "role", "user")
            .filter(
                user_id=applicant_identity.inviter_id,
                is_active=True,
                user__is_active=True,
                organization__is_active=True,
            )
            .first()
        )
    organization = application.organization
    if (
        inviter_identity
        and inviter_identity.organization_id != organization.pk
        and inviter_identity.organization.get_factory_root()
        and organization.get_factory_root()
        and inviter_identity.organization.get_factory_root().pk
        == organization.get_factory_root().pk
    ):
        return inviter_identity.organization

    relation = (
        OrganizationRelation.objects.select_related(
            "source_organization", "source_organization__organization_type"
        )
        .filter(
            target_organization=organization,
            relation_type="DISTRIBUTION",
            is_active=True,
            source_organization__is_active=True,
        )
        .order_by("id")
        .first()
    )
    return relation.source_organization if relation else None


def _result_notice_recipients(application):
    """Resolve the application audience from current organization links."""
    recipients = []
    applicant_identity = (
        UserOrganization.objects.select_related("organization", "role", "user")
        .filter(user_id=application.submitted_by_id)
        .first()
    )
    applicant_openid = _usable_openid(application.submitted_by)
    if applicant_openid:
        recipients.append(applicant_openid)

    for link in application.liaison_links.select_related("identity__user"):
        openid = _usable_openid(link.identity.user)
        if openid:
            recipients.append(openid)

    organization = application.organization
    root = organization.get_factory_root()
    if root:
        factory_aftersales = UserOrganization.objects.select_related("user").filter(
            organization=root,
            organization__is_active=True,
            user__is_active=True,
            is_active=True,
            role__is_active=True,
            role__code="factory_aftersales",
        ).order_by("id")
        recipients.extend(_active_openids(factory_aftersales))

    parent = _parent_organization_for(application, applicant_identity)
    if parent and parent.organization_type.code == "MERCHANT":
        sales_role = OrganizationRole.objects.filter(
            organization_type_id=parent.organization_type_id,
            code="merchant_sales",
            is_active=True,
        ).first()
        delegated_managers = []
        if sales_role:
            delegated_managers = list(
                UserOrganization.objects.select_related("user")
                .filter(
                    organization=parent,
                    organization__is_active=True,
                    is_active=True,
                    user__is_active=True,
                    role__is_active=True,
                    role__code="merchant_senior_manager",
                    senior_manager_profile__managed_roles=sales_role,
                )
                .exclude(user__wx_openid__isnull=True)
                .exclude(user__wx_openid="")
                .order_by("id")
            )

        delegated_openids = _active_openids(delegated_managers)
        if delegated_openids:
            recipients.extend(delegated_openids)
        else:
            owner_role = OrganizationRole.objects.filter(
                organization_type_id=parent.organization_type_id,
                code="merchant_owner",
                is_active=True,
            ).first()
            if owner_role:
                owners = UserOrganization.objects.select_related("user").filter(
                    organization=parent,
                    organization__is_active=True,
                    is_active=True,
                    user__is_active=True,
                    role=owner_role,
                ).order_by("id")
                recipients.extend(_active_openids(owners))

    if applicant_identity and applicant_identity.inviter_id:
        inviter_openid = (
            UserOrganization.objects.filter(
                user_id=applicant_identity.inviter_id,
                is_active=True,
                user__is_active=True,
                organization__is_active=True,
            )
            .values_list("user__wx_openid", flat=True)
            .first()
        )
        inviter_openid = str(inviter_openid or "").strip()
        if inviter_openid:
            recipients.append(inviter_openid)

    # Stable de-duplication matters when the applicant, liaison and inviter
    # are the same WeChat account.
    return list(dict.fromkeys(value for value in recipients if value))


def _publish_station_result_notice(application, *, actor):
    """Update the existing application task with its decision and recipients.

    A result is the next update to the station application task, not a second
    task. Reuse its review WorkItem so an account that is both a reviewer and
    an inviter keeps one inbox card for the application.
    """
    approved = application.status == AfterSalesServiceStationApplication.Status.APPROVED
    status_label = "已通过" if approved else "未通过"
    actor_name = actor.wx_nickname or actor.username
    reviewed_at = application.reviewed_at
    if not reviewed_at:
        raise AfterSalesServiceStationError("审核时间缺失，无法发布申请结果")

    workitem = (
        WorkItem.objects.select_for_update()
        .filter(uniq_key=_station_review_key(application.pk))
        .first()
    )
    if not workitem:
        raise AfterSalesServiceStationError("原申请任务不存在，无法更新审核结果")

    result_marker = reviewed_at.isoformat()
    payload = dict(workitem.payload or {})
    if payload.get("station_result_reviewed_at") == result_marker:
        # Protect the already-read card if a retry repeats the same result push.
        return

    recipients = _result_notice_recipients(application)
    _root, _sales_role, _sales_identities, reviewer_openids = (
        _factory_sales_review_audience(application.organization)
    )
    reviewer_openids = set(reviewer_openids)
    actor_openid = _usable_openid(actor)
    title = f"售后服务站申请{status_label}"
    summary = (
        f"{application.organization.name}的服务站申请{status_label}。"
        + (f"审核说明：{application.review_note}" if application.review_note else "")
    )
    now = timezone.now()

    # Keep the existing review key/type and update its business snapshot.
    payload.update(
        {
            "station_application_id": application.pk,
            "organization_id": application.organization_id,
            "organization_name": application.organization.name,
            "result": application.status,
            "reviewed_by_name": actor_name,
            "review_note": application.review_note,
            "station_result_reviewed_at": result_marker,
        }
    )
    workitem.title = title
    workitem.summary = summary
    workitem.payload = payload
    workitem.last_activity_at = now
    workitem.save(
        update_fields=("title", "summary", "payload", "last_activity_at", "updated_at")
    )

    # Reviewer copies already exist on this WorkItem. Add the awareness audience
    # to the same WorkItem, upserting by (workitem, openid), so overlaps never
    # produce a second task card for the same application.
    all_recipients = list(dict.fromkeys([*reviewer_openids, *recipients]))
    for openid in all_recipients:
        is_reviewer = openid in reviewer_openids
        subtitle = summary
        if is_reviewer:
            subtitle = (
                f"{summary}由你完成审核。"
                if openid == actor_openid
                else f"{summary}已由{actor_name}审核，无需重复操作。"
            )
        task_defaults = {
            "to_role_snapshot": get_role_snapshot(openid),
            "need_action": False,
            "state": "DONE" if is_reviewer else "NEW",
            "done_at": reviewed_at if is_reviewer else None,
            "done_reason": (
                "SELF_ACTION" if openid == actor_openid else "OTHER_ACTION"
            ) if is_reviewer else "",
            "cursor_time": now,
            "badge_group": "MY",
            "display_title_snapshot": title,
            "display_subtitle_snapshot": subtitle,
        }
        if not is_reviewer:
            # This is new result information for awareness recipients. If this
            # same task is updated in a later review round, mark the card unread.
            task_defaults["read_at"] = None
        UserTask.objects.update_or_create(
            workitem=workitem,
            to_openid=openid,
            defaults=task_defaults,
        )


def _application_for(organization):
    return (
        AfterSalesServiceStationApplication.objects.filter(
            organization=organization
        )
        .prefetch_related(
            "liaison_links__identity__user",
            "liaison_links__identity__role",
        )
        .order_by("-submitted_at", "-id")
        .first()
    )


def _serialize_application(application):
    if not application:
        return None

    liaison_links = list(application.liaison_links.all())
    return {
        "id": application.pk,
        "status": application.status,
        "status_name": application.get_status_display(),
        "service_location": {
            "name": application.service_location_name,
            "address": application.service_location_address,
            "latitude": str(application.service_latitude),
            "longitude": str(application.service_longitude),
            "captured_at": application.location_captured_at.isoformat(),
        },
        "receiving": {
            "address": application.receiving_address,
            "contact_name": application.receiving_contact_name,
            "contact_phone": application.receiving_contact_phone,
        },
        "liaison_identity_ids": [link.identity_id for link in liaison_links],
        "liaisons": [
            {
                "identity_id": link.identity_id,
                "name": link.member_name_snapshot,
                "role_code": link.role_code_snapshot,
                "role_name": link.role_name_snapshot,
            }
            for link in liaison_links
        ],
        "notice_version": application.notice_version,
        "submitted_at": application.submitted_at.isoformat(),
        "review_note": application.review_note,
    }


def _eligible_identity(user):
    identity = get_active_identity(user)
    if (
        not identity
        or identity.organization.organization_type.code != "MERCHANT"
        or identity.role.code != "merchant_owner"
    ):
        return None
    return identity


def _liaison_candidates_for(organization):
    """Return active local members from the highest-priority available role."""

    members = list(
        UserOrganization.objects.select_related("user", "role")
        .filter(
            organization=organization,
            is_active=True,
            user__is_active=True,
            role__is_active=True,
            role__code__in=LIAISON_ROLE_CODES,
        )
        .order_by("user__wx_nickname", "user__username", "id")
    )
    for role_code in LIAISON_ROLE_PRIORITY:
        preferred = [member for member in members if member.role.code == role_code]
        if preferred:
            return preferred
    return []


def get_service_station_application_context(*, user):
    """Return owner-only eligibility, current application, and local contacts."""

    identity = _eligible_identity(user)
    if not identity:
        return {
            "eligible": False,
            "can_submit": False,
            "reason": "此功能仅供商家负责人账号使用。",
            "application": None,
            "liaison_candidates": [],
        }

    organization = identity.organization
    if not organization.provides_after_sales:
        return {
            "eligible": False,
            "can_submit": False,
            "reason": "上级组织尚未将本商家设置为提供售后服务。",
            "organization": {"id": organization.pk, "name": organization.name},
            "application": None,
            "liaison_candidates": [],
        }

    application = _application_for(organization)
    candidates = _liaison_candidates_for(organization)
    selected_ids = (
        set(application.liaison_links.values_list("identity_id", flat=True))
        if application
        else set()
    )
    selected_candidate = next(
        (member for member in candidates if member.pk in selected_ids),
        candidates[0] if candidates else None,
    )
    can_submit = not application or application.status in {
        AfterSalesServiceStationApplication.Status.PENDING_REVIEW,
        AfterSalesServiceStationApplication.Status.REJECTED,
    }
    can_edit_receiving_address = bool(
        application
        and application.status == AfterSalesServiceStationApplication.Status.APPROVED
    )

    return {
        "eligible": True,
        "can_submit": can_submit,
        "can_edit_receiving_address": can_edit_receiving_address,
        "reason": "",
        "organization": {
            "id": organization.pk,
            "name": organization.name,
            "address": organization.address,
        },
        "application": _serialize_application(application),
        "notice": {
            "version": NOTICE_VERSION,
            "items": [
                "维修服务定位应为实际提供维修服务的地点，请在现场通过地图重新定位。",
                "官方售后服务收件地址和联系人将用于接收故障设备，请确保地址可签收且电话畅通。",
                "售后服务对接人按商家电工、商家销售经理、商家负责人的顺序选择，且只指定一人。",
            ],
        },
        "liaison_candidates": [
            {
                "identity_id": member.pk,
                "name": member.user.wx_nickname or member.user.username,
                "phone": member.user.phone or "",
                "role_code": member.role.code,
                "role_name": member.role.name,
                "selected": bool(selected_candidate and member.pk == selected_candidate.pk),
            }
            for member in candidates
        ],
    }


def _review_application_payload(application):
    links = list(
        application.liaison_links.select_related("identity__user", "identity__role")
    )
    reviewer_name = ""
    if application.reviewed_by_id:
        reviewer_name = (
            application.reviewed_by.wx_nickname
            or application.reviewed_by.username
        )
    return {
        "id": application.pk,
        "status": application.status,
        "status_name": application.get_status_display(),
        "organization": {
            "id": application.organization_id,
            "name": application.organization.name,
            "address": application.organization.address,
        },
        "applicant": {
            "user_id": application.submitted_by_id,
            "name": application.submitted_by.wx_nickname
            or application.submitted_by.username,
        },
        "service_location": {
            "name": application.service_location_name,
            "address": application.service_location_address,
            "latitude": str(application.service_latitude),
            "longitude": str(application.service_longitude),
            "captured_at": application.location_captured_at.isoformat(),
        },
        "receiving": {
            "address": application.receiving_address,
            "contact_name": application.receiving_contact_name,
            "contact_phone": application.receiving_contact_phone,
        },
        "liaisons": [
            {
                "identity_id": link.identity_id,
                "name": link.member_name_snapshot,
                "role_code": link.role_code_snapshot,
                "role_name": link.role_name_snapshot,
            }
            for link in links
        ],
        "submitted_at": application.submitted_at.isoformat(),
        "review_note": application.review_note,
        "reviewed_by_name": reviewer_name,
        "reviewed_at": application.reviewed_at.isoformat()
        if application.reviewed_at
        else "",
    }


def _is_factory_sales_reviewer(identity, application):
    if not identity:
        return False
    root = application.organization.get_factory_root()
    if not root or identity.organization_id != root.pk:
        return False
    if identity.role.code == "factory_sales":
        return True
    if identity.role.code != "factory_senior_manager":
        return False
    return SeniorManagerProfile.objects.filter(
        identity=identity,
        identity__is_active=True,
        identity__organization__is_active=True,
        identity__role__is_active=True,
        managed_roles__organization_type_id=root.organization_type_id,
        managed_roles__code="factory_sales",
        managed_roles__is_active=True,
    ).exists()


def _station_task_for_user(application, openid):
    task_scope = Q(workitem__uniq_key=_station_review_key(application.pk)) | Q(
        workitem__domain="AFTERSALE",
        workitem__type=STATION_RESULT_TYPE,
        workitem__biz_id=str(application.pk),
    )
    return list(
        UserTask.objects.select_related("workitem")
        .filter(to_openid=openid)
        .filter(task_scope)
        .order_by("-workitem__created_at", "-id")
    )


def get_service_station_review_context(*, user, application_id):
    """Return review details only to an account that received this application task."""
    application = (
        AfterSalesServiceStationApplication.objects.select_related(
            "organization",
            "organization__organization_type",
            "organization__factory_root",
            "submitted_by",
            "reviewed_by",
        )
        .filter(pk=application_id)
        .first()
    )
    if not application:
        raise AfterSalesServiceStationError("售后服务站申请不存在")

    identity = get_active_identity(user)
    openid = _usable_openid(user)
    if not identity or not openid:
        raise AfterSalesServiceStationPermissionError("当前账号无权查看此申请")

    tasks = _station_task_for_user(application, openid)
    if not tasks:
        raise AfterSalesServiceStationPermissionError("当前账号无权查看此申请")

    review_task = next(
        (task for task in tasks if task.workitem.uniq_key == _station_review_key(application.pk)),
        None,
    )
    review_item = review_task.workitem if review_task else None
    can_review = bool(
        review_item
        and review_item.status == "OPEN"
        and review_task.need_action
        and review_task.state in {"NEW", "READ"}
        and _is_factory_sales_reviewer(identity, application)
    )
    review_payload = review_item.payload if review_item and isinstance(review_item.payload, dict) else {}
    return {
        "application": _review_application_payload(application),
        "can_review": can_review,
        "task_completed_by_name": review_payload.get("completed_by_name", ""),
    }


def get_service_station_organization_badge(*, user):
    """Return approved-station status for the caller's own merchant organization.

    The badge is organization-scoped: every active member of the approved
    merchant organization receives the same status, while other organizations
    in the same factory chain do not.
    """
    identity = get_active_identity(user)
    if not identity or identity.organization.organization_type.code != "MERCHANT":
        return {
            "organization_id": identity.organization_id if identity else None,
            "approved": False,
        }

    approved = AfterSalesServiceStationApplication.objects.filter(
        organization_id=identity.organization_id,
        status=AfterSalesServiceStationApplication.Status.APPROVED,
    ).exists()
    return {
        "organization_id": identity.organization_id,
        "approved": approved,
    }


def _complete_station_review_task(workitem, *, actor, decision):
    actor_openid = _usable_openid(actor)
    own_task = (
        UserTask.objects.select_for_update()
        .filter(
            workitem=workitem,
            to_openid=actor_openid,
            need_action=True,
            state__in=("NEW", "READ"),
        )
        .first()
    )
    if not own_task:
        raise AfterSalesServiceStationPermissionError("当前账号不是此审核任务的处理人")

    now = timezone.now()
    actor_name = actor.wx_nickname or actor.username
    payload = dict(workitem.payload or {})
    payload.update(
        {
            "decision": decision,
            "completed_by_user_id": actor.pk,
            "completed_by_name": actor_name,
            "completed_at": now.isoformat(),
        }
    )
    changed = WorkItem.objects.filter(pk=workitem.pk, status="OPEN").update(
        status="DONE",
        done_by_openid=actor_openid,
        done_at=now,
        last_activity_at=now,
        payload=payload,
        updated_at=now,
    )
    if changed != 1:
        raise AfterSalesServiceStationError("该申请已由其他审核人完成，请刷新页面")

    workitem.user_tasks.filter(to_openid=actor_openid).update(
        state="DONE",
        need_action=False,
        done_at=now,
        done_reason="SELF_ACTION",
        display_title_snapshot="售后服务站申请审核已完成",
        display_subtitle_snapshot="由你完成",
        cursor_time=now,
        updated_at=now,
    )
    workitem.user_tasks.exclude(to_openid=actor_openid).update(
        state="DONE",
        need_action=False,
        done_at=now,
        done_reason="OTHER_ACTION",
        display_title_snapshot=f"售后服务站申请已由{actor_name}审核",
        display_subtitle_snapshot="同一申请无需重复操作",
        cursor_time=now,
        updated_at=now,
    )


@transaction.atomic
def review_service_station_application(*, user, application_id, action, review_note=""):
    action = str(action or "").strip().lower()
    if action not in {"approve", "reject"}:
        raise AfterSalesServiceStationError("审核操作无效")
    note = str(review_note or "").strip()
    if len(note) > 2000:
        raise AfterSalesServiceStationError("审核说明不能超过2000字")
    if action == "reject" and not note:
        raise AfterSalesServiceStationError("未通过申请时请填写审核说明")

    identity = get_active_identity(user)
    actor_openid = _usable_openid(user)
    if not identity or not actor_openid:
        raise AfterSalesServiceStationPermissionError("当前账号无权审核此申请")

    application = (
        AfterSalesServiceStationApplication.objects.select_for_update()
        .select_related(
            "organization",
            "organization__organization_type",
            "organization__factory_root",
            "submitted_by",
        )
        .filter(pk=application_id)
        .first()
    )
    if not application:
        raise AfterSalesServiceStationError("售后服务站申请不存在")

    workitem = (
        WorkItem.objects.select_for_update()
        .filter(uniq_key=_station_review_key(application.pk))
        .first()
    )
    if (
        not workitem
        or workitem.status != "OPEN"
        or application.status != AfterSalesServiceStationApplication.Status.PENDING_REVIEW
    ):
        raise AfterSalesServiceStationError("该申请已完成审核，请刷新页面")
    if not _is_factory_sales_reviewer(identity, application):
        raise AfterSalesServiceStationPermissionError("仅所属厂家的销售经理或其分管高级经理可审核")

    own_task = UserTask.objects.select_for_update().filter(
        workitem=workitem,
        to_openid=actor_openid,
        need_action=True,
        state__in=("NEW", "READ"),
    ).exists()
    if not own_task:
        raise AfterSalesServiceStationPermissionError("当前账号不是此审核任务的处理人")

    now = timezone.now()
    application.status = (
        AfterSalesServiceStationApplication.Status.APPROVED
        if action == "approve"
        else AfterSalesServiceStationApplication.Status.REJECTED
    )
    application.reviewed_by = user
    application.reviewed_at = now
    application.review_note = note
    application.save(
        update_fields=("status", "reviewed_by", "reviewed_at", "review_note", "updated_at")
    )

    _complete_station_review_task(workitem, actor=user, decision=action)
    _publish_station_result_notice(application, actor=user)
    return get_service_station_review_context(user=user, application_id=application.pk)


def _parse_coordinate(value, *, label, lower, upper):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise AfterSalesServiceStationError(f"{label}无效，请重新选择维修服务地点")

    if not number.is_finite() or number < lower or number > upper:
        raise AfterSalesServiceStationError(f"{label}超出有效范围")

    return number.quantize(Decimal("0.0000001"))


def _parse_location_capture(value):
    try:
        captured_at = parse_datetime(str(value or ""))
    except (TypeError, ValueError, OverflowError):
        captured_at = None
    if not captured_at:
        raise AfterSalesServiceStationError("请重新选择维修服务地点并确认定位")
    if timezone.is_naive(captured_at):
        captured_at = timezone.make_aware(captured_at, timezone.get_current_timezone())

    age = (timezone.now() - captured_at).total_seconds()
    if age > LOCATION_CAPTURE_MAX_AGE_SECONDS or age < -LOCATION_FUTURE_TOLERANCE_SECONDS:
        raise AfterSalesServiceStationError("定位确认已过期，请重新选择维修服务地点")
    return captured_at


def _validate_payload(data, *, organization):
    if not isinstance(data, dict):
        raise AfterSalesServiceStationError("申请资料格式无效")

    latitude = _parse_coordinate(
        data.get("service_latitude"),
        label="纬度",
        lower=Decimal("-90"),
        upper=Decimal("90"),
    )
    longitude = _parse_coordinate(
        data.get("service_longitude"),
        label="经度",
        lower=Decimal("-180"),
        upper=Decimal("180"),
    )
    captured_at = _parse_location_capture(data.get("location_captured_at"))

    location_name = str(data.get("service_location_name") or "").strip()
    location_address = str(data.get("service_location_address") or "").strip()
    receiving_address = str(data.get("receiving_address") or "").strip()

    if len(location_name) > 128 or len(location_address) > 255:
        raise AfterSalesServiceStationError("维修服务点信息过长")
    if not receiving_address or len(receiving_address) > 255:
        raise AfterSalesServiceStationError("请填写有效的官方售后服务收件地址（不超过255字）")
    raw_identity_ids = data.get("liaison_identity_ids")
    if not isinstance(raw_identity_ids, list):
        raise AfterSalesServiceStationError("请选择本组织售后服务对接人员")
    identity_ids = set()
    for value in raw_identity_ids:
        if isinstance(value, bool):
            continue
        try:
            identity_ids.add(int(value))
        except (TypeError, ValueError):
            continue
    if len(identity_ids) != 1:
        raise AfterSalesServiceStationError("请选择且只能选择一名售后服务对接人员")

    candidates = _liaison_candidates_for(organization)
    member = next((item for item in candidates if item.pk in identity_ids), None)
    if not member:
        raise AfterSalesServiceStationError(
            "请按优先顺序选择本组织的商家电工、商家销售经理或商家负责人"
        )

    contact_name = str(member.user.wx_nickname or member.user.username).strip()
    contact_phone = str(member.user.phone or "").strip()
    if not contact_name or len(contact_name) > 64:
        raise AfterSalesServiceStationError("所选对接人员名称无效，请先修正成员资料")
    phone_digits = re.sub(r"\D", "", contact_phone)
    if (
        len(contact_phone) > 32
        or not re.fullmatch(r"[0-9+()\-\s]+", contact_phone)
        or not 7 <= len(phone_digits) <= 20
    ):
        raise AfterSalesServiceStationError(
            "所选对接人员尚未登记有效手机号，请先补充成员手机号"
        )

    if data.get("notice_agreed") is not True:
        raise AfterSalesServiceStationError("请先阅读并确认申请须知")

    return {
        "service_location_name": location_name,
        "service_location_address": location_address,
        "service_latitude": latitude,
        "service_longitude": longitude,
        "location_captured_at": captured_at,
        "receiving_address": receiving_address,
        "receiving_contact_name": contact_name,
        "receiving_contact_phone": contact_phone,
        "members": [member],
    }


@transaction.atomic
def submit_service_station_application(*, user, data):
    identity = _eligible_identity(user)
    if not identity:
        raise AfterSalesServiceStationError("只有商家负责人账号可以提交服务站申请")

    organization = Organization.objects.select_for_update().get(
        pk=identity.organization_id
    )
    if organization.organization_type.code != "MERCHANT":
        raise AfterSalesServiceStationError("当前组织不是商家组织")
    if not organization.provides_after_sales:
        raise AfterSalesServiceStationError("上级组织尚未将本商家设置为提供售后服务")

    # Do not leave a submitted application pending without a real reviewer.
    _factory_sales_review_audience(organization)
    values = _validate_payload(data, organization=organization)
    latest = _application_for(organization)
    if latest and latest.status == AfterSalesServiceStationApplication.Status.APPROVED:
        raise AfterSalesServiceStationError("本组织的售后服务站已通过申请")

    if latest and latest.status == AfterSalesServiceStationApplication.Status.PENDING_REVIEW:
        application = latest
        application.submitted_by = user
    else:
        application = AfterSalesServiceStationApplication(
            organization=organization,
            submitted_by=user,
            status=AfterSalesServiceStationApplication.Status.PENDING_REVIEW,
        )

    application.submitted_at = timezone.now()
    application.service_location_name = values["service_location_name"]
    application.service_location_address = values["service_location_address"]
    application.service_latitude = values["service_latitude"]
    application.service_longitude = values["service_longitude"]
    application.location_captured_at = values["location_captured_at"]
    application.receiving_address = values["receiving_address"]
    application.receiving_contact_name = values["receiving_contact_name"]
    application.receiving_contact_phone = values["receiving_contact_phone"]
    application.notice_version = NOTICE_VERSION
    application.notice_agreed_at = timezone.now()
    application.review_note = ""
    application.save()

    application.liaison_links.all().delete()
    AfterSalesServiceStationLiaison.objects.bulk_create(
        [
            AfterSalesServiceStationLiaison(
                application=application,
                identity=member,
                member_name_snapshot=member.user.wx_nickname or member.user.username,
                role_code_snapshot=member.role.code,
                role_name_snapshot=member.role.name,
            )
            for member in values["members"]
        ]
    )

    _publish_station_review_task(
        application,
        actor_openid=_usable_openid(user),
    )
    return get_service_station_application_context(user=user)


@transaction.atomic
def update_approved_service_station_receiving_address(*, user, data):
    """Edit only the receiving address of an already approved service station."""

    identity = _eligible_identity(user)
    if not identity:
        raise AfterSalesServiceStationError(
            "只有商家负责人账号可以编辑售后服务站收件地址"
        )

    organization = Organization.objects.select_for_update().get(
        pk=identity.organization_id
    )
    if not organization.provides_after_sales:
        raise AfterSalesServiceStationError("本商家当前不具备售后服务站资格")

    address = str(data.get("receiving_address") or "").strip()
    if not address or len(address) > 255:
        raise AfterSalesServiceStationError(
            "请填写有效的官方售后服务收件地址（不超过255字）"
        )

    application = (
        AfterSalesServiceStationApplication.objects.select_for_update()
        .filter(organization=organization)
        .order_by("-submitted_at", "-id")
        .first()
    )
    if (
        not application
        or application.status != AfterSalesServiceStationApplication.Status.APPROVED
    ):
        raise AfterSalesServiceStationError(
            "只有已通过申请的售后服务站可单独编辑收件地址"
        )

    application.receiving_address = address
    application.save(update_fields=("receiving_address", "updated_at"))
    return get_service_station_application_context(user=user)
