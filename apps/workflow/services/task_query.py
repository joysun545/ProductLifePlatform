import hashlib
import re
from urllib.parse import parse_qs, urlsplit

from workflow.models import UserTask


FACTORY_RETURN_TYPE_PREFIX = "FACTORY_RETURN_"
QUALITY_RECALL_TYPE_PREFIX = "QUALITY_RECALL_"
GOODS_TRANSFER_TYPE_PREFIX = "GOODS_TRANSFER_"
# A grouped inbox card normally opens the newest unread update.  Some business
# flows also place a real operation and read-only progress notices in the same
# group.  In those flows the operation must win the card entry so the user is
# taken to the node they can actually complete.
ACTION_PREFERRED_GROUP_KINDS = frozenset({"QUALITY_RECALL", "ORDER_PLAN", "GOODS_TRANSFER"})
# A grouped V2 order plan is one inbox card even though it deliberately keeps
# every auditable child UserTask.  The card-level counters below are used by
# the tab badge and the task-list badge, while ``child_*`` counters remain
# available to the detail view for the real underlying task count.
CARD_COUNTED_GROUP_KINDS = frozenset({"QUALITY_RECALL", "ORDER_PLAN", "GOODS_TRANSFER"})
SUPPLIER_COLLABORATION_TYPES = frozenset(
    {
        # The first two are the verified legacy supplier-confirmation flow.
        "ORDER_PLAN_SUPPLIER_PREPARE",
        "ORDER_PLAN_SUPPLIER_CONFIRMED",
        # V2 sends an independent material-delivery task.  It uses the same
        # immutable responsibility scope, but its link opens the V2 detail
        # rather than the legacy list.
        "ORDER_PLAN_SUPPLIER_DELIVERY",
    }
)
SUPPLIER_ACTION_TYPES = frozenset(
    {
        "ORDER_PLAN_SUPPLIER_PREPARE",
        "ORDER_PLAN_SUPPLIER_DELIVERY",
    }
)


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
    if workitem.type.startswith(QUALITY_RECALL_TYPE_PREFIX):
        campaign_id = payload.get("recall_campaign_id") or workitem.biz_id
        if campaign_id:
            return _append_query(
                "/pages/quality_trace/recall_detail/recall_detail",
                {
                    "campaign_id": campaign_id,
                    "recall_device_id": payload.get("recall_device_id"),
                    "focus_stage": payload.get("stage"),
                },
            )
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
    transfer_id = _goods_transfer_id(workitem)
    if transfer_id:
        page = (
            "goods_transfer_accounts"
            if workitem.type.startswith("GOODS_TRANSFER_ACCOUNT_")
            else (
                "goods_transfer_settlement"
                if workitem.type in {
                    "GOODS_TRANSFER_DOCUMENTS_UPLOAD",
                    "GOODS_TRANSFER_PAYMENT_UPLOAD",
                    "GOODS_TRANSFER_PAYMENT_CONFIRM",
                }
                else "goods_transfer_detail"
            )
        )
        return f"/pages/stock/{page}/{page}?transfer_id={transfer_id}"
    if workitem.type == "ORDER_PLAN_MATCHING" and payload.get("order_plan_id"):
        return (
            "/pages/sales/matching_confirm/matching_confirm"
            f"?order_plan_id={payload['order_plan_id']}"
        )
    return ""


def _collaboration_data(task):
    workitem = task.workitem
    if workitem.type not in SUPPLIER_COLLABORATION_TYPES:
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
            workitem.type in SUPPLIER_ACTION_TYPES
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

    A grouped inbox card is only a query projection. Child rows remain the
    actual auditable UserTask records. After-sales groups are acknowledged as
    one service order when opened; other domains retain per-child behavior.
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


def _positive_id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    raw = str(value).strip()
    if not raw.isascii() or not raw.isdigit() or len(raw) > 19:
        return None
    number = int(raw)
    return number if 0 < number <= 9223372036854775807 else None


