from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from account.models import OrganizationRelation, OrganizationRelationType, UserOrganization
from account.services.auth_service import get_active_identity
from account.services.role_delegation_service import (
    has_open_action_task_copy,
    identity_has_role,
)
from lifecycle.models import (
    CustomerDeviceBinding, DeviceState, GoodsTransferItem, LifecycleEvent,
    OrganizationDevice,
)
from quality_trace.models import (
    QualityRecallCampaign,
    QualityRecallCustomerHandover,
    QualityRecallDevice,
    QualityRecallDeviceEvent,
    QualityRecallIsolationReceipt,
    QualityRecallInventoryBatchConfirmation,
    QualityRecallTransportLeg,
    QualityTraceEvent,
)
from workflow.models import UserTask, WorkItem
from workflow.services.workflow_service import publish_workitem

from .recall_collaboration_service import (
    close_factory_recall_progress,
    close_workitem,
    close_merchant_return_task,
    publish_factory_recall_progress,
    publish_merchant_return_task,
    publish_factory_transport_tasks,
    publish_inventory_posted_notice,
    publish_inventory_posting_task,
    organization_recipients,
)
from .recall_service import QualityRecallError


MERCHANT_ROLES = {
    "merchant_owner", "merchant_manager", "merchant_senior_manager",
    "merchant_sales", "merchant_stock",
}
FACTORY_RECEIPT_ROLES = {"factory_logistics"}
FACTORY_ISOLATION_ROLES = {"factory_sales_assistant"}


def _identity(user):
    identity = get_active_identity(user)
    if not identity:
        raise QualityRecallError("当前账号没有有效组织身份。")
    return identity


def _require_manager_task_copy(identity, *uniq_keys):
    if not has_open_action_task_copy(identity, uniq_keys):
        raise QualityRecallError("当前高级经理没有收到这项召回协同任务。")


def _evidence(value, label):
    data = dict(value or {})
    if not str(data.get("storage_key") or "").strip():
        raise QualityRecallError(f"请上传{label}凭证。")
    return data


def _snapshot(organization):
    return {
        "id": organization.pk,
        "name": organization.name,
        "type": organization.organization_type.code,
    }


def _recipients(organization, roles):
    return list(
        UserOrganization.objects.filter(
            organization=organization, role__code__in=roles, role__is_active=True,
            user__is_active=True, is_active=True,
        ).exclude(user__wx_openid__isnull=True).exclude(user__wx_openid="")
        .values_list("user__wx_openid", flat=True)
    )


def _event(row, event_type, user, role, payload):
    QualityTraceEvent.objects.create(
        quality_case=row.campaign.quality_case,
        alert=row.campaign.quality_case.source_alert,
        event_type=event_type,
        actor=user,
        actor_role_snapshot=role,
        payload={
            "recall_campaign_id": row.campaign_id,
            "recall_device_id": row.pk,
            **payload,
        },
    )


def _complete_workitem(leg, user):
    now = timezone.now()
    items = WorkItem.objects.select_for_update().filter(
        uniq_key=f"quality-recall-transport:{leg.pk}:receive", status="OPEN",
    )
    for item in items:
        item.status = "DONE"
        item.done_by_openid = user.wx_openid or ""
        item.done_at = now
        item.last_activity_at = now
        item.save(update_fields=(
            "status", "done_by_openid", "done_at", "last_activity_at", "updated_at",
        ))
        UserTask.objects.filter(workitem=item).exclude(state="DONE").update(
            state="DONE", done_at=now, done_reason="SELF_ACTION",
            cursor_time=now, updated_at=now,
        )


