from uuid import uuid4

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from account.models import (
    InvitationRecord,
    OrganizationRelation,
    OrganizationRelationType,
    UserOrganization,
)
from account.services.auth_service import get_active_identity
from lifecycle.models import (
    DeviceState,
    GoodsTransfer,
    GoodsTransferItem,
    LifecycleEvent,
    OrganizationDevice,
    QRCode,
)
from lifecycle.services.scan_service import QRCodeResolveError, ScanService
from workflow.models import WorkItem
from workflow.services.workflow_service import publish_workitem


MERCHANT_FLOW_ROLES = {
    "merchant_owner",
    "merchant_sales",
    "merchant_stock",
}
TRANSFER_OPERATOR_ROLES = {
    "merchant_owner",
    "merchant_sales",
}
FACTORY_RECEIPT_ROLES = {"factory_stock"}
FLOW_VIEW_ROLES = MERCHANT_FLOW_ROLES | FACTORY_RECEIPT_ROLES | {
    "factory_sales",
}
RECEIPT_WORK_TYPE = "GOODS_TRANSFER_RECEIPT_CONFIRM"
NOTICE_WORK_TYPE = "GOODS_TRANSFER_NOTICE"


class GoodsTransferError(Exception):
    pass


def _related(instance, name):
    try:
        return getattr(instance, name)
    except ObjectDoesNotExist:
        return None


def _display_name(user):
    return user.wx_nickname or user.get_full_name() or user.username


def _identity(user, allowed_roles=None):
    identity = get_active_identity(user)
    if not identity or identity.organization.organization_type.code not in {
        "MERCHANT",
        "OWNER",
    }:
        raise GoodsTransferError("当前账号没有商品流转权限。")
    if allowed_roles and identity.role.code not in allowed_roles:
        raise GoodsTransferError("当前岗位不能执行该商品流转操作。")
    return identity


def _account_payload(user, organization, relation):
    identity = _related(user, "organization_identity")
    return {
        "user_id": user.pk,
        "nickname": _display_name(user),
        "phone": user.phone or "",
        "role": identity.role.code if identity else "",
        "role_name": identity.role.name if identity else "",
        "organization_id": organization.pk,
        "organization_name": organization.name,
        "organization_type": organization.organization_type.code,
        "region": organization.region or "",
        "relation_id": relation.pk,
    }


def _valid_invitee_owner(relation):
    invitation = _related(relation, "created_by_invitation")
    if (
        not invitation
        or invitation.status != InvitationRecord.Status.ACCEPTED
        or not invitation.invitee_id
        or invitation.target_role.code != "merchant_owner"
    ):
        return None
    user = invitation.invitee
    identity = _related(user, "organization_identity")
    if (
        not user.is_active
        or not identity
        or not identity.is_active
        or identity.organization_id != relation.target_organization_id
        or identity.role.code != "merchant_owner"
        or not identity.role.is_active
    ):
        return None
    return user


def _valid_upstream_account(relation):
    invitation = _related(relation, "created_by_invitation")
    if (
        not invitation
        or invitation.status != InvitationRecord.Status.ACCEPTED
        or not invitation.inviter_id
    ):
        return None
    user = invitation.inviter
    identity = _related(user, "organization_identity")
    if (
        not user.is_active
        or not identity
        or not identity.is_active
        or identity.organization_id != relation.source_organization_id
        or not identity.role.is_active
    ):
        return None
    return user


def _distribution_relations():
    return OrganizationRelation.objects.select_related(
        "source_organization__organization_type",
        "target_organization__organization_type",
        "created_by_invitation__inviter__organization_identity__role",
        "created_by_invitation__inviter__organization_identity__organization",
        "created_by_invitation__invitee__organization_identity__role",
        "created_by_invitation__invitee__organization_identity__organization",
        "created_by_invitation__target_role",
    ).filter(
        relation_type=OrganizationRelationType.DISTRIBUTION,
        is_active=True,
        source_organization__is_active=True,
        target_organization__is_active=True,
    )