def _goods_transfer_id(workitem):
    """Resolve one immutable transfer id, never a counterparty/account id.

    Current publishers all carry goods_transfer_id. The other anchored
    references support older rows without rewriting their payload/history.
    Conflicting concrete ids stay separate for review instead of choosing a
    shipment arbitrarily. Neither merchant names nor account ids are a key.
    Restrict this to goods-flow types: factory-return processing and order
    plan tasks have their own existing grouping semantics.
    """

    if not workitem.type.startswith(GOODS_TRANSFER_TYPE_PREFIX):
        return None
    payload = workitem.payload if isinstance(workitem.payload, dict) else {}
    references = set()
    transfer_id = _positive_id(payload.get("goods_transfer_id"))
    if transfer_id:
        references.add(transfer_id)
    match = re.fullmatch(
        r"(?:apps\.)?(?:lifecycle\.)?GoodsTransfer:([0-9]+)",
        workitem.biz_ref or "",
    )
    if match:
        transfer_id = _positive_id(match.group(1))
        if transfer_id:
            references.add(transfer_id)
    try:
        link = urlsplit(workitem.link or "")
        if re.fullmatch(
            r"/pages/stock/(goods_transfer_detail|goods_transfer_settlement|goods_transfer_accounts)/\1",
            link.path,
        ):
            values = parse_qs(link.query).get("transfer_id", [])
            if len(values) == 1:
                transfer_id = _positive_id(values[0])
                if transfer_id:
                    references.add(transfer_id)
    except ValueError:
        pass
    match = re.match(
        r"^lifecycle:goods-transfer:([0-9]+):", workitem.uniq_key or ""
    )
    if match:
        transfer_id = _positive_id(match.group(1))
        if transfer_id:
            references.add(transfer_id)
    if len(references) > 1:
        workitem._inbox_goods_transfer_conflict = True
        return None
    if references:
        return references.pop()
    if getattr(workitem, "_inbox_goods_transfer_conflict", False):
        return None
    return getattr(workitem, "_inbox_goods_transfer_id", None)


def _prime_goods_transfer_scopes(tasks):
    """Batch-resolve legacy ledger/flow references for one account's inbox.

    The usual path (explicit transfer id, biz_ref or detail link) issues no
    queries. An unresolvable historical row remains a separate task rather
    than accidentally merging two shipments to the same merchant.
    """

    unresolved = [
        task.workitem for task in tasks
        if task.workitem.type.startswith(GOODS_TRANSFER_TYPE_PREFIX)
        and not _goods_transfer_id(task.workitem)
        and not getattr(task.workitem, "_inbox_goods_transfer_conflict", False)
    ]
    if not unresolved:
        return
    from lifecycle.models import (
        GoodsTransfer, GoodsTransferAccountCase, GoodsTransferAccountPayment,
        GoodsTransferPayment, GoodsTransferSettlement,
    )
    flow_refs = {}
    ledger_refs = {}
    for workitem in unresolved:
        payload = workitem.payload if isinstance(workitem.payload, dict) else {}
        flow_no = payload.get("flow_no") or workitem.biz_id
        if isinstance(flow_no, str) and flow_no and len(flow_no) <= 64:
            flow_refs[workitem.pk] = flow_no
        account_flow = workitem.type.startswith("GOODS_TRANSFER_ACCOUNT_")
        for field, kind in (
            ("account_case_id", "case"),
            ("settlement_id", "settlement"),
            ("payment_id", "account_payment" if account_flow else "payment"),
        ):
            ref_id = _positive_id(payload.get(field))
            if ref_id:
                ledger_refs.setdefault(kind, {})[workitem.pk] = ref_id
    resolved = {}
    for kind, model, transfer_field in (
        ("case", GoodsTransferAccountCase, "transfer_id"),
        ("settlement", GoodsTransferSettlement, "transfer_id"),
        ("payment", GoodsTransferPayment, "settlement__transfer_id"),
        ("account_payment", GoodsTransferAccountPayment, "account_case__transfer_id"),
    ):
        references = ledger_refs.get(kind, {})
        if not references:
            continue
        mapping = dict(model.objects.filter(pk__in=set(references.values())).values_list("pk", transfer_field))
        for work_id, ref_id in references.items():
            if ref_id in mapping:
                resolved.setdefault(work_id, set()).add(mapping[ref_id])
    if flow_refs:
        mapping = dict(GoodsTransfer.objects.filter(flow_no__in=set(flow_refs.values())).values_list("flow_no", "pk"))
        for work_id, flow_no in flow_refs.items():
            if flow_no in mapping:
                resolved.setdefault(work_id, set()).add(mapping[flow_no])
    for workitem in unresolved:
        references = resolved.get(workitem.pk, set())
        workitem._inbox_goods_transfer_conflict = len(references) > 1
        workitem._inbox_goods_transfer_id = next(iter(references)) if len(references) == 1 else None