def _current_custodian(row):
    last = row.transport_legs.filter(status=QualityRecallTransportLeg.Status.RECEIVED).order_by(
        "-sequence_no",
    ).first()
    if last:
        return last.to_organization
    handover = getattr(row, "customer_handover", None)
    if handover and handover.status == QualityRecallCustomerHandover.Status.MERCHANT_RECEIVED:
        return handover.merchant_organization
    if handover:
        raise QualityRecallError("召回设备尚未由商家完成实物接管。")
    if row.return_legs.exclude(status="CANCELLED").exists():
        raise QualityRecallError("该设备已有历史退货关联，请先核对原记录，不能重复发运。")
    ownership = OrganizationDevice.objects.select_related(
        "organization__organization_type",
    ).filter(device_id=row.device_id).first()
    state = DeviceState.objects.filter(device_id=row.device_id).first()
    if not (
        ownership and state
        and ownership.organization.is_active
        and ownership.organization.organization_type.code == "MERCHANT"
        and ownership.status == "IN_STOCK"
        and state.current_organization_id == ownership.organization_id
        and state.status == "IN_STOCK"
        and state.install_status == "NOT_INSTALLED"
        and state.activation_status == "NOT_ACTIVATED"
    ):
        raise QualityRecallError("该设备当前不是可由商家直接退回的库存。")
    if CustomerDeviceBinding.objects.filter(device_id=row.device_id, status="ACTIVE").exists():
        raise QualityRecallError("设备已有用户绑定，请先完成用户召回交接。")
    if GoodsTransferItem.objects.filter(
        device_id=row.device_id,
        transfer__status__in=("SCANNING", "PENDING_RECEIPT", "PENDING_FACTORY_PICKUP", "PENDING_FACTORY_POSTING"),
    ).exists():
        raise QualityRecallError("该设备仍有未完成商品流转，请先核对实物归属。")
    return ownership.organization


def _upstream_target(current, factory_id):
    """Require one unambiguous upstream path ending at this recall's factory."""
    node, seen, target = current, set(), None
    while node.organization_type.code == "MERCHANT":
        if node.pk in seen:
            raise QualityRecallError("召回上返经销链存在循环，请核对组织关系。")
        seen.add(node.pk)
        relations = list(OrganizationRelation.objects.select_related(
            "source_organization__organization_type",
        ).filter(
            target_organization=node, relation_type=OrganizationRelationType.DISTRIBUTION,
            is_active=True, source_organization__is_active=True,
            source_organization__organization_type__code__in=("MERCHANT", "OWNER"),
        ))
        if len(relations) != 1:
            raise QualityRecallError("当前商家的有效上级经销链必须且只能有一个。")
        relation = relations[0]
        if target is None:
            target = relation
        node = relation.source_organization
    if node.pk != factory_id:
        raise QualityRecallError("该上返链路未通向本次召回厂家。")
    return target


def _locked_recall_device(recall_device_id):
    campaign_id = QualityRecallDevice.objects.filter(pk=recall_device_id).values_list(
        "campaign_id", flat=True,
    ).first()
    campaign = QualityRecallCampaign.objects.select_for_update().filter(
        pk=campaign_id, status=QualityRecallCampaign.Status.ACTIVE,
    ).first()
    if not campaign:
        raise QualityRecallError("召回设备不存在或召回批次未发布。")
    row = QualityRecallDevice.objects.select_for_update().select_related(
        "campaign__quality_case", "customer_handover__merchant_organization",
    ).get(pk=recall_device_id)
    if row.status in {"DISPOSED", "EXEMPTED", "UNREACHABLE"}:
        raise QualityRecallError("该设备当前召回状态不能继续流转。")
    return row


def _locked_transport_leg(leg_id):
    row_id = QualityRecallTransportLeg.objects.filter(pk=leg_id).values_list(
        "recall_device_id", flat=True,
    ).first()
    if not row_id:
        raise QualityRecallError("召回运输记录不存在。")
    _locked_recall_device(row_id)
    return QualityRecallTransportLeg.objects.select_for_update().select_related(
        "recall_device__campaign__quality_case", "from_organization", "to_organization",
    ).get(pk=leg_id)


def _reserve_inventory_for_recall(row, current, user):
    if getattr(row, "customer_handover", None) or row.transport_legs.exclude(status="CANCELLED").exists():
        return
    ownership = OrganizationDevice.objects.select_for_update().get(device_id=row.device_id)
    if ownership.organization_id != current.pk or ownership.status != "IN_STOCK":
        raise QualityRecallError("设备库存状态已变化，请刷新后重试。")
    event = LifecycleEvent.objects.create(
        device_id=row.device_id, event_type="RETURN", operator=user,
        organization=current, from_organization=current,
        data={"source": "QUALITY_RECALL", "stage": "PREPARING",
              "campaign_id": row.campaign_id, "recall_device_id": row.pk},
    )
    ownership.status = "RETURNED"
    ownership.last_event = event
    ownership.save(update_fields=("status", "last_event", "updated_at"))


