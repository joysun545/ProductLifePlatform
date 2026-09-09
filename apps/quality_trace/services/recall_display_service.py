"""Read-only recall stage projection shared by the inbox and recall pages."""
from collections import Counter


def latest_transport(row):
    return max((leg for leg in row.transport_legs.all() if leg.status != "CANCELLED"),
               key=lambda leg: leg.sequence_no, default=None)


def row_stage(row, viewer):
    org_id, role = viewer.organization_id, viewer.role
    handover = getattr(row, "customer_handover", None)
    disposition = getattr(row, "post_disposition", None)
    isolation = getattr(row, "isolation_receipt", None)
    leg = latest_transport(row)
    actionable = False
    if row.status in {"UNREACHABLE", "EXEMPTED"}:
        code, label = row.status, row.get_status_display()
        hint = "请查看本台设备的例外处理记录。"
    elif disposition:
        code = disposition.execution_stage
        label = disposition.get_execution_stage_display()
        if disposition.status == "COMPLETED":
            label = disposition.get_stock_result_display()
            hint = "本台处置已完成，可查看处置记录。"
        elif code == "WAITING_SUPPLIER":
            confirmation = next((item for item in disposition.supplier_confirmations.all()
                                 if item.supplier_id == org_id), None)
            actionable = bool(confirmation and not confirmation.confirmed_at and role == "supplier_owner")
            hint = ("请确认本供应商的换件协同。" if actionable else
                    "本供应商已确认，等待其余供应商确认。" if confirmation and confirmation.confirmed_at else
                    "请与本组织负责人协同，由负责人确认换件；您可查看进度。" if viewer.kind == "SUPPLIER" else
                    "等待相关供应商负责人确认换件。")
        else:
            executor = {"WAITING_MATCHING": "factory_matching",
                        "WAITING_PRODUCTION": "factory_production",
                        "WAITING_PRODUCTION_COMPLETION": "factory_production",
                        "WAITING_CHIEF_CONFIRMATION": "factory_chief_engineer"}.get(code)
            actionable = viewer.factory(row.campaign) and role == executor
            hint = "请在下方完成本台处置操作。" if actionable else label + "。"
    elif isolation:
        code, label = "FACTORY_INVENTORY_POSTED", "已完成召回专用入库记账"
        hint = "等待厂家完成整批入库确认及召回后处置。"
    elif leg and leg.status == "IN_TRANSIT":
        code, label = "RETURN_IN_TRANSIT", f"正在运往{leg.to_organization.name}"
        actionable = org_id == leg.to_organization_id and (
            role == "factory_logistics" if leg.is_factory_destination else viewer.kind == "MERCHANT")
        hint = (f"{leg.from_organization.name}已发出，实物到货后请按批次确认接收。" if actionable else
                f"已发出至{leg.to_organization.name}，等待其确认接收。")
    elif leg and leg.status == "RECEIVED":
        if leg.is_factory_destination:
            code, label = "FACTORY_RECEIVED_PENDING_POSTING", "厂家已收货，待销售助理入库记账"
            actionable = viewer.factory(row.campaign) and role == "factory_sales_assistant"
            hint = "请核对设备并批量完成隔离入账。" if actionable else "等待厂家销售助理完成召回专用入库记账。"
        else:
            code, label = "UPSTREAM_MERCHANT_RECEIVED", f"{leg.to_organization.name}已收货，待继续退回"
            actionable = viewer.kind == "MERCHANT" and org_id == leg.to_organization_id
            hint = "本组织已接收，可与本组织其他待退设备一起退回上级。" if actionable else f"等待{leg.to_organization.name}继续退回上级。"
    elif leg and leg.status == "PREPARING" and (not handover or handover.status == "MERCHANT_RECEIVED"):
        code, label = "RETURN_LEG_CREATED", "已建立退回运输，待发出"
        actionable = viewer.kind == "MERCHANT" and org_id == leg.from_organization_id
        hint = "请核对批量清单，填写物流并确认发出。" if actionable else f"等待{leg.from_organization.name}发出设备。"
    elif handover:
        code, label = handover.status, handover.get_status_display()
        customer = viewer.customer_actor(row)
        merchant = viewer.kind == "MERCHANT" and org_id == handover.merchant_organization_id
        merchant_name = handover.merchant_organization.name
        if code == "MERCHANT_RECEIVED":
            label = f"{merchant_name}已接管，待退回上级"
            actionable = merchant
            hint = "已接管用户设备，可在批量清单中与本组织待退库存一起退回。" if merchant else f"设备已交给{merchant_name}，可跟踪后续返厂进度。"
        elif code == "PICKUP_REQUESTED":
            actionable = viewer.factory(row.campaign) and role in {"factory_chief_engineer", "factory_logistics"}
            hint = "请核对取件信息并受理取件。" if actionable else "等待厂家总工程师或物流经理受理取件。"
        elif code == "PICKUP_ACCEPTED":
            actionable = customer
            hint = "设备交给取件物流后，请上传凭证并确认发出。" if customer else "厂家已安排取件，等待用户交付物流并提交凭证。"
        elif code == "METHOD_SELECTED" and handover.delivery_method == "MERCHANT_HANDOVER":
            actionable = customer or merchant
            hint = (f"请向{merchant_name}出示临时召回流转码，由商家扫码接管。" if customer else
                    "收到用户设备后，请扫描用户出示的临时召回流转码，核验并接管设备。" if merchant else
                    f"等待用户将设备交给{merchant_name}。")
        elif code == "METHOD_SELECTED" and handover.delivery_method == "CUSTOMER_DIRECT_SHIP":
            actionable = customer
            hint = "请填写直寄物流单号，上传凭证并确认发出。" if customer else "用户选择直寄厂家，等待用户提交发货物流。"
        elif code in {"ACKNOWLEDGED", "PICKUP_SCHEDULED"}:
            actionable = merchant
            hint = ("请预约取回设备。" if code == "ACKNOWLEDGED" else "请核验设备并上传凭证，确认接管。") if merchant else f"等待{merchant_name}完成取回交接。"
        elif code == "CANCELLED":
            hint = "本次用户交接已取消，请查看召回批次状态。"
        else:
            actionable = customer
            hint = "请与关联司机协同，为本台设备选择交付方式。" if customer else "等待设备所属用户负责人或关联司机选择交付方式。"
    else:
        groups = (row.responsibility_snapshot or {}).get("groups", [])
        own = next((group for group in groups if group.get("scope_section") == "own_inventory"), {})
        owner_name = own.get("organization_name") or "责任组织"
        code, label = "INVENTORY_PENDING_RETURN", f"{owner_name}待退回"
        actionable = viewer.kind == "MERCHANT" and str(own.get("organization_id")) == str(org_id)
        hint = "请核对本组织库存，在批量清单中一起退回。" if actionable else f"等待{owner_name}退回设备。"
    if row.campaign.status != "ACTIVE":
        actionable = False
        hint = {"DRAFT": "召回尚未发布。", "COMPLETED": "本批召回已完成，可查看历史记录。",
                "SUPERSEDED": "本批召回已被替代，请查看最新召回批次。",
                "CANCELLED": "本批召回已取消。"}.get(row.campaign.status, "当前批次不可继续操作。")
    return {"code": code, "label": label, "next_step": hint, "actionable": bool(actionable)}