def _task_group_scope(task):
    workitem = task.workitem
    payload = workitem.payload if isinstance(workitem.payload, dict) else {}

    # Every auditable recall update remains a real UserTask, while one account
    # sees only one inbox card for the whole recall campaign. This also groups
    # historical rows whose explicit key used to contain an organization id.
    if workitem.type.startswith(QUALITY_RECALL_TYPE_PREFIX) and workitem.biz_id:
        return f"quality-recall:campaign:{workitem.biz_id}"

    transfer_id = _goods_transfer_id(workitem)
    if transfer_id:
        return f"goods-transfer:transfer:{transfer_id}"
    if getattr(workitem, "_inbox_goods_transfer_conflict", False):
        return f"task:{task.pk}"

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


def _group_metadata(scope, tasks):
    """Return small display hints without changing any underlying UserTask."""

    representative = tasks[0]
    payload = (
        representative.workitem.payload
        if isinstance(representative.workitem.payload, dict)
        else {}
    )
    is_aftersales = not scope.startswith("goods-transfer:transfer:") and (
        scope.startswith("explicit:aftersales:")
        or payload.get("inbox_group_kind") == "AFTERSALES"
    )
    is_quality_recall = scope.startswith("quality-recall:campaign:")
    if is_aftersales:
        return {
            "group_kind": "AFTERSALES",
            "group_label": "售后工单",
            "is_workorder": True,
        }
    if is_quality_recall:
        return {
            "group_kind": "QUALITY_RECALL",
            "group_label": "召回任务",
            "is_workorder": False,
        }
    if scope.startswith("goods-transfer:transfer:"):
        transfer_id = int(scope.rsplit(":", 1)[1])
        flow_no = next((
            (task.workitem.payload or {}).get("flow_no") or task.workitem.biz_id
            for task in tasks
            if isinstance(task.workitem.payload, dict)
            and ((task.workitem.payload or {}).get("flow_no") or task.workitem.biz_id)
        ), "")
        return {
            "group_kind": "GOODS_TRANSFER",
            "group_label": f"商品流转：{flow_no}" if flow_no else f"商品流转单：{transfer_id}",
            "is_workorder": False,
            "single_business_card": True,
            "goods_transfer_id": transfer_id,
            "flow_no": flow_no,
        }
    return {
        "group_kind": payload.get("inbox_group_kind") or "",
        "group_label": payload.get("inbox_group_label") or "同一业务任务",
        "is_workorder": False,
    }


def _uses_card_level_badge(scope, tasks):
    """Whether a grouped inbox projection contributes one badge unit.

    Recall has always used campaign-level badges.  Material-stock V2 order
    plans follow the same rule: a plan can have several independent auditable
    tasks, but it must present one task-card badge instead of inflating the
    user's pending count.  Inspect all children so a historical row lacking
    metadata cannot accidentally make a correctly grouped V2 card regress.
    """

    if scope.startswith(("quality-recall:campaign:", "goods-transfer:transfer:")):
        return True
    for task in tasks:
        payload = (
            task.workitem.payload
            if isinstance(task.workitem.payload, dict)
            else {}
        )
        if payload.get("inbox_group_kind") == "ORDER_PLAN":
            return True
    return False


def _compose_group_title(tasks, *, prefer_action=False):
    unread = [task for task in tasks if task.state == "NEW"]
    pending_action = [
        task for task in tasks
        if task.need_action and task.state != "DONE"
    ]
    candidates = (pending_action or unread or tasks) if prefer_action else (unread or pending_action or tasks)

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


