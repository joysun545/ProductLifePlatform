from collections import defaultdict, deque
from copy import deepcopy

from django.db import transaction
from django.utils import timezone

from account.models import (
    OrganizationRelation,
    OrganizationRelationType,
    User,
    UserOrganization,
)
from account.services.auth_service import get_active_identity
from lifecycle.models import (
    CustomerDeviceBinding,
    DeviceState,
    DriverVehicleBinding,
    GoodsTransfer,
    GoodsTransferItem,
    OrganizationDevice,
)
from quality_trace.models import (
    QualityRecallCampaign,
    QualityRecallCustomerHandover,
    QualityRecallDevice,
    QualityRecallDeviceEvent,
    QualityTraceEvent,
)
from workflow.models import UserTask, WorkItem
from workflow.services.workflow_service import publish_workitem

from .case_service import RECALL_MANAGER_ROLES, _identity
from .recall_service import QualityRecallError


MERCHANT_TASK_ROLES = (
    "merchant_owner",
    "merchant_manager",
    "merchant_senior_manager",
    "merchant_sales",
    "merchant_stock",
)
FACTORY_TASK_ROLES = (
    "factory_chief_engineer",
    "factory_sales_assistant",
    "factory_logistics",
)
CUSTOMER_TASK_ROLES = ("customer_owner", "driver")

ACTIVE_TRANSFER_STATES = {
    GoodsTransfer.Status.SCANNING,
    GoodsTransfer.Status.PENDING_RECEIPT,
    GoodsTransfer.Status.PENDING_FACTORY_PICKUP,
    GoodsTransfer.Status.PENDING_FACTORY_POSTING,
}


def _recipients(organization_id, role_codes, device_ids=None):
    """Return role recipients, scoping drivers to the affected vehicles only."""
    role_codes = tuple(role_codes)
    permitted_driver_ids = set()
    if "driver" in role_codes and device_ids:
        # Keep this query explicit: CustomerDeviceBinding names the customer
        # organization, while DriverVehicleBinding names the shared person.
        vehicle_ids = CustomerDeviceBinding.objects.filter(
            customer_organization_id=organization_id,
            device_id__in=device_ids,
            status=CustomerDeviceBinding.Status.ACTIVE,
            vehicle_id__isnull=False,
        ).values_list("vehicle_id", flat=True)
        permitted_driver_ids = set(
            DriverVehicleBinding.objects.filter(
                vehicle_id__in=vehicle_ids,
                is_active=True,
            ).values_list("driver_id", flat=True)
        )
    rows = (
        UserOrganization.objects.select_related("user", "role")
        .filter(
            organization_id=organization_id,
            role__code__in=role_codes,
            role__is_active=True,
            user__is_active=True,
            is_active=True,
        )
        .exclude(user__wx_openid__isnull=True)
        .exclude(user__wx_openid="")
        .order_by("role__code", "user_id")
    )
    recipients = [
        {
            "openid": row.user.wx_openid,
            "user_id": row.user_id,
            "identity_id": row.pk,
            "role": row.role.code,
            "name": row.user.wx_nickname or row.user.username,
        }
        for row in rows
        if row.role.code != "driver" or row.user_id in permitted_driver_ids
    ]
    seen = set()
    return [item for item in recipients if not (item["openid"] in seen or seen.add(item["openid"]))]


def _organization_payload(organization):
    return {
        "id": organization.pk,
        "name": organization.name,
        "code": organization.code,
        "type": organization.organization_type.code,
    }


def _device_payload(recall_device):
    qrcode = getattr(recall_device.device, "qrcode", None)
    return {
        "recall_device_id": recall_device.pk,
        "device_id": recall_device.device_id,
        "sn": recall_device.sn_snapshot,
        "lifecycle_code": qrcode.code if qrcode else "",
        "product_model": recall_device.product_model_snapshot,
        "production_batch": recall_device.production_batch_snapshot,
    }