def _downstream_accounts(organization):
    items = []
    for relation in _distribution_relations().filter(
        source_organization=organization,
        target_organization__organization_type__code="MERCHANT",
    ):
        user = _valid_invitee_owner(relation)
        if user:
            items.append(
                _account_payload(user, relation.target_organization, relation)
            )
    return items


def _upstream_accounts(organization):
    items = []
    for relation in _distribution_relations().filter(
        target_organization=organization,
        source_organization__organization_type__code__in=("MERCHANT", "OWNER"),
    ):
        user = _valid_upstream_account(relation)
        if user:
            items.append(
                _account_payload(user, relation.source_organization, relation)
            )
    return items


def get_goods_transfer_options(*, user, flow_type):
    identity = _identity(user, MERCHANT_FLOW_ROLES)
    flow_type = str(flow_type or "").upper()
    if flow_type == GoodsTransfer.FlowType.SHIPMENT:
        return {
            "flow_type": flow_type,
            "source_organization": {
                "id": identity.organization_id,
                "name": identity.organization.name,
            },
            "targets": _downstream_accounts(identity.organization),
        }
    if flow_type == GoodsTransfer.FlowType.RETURN:
        return {
            "flow_type": flow_type,
            "source_organization": {
                "id": identity.organization_id,
                "name": identity.organization.name,
            },
            "targets": _upstream_accounts(identity.organization),
        }
    if flow_type == GoodsTransfer.FlowType.TRANSFER:
        if identity.role.code not in TRANSFER_OPERATOR_ROLES:
            raise GoodsTransferError("只有上级商家负责人或销售经理可以调货。")
        children = _downstream_accounts(identity.organization)
        return {
            "flow_type": flow_type,
            "instruction_organization": {
                "id": identity.organization_id,
                "name": identity.organization.name,
            },
            "sources": children,
            "targets": children,
        }
    raise GoodsTransferError("商品流转类型无效。")


def _candidate_by_user(items, user_id, label):
    try:
        expected_id = int(user_id)
    except (TypeError, ValueError) as exc:
        raise GoodsTransferError(f"请选择有效的{label}账号。") from exc
    item = next((row for row in items if row["user_id"] == expected_id), None)
    if not item:
        raise GoodsTransferError(f"所选{label}账号不在当前有效业务链路中。")
    return item


def _flow_number():
    return f"GF-{timezone.now():%Y%m%d%H%M%S}-{uuid4().hex[:8].upper()}"


@transaction.atomic
def create_goods_transfer(
    *,
    user,
    flow_type,
    client_request_id,
    source_account_id=None,
    target_account_id=None,
    remark="",
):
    identity = _identity(user, MERCHANT_FLOW_ROLES)
    request_id = str(client_request_id or "").strip()
    if not request_id or len(request_id) > 64:
        raise GoodsTransferError("商品流转请求标识无效。")
    existing = GoodsTransfer.objects.filter(client_request_id=request_id).first()
    if existing:
        if existing.initiated_by_id != user.pk:
            raise GoodsTransferError("商品流转请求标识已被其他账号使用。")
        return serialize_goods_transfer(existing, identity), False

    flow_type = str(flow_type or "").upper()
    if flow_type == GoodsTransfer.FlowType.SHIPMENT:
        candidate = _candidate_by_user(
            _downstream_accounts(identity.organization),
            target_account_id,
            "下级商家负责人",
        )
        source_account = None
        target_account_id = candidate["user_id"]
        from_org_id = identity.organization_id
        to_org_id = candidate["organization_id"]
    elif flow_type == GoodsTransfer.FlowType.RETURN:
        candidate = _candidate_by_user(
            _upstream_accounts(identity.organization),
            target_account_id,
            "上级业务",
        )
        source_account = None
        target_account_id = candidate["user_id"]
        from_org_id = identity.organization_id
        to_org_id = candidate["organization_id"]
    elif flow_type == GoodsTransfer.FlowType.TRANSFER:
        if identity.role.code not in TRANSFER_OPERATOR_ROLES:
            raise GoodsTransferError("只有上级商家负责人或销售经理可以调货。")
        children = _downstream_accounts(identity.organization)
        source = _candidate_by_user(children, source_account_id, "调出商家负责人")
        target = _candidate_by_user(children, target_account_id, "调入商家负责人")
        if source["organization_id"] == target["organization_id"]:
            raise GoodsTransferError("调出商家和调入商家不能相同。")
        source_account_id = source["user_id"]
        target_account_id = target["user_id"]
        from_org_id = source["organization_id"]
        to_org_id = target["organization_id"]
        source_account = source_account_id
    else:
        raise GoodsTransferError("商品流转类型无效。")

    transfer = GoodsTransfer.objects.create(
        flow_no=_flow_number(),
        client_request_id=request_id,
        flow_type=flow_type,
        instruction_organization=identity.organization,
        from_organization_id=from_org_id,
        to_organization_id=to_org_id,
        source_account_id=source_account,
        target_account_id=target_account_id,
        initiated_by=user,
        remark=str(remark or "").strip()[:255],
        receipt_required=flow_type in {
            GoodsTransfer.FlowType.SHIPMENT,
            GoodsTransfer.FlowType.RETURN,
        },
    )
    return serialize_goods_transfer(transfer, identity), True