@transaction.atomic
def create_next_recall_transport_leg(*, user, recall_device_id, notify=True):
    identity = _identity(user)
    row = _locked_recall_device(recall_device_id)
    current = _current_custodian(row)
    if identity.organization_id != current.pk or not identity_has_role(identity, MERCHANT_ROLES):
        raise QualityRecallError("只有当前保管商家的授权岗位可以发起下一程退回。")
    _require_manager_task_copy(
        identity,
        f"quality-recall-merchant-return:{row.pk}:org:{current.pk}",
        f"quality-recall:{row.campaign_id}:org:{current.pk}",
    )
    if row.transport_legs.exclude(
        status__in=(QualityRecallTransportLeg.Status.RECEIVED, QualityRecallTransportLeg.Status.CANCELLED),
    ).exists():
        raise QualityRecallError("当前设备已有未完成的召回运输程次。")
    relation = _upstream_target(current, row.campaign.factory_organization_id)
    target = relation.source_organization
    _reserve_inventory_for_recall(row, current, user)
    sequence = (row.transport_legs.aggregate(value=Max("sequence_no"))["value"] or 0) + 1
    leg = QualityRecallTransportLeg.objects.create(
        campaign=row.campaign,
        recall_device=row,
        sequence_no=sequence,
        from_organization=current,
        to_organization=target,
        relation_snapshot={
            "organization_relation_id": relation.pk,
            "relation_type": relation.relation_type,
            "from": _snapshot(current),
            "to": _snapshot(target),
        },
        is_factory_destination=target.organization_type.code == "OWNER",
    )
    if notify and leg.is_factory_destination:
        try:
            publish_factory_transport_tasks(leg, user=user)
        except ValueError as exc:
            raise QualityRecallError(str(exc)) from exc
    elif notify:
        recipients = _recipients(target, MERCHANT_ROLES)
        if not recipients:
            raise QualityRecallError("上级接收组织没有有效召回收件岗位。")
        publish_workitem(
            domain="SYSTEM",
            type="QUALITY_RECALL_TRANSPORT_RECEIPT",
            biz_id=str(row.campaign_id),
            biz_ref=f"QualityRecallTransportLeg:{leg.pk}",
            uniq_key=f"quality-recall-transport:{leg.pk}:receive",
            title="请验收召回退回设备",
            summary=f"召回设备{row.sn_snapshot}将由{current.name}退回，请验收并保持隔离。",
            link=(
                "/pages/quality_trace/recall_detail/recall_detail"
                f"?campaign_id={row.campaign_id}&recall_device_id={row.pk}"
            ),
            recipients_openids=recipients,
            need_action=True,
            action_mode="ANYONE_DO",
            payload={
                "transport_leg_id": leg.pk,
                "recall_device_id": row.pk,
                "device_sn": row.sn_snapshot,
                "sequence_no": sequence,
                "from_organization": _snapshot(current),
                "to_organization": _snapshot(target),
                "is_factory_destination": leg.is_factory_destination,
            },
            actor_openid=user.wx_openid or "",
            mark_workitem_done_immediately=False,
        )
    handover = getattr(row, "customer_handover", None)
    if handover and notify:
        close_merchant_return_task(handover, current, user=user)
        publish_factory_recall_progress(
            handover,
            user=user,
            stage="RETURN_LEG_CREATED",
            summary=(
                f"{current.name}已建立召回专用退回程次，设备{row.sn_snapshot}"
                f"将发往{target.name}。"
            ),
        )
    _event(row, "RECALL_TRANSPORT_LEG_CREATED", user, identity.role.code, {
        "transport_leg_id": leg.pk, "sequence_no": sequence,
        "from_organization_id": current.pk, "to_organization_id": target.pk,
    })
    return leg


