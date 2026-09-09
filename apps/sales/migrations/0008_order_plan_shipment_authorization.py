from django.conf import settings
from django.db import migrations, models
from django.utils import timezone
import django.db.models.deletion


AUTHORIZATION_TYPE = "ORDER_PLAN_SHIPMENT_AUTHORIZATION"
STOCK_NOTICE_TYPE = "ORDER_PLAN_SHIPMENT_AUTHORIZED_STOCK"
STOCK_OUTBOUND_TYPE = "ORDER_PLAN_STOCK_OUTBOUND"


def _plan_payload(plan):
    return {
        "order_plan_id": plan.pk,
        "plan_code": plan.plan_code,
    }


def backfill_authorization(apps, schema_editor):
    OrderPlan = apps.get_model("sales", "OrderPlan")
    WorkItem = apps.get_model("workflow", "WorkItem")
    UserTask = apps.get_model("workflow", "UserTask")
    now = timezone.now()

    # 已经完成实际发货或商家入库的历史订单视为已由创建销售经理授权，
    # 避免部署新门禁后改变已经完成的业务事实。
    for plan in OrderPlan.objects.select_related("created_by").filter(
        status__in=("SHIPPED", "RECEIVED")
    ):
        plan.shipment_authorized_at = plan.shipped_at or plan.updated_at or now
        plan.shipment_authorized_by_id = plan.created_by_id
        plan.save(
            update_fields=(
                "shipment_authorized_at",
                "shipment_authorized_by",
            )
        )

    # 部署时仍停留在“生产完成”的订单必须重新经过销售经理授权。
    # 如果旧逻辑已经提前创建了库管出库任务，先将其关闭；销售经理授权后
    # 服务层会在二维码全部打印的情况下用同一 uniq_key 原子地重新打开。
    for plan in OrderPlan.objects.select_related(
        "created_by", "merchant_organization", "brand"
    ).filter(status="PRODUCTION_COMPLETED"):
        recipient = (plan.created_by.wx_openid or "").strip()
        if not recipient:
            continue

        production_workitem = WorkItem.objects.filter(
            uniq_key=f"sales:order-plan:{plan.pk}:production-confirm"
        ).first()
        actor_openid = (
            production_workitem.done_by_openid if production_workitem else ""
        )
        summary = (
            f"{plan.merchant_organization.name}｜"
            f"{plan.brand.name}｜共{plan.total_quantity}台"
        )
        payload = _plan_payload(plan)
        workitem, _ = WorkItem.objects.update_or_create(
            uniq_key=f"sales:order-plan:{plan.pk}:shipment-authorization",
            defaults={
                "domain": "SALES",
                "type": AUTHORIZATION_TYPE,
                "biz_id": plan.plan_code,
                "biz_ref": f"sales.OrderPlan:{plan.pk}",
                "title": "生产完成，待下达发货指令",
                "summary": summary,
                "link": (
                    "/pages/sales/order_plan_list/order_plan_list"
                    f"?focus_plan_id={plan.pk}"
                ),
                "payload": payload,
                "action_mode": "ANYONE_DO",
                "status": "OPEN",
                "actor_openid": actor_openid,
                "done_by_openid": "",
                "done_at": None,
                "last_activity_at": now,
            },
        )
        UserTask.objects.update_or_create(
            workitem=workitem,
            to_openid=recipient,
            defaults={
                "to_role_snapshot": "factory_sales",
                "need_action": True,
                "state": "NEW",
                "read_at": None,
                "done_at": None,
                "done_reason": "",
                "cursor_time": now,
                "badge_group": "MY",
                "display_title_snapshot": "生产完成，待下达发货指令",
                "display_subtitle_snapshot": summary,
            },
        )

        stock_workitem = WorkItem.objects.filter(
            type=STOCK_OUTBOUND_TYPE,
            uniq_key=f"sales:order-plan:{plan.pk}:stock-outbound",
            status="OPEN",
        ).first()
        if stock_workitem:
            stock_workitem.status = "DONE"
            stock_workitem.done_at = now
            stock_workitem.last_activity_at = now
            stock_workitem.save(
                update_fields=(
                    "status",
                    "done_at",
                    "last_activity_at",
                    "updated_at",
                )
            )
            stock_workitem.user_tasks.update(
                state="DONE",
                need_action=False,
                done_at=now,
                done_reason="OTHER_ACTION",
                cursor_time=now,
                updated_at=now,
            )


def reverse_backfill(apps, schema_editor):
    OrderPlan = apps.get_model("sales", "OrderPlan")
    WorkItem = apps.get_model("workflow", "WorkItem")
    now = timezone.now()

    # 回滚到旧逻辑时，已完成全部二维码打印但尚未出库的订单重新开放
    # 原库管任务，恢复迁移前的行为。
    for plan in OrderPlan.objects.filter(status="PRODUCTION_COMPLETED"):
        if plan.items.filter(qrcode_printed_at__isnull=True).exists():
            continue
        stock_workitem = WorkItem.objects.filter(
            type=STOCK_OUTBOUND_TYPE,
            uniq_key=f"sales:order-plan:{plan.pk}:stock-outbound",
        ).first()
        if not stock_workitem:
            continue
        stock_workitem.status = "OPEN"
        stock_workitem.done_by_openid = ""
        stock_workitem.done_at = None
        stock_workitem.last_activity_at = now
        stock_workitem.save(
            update_fields=(
                "status",
                "done_by_openid",
                "done_at",
                "last_activity_at",
                "updated_at",
            )
        )
        stock_workitem.user_tasks.update(
            state="NEW",
            need_action=True,
            read_at=None,
            done_at=None,
            done_reason="",
            cursor_time=now,
            updated_at=now,
        )

    WorkItem.objects.filter(
        type__in=(AUTHORIZATION_TYPE, STOCK_NOTICE_TYPE)
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0007_order_plan_finance"),
        ("workflow", "0003_alter_workitem_action_mode"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="orderplan",
            name="shipment_authorized_at",
            field=models.DateTimeField(
                blank=True,
                db_index=True,
                null=True,
                verbose_name="发货授权时间",
            ),
        ),
        migrations.AddField(
            model_name="orderplan",
            name="shipment_authorized_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="authorized_order_plan_shipments",
                to=settings.AUTH_USER_MODEL,
                verbose_name="发货授权人",
            ),
        ),
        migrations.RunPython(backfill_authorization, reverse_backfill),
        migrations.AddConstraint(
            model_name="orderplan",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        shipment_authorized_at__isnull=True,
                        shipment_authorized_by__isnull=True,
                    )
                    | models.Q(
                        shipment_authorized_at__isnull=False,
                        shipment_authorized_by__isnull=False,
                    )
                ),
                name="sales_shipment_authorization_fields_together",
            ),
        ),
    ]