def _locked_device_for_transfer(transfer, code):
    qrcode = QRCode.objects.select_related(
        "device__product_model",
    ).filter(code=code).first()
    if not qrcode or qrcode.status != "ACTIVE" or not qrcode.device_id:
        raise GoodsTransferError("未找到可用的设备二维码。")
    device = qrcode.device
    ownership = OrganizationDevice.objects.select_for_update().filter(
        device=device
    ).first()
    state = DeviceState.objects.select_for_update().filter(device=device).first()
    if (
        not ownership
        or ownership.organization_id != transfer.from_organization_id
        or ownership.status != "IN_STOCK"
        or not state
        or state.current_organization_id != transfer.from_organization_id
        or state.status != "IN_STOCK"
        or state.install_status != "NOT_INSTALLED"
        or state.activation_status != "NOT_ACTIVATED"
    ):
        raise GoodsTransferError("只能流转调出组织直接库存且未安装、未激活的设备。")
    return device, ownership, state


@transaction.atomic
def scan_goods_transfer_device(*, user, transfer_id, qr_text):
    identity = _identity(user, MERCHANT_FLOW_ROLES)
    transfer = GoodsTransfer.objects.select_for_update().filter(
        pk=transfer_id,
        initiated_by=user,
        instruction_organization=identity.organization,
    ).first()
    if not transfer:
        raise GoodsTransferError("商品流转单不存在或不属于当前账号。")
    if transfer.status != GoodsTransfer.Status.SCANNING:
        raise GoodsTransferError("该商品流转单已经停止扫码。")
    try:
        code = ScanService.extract_code(qr_text)
    except QRCodeResolveError as exc:
        raise GoodsTransferError(str(exc)) from exc
    device, _ownership, _state = _locked_device_for_transfer(transfer, code)
    if GoodsTransferItem.objects.filter(transfer=transfer, device=device).exists():
        raise GoodsTransferError("该设备已经加入本次商品流转。")
    if GoodsTransferItem.objects.filter(
        device=device,
        transfer__status__in=(
            GoodsTransfer.Status.SCANNING,
            GoodsTransfer.Status.PENDING_RECEIPT,
        ),
    ).exists():
        raise GoodsTransferError("该设备正在其他未完成的商品流转中。")
    item = GoodsTransferItem.objects.create(
        transfer=transfer,
        device=device,
        scanned_by=user,
    )
    return {
        "item": _serialize_item(item),
        "item_count": transfer.items.count(),
    }


def _role_openids(organization, role_codes):
    return tuple(
        UserOrganization.objects.filter(
            organization=organization,
            is_active=True,
            role__code__in=role_codes,
            role__is_active=True,
            user__is_active=True,
        )
        .exclude(user__wx_openid__isnull=True)
        .exclude(user__wx_openid="")
        .values_list("user__wx_openid", flat=True)
        .distinct()
    )


