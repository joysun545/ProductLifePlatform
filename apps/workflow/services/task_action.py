# apps/workflow/services/task_action.py

from django.db import transaction
from django.utils import timezone

from workflow.models import UserTask


class TaskActionError(Exception):
    """任务不存在或当前账号无权操作。"""


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
