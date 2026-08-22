import hashlib

from django.db.models import Q

from workflow.models import UserTask


FACTORY_RETURN_TYPE_PREFIX = "FACTORY_RETURN_"


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


def _task_title(task):
    return (
        task.display_title_snapshot
        or task.workitem.title
        or task.workitem.type
    )


def _task_summary(task):
    return (
        task.display_subtitle_snapshot
        or task.workitem.summary
        or ""
    )


def _serialize_task(task):
    """Serialize one real inbox row.

    A grouped inbox card is only a query projection.  Child rows returned by
    this function remain the actual auditable UserTask records and must still
    be opened/acknowledged one at a time.
    """

    workitem = task.workitem
    return {
        "id": task.id,
        "state": task.state,
        "need_action": bool(task.need_action),
        "cursor_time": task.cursor_time.strftime("%Y-%m-%d %H:%M"),
        "done_reason": task.done_reason or "",
        "workitem_id": workitem.id,
        "domain": workitem.domain,
        "type": workitem.type,
        "biz_id": workitem.biz_id,
        "biz_ref": workitem.biz_ref,
        "title": _task_title(task),
        "summary": _task_summary(task),
        "link": _resolved_link(task),
        "action_mode": workitem.action_mode,
        "workitem_status": workitem.status,
        "collaboration": _collaboration_data(task),
    }


def _factory_return_scope(workitem):
    """Return a stable batch scope shared by all factory-return child tasks.

    Production/matching tasks carry ``goods_transfer_id``/``flow_no`` while
    supplier warranty and repair notices have their own immutable biz_ref but
    carry the common ``return_flow_no``.  Prefer the human/audit flow number so
    old and new task kinds can be grouped without rewriting historical rows.
    """

    payload = workitem.payload if isinstance(workitem.payload, dict) else {}
    flow_no = payload.get("return_flow_no") or payload.get("flow_no")
    if flow_no:
        return f"flow:{flow_no}"

    transfer_id = (
        payload.get("goods_transfer_id")
        or payload.get("transfer_id")
        or payload.get("return_transfer_id")
    )
    if transfer_id:
        return f"transfer:{transfer_id}"

    # Most factory-return mother tasks use GoodsTransfer as biz_ref.  Keep this
    # fallback for already-deployed rows whose early payload was incomplete.
    if "GoodsTransfer:" in (workitem.biz_ref or ""):
        return f"ref:{workitem.biz_ref}"
    return ""


def _task_group_scope(task):
    workitem = task.workitem
    payload = workitem.payload if isinstance(workitem.payload, dict) else {}

    explicit_scope = payload.get("inbox_group_key")
    if explicit_scope:
        return f"explicit:{explicit_scope}"

    if workitem.type.startswith(FACTORY_RETURN_TYPE_PREFIX):
        factory_return_scope = _factory_return_scope(workitem)
        if factory_return_scope:
            return f"factory-return:{factory_return_scope}"

    # Do not silently merge unrelated legacy tasks.  Other business domains
    # can opt into aggregation later through payload.inbox_group_key.
    return f"task:{task.pk}"


def _group_id(scope):
    return hashlib.sha256(scope.encode("utf-8")).hexdigest()[:24]


def _compose_group_title(tasks):
    unread = [task for task in tasks if task.state == "NEW"]
    pending_action = [
        task for task in tasks
        if task.need_action and task.state != "DONE"
    ]
    candidates = unread or pending_action or tasks

    titles = []
    for task in candidates:
        title = _task_title(task)
        if title and title not in titles:
            titles.append(title)

    visible = titles[:2]
    title = "；".join(visible)
    # Repeated child titles are still separate tasks (for example five
    # different devices from one return batch), so the ellipsis is based on
    # hidden child rows rather than only on the number of unique strings.
    if len(candidates) > len(visible):
        title = f"{title}……"
    return title or "业务任务"


def _entry_task(tasks):
    """Select the one real inbox row opened by an aggregated card.

    Aggregation must not invent a parent UserTask or acknowledge every child.
    Prefer the newest unread child, then the newest pending action, and finally
    the newest child.  Prefer a row with a usable business link within each
    priority bucket so the card can enter the domain detail directly.
    """

    buckets = (
        [task for task in tasks if task.state == "NEW"],
        [
            task for task in tasks
            if task.need_action and task.state != "DONE"
        ],
        list(tasks),
    )
    for bucket in buckets:
        for task in bucket:
            if _resolved_link(task):
                return task
        if bucket:
            return bucket[0]
    return tasks[0]


def _serialize_group(scope, tasks, *, include_children=False):
    # Query order is newest first and is retained within every child list.
    representative = tasks[0]
    entry_task = _entry_task(tasks)
    child_items = [_serialize_task(task) for task in tasks]
    entry_item = _serialize_task(entry_task)
    entry_link = _resolved_link(entry_task)
    unread_count = sum(task.state == "NEW" for task in tasks)
    action_pending_count = sum(
        task.need_action and task.state != "DONE" for task in tasks
    )
    completed_count = sum(task.state == "DONE" for task in tasks)
    if completed_count == len(tasks):
        state = "DONE"
    elif unread_count:
        state = "NEW"
    else:
        state = "READ"

    group = {
        # Compatibility fields allow existing single-task clients/tests to keep
        # using id/link/collaboration unchanged.
        **entry_item,
        "group_id": _group_id(scope),
        "entry_task_id": entry_task.pk,
        "entry_task_type": entry_task.workitem.type,
        "grouped": len(tasks) > 1,
        "child_count": len(tasks),
        "unread_count": unread_count,
        "action_pending_count": action_pending_count,
        "completed_count": completed_count,
        "state": state,
        "need_action": bool(action_pending_count),
        "title": _compose_group_title(tasks),
        "summary": (
            f"共{len(tasks)}项任务｜{unread_count}项未读｜"
            f"{action_pending_count}项待处理"
            if len(tasks) > 1 else _task_summary(representative)
        ),
        "latest_summary": _task_summary(representative),
        # The projected parent opens exactly one real child, then goes straight
        # to the business detail.  The detail page receives the group id only
        # as display context; it must never bulk-acknowledge group children.
        "link": (
            _append_query(
                entry_link,
                {
                    "task_group_id": _group_id(scope),
                    "task_id": entry_task.pk,
                    "from_task": 1,
                },
            )
            if entry_link and len(tasks) > 1
            else entry_link
        ),
    }
    if include_children:
        group["items"] = child_items
    return group


def _task_groups(openid):
    tasks = list(
        UserTask.objects
        .select_related("workitem")
        .filter(to_openid=openid, badge_group="MY")
        .order_by("-cursor_time", "-id")
    )
    grouped = {}
    for task in tasks:
        scope = _task_group_scope(task)
        grouped.setdefault(scope, []).append(task)
    return grouped


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


    groups = _task_groups(openid)
    items = [
        _serialize_group(scope, tasks)
        for scope, tasks in list(groups.items())[:limit]
    ]


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

        "group_count": len(groups),

    }


def get_task_group(openid, group_id):
    """Return one projected inbox group and all real child UserTasks."""

    group_id = str(group_id or "").strip()
    if not group_id:
        return None
    for scope, tasks in _task_groups(openid).items():
        if _group_id(scope) == group_id:
            return _serialize_group(scope, tasks, include_children=True)
    return None