def _receipt_roles(organization):
    if organization.organization_type.code == "OWNER":
        return FACTORY_RECEIPT_ROLES
    return MERCHANT_FLOW_ROLES


def _task_payload(transfer):
    return {
        "goods_transfer_id": transfer.pk,
        "flow_no": transfer.flow_no,
        "flow_type": transfer.flow_type,
        "from_organization_id": transfer.from_organization_id,
        "from_organization_name": transfer.from_organization.name,
        "to_organization_id": transfer.to_organization_id,
        "to_organization_name": transfer.to_organization.name,
        "device_count": transfer.items.count(),
    }


def _detail_link(transfer):
    return (
        "/pages/stock/goods_transfer_detail/goods_transfer_detail"
        f"?transfer_id={transfer.pk}"
    )


def _publish_notice(transfer, organization, direction, title, summary):
    recipients = _role_openids(organization, MERCHANT_FLOW_ROLES)
    if not recipients:
        return None
    return publish_workitem(
        domain="LOGISTICS",
        type=NOTICE_WORK_TYPE,
        biz_id=transfer.flow_no,
        biz_ref=f"lifecycle.GoodsTransfer:{transfer.pk}",
        uniq_key=(
            f"lifecycle:goods-transfer:{transfer.pk}:notice:{direction}"
        ),
        title=title,
        summary=summary,
        link=_detail_link(transfer),
        recipients_openids=recipients,
        need_action=False,
        action_mode="EACH_ACK",
        payload={**_task_payload(transfer), "direction": direction},
        actor_openid=transfer.initiated_by.wx_openid or "",
        mark_workitem_done_immediately=True,
    )


def _publish_receipt_task(transfer):
    recipients = _role_openids(
        transfer.to_organization,
        _receipt_roles(transfer.to_organization),
    )
    if not recipients:
        raise GoodsTransferError("收货组织没有可接收入库任务的有效账号。")
    action_name = (
        "退货"
        if transfer.flow_type == GoodsTransfer.FlowType.RETURN
        else "发货"
    )
    return publish_workitem(
        domain="LOGISTICS",
        type=RECEIPT_WORK_TYPE,
        biz_id=transfer.flow_no,
        biz_ref=f"lifecycle.GoodsTransfer:{transfer.pk}",
        uniq_key=f"lifecycle:goods-transfer:{transfer.pk}:receipt",
        title=f"{action_name}设备待确认入库",
        summary=(
            f"{transfer.from_organization.name} → "
            f"{transfer.to_organization.name}｜"
            f"共{transfer.items.count()}台"
        ),
        link=_detail_link(transfer),
        recipients_openids=recipients,
        need_action=True,
        action_mode="ANYONE_DO",
        payload=_task_payload(transfer),
        actor_openid=transfer.initiated_by.wx_openid or "",
        mark_workitem_done_immediately=False,
    )


def _create_flow_event(transfer, item, event_type, extra=None):
    data = {
        "goods_transfer_id": transfer.pk,
        "flow_no": transfer.flow_no,
        "flow_type": transfer.flow_type,
        "instruction_organization_id": transfer.instruction_organization_id,
        "source_account_id": transfer.source_account_id,
        "target_account_id": transfer.target_account_id,
    }
    data.update(extra or {})
    return LifecycleEvent.objects.create(
        device=item.device,
        event_type=event_type,
        operator=transfer.initiated_by,
        organization=transfer.instruction_organization,
        from_organization=transfer.from_organization,
        to_organization=transfer.to_organization,
        data=data,
    )


def _move_ownership(transfer, item, event, now, *, factory_terminal=False):
    state_status = "RETURNED_TO_FACTORY" if factory_terminal else "IN_STOCK"
    ownership_status = (
        "RETURNED_TO_FACTORY" if factory_terminal else "IN_STOCK"
    )
    DeviceState.objects.filter(device=item.device).update(
        current_organization=transfer.to_organization,
        status=state_status,
        last_event=event,
        last_event_time=now,
        updated_at=now,
    )
    OrganizationDevice.objects.filter(device=item.device).update(
        organization=transfer.to_organization,
        status=ownership_status,
        received_at=now,
        last_event=event,
        updated_at=now,
    )


