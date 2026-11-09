# apps/workflow/services/task_action.py

from django.db import transaction
from django.utils import timezone

from workflow.models import UserTask
from workflow.services.task_root import task_root_organizations


class TaskActionError(Exception):
    """任务不存在或当前账号无权操作。"""


def _open_goods_transfer_group(task, now):
    """Acknowledge this account's transfer updates, preserving domain actions.

    Reuse the list's canonical scope, including historical references. An old
    completed entry can still open newly published updates to the same card.
    No other account, transfer or WorkItem is completed by opening this card.
    """
    if not task.workitem.type.startswith("GOODS_TRANSFER_"):
        return None

    from .task_query import _task_groups, _serialize_group

    match = next((
        (scope, children) for scope, children in _task_groups(task.to_openid).items()
        if scope.startswith("goods-transfer:")
        and any(child.pk == task.pk for child in children)
    ), None)
    if match is None:
        return None
    scope, children = match
    child_ids = [child.pk for child in children]
    pending = (
        UserTask.objects.select_for_update().select_related("workitem")
        .filter(pk__in=child_ids, to_openid=task.to_openid,
                badge_group="MY", state__in=("NEW", "READ"))
        .order_by("pk")
    )
    for child in pending:
        if child.state == "NEW":
            child.read_at = now
        if child.need_action:
            child.state = "READ"
        else:
            child.state = "DONE"
            child.done_at = now
            child.done_reason = "GOODS_TRANSFER_GROUP_OPENED"
        child.cursor_time = child.workitem.last_activity_at
        child.save(update_fields=[
            "state", "read_at", "done_at", "done_reason",
            "cursor_time", "updated_at",
        ])

    # Resolve the remaining operation after acknowledgement. The client must
    # not follow an obsolete notice link from a list fetched before this open.
    refreshed = next(
        members for key, members in _task_groups(task.to_openid).items() if key == scope
    )
    roots = task_root_organizations(child.workitem for child in refreshed)
    card = _serialize_group(scope, refreshed, roots)
    task.refresh_from_db(fields=("state",))
    return {
        "state": task.state,
        "group_id": card["group_id"],
        "group_kind": card["group_kind"],
        "entry_task_id": card["entry_task_id"],
        "link": card["link"],
        "root_organization": card["root_organization"],
    }


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
    observer = bool(user and RecallViewer(user).has_role(FACTORY_RECALL_OBSERVER_ROLES))
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

    tasks = list(
        UserTask.objects.select_for_update()
        .select_related("workitem")
        .filter(
            to_openid=task.to_openid,
            workitem__domain="AFTERSALE",
            workitem__payload__inbox_group_key=group_key,
            state__in=("NEW", "READ"),
        )
    )
    from account.models import User
    from lifecycle.services.replacement_task_scope import resolve_scopes
    user = User.objects.filter(wx_openid=task.to_openid, is_active=True).first()
    replacement_contexts = resolve_scopes([row.workitem for row in tasks], user)
    for grouped_task in tasks:
        if grouped_task.state == "NEW":
            grouped_task.read_at = now
        replacement = replacement_contexts.get(grouped_task.workitem_id)
        replacement_observer = bool(
            replacement and replacement["observer"] and not replacement["responsible"]
        )
        if grouped_task.need_action and not replacement_observer:
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


    now = timezone.now()
    goods_result = _open_goods_transfer_group(task, now)
    if goods_result is not None:
        return goods_result

    root = task_root_organizations([task.workitem]).get(task.workitem_id)
    context = {"root_organization": root} if root else {}

    if task.state == "DONE":
        return {"state": task.state, **context}


    if _open_quality_recall_group(task, now):
        task.refresh_from_db(fields=("state",))
        return {"state": task.state, **context}

    if _open_aftersales_group(task, now):
        task.refresh_from_db(fields=("state",))
        return {"state": task.state, **context}


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


    return {"state": task.state, **context}
