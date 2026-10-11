# apps/workflow/services/workflow_service.py


from typing import Iterable, Optional, Dict, Any

from django.db import transaction
from django.utils import timezone

from account.models import SeniorManagerProfile, UserOrganization
from workflow.models import WorkItem, UserTask



def get_role_snapshot(openid):
    identity = (
        UserOrganization.objects.select_related("role")
        .filter(
            user__wx_openid=openid,
            user__is_active=True,
            is_active=True,
            role__is_active=True,
        )
        .first()
    )
    return identity.role.code if identity else ""


def _add_delegated_senior_manager_recipients(receivers):
    """Copy a role task to active senior managers of that same organization."""

    expanded = list(receivers)
    seen = set(expanded)
    role_recipients = list(
        UserOrganization.objects.select_related("organization", "role")
        .filter(
            user__wx_openid__in=receivers,
            user__is_active=True,
            is_active=True,
            organization__is_active=True,
            role__is_active=True,
        )
    )
    for recipient in role_recipients:
        # The senior manager invited the role holder whose task is being
        # copied.  This prevents one manager from receiving a peer manager's
        # tasks merely because both were assigned the same role code.
        if not recipient.inviter_id:
            continue
        if recipient.role.code in {
            "factory_senior_manager",
            "merchant_senior_manager",
        }:
            continue
        managers = SeniorManagerProfile.objects.filter(
            identity__user_id=recipient.inviter_id,
            identity__organization_id=recipient.organization_id,
            identity__organization__is_active=True,
            identity__is_active=True,
            identity__role__is_active=True,
            identity__role__code__in=(
                "factory_senior_manager",
                "merchant_senior_manager",
            ),
            identity__user__is_active=True,
            identity__user__wx_openid__isnull=False,
            managed_roles=recipient.role,
        ).values_list("identity__user__wx_openid", flat=True)
        for openid in managers:
            openid = (openid or "").strip()
            if openid and openid not in seen:
                seen.add(openid)
                expanded.append(openid)
    return expanded



@transaction.atomic
def publish_workitem(
    *,
    domain: str,
    type: str,
    biz_id: str,
    uniq_key: str,

    title: str,
    summary: str = "",
    link: str = "",

    recipients_openids: Iterable[str],

    need_action: bool = False,

    action_mode: str = "EACH_ACK",

    payload: Optional[Dict[str, Any]] = None,

    biz_ref: str = "",

    actor_openid: str = "",

    mark_workitem_done_immediately: bool = True,

):

    """
    创建工作项并分发任务

    WorkItem:
        业务母单

    UserTask:
        用户收件箱
    """


    now = timezone.now()

    payload = payload or {}



    # ==========================
    # 1. 创建/更新 WorkItem
    # ==========================

    workitem, created = WorkItem.objects.select_for_update().get_or_create(

        uniq_key=uniq_key,

        defaults={

            "domain": domain,

            "type": type,

            "biz_id": biz_id,

            "biz_ref": biz_ref,

            "title": title,

            "summary": summary,

            "link": link,

            "payload": payload,

            "action_mode": action_mode,

            "status": "OPEN",

            "actor_openid": actor_openid,

            "last_activity_at": now,

        }

    )



    if not created:


        workitem.title = title

        workitem.summary = summary

        workitem.link = link

        workitem.payload = payload

        workitem.action_mode = action_mode

        workitem.last_activity_at = now


        if workitem.status != "CANCELLED":

            workitem.status = "OPEN"


        workitem.save()



    # ==========================
    # 2. 收件人去重
    # ==========================


    receivers = []

    seen = set()


    for oid in recipients_openids:


        oid = (oid or "").strip()


        if oid and oid not in seen:

            seen.add(oid)

            receivers.append(oid)

    receivers = _add_delegated_senior_manager_recipients(receivers)



    # ==========================
    # 3. 创建 UserTask
    # ==========================


    for oid in receivers:


        UserTask.objects.update_or_create(

            workitem=workitem,

            to_openid=oid,


            defaults={


                "to_role_snapshot":
                    get_role_snapshot(oid),


                "need_action":
                    need_action,


                "state":
                    "NEW",


                "read_at":
                    None,


                "done_at":
                    None,


                "done_reason":
                    "",


                "cursor_time":
                    now,


                "badge_group":
                    "MY",


                "display_title_snapshot":
                    title,


                "display_subtitle_snapshot":
                    summary,

            }

        )



    # ==========================
    # 4. 通知型任务自动完成母单
    # ==========================


    if mark_workitem_done_immediately:


        workitem.status = "DONE"

        workitem.done_by_openid = actor_openid

        workitem.done_at = now

        workitem.last_activity_at = now


        workitem.save(
            update_fields=[
                "status",
                "done_by_openid",
                "done_at",
                "last_activity_at",
                "updated_at",
            ]
        )



    return workitem