def _distribution_parent_map():
    parent_map = defaultdict(list)
    relations = (
        OrganizationRelation.objects.select_related(
            "source_organization__organization_type",
            "target_organization__organization_type",
        )
        .filter(
            relation_type=OrganizationRelationType.DISTRIBUTION,
            is_active=True,
            source_organization__is_active=True,
            target_organization__is_active=True,
        )
        .order_by("id")
    )
    for relation in relations:
        parent_map[relation.target_organization_id].append(
            relation.source_organization
        )
    return parent_map


def _merchant_chain(anchor, parent_map, factory_id):
    """Return merchant nodes only on an upstream path to this campaign factory."""
    if not anchor or anchor.organization_type.code != "MERCHANT":
        return []
    queue = deque([(anchor, [anchor])])
    successful_paths = []
    while queue:
        organization, path = queue.popleft()
        path_ids = {item.pk for item in path}
        for parent in parent_map.get(organization.pk, []):
            if parent.pk == factory_id:
                successful_paths.append(path)
            elif (
                parent.organization_type.code == "MERCHANT"
                and parent.pk not in path_ids
            ):
                queue.append((parent, [*path, parent]))
    if not successful_paths:
        # The current holder still needs its own task.  Do not expose the
        # device to unrelated upstream organizations when the factory path is
        # incomplete or inconsistent.
        return [anchor]
    merchants = []
    seen = set()
    for path in successful_paths:
        for organization in path:
            if organization.pk not in seen:
                seen.add(organization.pk)
                merchants.append(organization)
    return merchants


def _scope(organization):
    organization_type = organization.organization_type.code
    if organization_type == "CUSTOMER":
        audience_type = "CUSTOMER"
        roles = CUSTOMER_TASK_ROLES
    elif organization_type == "MERCHANT":
        audience_type = "MERCHANT"
        roles = MERCHANT_TASK_ROLES
    else:
        audience_type = "FACTORY"
        roles = FACTORY_TASK_ROLES
    return {
        "organization": _organization_payload(organization),
        "audience_type": audience_type,
        "role_codes": list(roles),
        "own_inventory": {},
        "downstream_inventory": {},
        "activated_user_devices": {},
        "customer_devices": {},
        "in_transit_devices": {},
        "campaign_devices": {},
        "factory_inventory": {},
        "manual_trace": {},
        "unassigned_organizations": {},
    }


def _add_device(scope, section, device):
    scope[section][str(device["recall_device_id"])] = device


def _add_grouped_device(scope, section, group_organization, device, **extra):
    key = str(group_organization.pk)
    group = scope[section].setdefault(
        key,
        {
            "organization": _organization_payload(group_organization),
            "devices": {},
            **extra,
        },
    )
    group["devices"][str(device["recall_device_id"])] = device


def _group_snapshot(key, organization, role_codes, recipients, section):
    return {
        "key": key,
        "organization_id": organization.pk,
        "organization_name": organization.name,
        "organization_type": organization.organization_type.code,
        "role_codes": list(role_codes),
        "recipients": recipients,
        "scope_section": section,
        "visibility": "DISTRIBUTION_CHAIN_SCOPED",
    }


def _append_visibility(device_groups, recall_device_id, group):
    existing = device_groups[recall_device_id]
    identity = (group["organization_id"], group["scope_section"])
    if all(
        (item["organization_id"], item["scope_section"]) != identity
        for item in existing
    ):
        existing.append(group)


