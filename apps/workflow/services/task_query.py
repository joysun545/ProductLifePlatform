from django.db.models import Q

from workflow.models import UserTask


def _append_query(link, values):
    separator = "&" if "?" in link else "?"
    query = "&".join(
        f"{key}={value}"
        for key, value in values.items()
        if value not in (None, "")
    )
    return f"{link}{separator}{query}" if query else link


def _resolved_link(task):
    """为部署前已经创建且link为空的业务任务补出目标页。"""

    workitem = task.workitem
    payload = workitem.payload if isinstance(workitem.payload, dict) else {}
    if workitem.type == "ORDER_PLAN_SUPPLIER_PREPARE":
        return _append_query(
            "/pages/sales/order_plan_list/order_plan_list",
            {
                "focus_plan_id": payload.get("order_plan_id"),
                "supplier_id": payload.get("supplier_id"),
                "task_id": task.pk,
                "from_task": 1,
            },
        )
    if workitem.type == "ORDER_PLAN_SUPPLIER_CONFIRMED":
        return _append_query(
            "/pages/sales/order_plan_list/order_plan_list",
            {
                "focus_plan_id": payload.get("order_plan_id"),
                "supplier_id": payload.get("supplier_id"),
                "task_id": task.pk,
                "from_task": 1,
                "confirmation_notice": 1,
            },
        )
    if workitem.type == "ORDER_PLAN_STOCK_OUTBOUND":
        return _append_query(
            "/pages/sales/order_plan_list/order_plan_list",
            {
                "focus_plan_id": payload.get("order_plan_id"),
                "task_id": task.pk,
                "from_task": 1,
            },
        )
    if workitem.link:
        return workitem.link
    if workitem.type == "ORDER_PLAN_MATCHING" and payload.get("order_plan_id"):
        return (
            "/pages/sales/matching_confirm/matching_confirm"
            f"?order_plan_id={payload['order_plan_id']}"
        )
    return ""


def _collaboration_data(task):
    workitem = task.workitem
    if workitem.type not in {
        "ORDER_PLAN_SUPPLIER_PREPARE",
        "ORDER_PLAN_SUPPLIER_CONFIRMED",
    }:
        return None
    payload = workitem.payload if isinstance(workitem.payload, dict) else {}
    scopes = payload.get("recipient_scopes", {})
    scope = scopes.get(task.to_openid, {}) if isinstance(scopes, dict) else {}
    material_ids = {
        int(value) for value in scope.get("material_ids", [])
    }
    if (
        workitem.type == "ORDER_PLAN_SUPPLIER_CONFIRMED"
        and not material_ids
    ):
        material_ids = {
            int(row["material_id"])
            for row in payload.get("materials", [])
            if isinstance(row, dict) and row.get("material_id")
        }
    material_count = sum(
        1
        for row in payload.get("materials", [])
        if isinstance(row, dict)
        and row.get("material_id")
        and int(row["material_id"]) in material_ids
    )
    actors = payload.get("processed_actors", [])
    if not isinstance(actors, list):
        actors = []
    if workitem.status == "DONE":
        state = "CONFIRMED"
        state_name = "已处理"
    elif task.state == "DONE":
        state = "PROCESSED"
        state_name = "本人已处理，等待协同角色"
    else:
        state = "PENDING"
        state_name = "需要处理"
    return {
        "state": state,
        "state_name": state_name,
        "can_confirm": (
            workitem.type == "ORDER_PLAN_SUPPLIER_PREPARE"
            and workitem.status == "OPEN"
            and task.state != "DONE"
            and task.need_action
        ),
        "completion_policy": payload.get("completion_policy", ""),
        "material_count": material_count,
        "scope_material_ids": sorted(material_ids),
        "processed_actors": actors,
        "processed_by_text": "、".join(
            f"{actor.get('name', '协同账号')}已处理" for actor in actors
        ),
        "waiting_for_owner": bool(payload.get("waiting_for_owner")),
        "order_plan_id": payload.get("order_plan_id"),
        "supplier_id": payload.get("supplier_id"),
    }


def get_task_summary(openid):
    """
    获取用户任务汇总

    用于：
    - TabBar 红点
    - 我的任务数量

    return:
    {
        "unread_count": int,
        "action_pending_count": int,
        "attention_count": int,
    }
    """

    qs = UserTask.objects.filter(
        to_openid=openid,
        badge_group="MY"
    )

    unread_count = qs.filter(
        state="NEW"
    ).count()

    action_pending_count = qs.filter(
        need_action=True
    ).exclude(
        state="DONE"
    ).count()

    # 徽标表示“需要关注”，既包含未读通知，也包含已经打开但尚未完成的
    # 操作任务。使用 OR 查询保证“未读且待处理”的同一任务只计一次。
    attention_count = qs.filter(
        Q(state="NEW") |
        (Q(need_action=True) & ~Q(state="DONE"))
    ).count()

    return {
        "unread_count": unread_count,
        "action_pending_count": action_pending_count,
        "attention_count": attention_count,
    }



def get_task_list(openid, limit=50):
    """
    获取用户任务列表

    用于：
    小程序任务页面
    """

    try:
        limit = int(limit)
    except Exception:
        limit = 50


    if limit <= 0 or limit > 200:
        limit = 50


    tasks = (
        UserTask.objects
        .select_related("workitem")
        .filter(
            to_openid=openid,
            badge_group="MY"
        )
        .order_by(
            "-cursor_time",
            "-id"
        )[:limit]
    )


    items = []


    for task in tasks:

        workitem = task.workitem


        items.append({

            "id": task.id,

            "state": task.state,

            "need_action": bool(
                task.need_action
            ),

            "cursor_time":
                task.cursor_time.strftime(
                    "%Y-%m-%d %H:%M"
                ),

            "done_reason":
                task.done_reason or "",


            "workitem_id":
                workitem.id,


            "domain":
                workitem.domain,


            "type":
                workitem.type,


            "biz_id":
                workitem.biz_id,


            "title":
                task.display_title_snapshot
                or workitem.title
                or workitem.type,


            "summary":
                task.display_subtitle_snapshot
                or workitem.summary
                or "",


            "link":
                _resolved_link(task),


            "action_mode":
                workitem.action_mode,


            "workitem_status":
                workitem.status,

            "collaboration":
                _collaboration_data(task),

        })


    unread_count = (
        UserTask.objects
        .filter(
            to_openid=openid,
            badge_group="MY",
            state="NEW"
        )
        .count()
    )


    return {

        "unread_count": unread_count,

        "items": items,

    }