def _entry_task(tasks, *, prefer_action=False):
    """Select the one real inbox row opened by an aggregated card.

    Aggregation must not invent a parent UserTask or acknowledge every child.
    Prefer the newest unread child, then the newest pending action, and finally
    the newest child.  Prefer a row with a usable business link within each
    priority bucket so the card can enter the domain detail directly.
    """

    unread = [task for task in tasks if task.state == "NEW"]
    pending_action = [
        task for task in tasks
        if task.need_action and task.state != "DONE"
    ]
    # A recall card can contain both knowledge updates and an executable
    # node. Enter the executable node when one exists so the direct detail
    # jump focuses the operation that currently needs this account.
    buckets = (
        (pending_action, unread, list(tasks))
        if prefer_action else (unread, pending_action, list(tasks))
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
    metadata = _group_metadata(scope, tasks)
    entry_task = _entry_task(
        tasks,
        prefer_action=(
            metadata["group_kind"] in ACTION_PREFERRED_GROUP_KINDS
        ),
    )
    child_items = [_serialize_task(task) for task in tasks]
    entry_item = _serialize_task(entry_task)
    entry_link = _resolved_link(entry_task)
    child_unread_count = sum(task.state == "NEW" for task in tasks)
    child_action_pending_count = sum(
        task.need_action and task.state != "DONE" for task in tasks
    )
    uses_card_level_badge = _uses_card_level_badge(scope, tasks)
    unread_count = (
        int(bool(child_unread_count)) if uses_card_level_badge
        else child_unread_count
    )
    action_pending_count = (
        int(bool(child_action_pending_count)) if uses_card_level_badge
        else child_action_pending_count
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
        "card_id": _group_id(scope),
        "entry_task_id": entry_task.pk,
        "entry_task_type": entry_task.workitem.type,
        "grouped": len(tasks) > 1,
        "child_count": len(tasks),
        "unread_count": unread_count,
        "action_pending_count": action_pending_count,
        "child_unread_count": child_unread_count,
        "child_action_pending_count": child_action_pending_count,
        "completed_count": completed_count,
        "state": state,
        "need_action": bool(action_pending_count),
        # An after-sales case is one service order, not a bundle of unrelated
        # jobs.  Historical duplicate notifications therefore present the
        # newest service status as one clean card while their child rows stay
        # auditable in the group/detail view.
        "title": (
            _task_title(representative)
            if metadata["is_workorder"] or metadata["group_kind"] == "QUALITY_RECALL"
            else _compose_group_title(tasks, prefer_action=metadata["group_kind"] in ACTION_PREFERRED_GROUP_KINDS)
        ),
        "summary": (
            _task_summary(representative)
            if metadata["is_workorder"] or metadata["group_kind"] == "QUALITY_RECALL"
            else (
                f"共{len(tasks)}项任务｜{unread_count}项未读｜"
                f"{action_pending_count}项待处理"
                if len(tasks) > 1 else _task_summary(representative)
            )
        ),
        "latest_summary": _task_summary(representative),
        **metadata,
        # The projected parent opens one real entry row. The open service uses
        # its after-sales group payload to acknowledge all knowledge updates
        # while leaving action rows pending until their domain operation.
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
    if metadata["group_kind"] == "GOODS_TRANSFER":
        # One transfer stays one card through documents, physical receipt and
        # independent payment rounds. Keep the actionable stage visible even
        # if a newer knowledge update arrived; without actions show the latest
        # stage instead of concatenating already-completed historical titles.
        group["title"] = (
            _compose_group_title(tasks, prefer_action=True)
            if child_action_pending_count else _task_title(representative)
        )
        group["summary"] = _task_summary(entry_task)
        if entry_link:
            group["link"] = _append_query(entry_link, {
                "task_group_id": _group_id(scope),
                "task_id": entry_task.pk,
                "from_task": 1,
            })
    if metadata["group_kind"] == "QUALITY_RECALL":
        _recall_display(group, entry_task)
    context = getattr(entry_task, "_recall_scope_context", None)
    if context:
        campaign, viewer, rows = context
        if not viewer.full_campaign(campaign):
            # Older notifications can embed a whole-batch count in any display
            # field. Project every exposed child, not only the parent title.
            group["latest_summary"] = group["summary"]
            allowed = {row.pk for row in rows}
            for child, real_task in zip(child_items, tasks):
                payload = real_task.workitem.payload if isinstance(real_task.workitem.payload, dict) else {}
                focus = payload.get("recall_device_id")
                row = next((row for row in rows if str(row.pk) == str(focus)), None)
                child["title"] = (f"召回节点记录：{row.sn_snapshot}" if row else group["title"])
                child["summary"] = group["summary"]
                child["link"] = (f"/pages/quality_trace/recall_detail/recall_detail?campaign_id={campaign.pk}"
                                 if rows or viewer.observer(campaign) else "")
                if row:
                    child["link"] += f"&recall_device_id={row.pk}"
                child["done_reason"] = ""  # Historical free text may quote other device counts.
            group["done_reason"] = ""
            group["link"] = (f"/pages/quality_trace/recall_detail/recall_detail?campaign_id={campaign.pk}"
                             if rows or viewer.observer(campaign) else "")
            focus = (entry_task.workitem.payload or {}).get("recall_device_id")
            if str(focus) in {str(value) for value in allowed}:
                group["link"] += f"&recall_device_id={focus}"
    if include_children:
        group["items"] = child_items
    return group


def _recall_display(group, task):
    # A DONE inbox row closes one stage, not the entire recall. Read current
    # chain facts without rewriting historical tasks, recipients or badges.
    from account.models import User
    from quality_trace.services.recall_access_service import (
        RecallViewer, recall_campaign_queryset, visible_rows,
    )
    from quality_trace.services.recall_display_service import campaign_context
    context_data = getattr(task, "_recall_scope_context", None)
    if not context_data:
        return
    campaign, viewer, rows = context_data
    if not rows and not viewer.metadata_access(campaign):
        group.update(title=f"召回历史任务：{campaign.recall_no}",
                     summary="当前组织或岗位不在此召回链路，请核对登录身份。",
                     display_state="历史任务", link="", need_action=False, action_pending_count=0)
        return
    context = campaign_context(campaign, rows, viewer)
    group["title"] = (f"召回知晓：{campaign.recall_no}" if viewer.observer(campaign)
                      else f"召回任务：{campaign.recall_no}（{len(rows)}台）")
    group["summary"] = context["scope_summary"] + "。" + context["stage_summary"]
    group["recall_context"] = context
    if viewer.observer(campaign):
        group.update(display_state="待知晓" if group["unread_count"] else "已知晓",
                     need_action=False, action_pending_count=0, child_action_pending_count=0)
        return
    if campaign.status == "COMPLETED":
        state = "召回已完成"
    elif campaign.status != "ACTIVE":
        state = campaign.get_status_display()
    elif context["actionable_device_count"]:
        state = "本节点可办理"
    elif group["action_pending_count"]:
        state = "本阶段待处理"
    else:
        state = "跟踪中"
    group["display_state"] = state


def _task_groups(openid):
    tasks = list(
        UserTask.objects
        .select_related("workitem")
        .filter(to_openid=openid, badge_group="MY")
        .order_by("-cursor_time", "-id")
    )
    _prime_goods_transfer_scopes(tasks)
    grouped = {}
    for task in tasks:
        scope = _task_group_scope(task)
        grouped.setdefault(scope, []).append(task)
    from account.models import User
    from quality_trace.services.recall_access_service import RecallViewer, recall_campaign_queryset, visible_rows
    user = User.objects.filter(wx_openid=openid, is_active=True).first()
    if user:
        viewer = RecallViewer(user)
        for scope, group_tasks in grouped.items():
            if not scope.startswith("quality-recall:campaign:"):
                continue
            campaign_id = str(group_tasks[0].workitem.biz_id or "")
            campaign = (recall_campaign_queryset().filter(pk=int(campaign_id)).first()
                        if campaign_id.isdigit() else None)
            if not campaign:
                continue
            rows = visible_rows(campaign, viewer)
            for task in group_tasks:
                task._recall_scope_context = (campaign, viewer, rows)
                if viewer.observer(campaign) or (not rows and not viewer.full_campaign(campaign)):
                    task.need_action = False  # Query projection, not a database mutation.
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

    unread_count = 0
    action_pending_count = 0
    attention_count = 0
    for scope, tasks in _task_groups(openid).items():
        group_unread = sum(task.state == "NEW" for task in tasks)
        group_action = sum(
            task.need_action and task.state != "DONE" for task in tasks
        )
        if _uses_card_level_badge(scope, tasks):
            unread_count += int(bool(group_unread))
            action_pending_count += int(bool(group_action))
            attention_count += int(bool(group_unread or group_action))
            continue
        unread_count += group_unread
        action_pending_count += group_action
        attention_count += sum(
            task.state == "NEW"
            or (task.need_action and task.state != "DONE")
            for task in tasks
        )

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


    unread_count = sum(
        int(any(task.state == "NEW" for task in tasks))
        if _uses_card_level_badge(scope, tasks)
        else sum(task.state == "NEW" for task in tasks)
        for scope, tasks in groups.items()
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