@transaction.atomic
def dispatch_recall_transport_leg(*, user, leg_id, logistics_provider, tracking_no, evidence, notify=True):
    identity = _identity(user)
    leg = _locked_transport_leg(leg_id)
    if identity.organization_id != leg.from_organization_id or not identity_has_role(identity, MERCHANT_ROLES):
        raise QualityRecallError("只有本程发出商家的授权岗位可以确认发出。")
    _require_manager_task_copy(
        identity,
        f"quality-recall-merchant-return:{leg.recall_device_id}:org:{leg.from_organization_id}",
        f"quality-recall:{leg.campaign_id}:org:{leg.from_organization_id}",
    )
    if leg.status != QualityRecallTransportLeg.Status.PREPARING:
        raise QualityRecallError("当前召回运输程次不能重复发出。")
    provider = str(logistics_provider or "").strip()
    number = str(tracking_no or "").strip()
    if not provider or not number:
        raise QualityRecallError("请填写物流承运方和物流单号。")
    now = timezone.now()
    leg.status = QualityRecallTransportLeg.Status.IN_TRANSIT
    leg.logistics_provider = provider
    leg.tracking_no = number
    leg.dispatch_evidence = _evidence(evidence, "发出")
    leg.dispatched_by = user
    leg.dispatched_at = now
    leg.save(update_fields=(
        "status", "logistics_provider", "tracking_no", "dispatch_evidence",
        "dispatched_by", "dispatched_at", "updated_at",
    ))
    row = leg.recall_device
    if row.status != QualityRecallDevice.Status.RETURN_IN_PROGRESS:
        old = row.status
        row.status = QualityRecallDevice.Status.RETURN_IN_PROGRESS
        row.status_updated_at = now
        row.save(update_fields=("status", "status_updated_at"))
        QualityRecallDeviceEvent.objects.create(
            campaign=row.campaign, recall_device=row, from_status=old,
            to_status=row.status, reason="召回专用运输已发出", actor=user,
            actor_role_snapshot=identity.role.code, payload={"transport_leg_id": leg.pk},
        )
    handover = getattr(row, "customer_handover", None)
    if not handover:
        event = LifecycleEvent.objects.create(
            device_id=row.device_id, event_type="RETURN", operator=user,
            organization=leg.from_organization, from_organization=leg.from_organization,
            to_organization=leg.to_organization,
            data={"source": "QUALITY_RECALL", "stage": "IN_TRANSIT",
                  "campaign_id": row.campaign_id, "transport_leg_id": leg.pk},
        )
        DeviceState.objects.filter(device_id=row.device_id).update(
            status="SHIPPED", last_event=event, last_event_time=now, updated_at=now,
        )
        OrganizationDevice.objects.filter(device_id=row.device_id).update(
            status="RETURNED", delivered_at=now, last_event=event, updated_at=now,
        )
    if handover and notify:
        publish_factory_recall_progress(
            handover,
            user=user,
            stage="RETURN_IN_TRANSIT",
            summary=(
                f"召回设备{row.sn_snapshot}已由{leg.from_organization.name}发出，"
                f"正在运往{leg.to_organization.name}；物流：{provider} {number}。"
            ),
        )
    _event(row, "RECALL_TRANSPORT_DISPATCHED", user, identity.role.code, {
        "transport_leg_id": leg.pk, "tracking_no": number,
    })
    return leg


@transaction.atomic
def receive_recall_transport_leg(*, user, leg_id, evidence, receipt_note="", notify=True):
    identity = _identity(user)
    leg = _locked_transport_leg(leg_id)
    roles = FACTORY_RECEIPT_ROLES if leg.is_factory_destination else MERCHANT_ROLES
    if identity.organization_id != leg.to_organization_id or not identity_has_role(identity, roles):
        raise QualityRecallError("当前账号不是本程召回设备的授权验收人。")
    _require_manager_task_copy(
        identity,
        f"quality-recall-transport:{leg.pk}:receive",
    )
    if leg.status != QualityRecallTransportLeg.Status.IN_TRANSIT:
        raise QualityRecallError("只有运输中的召回设备可以验收。")
    now = timezone.now()
    leg.status = QualityRecallTransportLeg.Status.RECEIVED
    leg.receipt_evidence = _evidence(evidence, "验收")
    leg.received_by = user
    leg.received_at = now
    leg.custody_snapshot = {
        "custodian_type": (
            "RECALL_FACTORY_PENDING_ISOLATION" if leg.is_factory_destination
            else "RECALL_MERCHANT_CUSTODY"
        ),
        "organization": _snapshot(leg.to_organization),
        "received_by_user_id": user.pk,
        "received_at": now.isoformat(),
        "receipt_note": str(receipt_note or "").strip(),
        "ordinary_inventory_created": False,
    }
    leg.save(update_fields=(
        "status", "receipt_evidence", "received_by", "received_at",
        "custody_snapshot", "updated_at",
    ))
    row = leg.recall_device
    if leg.is_factory_destination and row.status != QualityRecallDevice.Status.RECEIVED:
        old_status = row.status
        row.status = QualityRecallDevice.Status.RECEIVED
        row.status_updated_at = now
        row.save(update_fields=("status", "status_updated_at"))
        QualityRecallDeviceEvent.objects.create(
            campaign=row.campaign,
            recall_device=row,
            from_status=old_status,
            to_status=row.status,
            reason="厂家物流经理已确认收到召回设备，待销售助理入库记账",
            actor=user,
            actor_role_snapshot=identity.role.code,
            payload={"transport_leg_id": leg.pk},
        )
    _complete_workitem(leg, user)
    handover = getattr(row, "customer_handover", None)
    if notify and leg.is_factory_destination:
        try:
            publish_inventory_posting_task(leg, user=user)
        except ValueError as exc:
            raise QualityRecallError(str(exc)) from exc
    if handover and notify:
        if leg.is_factory_destination:
            publish_factory_recall_progress(
                handover,
                user=user,
                stage="FACTORY_RECEIVED_PENDING_POSTING",
                summary=(
                    f"召回设备{row.sn_snapshot}已由厂家验收，"
                    "当前待厂家销售助理完成召回专用入库记账。"
                ),
            )
        else:
            publish_merchant_return_task(handover, leg.to_organization, user=user)
            publish_factory_recall_progress(
                handover,
                user=user,
                stage="UPSTREAM_MERCHANT_RECEIVED",
                summary=(
                    f"{leg.to_organization.name}已验收召回设备"
                    f"{row.sn_snapshot}，待继续逐级退回厂家。"
                ),
            )
    _event(row, "RECALL_TRANSPORT_RECEIVED", user, identity.role.code, {
        "transport_leg_id": leg.pk,
        "factory_pending_isolation": leg.is_factory_destination,
        "ordinary_inventory_created": False,
    })
    return leg