def _lock_and_validate_items(transfer):
    items = list(
        GoodsTransferItem.objects.select_for_update()
        .select_related("device__product_model")
        .filter(transfer=transfer)
        .order_by("id")
    )
    if not items:
        raise GoodsTransferError("请至少扫描一台设备后再完成流转。")
    for item in items:
        _locked_device_for_transfer(transfer, item.device.qrcode.code)
    return items


@transaction.atomic
def submit_goods_transfer(*, user, transfer_id):
    identity = _identity(user, MERCHANT_FLOW_ROLES)
    transfer = (
        GoodsTransfer.objects.select_for_update()
        .select_related(
            "instruction_organization__organization_type",
            "from_organization__organization_type",
            "to_organization__organization_type",
            "initiated_by",
        )
        .filter(
            pk=transfer_id,
            initiated_by=user,
            instruction_organization=identity.organization,
        )
        .first()
    )
    if not transfer:
        raise GoodsTransferError("商品流转单不存在或不属于当前账号。")
    if transfer.status != GoodsTransfer.Status.SCANNING:
        return serialize_goods_transfer(transfer, identity), False

    items = _lock_and_validate_items(transfer)
    now = timezone.now()
    if transfer.receipt_required:
        event_type = (
            "RETURN"
            if transfer.flow_type == GoodsTransfer.FlowType.RETURN
            else "SHIP"
        )
        for item in items:
            event = _create_flow_event(
                transfer,
                item,
                event_type,
                {"receipt_status": "PENDING"},
            )
            DeviceState.objects.filter(device=item.device).update(
                status="SHIPPED",
                last_event=event,
                last_event_time=now,
                updated_at=now,
            )
            OrganizationDevice.objects.filter(device=item.device).update(
                status=(
                    "RETURNED"
                    if transfer.flow_type == GoodsTransfer.FlowType.RETURN
                    else "TRANSFERRED"
                ),
                delivered_at=now,
                last_event=event,
                updated_at=now,
            )
            item.status = GoodsTransferItem.Status.PENDING_RECEIPT
            item.lifecycle_event = event
            item.save(
                update_fields=("status", "lifecycle_event", "updated_at")
            )
        transfer.status = GoodsTransfer.Status.PENDING_RECEIPT
        transfer.submitted_at = now
        transfer.save(
            update_fields=("status", "submitted_at", "updated_at")
        )
        if transfer.flow_type == GoodsTransfer.FlowType.SHIPMENT:
            # 商业单据和收款是独立协作链，不改变实物收货前置条件。
            from lifecycle.services.goods_transfer_settlement_service import (
                initialize_goods_transfer_settlement,
            )

            initialize_goods_transfer_settlement(transfer=transfer, actor=user)
        _publish_receipt_task(transfer)
    else:
        for item in items:
            event = _create_flow_event(transfer, item, "TRANSFER")
            _move_ownership(transfer, item, event, now)
            item.status = GoodsTransferItem.Status.COMPLETED
            item.lifecycle_event = event
            item.completed_at = now
            item.save(
                update_fields=(
                    "status",
                    "lifecycle_event",
                    "completed_at",
                    "updated_at",
                )
            )
        transfer.status = GoodsTransfer.Status.COMPLETED
        transfer.submitted_at = now
        transfer.completed_at = now
        transfer.save(
            update_fields=(
                "status",
                "submitted_at",
                "completed_at",
                "updated_at",
            )
        )
        summary = (
            f"{transfer.from_organization.name} → "
            f"{transfer.to_organization.name}｜共{len(items)}台"
        )
        _publish_notice(
            transfer,
            transfer.from_organization,
            "OUT",
            "上级商家已完成调出",
            summary,
        )
        _publish_notice(
            transfer,
            transfer.to_organization,
            "IN",
            "上级商家已完成调入",
            summary,
        )
        # 调货的物权仍然直接由B转至C；这里只创建A-B、A-C两条
        # 独立上下级账务，不制造A名下的虚假库存流转。
        from lifecycle.services.goods_transfer_account_service import (
            initialize_goods_transfer_accounts,
        )

        initialize_goods_transfer_accounts(transfer=transfer, actor=user)
    return serialize_goods_transfer(transfer, identity), True


