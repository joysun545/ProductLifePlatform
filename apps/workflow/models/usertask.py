# apps/workflow/models/usertask.py

from django.db import models
from django.utils import timezone

from .workitem import WorkItem



class UserTask(models.Model):

    """
    用户任务收件箱

    一个 WorkItem
    可以对应多个 UserTask
    """


    STATE_CHOICES = [

        ("NEW","未读"),
        ("READ","已读"),
        ("DONE","完成"),

    ]


    id = models.BigAutoField(
        primary_key=True
    )


    workitem = models.ForeignKey(

        WorkItem,

        on_delete=models.CASCADE,

        related_name="user_tasks"

    )


    to_openid = models.CharField(

        max_length=64,

        db_index=True

    )


    to_role_snapshot = models.CharField(

        max_length=32,

        blank=True,

        default=""

    )


    need_action = models.BooleanField(

        default=False,

        db_index=True

    )


    state = models.CharField(

        max_length=16,

        choices=STATE_CHOICES,

        default="NEW",

        db_index=True

    )


    read_at = models.DateTimeField(

        null=True,

        blank=True

    )


    done_at = models.DateTimeField(

        null=True,

        blank=True

    )


    DONE_REASON_CHOICES = [

        ("", ""),

        ("OPENED", "点开即完成"),

        ("SELF_ACTION", "本人操作完成"),

        ("OTHER_ACTION", "他人操作导致完成"),

        ("ROLE_REASSIGNED", "岗位分管调整"),

    ]


    done_reason = models.CharField(

        max_length=32,

        choices=DONE_REASON_CHOICES,

        blank=True,

        default=""

    )


    cursor_time = models.DateTimeField(

        default=timezone.now,

        db_index=True

    )


    badge_group = models.CharField(

        max_length=32,

        default="MY",

        db_index=True

    )


    display_title_snapshot = models.CharField(

        max_length=255,

        blank=True,

        default=""

    )


    display_subtitle_snapshot = models.CharField(

        max_length=255,

        blank=True,

        default=""

    )


    created_at = models.DateTimeField(
        auto_now_add=True
    )


    updated_at = models.DateTimeField(
        auto_now=True
    )


    class Meta:

        db_table = "workflow_user_task"

        constraints = [

            models.UniqueConstraint(

                fields=[
                    "workitem",
                    "to_openid"
                ],

                name="uniq_workitem_user_task"

            )

        ]
