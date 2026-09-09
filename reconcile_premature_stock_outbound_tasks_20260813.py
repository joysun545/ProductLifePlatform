"""校准旧版本过早创建的厂家库管出库任务。

默认只预览；确认输出无误后设置 PLDP_APPLY=1 才会真正删除。
仅处理仍为 OPEN、且订单尚有未打印产品型号的 ORDER_PLAN_STOCK_OUTBOUND。
删除后，订单最后一个产品型号确认打印时会按新版逻辑重新创建任务。
"""

import os

from django.db import transaction

from sales.models import OrderPlan
from workflow.models import WorkItem


apply_changes = os.environ.get("PLDP_APPLY", "").strip() == "1"
candidates = []

for workitem in (
    WorkItem.objects.filter(
        type="ORDER_PLAN_STOCK_OUTBOUND",
        status="OPEN",
    )
    .prefetch_related("user_tasks")
    .order_by("id")
):
    plan = OrderPlan.objects.filter(plan_code=workitem.biz_id).first()
    if not plan and workitem.biz_ref.startswith("sales.OrderPlan:"):
        raw_id = workitem.biz_ref.rsplit(":", 1)[-1]
        if raw_id.isdigit():
            plan = OrderPlan.objects.filter(pk=int(raw_id)).first()
    if not plan:
        print(
            f"跳过：workitem_id={workitem.pk}，找不到对应订单 "
            f"biz_id={workitem.biz_id}"
        )
        continue

    unprinted = list(
        plan.items.filter(qrcode_printed_at__isnull=True)
        .select_related("product_model")
        .values_list("product_model__name", flat=True)
    )
    if unprinted:
        candidates.append((workitem, plan, unprinted))

print("===== 过早创建的厂家库管出库任务校准 =====")
print("运行模式：" + ("执行" if apply_changes else "仅预览"))

if not candidates:
    print("没有需要校准的任务。")

for workitem, plan, unprinted in candidates:
    print(
        f"workitem_id={workitem.pk} | order={plan.plan_code} | "
        f"未打印型号={'、'.join(unprinted)} | "
        f"收件任务数={workitem.user_tasks.count()}"
    )

if apply_changes and candidates:
    with transaction.atomic():
        locked_ids = [row[0].pk for row in candidates]
        deleted = WorkItem.objects.select_for_update().filter(
            pk__in=locked_ids,
            type="ORDER_PLAN_STOCK_OUTBOUND",
            status="OPEN",
        ).delete()
    print(f"已删除过早任务：{len(candidates)} 个；级联记录：{deleted[0]} 条。")
    print("这些订单完成最后一个产品型号打印后，将自动重新生成库管任务。")
elif candidates:
    print("未修改数据库。确认后使用 PLDP_APPLY=1 再执行一次。")