def resolve_recall_responsibility(recall_device):
    """Resolve current physical responsibility before distribution-chain fan-out."""
    device = recall_device.device
    binding = (
        CustomerDeviceBinding.objects.select_related(
            "customer_organization__organization_type",
            "source_organization__organization_type",
        )
        .filter(device=device, status=CustomerDeviceBinding.Status.ACTIVE)
        .first()
    )
    if binding:
        return {
            "mode": "ACTIVATED_USER",
            "device_id": device.pk,
            "customer_organization": binding.customer_organization,
            "source_organization": binding.source_organization,
            "resolved_at": timezone.now().isoformat(),
        }

    transfer_item = (
        GoodsTransferItem.objects.select_related(
            "transfer__from_organization__organization_type",
            "transfer__to_organization__organization_type",
        )
        .filter(device=device, transfer__status__in=ACTIVE_TRANSFER_STATES)
        .order_by("-transfer__created_at", "-id")
        .first()
    )
    if transfer_item and transfer_item.transfer.status != GoodsTransfer.Status.SCANNING:
        return {
            "mode": "IN_TRANSIT",
            "device_id": device.pk,
            "transfer": transfer_item.transfer,
            "resolved_at": timezone.now().isoformat(),
        }

    ownership = (
        OrganizationDevice.objects.select_related("organization__organization_type")
        .filter(device=device)
        .first()
    )
    state = DeviceState.objects.filter(device=device).first()
    if ownership and ownership.organization.organization_type.code == "MERCHANT":
        return {
            "mode": "MERCHANT_INVENTORY",
            "device_id": device.pk,
            "organization": ownership.organization,
            "device_state": state.status if state else "",
            "resolved_at": timezone.now().isoformat(),
        }
    if ownership and ownership.organization.organization_type.code == "OWNER":
        return {
            "mode": "FACTORY_INVENTORY",
            "device_id": device.pk,
            "organization": ownership.organization,
            "device_state": state.status if state else "",
            "resolved_at": timezone.now().isoformat(),
        }
    return {
        "mode": "MANUAL_TRACE",
        "device_id": device.pk,
        "organization": recall_device.campaign.factory_organization,
        "resolved_at": timezone.now().isoformat(),
    }


def _operation_guides(scope):
    if scope["audience_type"] == "CUSTOMER":
        return [
            "请先确认收到召回通知，并保持设备停止转售或转交。",
            "已安装激活设备无需原包装二维码，可选择商家接管、用户直寄厂家或厂家上门取件。",
            "选择商家接管时，请向接管商家出示用户端生成的临时召回流转码。",
        ]
    if scope["audience_type"] == "MERCHANT":
        guides = []
        if scope["own_inventory"]:
            guides.append(
                "请在召回详情页核对本组织库存清单，一次填写本批物流及凭证后批量退回上级。"
            )
        if scope["downstream_inventory"]:
            guides.append(
                "请按清单联系下级商家隔离库存；下级发出后，在召回详情页按到货批次接收并批量继续上返。"
            )
        if scope["activated_user_devices"]:
            guides.append(
                "请联系本链路已激活用户；商家接管时扫描用户端临时召回流转码，接管后使用召回专用流转并上传物流单据和实物包装照片。"
            )
        if scope["in_transit_devices"]:
            guides.append("在途设备应停止继续交付，收货后隔离并转入召回退回。")
        return guides
    guides = [
        "请持续跟踪本召回批次全部设备的用户确认、商家接管、在途运输和返厂入库进度。",
    ]
    if scope["factory_inventory"]:
        guides.append(
            "厂家库存召回设备应立即隔离，禁止继续出库、发货、流转或激活。"
        )
    if scope["manual_trace"] or scope["unassigned_organizations"]:
        guides.append(
            "请核对无法自动送达的组织或设备位置，并通过质量追溯案件补充协同处置。"
        )
    return guides


def _finalize_scope(scope):
    def values(mapping):
        return list(mapping.values())

    own_inventory = values(scope["own_inventory"])
    customer_devices = values(scope["customer_devices"])
    in_transit = values(scope["in_transit_devices"])
    campaign_devices = values(scope["campaign_devices"])
    factory_inventory = values(scope["factory_inventory"])
    manual_trace = values(scope["manual_trace"])
    downstream = []
    for group in scope["downstream_inventory"].values():
        devices = values(group["devices"])
        downstream.append({
            "group_id": f"merchant-{group['organization']['id']}",
            "organization": group["organization"],
            "device_count": len(devices),
            "devices": devices,
        })
    activated = []
    for group in scope["activated_user_devices"].values():
        devices = values(group["devices"])
        activated.append({
            "group_id": f"customer-{group['organization']['id']}",
            "customer_organization": group["organization"],
            "source_merchant": group.get("source_merchant"),
            "device_count": len(devices),
            "devices": devices,
        })
    unassigned = values(scope["unassigned_organizations"])
    all_devices = {}
    for device in (
        own_inventory
        + customer_devices
        + in_transit
        + campaign_devices
        + factory_inventory
        + manual_trace
    ):
        all_devices[str(device["recall_device_id"])] = device
    for group in downstream + activated:
        for device in group["devices"]:
            all_devices[str(device["recall_device_id"])] = device
    result = {
        "organization": scope["organization"],
        "audience_type": scope["audience_type"],
        "device_count": len(all_devices),
        "devices": list(all_devices.values()),
        "own_inventory": own_inventory,
        "own_inventory_count": len(own_inventory),
        "downstream_inventory": downstream,
        "downstream_inventory_count": sum(item["device_count"] for item in downstream),
        "activated_user_devices": activated,
        "activated_user_device_count": sum(item["device_count"] for item in activated),
        "customer_devices": customer_devices,
        "customer_device_count": len(customer_devices),
        "in_transit_devices": in_transit,
        "in_transit_device_count": len(in_transit),
        "campaign_devices": campaign_devices,
        "campaign_device_count": len(campaign_devices),
        "factory_inventory": factory_inventory,
        "manual_trace": manual_trace,
        "unassigned_organizations": unassigned,
    }
    result["operation_guides"] = _operation_guides({**scope, **result})
    return result