def _complete_receipt_workitem(transfer, user, now):
    workitem = WorkItem.objects.select_for_update().filter(
        type=RECEIPT_WORK_TYPE,
        uniq_key=f"lifecycle:goods-transfer:{transfer.pk}:receipt",
    ).first()
    if not workitem:
        raise GoodsTransferError("未找到对应的确认入库任务。")
    task = workitem.user_tasks.select_for_update().filter(
        to_openid=user.wx_openid,
        need_action=True,
    ).exclude(state="DONE").first()
    if not task:
        raise GoodsTransferError("当前账号不是该确认入库任务的处理人。")
    if workitem.status != "OPEN":
        raise GoodsTransferError("该入库任务已经由其他账号完成。")
    actor_name = _display_name(user)
    workitem.status = "DONE"
    workitem.done_by_openid = user.wx_openid or ""
    workitem.done_at = now
    workitem.last_activity_at = now
    workitem.save(
        update_fields=(
            "status",
            "done_by_openid",
            "done_at",
            "last_activity_at",
            "updated_at",
        )
    )
    workitem.user_tasks.filter(to_openid=user.wx_openid).update(
        state="DONE",
        need_action=False,
        done_at=now,
        done_reason="SELF_ACTION",
        display_title_snapshot="商品入库已确认",
        display_subtitle_snapshot="由你完成",
        cursor_time=now,
        updated_at=now,
    )
    workitem.user_tasks.exclude(to_openid=user.wx_openid).update(
        state="DONE",
        need_action=False,
        done_at=now,
        done_reason="OTHER_ACTION",
        display_title_snapshot=f"已由{actor_name}确认商品入库",
        display_subtitle_snapshot="同组织无需重复操作",
        cursor_time=now,
        updated_at=now,
    )