@transaction.atomic
def isolate_recalled_device(*, user, leg_id, disposition, isolation_location, evidence, notify=True):
    identity = _identity(user)
    leg = _locked_transport_leg(leg_id)
    if (
        not leg.is_factory_destination
        or identity.organization_id != leg.to_organization_id
        or not identity_has_role(identity, FACTORY_ISOLATION_ROLES)
    ):
        raise QualityRecallError("只有目标厂家的销售助理可以执行召回入库记账。")
    _require_manager_task_copy(
        identity,
        f"quality-recall-transport:{leg.pk}:inventory-posting",
    )
    if leg.status != QualityRecallTransportLeg.Status.RECEIVED:
        raise QualityRecallError("厂家物流经理确认收货后才能入库记账。")
    valid = {value for value, _ in QualityRecallIsolationReceipt.Disposition.choices}
    if disposition not in valid:
        raise QualityRecallError("隔离处置分类无效。")
    location = str(isolation_location or "").strip()
    if not location:
        raise QualityRecallError("请填写召回隔离位置。")
    now = timezone.now()
    row = leg.recall_device
    receipt, created = QualityRecallIsolationReceipt.objects.get_or_create(
        recall_device=row,
        defaults={
            "campaign": row.campaign, "transport_leg": leg,
            "factory_organization": leg.to_organization,
            "disposition": disposition, "isolation_location": location,
            "evidence_snapshot": _evidence(evidence, "隔离"),
            "isolated_by": user, "isolated_at": now,
            "source_activation_preserved": True,
            "ordinary_inventory_created": False,
        },
    )
    if not created:
        raise QualityRecallError("该召回设备已经完成入库记账。")
    if row.status != QualityRecallDevice.Status.RECEIVED:
        old = row.status
        row.status = QualityRecallDevice.Status.RECEIVED
        row.status_updated_at = now
        row.save(update_fields=("status", "status_updated_at"))
        QualityRecallDeviceEvent.objects.create(
            campaign=row.campaign, recall_device=row, from_status=old,
            to_status=row.status, reason="厂家销售助理已完成召回专用入库记账", actor=user,
            actor_role_snapshot=identity.role.code,
            payload={"transport_leg_id": leg.pk, "isolation_receipt_id": receipt.pk},
        )
    _event(row, "RECALL_DEVICE_FACTORY_ISOLATED", user, identity.role.code, {
        "transport_leg_id": leg.pk, "isolation_receipt_id": receipt.pk,
        "disposition": disposition, "source_activation_preserved": True,
        "ordinary_inventory_created": False,
    })
    handover = getattr(row, "customer_handover", None)
    close_workitem(
        f"quality-recall-transport:{leg.pk}:inventory-posting",
        user=user,
        reason="RECALL_FACTORY_INVENTORY_POSTED",
    )
    if notify:
        publish_inventory_posted_notice(receipt, user=user)
    if handover and notify:
        publish_factory_recall_progress(
            handover,
            user=user,
            stage="FACTORY_INVENTORY_POSTED",
            summary=(
                f"召回设备{row.sn_snapshot}已由销售助理完成隔离入库记账，"
                f"隔离位置：{location}。"
            ),
        )
        close_factory_recall_progress(handover, user=user)
    if notify:
        publish_batch_posting_confirmation(row.campaign, user=user)
    return receipt