def campaign_context(campaign, rows, viewer):
    if viewer.observer(campaign):
        return {"scope_label": "召回知晓", "device_count": None, "origin_counts": {},
                "scope_summary": "本岗位仅接收召回知晓通知，无需办理召回操作。",
                "actionable_device_count": 0, "stage_summary": "", "stages": []}
    scope_label = {"OWNER": "本岗位负责设备", "CUSTOMER": "我的召回设备",
                   "MERCHANT": "本组织链路设备", "SUPPLIER": "本供应商相关设备"}.get(viewer.kind, "本账号相关设备")
    if viewer.full_campaign(campaign):
        scope_label = "全部召回设备"
    elif viewer.role == "factory_sales":
        scope_label = "本人负责渠道及下级设备"
    counts = Counter(own_inventory=0, downstream_inventory=0, activated_user=0, other=0)
    stages = Counter()
    action_count = 0
    for row in rows:
        groups = (row.responsibility_snapshot or {}).get("groups", [])
        mode = (row.responsibility_snapshot or {}).get("mode")
        if mode == "ACTIVATED_USER" or getattr(row, "customer_handover", None):
            counts["activated_user"] += 1
        elif mode == "MERCHANT_INVENTORY":
            own = any(str(group.get("organization_id")) == str(viewer.organization_id)
                      and group.get("scope_section") == "own_inventory" for group in groups)
            counts["own_inventory" if own else "downstream_inventory"] += 1
        else:
            counts["other"] += 1
        stage = row_stage(row, viewer)
        stages[stage["next_step"]] += 1
        action_count += int(stage["actionable"])
    if viewer.kind == "CUSTOMER":
        description = f"{'关联车辆' if viewer.role == 'driver' else '本用户名下'}召回设备 {len(rows)} 台"
    elif viewer.kind == "MERCHANT":
        description = (f"本链路 {len(rows)} 台；召回发起时：本组织库存 {counts['own_inventory']} 台，"
                       f"下级库存 {counts['downstream_inventory']} 台，用户已激活 {counts['activated_user']} 台")
        if counts["other"]:
            description += f"，其他 {counts['other']} 台"
    else:
        description = f"{scope_label} {len(rows)} 台"
    return {"scope_label": scope_label, "device_count": len(rows), "origin_counts": dict(counts),
            "scope_summary": description, "actionable_device_count": action_count,
            "stage_summary": "；".join(f"{count} 台：{hint}" for hint, count in stages.items()),
            "stages": [{"text": hint, "count": count} for hint, count in stages.items()]}


def campaign_text(campaign, viewer):
    """Unstructured global titles/reasons can contain totals; don't reuse them
    in a scoped view. Keep the original stored text intact for the full view.
    """
    if viewer.full_campaign(campaign):
        return {"title": campaign.title, "reason": campaign.reason,
                "supersession_reason": campaign.supersession_reason}
    if viewer.observer(campaign):
        return {"title": "召回知晓通知", "reason": "请知晓本厂家召回事项，无需操作。",
                "supersession_reason": ""}
    return {"title": "本链路设备召回", "reason": "请按本链路设备清单查看召回进度和交付要求。",
            "supersession_reason": ""}


def decorate_batch_flow(flow, context, viewer):
    flow = dict(flow)
    role = viewer.role
    if role == "factory_logistics":
        flow["guidance"] = "实物到厂后，按物流到货批次核验并批量确认接收。随后由厂家销售助理隔离入账。"
    elif role == "factory_sales_assistant":
        flow["guidance"] = "厂家物流经理确认收货后，可批量隔离入账；整批核对完成后，再确认整批入库。"
    else:
        flow["guidance"] = "商家负责人、销售经理和库管协同办理，同一批由任一岗位提交即可。可将本组织已持有的待退设备一起退回；收到新到货后按批次接收，再继续退回。"
    flow["empty_message"] = context["stage_summary"] or "当前没有本组织可批量办理的设备。"
    return flow