def _summary(scope):
    if scope["audience_type"] == "CUSTOMER":
        return f"您名下有{scope['customer_device_count']}台已激活设备已确认召回，请查看交付方式。"
    if scope["audience_type"] == "MERCHANT":
        return (
            f"本链路共{scope['device_count']}台：本组织库存{scope['own_inventory_count']}台，"
            f"下级库存{scope['downstream_inventory_count']}台，"
            f"已激活用户设备{scope['activated_user_device_count']}台。"
        )
    return f"本召回批次共{scope['device_count']}台设备，请协同跟踪召回与返厂进度。"


def _task_type(scope):
    return {
        "CUSTOMER": "QUALITY_RECALL_CUSTOMER_NOTICE",
        "MERCHANT": "QUALITY_RECALL_MERCHANT_SCOPE",
        "FACTORY": "QUALITY_RECALL_FACTORY_COORDINATION",
    }[scope["audience_type"]]


def _publish_scope_workitem(*, campaign, scope, recipients, user):
    uniq_key = f"quality-recall:{campaign.pk}:org:{scope['organization']['id']}"
    link = (
        "/pages/quality_trace/recall_detail/recall_detail"
        f"?campaign_id={campaign.pk}"
    )
    title = f"召回任务：{campaign.recall_no}（{scope['device_count']}台）"
    summary = _summary(scope)
    recipient_openids = [item["openid"] for item in recipients]
    payload = {
        "recall_campaign_id": campaign.pk,
        "recall_no": campaign.recall_no,
        "quality_case_id": campaign.quality_case_id,
        "deadline": campaign.deadline.isoformat(),
        "recipient_organization_id": scope["organization"]["id"],
        "recipient_organization_type": scope["audience_type"],
        "scope": scope,
        "inbox_group_key": uniq_key,
        "inbox_group_kind": "QUALITY_RECALL",
        "inbox_group_label": "召回协同",
    }
    existing = WorkItem.objects.select_for_update().filter(uniq_key=uniq_key).first()
    existing_recipients = set(
        existing.user_tasks.values_list("to_openid", flat=True)
    ) if existing else set()
    expected_need_action = scope["audience_type"] != "FACTORY"
    existing_need_action = set(
        existing.user_tasks.values_list("need_action", flat=True)
    ) if existing else set()
    unchanged = bool(
        existing
        and existing.title == title
        and existing.summary == summary
        and existing.link == link
        and existing.payload == payload
        and existing_recipients == set(recipient_openids)
        and existing.action_mode == (
            "EACH_ACK" if scope["audience_type"] == "FACTORY" else "ANYONE_DO"
        )
        and existing_need_action == {expected_need_action}
    )
    if unchanged:
        return existing, False
    workitem = publish_workitem(
        domain="SYSTEM",
        type=_task_type(scope),
        biz_id=str(campaign.pk),
        biz_ref=f"QualityRecallCampaign:{campaign.pk}:Organization:{scope['organization']['id']}",
        uniq_key=uniq_key,
        title=title,
        summary=summary,
        link=link,
        recipients_openids=recipient_openids,
        need_action=expected_need_action,
        action_mode=("EACH_ACK" if scope["audience_type"] == "FACTORY" else "ANYONE_DO"),
        payload=payload,
        actor_openid=user.wx_openid or "",
        mark_workitem_done_immediately=False,
    )
    return workitem, existing is None


