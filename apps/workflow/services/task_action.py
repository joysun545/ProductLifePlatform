# apps/workflow/services/task_action.py

from django.db import transaction
from django.utils import timezone

from workflow.models import UserTask


class TaskActionError(Exception):
    """任务不存在或当前账号无权操作。"""


def _open_quality_recall_group(task, now):
    """Acknowledge one account's whole recall-campaign inbox projection.

    The real UserTask rows remain auditable. Knowledge rows finish on open;
    executable rows only become READ and remain pending until their domain
    action completes them.
    """

    workitem = task.workitem
    if not workitem.type.startswith("QUALITY_RECALL_") or not workitem.biz_id:
        return False

    tasks = (
        UserTask.objects.select_for_update()
        .select_related("workitem")
        .filter(
            to_openid=task.to_openid,
            workitem__type__startswith="QUALITY_RECALL_",
            workitem__biz_id=workitem.biz_id,
            state__in=("NEW", "READ"),
        )
    )
    from account.models import User
    from quality_trace.services.recall_access_service import RecallViewer, FACTORY_RECALL_OBSERVER_ROLES
    user = User.objects.filter(wx_openid=task.to_openid, is_active=True).first()
    observer = bool(user and RecallViewer(user).role in FACTORY_RECALL_OBSERVER_ROLES)
    for grouped_task in tasks:
        if grouped_task.state == "NEW":
            grouped_task.read_at = now
        if grouped_task.need_action and not observer:
            grouped_task.state = "READ"
        else:
            grouped_task.state = "DONE"
            grouped_task.done_at = now
            grouped_task.done_reason = "RECALL_GROUP_OPENED"
        grouped_task.cursor_time = grouped_task.workitem.last_activity_at
        grouped_task.save(update_fields=[
            "state", "read_at", "done_at", "done_reason",
            "cursor_time", "updated_at",
        ])
    return True


def _open_aftersales_group(task, now):
    """Opening one after-sales card acknowledges all visible case updates.

    Knowledge-only rows are completed by opening the service order. Action
    rows only become READ; the domain operation remains responsible for
    completing them.
    """

    payload = task.workitem.payload if isinstance(task.workitem.payload, dict) else {}
    group_key = payload.get("inbox_group_key")
    if not group_key or payload.get("inbox_group_kind") != "AFTERSALES":
        return False

    tasks = (
        UserTask.objects.select_for_update()
        .select_related("workitem")
        .filter(
            to_openid=task.to_openid,
            workitem__domain="AFTERSALE",
            workitem__payload__inbox_group_key=group_key,
            state__in=("NEW", "READ"),
        )
    )
    for grouped_task in tasks:
        if grouped_task.state == "NEW":
            grouped_task.read_at = now
        if grouped_task.need_action:
            grouped_task.state = "READ"
        else:
            grouped_task.state = "DONE"
            grouped_task.done_at = now
            grouped_task.done_reason = "GROUP_OPENED"
        grouped_task.cursor_time = grouped_task.workitem.last_activity_at
        grouped_task.save(
            update_fields=[
                "state", "read_at", "done_at", "done_reason",
                "cursor_time", "updated_at",
            ]
        )
    return True


@transaction.atomic
def open_task(openid, usertask_id):
    """
    用户打开任务

    NEW -> READ

    知晓类任务:
    EACH_ACK + need_action=False

    READ -> DONE
    """

    try:
        task = (
            UserTask.objects
            .select_for_update()
            .select_related("workitem")
            .get(id=usertask_id, to_openid=openid)
        )

    except (UserTask.DoesNotExist, ValueError, TypeError) as exc:
        raise TaskActionError("任务不存在或无权操作") from exc


    if task.state == "DONE":
        return {
            "state": task.state
        }


    now = timezone.now()

    if _open_quality_recall_group(task, now):
        task.refresh_from_db(fields=("state",))
        return {"state": task.state}

    if _open_aftersales_group(task, now):
        task.refresh_from_db(fields=("state",))
        return {"state": task.state}


    # 已读
    if task.state == "NEW":
        task.state = "READ"
        task.read_at = now


    workitem = task.workitem


    # 知晓类任务自动完成
    if (
        not task.need_action
        and workitem.action_mode == "EACH_ACK"
    ):

        task.state = "DONE"
        task.done_at = now
        task.done_reason = "OPENED"


    task.cursor_time = workitem.last_activity_at


    task.save(
        update_fields=[
            "state",
            "read_at",
            "done_at",
            "done_reason",
            "cursor_time",
            "updated_at",
        ]
    )


    return {
        "state": task.state
    }
