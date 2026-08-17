from django.db import migrations


NOTICE_TYPE = "ORDER_PLAN_LOGISTICS_DOCUMENT_AVAILABLE"
FACTORY_SALES_ROLE = "factory_sales"


def add_factory_sales_notice_tasks(apps, schema_editor):
    OrderPlan = apps.get_model("sales", "OrderPlan")
    UserOrganization = apps.get_model("account", "UserOrganization")
    WorkItem = apps.get_model("workflow", "WorkItem")
    UserTask = apps.get_model("workflow", "UserTask")

    notices = WorkItem.objects.filter(type=NOTICE_TYPE).order_by("id")
    for notice in notices.iterator():
        reference = str(notice.biz_ref or "")
        prefix = "sales.OrderPlan:"
        if not reference.startswith(prefix):
            continue
        try:
            plan_id = int(reference[len(prefix):])
        except (TypeError, ValueError):
            continue

        plan = OrderPlan.objects.filter(pk=plan_id).first()
        if not plan or not plan.created_by_id:
            continue
        openid = (
            UserOrganization.objects.filter(
                user_id=plan.created_by_id,
                organization_id=plan.factory_organization_id,
                role__code=FACTORY_SALES_ROLE,
                role__is_active=True,
                is_active=True,
                user__is_active=True,
            )
            .exclude(user__wx_openid__isnull=True)
            .exclude(user__wx_openid="")
            .values_list("user__wx_openid", flat=True)
            .first()
        )
        if not openid:
            continue

        UserTask.objects.get_or_create(
            workitem=notice,
            to_openid=openid,
            defaults={
                "to_role_snapshot": FACTORY_SALES_ROLE,
                "need_action": False,
                "state": "NEW",
                "cursor_time": notice.last_activity_at,
                "badge_group": "MY",
                "display_title_snapshot": notice.title,
                "display_subtitle_snapshot": notice.summary,
            },
        )


def remove_factory_sales_notice_tasks(apps, schema_editor):
    UserTask = apps.get_model("workflow", "UserTask")
    UserTask.objects.filter(
        workitem__type=NOTICE_TYPE,
        to_role_snapshot=FACTORY_SALES_ROLE,
        need_action=False,
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0009_order_plan_logistics_documents"),
        ("workflow", "0003_alter_workitem_action_mode"),
    ]

    operations = [
        migrations.RunPython(
            add_factory_sales_notice_tasks,
            remove_factory_sales_notice_tasks,
        ),
    ]