@transaction.atomic
def publish_recall_tasks(*, user, campaign_id):
    """Synchronize one chain-scoped task per organization for an active recall."""
    identity = _identity(user, RECALL_MANAGER_ROLES)
    campaign = (
        QualityRecallCampaign.objects.select_for_update()
        .select_related("quality_case", "factory_organization__organization_type")
        .filter(pk=campaign_id, factory_organization=identity.organization)
        .first()
    )
    if not campaign:
        raise QualityRecallError("召回批次不存在或不属于当前厂家。")
    if campaign.status != QualityRecallCampaign.Status.ACTIVE:
        raise QualityRecallError("只有已发布的召回批次可以生成召回任务。")

    rows = list(
        campaign.devices.select_for_update()
        .select_related("device__qrcode")
        .order_by("device_id")
    )
    parent_map = _distribution_parent_map()
    scopes = {}
    device_groups = defaultdict(list)
    modes = {}

    def get_scope(organization):
        return scopes.setdefault(organization.pk, _scope(organization))

    def add_visibility(row, organization, section, key=None):
        role_codes = {
            "CUSTOMER": CUSTOMER_TASK_ROLES,
            "MERCHANT": MERCHANT_TASK_ROLES,
        }.get(organization.organization_type.code, FACTORY_TASK_ROLES)
        recipients = _recipients(
            organization.pk,
            role_codes,
            device_ids=[row.device_id],
        )
        _append_visibility(
            device_groups,
            row.pk,
            _group_snapshot(
                key or f"org_{organization.pk}_{section}",
                organization,
                role_codes,
                recipients,
                section,
            ),
        )

    # The factory is the owner of the recall campaign and must always receive
    # one full-batch coordination task.  This scope is independent from the
    # device's current holder: a completely resolved customer/merchant chain
    # must not suppress factory coordination.
    factory_scope = get_scope(campaign.factory_organization)
    for row in rows:
        _add_device(factory_scope, "campaign_devices", _device_payload(row))
        add_visibility(
            row,
            campaign.factory_organization,
            "campaign_devices",
            key="factory_coordination",
        )

    for row in rows:
        responsibility = resolve_recall_responsibility(row)
        modes[row.pk] = responsibility["mode"]
        device = _device_payload(row)
        mode = responsibility["mode"]
        if mode == "ACTIVATED_USER":
            customer = responsibility["customer_organization"]
            source = responsibility["source_organization"]
            customer_scope = get_scope(customer)
            _add_device(customer_scope, "customer_devices", device)
            add_visibility(row, customer, "customer_devices", key="customer_ack")
            for merchant in _merchant_chain(source, parent_map, campaign.factory_organization_id):
                merchant_scope = get_scope(merchant)
                _add_grouped_device(
                    merchant_scope,
                    "activated_user_devices",
                    customer,
                    device,
                    source_merchant=_organization_payload(source),
                )
                add_visibility(
                    row,
                    merchant,
                    "activated_user_devices",
                    key=("merchant_coordinate" if merchant.pk == source.pk else None),
                )
        elif mode == "MERCHANT_INVENTORY":
            owner = responsibility["organization"]
            for merchant in _merchant_chain(owner, parent_map, campaign.factory_organization_id):
                merchant_scope = get_scope(merchant)
                if merchant.pk == owner.pk:
                    _add_device(merchant_scope, "own_inventory", device)
                    section = "own_inventory"
                else:
                    _add_grouped_device(
                        merchant_scope, "downstream_inventory", owner, device,
                    )
                    section = "downstream_inventory"
                add_visibility(row, merchant, section)
        elif mode == "IN_TRANSIT":
            transfer = responsibility["transfer"]
            anchors = [transfer.from_organization, transfer.to_organization]
            visited = set()
            transit_device = {
                **device,
                "goods_transfer_id": transfer.pk,
                "flow_no": transfer.flow_no,
                "from_organization": _organization_payload(transfer.from_organization),
                "to_organization": _organization_payload(transfer.to_organization),
            }
            for anchor in anchors:
                for merchant in _merchant_chain(anchor, parent_map, campaign.factory_organization_id):
                    if merchant.pk in visited:
                        continue
                    visited.add(merchant.pk)
                    _add_device(get_scope(merchant), "in_transit_devices", transit_device)
                    add_visibility(row, merchant, "in_transit_devices")
        elif mode == "FACTORY_INVENTORY":
            factory = responsibility["organization"]
            _add_device(get_scope(factory), "factory_inventory", device)
            add_visibility(row, factory, "factory_inventory")
        else:
            factory = campaign.factory_organization
            _add_device(get_scope(factory), "manual_trace", device)
            add_visibility(row, factory, "manual_trace")

    # Missing role slots do not block recall. They become factory exceptions.
    for organization_id, raw_scope in list(scopes.items()):
        raw_devices = _finalize_scope(raw_scope)["devices"]
        recipients = _recipients(
            organization_id,
            raw_scope["role_codes"],
            device_ids=[item["device_id"] for item in raw_devices],
        )
        if recipients or organization_id == campaign.factory_organization_id:
            continue
        factory_scope = get_scope(campaign.factory_organization)
        missing_devices = _finalize_scope(raw_scope)["devices"]
        row_map = {row.pk: row for row in rows}
        for device in missing_devices:
            _add_device(factory_scope, "manual_trace", device)
            recall_row = row_map.get(device["recall_device_id"])
            if recall_row:
                add_visibility(recall_row, campaign.factory_organization, "manual_trace")
        factory_scope["unassigned_organizations"][str(organization_id)] = {
            "group_id": f"unassigned-{organization_id}",
            "organization": raw_scope["organization"],
            "device_count": len(missing_devices),
            "reason": "该组织没有可接收召回任务的有效微信岗位账号",
        }

    finalized = {key: _finalize_scope(value) for key, value in scopes.items()}
    workitems = {}
    created_workitem_count = 0
    for organization_id, scope in finalized.items():
        raw_scope = scopes[organization_id]
        recipients = _recipients(
            organization_id,
            raw_scope["role_codes"],
            device_ids=[item["device_id"] for item in scope["devices"]],
        )
        if not recipients:
            continue
        workitem, created = _publish_scope_workitem(
            campaign=campaign,
            scope=scope,
            recipients=recipients,
            user=user,
        )
        workitems[organization_id] = workitem
        created_workitem_count += int(created)

    published = []
    now = timezone.now()
    for row in rows:
        groups = device_groups[row.pk]
        task_ids = sorted({
            workitems[group["organization_id"]].pk
            for group in groups
            if group["organization_id"] in workitems
        })
        old_status = row.status
        row.responsibility_snapshot = {
            "mode": modes[row.pk],
            "device_id": row.device_id,
            "resolved_at": now.isoformat(),
            "groups": groups,
            "workitem_ids": task_ids,
            "published_at": now.isoformat(),
            "delivery_policy": "AUTOMATIC_CHAIN_SCOPED",
        }
        if row.status == QualityRecallDevice.Status.PENDING_NOTICE and task_ids:
            row.status = QualityRecallDevice.Status.NOTIFIED
            row.status_updated_at = now
        row.save(update_fields=(
            "responsibility_snapshot", "status", "status_updated_at",
        ))
        if modes[row.pk] == "ACTIVATED_USER":
            # The handover is a consequence of publishing the recall, never a
            # factory engineer's per-device manual action.  This helper is
            # idempotent, so scheduled/backfill task synchronization is safe.
            from .recall_handover_service import ensure_customer_handover
            ensure_customer_handover(
                recall_device=row,
                actor=user,
                actor_role=identity.role.code,
            )
        if old_status != row.status:
            QualityRecallDeviceEvent.objects.create(
                campaign=campaign,
                recall_device=row,
                from_status=old_status,
                to_status=row.status,
                reason="启动召回时已按设备所属链路自动推送召回任务",
                actor=user,
                actor_role_snapshot=identity.role.code,
                payload={"mode": modes[row.pk], "workitem_ids": task_ids},
            )
        published.append({
            "recall_device_id": row.pk,
            "device_id": row.device_id,
            "mode": modes[row.pk],
            "workitem_ids": task_ids,
        })

    if created_workitem_count:
        QualityTraceEvent.objects.create(
            quality_case=campaign.quality_case,
            alert=campaign.quality_case.source_alert,
            event_type="RECALL_TASKS_AUTO_PUBLISHED",
            actor=user,
            actor_role_snapshot=identity.role.code,
            payload={
                "recall_campaign_id": campaign.pk,
                "recall_no": campaign.recall_no,
                "device_count": len(published),
                "organization_count": len(workitems),
                "workitem_count": len(workitems),
                "delivery_policy": "AUTOMATIC_CHAIN_SCOPED",
            },
        )
    from .recall_notice_service import sync_recall_policy_notices
    sync_recall_policy_notices(campaign=campaign, actor=user)
    return published