def publish_batch_posting_confirmation(campaign, *, user):
    accounted = campaign.isolation_receipts.count()
    if accounted == campaign.device_count and not hasattr(campaign, "inventory_batch_confirmation"):
        recipients = organization_recipients(
            campaign.factory_organization, {"factory_sales_assistant"},
        )
        if recipients:
            publish_workitem(
                domain="SYSTEM", type="QUALITY_RECALL_BATCH_POSTING_CONFIRM",
                biz_id=str(campaign.pk), biz_ref=f"QualityRecallCampaign:{campaign.pk}",
                uniq_key=f"quality-recall:{campaign.pk}:batch-posting-confirm",
                title=f"请确认本批召回设备全部入库记账：{campaign.recall_no}",
                summary=f"系统核对{accounted}台召回设备均已逐台入库记账，请销售助理完成批次确认。",
                link=("/pages/quality_trace/recall_detail/recall_detail"
                      f"?campaign_id={campaign.pk}"),
                recipients_openids=recipients, need_action=True, action_mode="ANYONE_DO",
                payload={"recall_campaign_id": campaign.pk,
                         "accounted_device_count": accounted,
                         "stage": "BATCH_POSTING_PENDING_CONFIRMATION"},
                actor_openid=user.wx_openid or "", mark_workitem_done_immediately=False,
            )


@transaction.atomic
def confirm_recall_inventory_batch(*, user, campaign_id, note=""):
    identity = _identity(user)
    campaign = QualityRecallCampaign.objects.select_for_update().get(pk=campaign_id)
    if (
        identity.organization_id != campaign.factory_organization_id
        or not identity_has_role(identity, {"factory_sales_assistant"})
    ):
        raise QualityRecallError("只有本厂家的销售助理可以确认整批入库记账。")
    _require_manager_task_copy(
        identity,
        f"quality-recall:{campaign.pk}:batch-posting-confirm",
        f"quality-recall:{campaign.pk}:org:{campaign.factory_organization_id}",
    )
    accounted = campaign.isolation_receipts.count()
    if accounted != campaign.device_count:
        raise QualityRecallError(
            f"本批共{campaign.device_count}台，目前仅{accounted}台完成入库记账。"
        )
    now = timezone.now()
    confirmation, created = QualityRecallInventoryBatchConfirmation.objects.get_or_create(
        campaign=campaign,
        defaults={"confirmed_by": user, "confirmed_role_snapshot": identity.role.code,
                  "confirmed_device_count": accounted, "confirmed_at": now,
                  "note": str(note or "").strip()},
    )
    if not created:
        return confirmation
    close_workitem(
        f"quality-recall:{campaign.pk}:batch-posting-confirm", user=user,
        reason="RECALL_BATCH_INVENTORY_POSTING_CONFIRMED",
    )
    recipients = organization_recipients(
        campaign.factory_organization, {"factory_chief_engineer"},
    )
    if not recipients:
        raise QualityRecallError("厂家没有有效的厂家总工程师账号。")
    publish_workitem(
        domain="SYSTEM", type="QUALITY_RECALL_POST_RECALL_DISPOSITION",
        biz_id=str(campaign.pk), biz_ref=f"QualityRecallCampaign:{campaign.pk}",
        uniq_key=f"quality-recall:{campaign.pk}:post-recall-disposition",
        title=f"召回后处置待决策：{campaign.recall_no}",
        summary=f"本批{accounted}台召回设备已全部完成入库记账，请启动召回后的风险与处置流程。",
        link=f"/pages/quality_trace/recall_detail/recall_detail?campaign_id={campaign.pk}",
        recipients_openids=recipients, need_action=True, action_mode="ANYONE_DO",
        payload={"recall_campaign_id": campaign.pk,
                 "confirmed_device_count": accounted,
                 "stage": "POST_RECALL_DISPOSITION_READY"},
        actor_openid=user.wx_openid or "", mark_workitem_done_immediately=False,
    )
    _event(campaign.devices.order_by("pk").first(),
           "RECALL_BATCH_INVENTORY_POSTING_CONFIRMED", user, identity.role.code,
           {"confirmed_device_count": accounted, "confirmation_id": confirmation.pk})
    return confirmation
