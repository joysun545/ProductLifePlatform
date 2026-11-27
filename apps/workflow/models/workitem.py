# apps/workflow/models/workitem.py

from django.db import models
from django.utils import timezone


class WorkItem(models.Model):
    """
    任务母单

    一次业务协作的主体：
    - 物流
    - 售后
    - 安装
    - 激活
    - 系统通知
    """

    DOMAIN_CHOICES = [

        ("LOGISTICS", "物流"),
        ("AFTERSALE", "售后"),
        ("INSTALL", "装车"),
        ("ACTIVATE", "激活"),
        ("PROFILE", "资料变更"),
        ("SALES", "销售订单"),
        ("SYSTEM", "系统通知"),

    ]


    ACTION_MODE_CHOICES = [

        ("EACH_ACK", "每人知晓即完成"),
        ("ANYONE_DO", "多人操作一人完成"),
        ("ALL_DO", "全部完成"),
        ("OWNER_OR_SALES", "负责人或全部销售经理完成"),

    ]


    STATUS_CHOICES = [

        ("OPEN", "进行中"),
        ("DONE", "已完成"),
        ("CANCELLED", "已取消"),

    ]


    id = models.BigAutoField(
        primary_key=True
    )


    domain = models.CharField(
        max_length=24,
        choices=DOMAIN_CHOICES,
        db_index=True
    )


    type = models.CharField(
        max_length=64,
        db_index=True
    )


    biz_id = models.CharField(
        max_length=128,
        db_index=True
    )


    biz_ref = models.CharField(
        max_length=255,
        blank=True,
        default=""
    )


    title = models.CharField(
        max_length=255,
        blank=True,
        default=""
    )


    summary = models.TextField(
        blank=True,
        default=""
    )


    link = models.CharField(
        max_length=255,
        blank=True,
        default=""
    )


    payload = models.JSONField(
        default=dict,
        blank=True
    )


    role_delivery = models.JSONField(default=dict, blank=True, editable=False)

    action_mode = models.CharField(
        max_length=16,
        choices=ACTION_MODE_CHOICES,
        default="EACH_ACK"
    )


    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default="OPEN",
        db_index=True
    )


    actor_openid = models.CharField(
        max_length=64,
        blank=True,
        default=""
    )


    done_by_openid = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True
    )


    done_at = models.DateTimeField(
        null=True,
        blank=True
    )

    assignee_openid = models.CharField(
        max_length=64,
        blank=True,
        default=""
    )


    last_activity_at = models.DateTimeField(
        default=timezone.now,
        db_index=True
    )


    uniq_key = models.CharField(
        max_length=255,
        unique=True
    )


    created_at = models.DateTimeField(
        auto_now_add=True
    )


    updated_at = models.DateTimeField(
        auto_now=True
    )


    class Meta:

        db_table = "workflow_work_item"


    def touch(self):

        self.last_activity_at = timezone.now()

        self.save(
            update_fields=[
                "last_activity_at",
                "updated_at"
            ]
        )