def get_recall_task_scope(*, user, campaign_id):
    """Legacy task-detail contract, projected from current authorized rows."""
    from .recall_access_service import RecallViewer, recall_campaign_queryset, visible_rows
    from .recall_display_service import campaign_context, campaign_text
    viewer = RecallViewer(user)
    campaign = recall_campaign_queryset().filter(pk=campaign_id).first()
    if not viewer.valid or not campaign:
        raise QualityRecallError("召回批次不存在或当前账号无权查看。")
    rows = visible_rows(campaign, viewer)
    if not rows and not viewer.metadata_access(campaign):
        raise QualityRecallError("当前岗位无权查看该召回链路。")
    context = campaign_context(campaign, rows, viewer)
    workitem = WorkItem.objects.filter(
        uniq_key=f"quality-recall:{campaign_id}:org:{viewer.organization_id}",
    ).first()
    scope = _finalize_scope(_scope(viewer.identity.organization))
    if workitem and not viewer.observer(campaign):
        payload = workitem.payload if isinstance(workitem.payload, dict) else {}
        allowed = {row.pk for row in rows}
        def trim(value):
            if isinstance(value, list):
                return [trim(item) for item in value if not isinstance(item, dict)
                        or not item.get("recall_device_id") or item["recall_device_id"] in allowed]
            if isinstance(value, dict):
                result = {key: trim(item) for key, item in value.items()}
                if isinstance(result.get("devices"), list):
                    result["device_count"] = len(result["devices"])
                return result
            return value
        scope.update(trim(deepcopy(payload.get("scope", {}))))
    scope["devices"] = [_device_payload(row) for row in rows]
    scope["device_count"] = context["device_count"]
    for section, count_key in (
        ("own_inventory", "own_inventory_count"), ("customer_devices", "customer_device_count"),
        ("in_transit_devices", "in_transit_device_count"), ("campaign_devices", "campaign_device_count"),
    ):
        scope[count_key] = len(scope.get(section, []))
    for section, count_key in (("downstream_inventory", "downstream_inventory_count"),
                               ("activated_user_devices", "activated_user_device_count")):
        scope[section] = [item for item in scope.get(section, []) if item.get("devices")]
        scope[count_key] = sum(len(item["devices"]) for item in scope[section])
    if not viewer.full_campaign(campaign):
        scope["unassigned_organizations"] = []
        scope["campaign_devices"] = []
        scope["campaign_device_count"] = 0
    scope["operation_guides"] = [item["text"] for item in context["stages"]]
    title = (f"召回知晓：{campaign.recall_no}" if viewer.observer(campaign)
             else f"召回任务：{campaign.recall_no}（{len(rows)}台）")
    return {
        "workitem_id": workitem.pk if workitem else None,
        "task_type": "QUALITY_RECALL_OBSERVER_NOTICE" if viewer.observer(campaign) else "QUALITY_RECALL_SCOPE",
        "title": title, "summary": context["scope_summary"] + "。" + context["stage_summary"],
        "campaign": {"id": campaign.pk, "recall_no": campaign.recall_no,
                     **campaign_text(campaign, viewer), "deadline": campaign.deadline.isoformat(),
                     "status": campaign.status},
        "scope": scope,
    }