@transaction.atomic
def confirm_goods_transfer_receipt(*, user, transfer_id):
    identity = _identity(user, FLOW_VIEW_ROLES)
    transfer = (
        GoodsTransfer.objects.select_for_update()
        .select_related(
            "instruction_organization__organization_type",
            "from_organization__organization_type",
            "to_organization__organization_type",
            "initiated_by",
        )
        .filter(pk=transfer_id, to_organization=identity.organization)
        .first()
    )
    if not transfer:
        raise GoodsTransferError("商品流转单不存在或不属于当前收货组织。")
    if identity.role.code not in _receipt_roles(transfer.to_organization):
        raise GoodsTransferError("当前岗位不能确认本次商品入库。")
    if transfer.status == GoodsTransfer.Status.COMPLETED:
        return serialize_goods_transfer(transfer, identity), False
    if transfer.status != GoodsTransfer.Status.PENDING_RECEIPT:
        raise GoodsTransferError("该商品流转尚未进入待确认入库状态。")

    items = list(
        GoodsTransferItem.objects.select_for_update()
        .select_related("device")
        .filter(transfer=transfer)
        .order_by("id")
    )
    device_ids = [item.device_id for item in items]
    ownership_by_device = {
        row.device_id: row
        for row in OrganizationDevice.objects.select_for_update().filter(
            device_id__in=device_ids
        )
    }
    states_by_device = {
        row.device_id: row
        for row in DeviceState.objects.select_for_update().filter(
            device_id__in=device_ids
        )
    }
    for item in items:
        ownership = ownership_by_device.get(item.device_id)
        state = states_by_device.get(item.device_id)
        expected_ownership_status = (
            "RETURNED"
            if transfer.flow_type == GoodsTransfer.FlowType.RETURN
            else "TRANSFERRED"
        )
        if (
            not ownership
            or ownership.organization_id != transfer.from_organization_id
            or ownership.status != expected_ownership_status
            or not state
            or state.current_organization_id != transfer.from_organization_id
            or state.status != "SHIPPED"
        ):
            raise GoodsTransferError("流转设备当前状态已经变化，不能确认入库。")

    now = timezone.now()
    factory_terminal = (
        transfer.flow_type == GoodsTransfer.FlowType.RETURN
        and transfer.to_organization.organization_type.code == "OWNER"
    )
    for item in items:
        event = LifecycleEvent.objects.create(
            device=item.device,
            event_type="RECEIVE",
            operator=user,
            organization=transfer.to_organization,
            from_organization=transfer.from_organization,
            to_organization=transfer.to_organization,
            data={
                "goods_transfer_id": transfer.pk,
                "flow_no": transfer.flow_no,
                "flow_type": transfer.flow_type,
                "receipt_confirmed": True,
                "factory_lifecycle_terminated": factory_terminal,
            },
        )
        _move_ownership(
            transfer,
            item,
            event,
            now,
            factory_terminal=factory_terminal,
        )
        item.status = GoodsTransferItem.Status.COMPLETED
        item.receipt_event = event
        item.completed_at = now
        item.save(
            update_fields=(
                "status",
                "receipt_event",
                "completed_at",
                "updated_at",
            )
        )
    _complete_receipt_workitem(transfer, user, now)
    transfer.status = GoodsTransfer.Status.COMPLETED
    transfer.received_by = user
    transfer.received_at = now
    transfer.completed_at = now
    transfer.factory_lifecycle_terminated = factory_terminal
    transfer.save(
        update_fields=(
            "status",
            "received_by",
            "received_at",
            "completed_at",
            "factory_lifecycle_terminated",
            "updated_at",
        )
    )
    _publish_notice(
        transfer,
        transfer.from_organization,
        "RECEIPT_CONFIRMED",
        "收货方已确认商品入库",
        (
            f"{transfer.to_organization.name}已确认｜"
            f"共{len(items)}台"
        ),
    )
    if transfer.flow_type == GoodsTransfer.FlowType.RETURN:
        # 退货金额与实物入库并行；确认入库后只创建待人工提交的冲抵账。
        from lifecycle.services.goods_transfer_account_service import (
            initialize_goods_transfer_accounts,
        )

        initialize_goods_transfer_accounts(transfer=transfer, actor=user)
    return serialize_goods_transfer(transfer, identity), True


def _serialize_item(item):
    return {
        "id": item.pk,
        "status": item.status,
        "status_name": item.get_status_display(),
        "device": {
            "id": item.device_id,
            "sn": item.device.sn,
            "product_model_id": item.device.product_model_id,
            "product_model_name": item.device.product_model.name,
        },
        "scanned_by_name": _display_name(item.scanned_by),
        "scanned_at": item.scanned_at.isoformat(),
        "completed_at": (
            item.completed_at.isoformat() if item.completed_at else ""
        ),
    }


