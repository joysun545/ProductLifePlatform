import django.db.models.deletion
from django.db import migrations, models
from django.utils import timezone

import sales.private_storage


UPLOAD_TYPE = "ORDER_PLAN_LOGISTICS_DOCUMENT_UPLOAD"
NOTICE_TYPE = "ORDER_PLAN_LOGISTICS_DOCUMENT_AVAILABLE"


def backfill_logistics_upload_tasks(apps, schema_editor):
    OrderPlan = apps.get_model("sales", "OrderPlan")
    OrganizationRole = apps.get_model("account", "OrganizationRole")
    UserOrganization = apps.get_model("account", "UserOrganization")
    WorkItem = apps.get_model("workflow", "WorkItem")
    UserTask = apps.get_model("workflow", "UserTask")
    now = timezone.now()

    OrganizationRole.objects.filter(code="factory_logistics").update(
        name="厂家物流经理"
    )

    for plan in OrderPlan.objects.select_related(
        "factory_organization",
        "merchant_organization",
        "brand",
        "shipped_by",
    ).filter(status__in=("SHIPPED", "RECEIVED")):
        recipients = list(
            UserOrganization.objects.filter(
                organization_id=plan.factory_organization_id,
                role__code="factory_logistics",
                role__is_active=True,
                is_active=True,
                user__is_active=True,
                user__wx_openid__isnull=False,
            )
            .exclude(user__wx_openid="")
            .values_list("user__wx_openid", flat=True)
            .distinct()
        )
        if not recipients:
            continue

        summary = (
            f"{plan.merchant_organization.name}｜"
            f"{plan.brand.name}｜共{plan.total_quantity}台"
        )
        workitem, _ = WorkItem.objects.update_or_create(
            uniq_key=f"sales:order-plan:{plan.pk}:logistics-document-upload",
            defaults={
                "domain": "SALES",
                "type": UPLOAD_TYPE,
                "biz_id": plan.plan_code,
                "biz_ref": f"sales.OrderPlan:{plan.pk}",
                "title": "订单已发货，待上传物流单据",
                "summary": summary,
                "link": (
                    "/pages/sales/order_logistics/order_logistics"
                    f"?order_plan_id={plan.pk}"
                ),
                "payload": {
                    "order_plan_id": plan.pk,
                    "plan_code": plan.plan_code,
                },
                "action_mode": "ANYONE_DO",
                "status": "OPEN",
                "actor_openid": (
                    plan.shipped_by.wx_openid if plan.shipped_by_id else ""
                ),
                "done_by_openid": "",
                "done_at": None,
                "last_activity_at": now,
            },
        )
        for openid in recipients:
            UserTask.objects.update_or_create(
                workitem=workitem,
                to_openid=openid,
                defaults={
                    "to_role_snapshot": "factory_logistics",
                    "need_action": True,
                    "state": "NEW",
                    "read_at": None,
                    "done_at": None,
                    "done_reason": "",
                    "cursor_time": now,
                    "badge_group": "MY",
                    "display_title_snapshot": "订单已发货，待上传物流单据",
                    "display_subtitle_snapshot": summary,
                },
            )


def reverse_logistics_tasks(apps, schema_editor):
    OrganizationRole = apps.get_model("account", "OrganizationRole")
    WorkItem = apps.get_model("workflow", "WorkItem")
    WorkItem.objects.filter(type__in=(UPLOAD_TYPE, NOTICE_TYPE)).delete()
    OrganizationRole.objects.filter(code="factory_logistics").update(
        name="厂家物流"
    )


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0008_order_plan_shipment_authorization"),
        ("workflow", "0003_alter_workitem_action_mode"),
    ]

    operations = [
        migrations.CreateModel(
            name="OrderPlanLogisticsDocument",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("sequence_no", models.PositiveIntegerField(verbose_name="单据序号")),
                (
                    "client_request_id",
                    models.CharField(max_length=64, verbose_name="客户端请求标识"),
                ),
                (
                    "document_file",
                    models.FileField(
                        storage=sales.private_storage.OrderLogisticsPrivateStorage(),
                        upload_to="documents/%Y/%m/",
                        verbose_name="物流单据文件",
                    ),
                ),
                (
                    "original_name",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=255,
                        verbose_name="原文件名",
                    ),
                ),
                (
                    "file_type",
                    models.CharField(
                        choices=(("IMAGE", "图片"), ("PDF", "PDF")),
                        max_length=16,
                        verbose_name="文件类型",
                    ),
                ),
                (
                    "content_type",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=100,
                        verbose_name="内容类型",
                    ),
                ),
                ("file_size", models.PositiveIntegerField(verbose_name="文件大小")),
                (
                    "remark",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=255,
                        verbose_name="单据备注",
                    ),
                ),
                (
                    "uploaded_at",
                    models.DateTimeField(
                        auto_now_add=True,
                        db_index=True,
                        verbose_name="上传时间",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                (
                    "order_plan",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="logistics_documents",
                        to="sales.orderplan",
                        verbose_name="订单计划",
                    ),
                ),
                (
                    "uploaded_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="uploaded_order_logistics_documents",
                        to="account.user",
                        verbose_name="上传人",
                    ),
                ),
            ],
            options={
                "verbose_name": "订单物流单据",
                "verbose_name_plural": "订单物流单据",
                "ordering": ("sequence_no", "id"),
            },
        ),
        migrations.AddConstraint(
            model_name="orderplanlogisticsdocument",
            constraint=models.UniqueConstraint(
                fields=("order_plan", "sequence_no"),
                name="sales_unique_logistics_document_sequence",
            ),
        ),
        migrations.AddConstraint(
            model_name="orderplanlogisticsdocument",
            constraint=models.UniqueConstraint(
                fields=("order_plan", "client_request_id"),
                name="sales_unique_logistics_document_request",
            ),
        ),
        migrations.RunPython(
            backfill_logistics_upload_tasks,
            reverse_logistics_tasks,
        ),
    ]