def complete_recall_scope_tasks_if_ready(*, recall_device, user, audience_type, workitem_ids=None):
    """Close a consolidated task only after every device in its scope advances."""
    if workitem_ids is None:
        workitem_ids = (recall_device.responsibility_snapshot or {}).get("workitem_ids") or []
    type_by_audience = {
        "CUSTOMER": "QUALITY_RECALL_CUSTOMER_NOTICE",
        "MERCHANT": "QUALITY_RECALL_MERCHANT_SCOPE",
    }
    task_type = type_by_audience.get(audience_type)
    if not task_type:
        return []
    pending_statuses = {
        "CUSTOMER": set(),
        "MERCHANT": {
            QualityRecallDevice.Status.PENDING_NOTICE,
            QualityRecallDevice.Status.NOTIFIED,
            QualityRecallDevice.Status.ACKNOWLEDGED,
        },
    }[audience_type]
    now = timezone.now()
    completed = []
    workitems = WorkItem.objects.select_for_update().filter(
        pk__in=workitem_ids,
        type=task_type,
        status="OPEN",
    )
    for workitem in workitems:
        payload = workitem.payload if isinstance(workitem.payload, dict) else {}
        scope = payload.get("scope", {}) if isinstance(payload.get("scope"), dict) else {}
        recall_device_ids = [
            item.get("recall_device_id")
            for item in scope.get("devices", [])
            if isinstance(item, dict) and item.get("recall_device_id")
        ]
        if not recall_device_ids:
            continue
        if audience_type == "CUSTOMER":
            selected_ids = set(
                QualityRecallCustomerHandover.objects.filter(
                    recall_device_id__in=recall_device_ids,
                ).exclude(delivery_method="").values_list("recall_device_id", flat=True)
            )
            expected_ids = set(recall_device_ids)
            # A shared driver's inbox entry is complete as soon as every
            # recalled device on that driver's associated vehicles has a
            # selected method.  The customer-owner task and parent WorkItem
            # remain open until the whole customer scope is complete.
            for task in workitem.user_tasks.select_for_update().filter(
                to_role_snapshot="driver",
            ).exclude(state="DONE"):
                driver = User.objects.filter(wx_openid=task.to_openid).first()
                if not driver:
                    continue
                vehicle_ids = DriverVehicleBinding.objects.filter(
                    driver=driver,
                    is_active=True,
                ).values_list("vehicle_id", flat=True)
                driver_device_ids = set(
                    CustomerDeviceBinding.objects.filter(
                        device__quality_recall_records__id__in=expected_ids,
                        vehicle_id__in=vehicle_ids,
                        status=CustomerDeviceBinding.Status.ACTIVE,
                    ).values_list("device__quality_recall_records__id", flat=True)
                )
                if driver_device_ids and driver_device_ids.issubset(selected_ids):
                    task.state = "DONE"
                    task.done_at = now
                    task.done_reason = "RECALL_SHARED_DRIVER_SCOPE_COMPLETED"
                    task.cursor_time = now
                    task.save(update_fields=(
                        "state", "done_at", "done_reason", "cursor_time", "updated_at",
                    ))
            if selected_ids != expected_ids:
                continue
        elif QualityRecallDevice.objects.filter(
            pk__in=recall_device_ids,
            status__in=pending_statuses,
        ).exists():
            continue
        workitem.status = "DONE"
        workitem.done_by_openid = user.wx_openid or ""
        workitem.done_at = now
        workitem.last_activity_at = now
        workitem.save(update_fields=(
            "status", "done_by_openid", "done_at", "last_activity_at", "updated_at",
        ))
        UserTask.objects.filter(workitem=workitem).exclude(state="DONE").update(
            state="DONE",
            done_at=now,
            done_reason=(
                "RECALL_CUSTOMER_DELIVERY_METHOD_SELECTED"
                if audience_type == "CUSTOMER"
                else "RECALL_SCOPE_RETURN_STARTED"
            ),
            cursor_time=now,
            updated_at=now,
        )
        completed.append(workitem.pk)
    return completed