def serialize_goods_transfer(transfer, identity=None):
    if identity is None:
        identity = get_active_identity(transfer.initiated_by)
    items = list(
        transfer.items.select_related(
            "device__product_model",
            "scanned_by",
        ).order_by("id")
    )
    can_receive = False
    if identity and transfer.status == GoodsTransfer.Status.PENDING_RECEIPT:
        can_receive = bool(
            identity.organization_id == transfer.to_organization_id
            and identity.role.code in _receipt_roles(transfer.to_organization)
        )
    return {
        "id": transfer.pk,
        "flow_no": transfer.flow_no,
        "flow_type": transfer.flow_type,
        "flow_type_name": transfer.get_flow_type_display(),
        "status": transfer.status,
        "status_name": transfer.get_status_display(),
        "instruction_organization": {
            "id": transfer.instruction_organization_id,
            "name": transfer.instruction_organization.name,
        },
        "from_organization": {
            "id": transfer.from_organization_id,
            "name": transfer.from_organization.name,
        },
        "to_organization": {
            "id": transfer.to_organization_id,
            "name": transfer.to_organization.name,
            "type": transfer.to_organization.organization_type.code,
        },
        "source_account": (
            {
                "id": transfer.source_account_id,
                "name": _display_name(transfer.source_account),
            }
            if transfer.source_account_id
            else None
        ),
        "target_account": {
            "id": transfer.target_account_id,
            "name": _display_name(transfer.target_account),
        },
        "initiated_by_name": _display_name(transfer.initiated_by),
        "received_by_name": (
            _display_name(transfer.received_by)
            if transfer.received_by_id
            else ""
        ),
        "remark": transfer.remark,
        "receipt_required": transfer.receipt_required,
        "factory_lifecycle_terminated": transfer.factory_lifecycle_terminated,
        "item_count": len(items),
        "items": [_serialize_item(item) for item in items],
        "actions": {
            "can_scan": bool(
                identity
                and transfer.status == GoodsTransfer.Status.SCANNING
                and transfer.initiated_by_id == identity.user_id
            ),
            "can_submit": bool(
                identity
                and transfer.status == GoodsTransfer.Status.SCANNING
                and transfer.initiated_by_id == identity.user_id
                and items
            ),
            "can_receive": can_receive,
            "can_view_accounts": bool(
                identity
                and transfer.status == GoodsTransfer.Status.COMPLETED
                and transfer.flow_type
                in {
                    GoodsTransfer.FlowType.RETURN,
                    GoodsTransfer.FlowType.TRANSFER,
                }
                and identity.organization_id
                in {
                    transfer.instruction_organization_id,
                    transfer.from_organization_id,
                    transfer.to_organization_id,
                }
            ),
        },
        "created_at": transfer.created_at.isoformat(),
        "submitted_at": (
            transfer.submitted_at.isoformat() if transfer.submitted_at else ""
        ),
        "received_at": (
            transfer.received_at.isoformat() if transfer.received_at else ""
        ),
        "completed_at": (
            transfer.completed_at.isoformat() if transfer.completed_at else ""
        ),
        "detail_link": _detail_link(transfer),
    }


def get_goods_transfer_detail(*, user, transfer_id):
    identity = _identity(user, FLOW_VIEW_ROLES)
    transfer = (
        GoodsTransfer.objects.select_related(
            "instruction_organization__organization_type",
            "from_organization__organization_type",
            "to_organization__organization_type",
            "source_account",
            "target_account",
            "initiated_by",
            "received_by",
        )
        .filter(pk=transfer_id)
        .filter(
            Q(instruction_organization=identity.organization)
            | Q(from_organization=identity.organization)
            | Q(to_organization=identity.organization)
            | Q(target_account=user)
        )
        .first()
    )
    if not transfer:
        raise GoodsTransferError("商品流转单不存在或当前账号无权查看。")
    return serialize_goods_transfer(transfer, identity)


def get_goods_transfer_list(*, user, limit=50):
    identity = _identity(user, FLOW_VIEW_ROLES)
    try:
        limit = min(max(int(limit or 50), 1), 100)
    except (TypeError, ValueError) as exc:
        raise GoodsTransferError("列表数量参数无效。") from exc
    transfers = (
        GoodsTransfer.objects.select_related(
            "instruction_organization__organization_type",
            "from_organization__organization_type",
            "to_organization__organization_type",
            "source_account",
            "target_account",
            "initiated_by",
            "received_by",
        )
        .filter(
            Q(instruction_organization=identity.organization)
            | Q(from_organization=identity.organization)
            | Q(to_organization=identity.organization)
            | Q(target_account=user)
        )
        .distinct()
        .order_by("-created_at", "-id")[:limit]
    )
    return {
        "viewer": {
            "role": identity.role.code,
            "role_name": identity.role.name,
            "organization_id": identity.organization_id,
            "organization_name": identity.organization.name,
        },
        "items": [serialize_goods_transfer(row, identity) for row in transfers],
    }
