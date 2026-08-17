import mimetypes
from pathlib import Path

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from account.models import UserOrganization
from sales.models import OrderPlan, OrderPlanLogisticsDocument
from sales.services.order_plan_receipt_service import display_name, role_openids
from workflow.models import WorkItem
from workflow.services.workflow_service import publish_workitem


UPLOAD_WORK_TYPE = "ORDER_PLAN_LOGISTICS_DOCUMENT_UPLOAD"
NOTICE_WORK_TYPE = "ORDER_PLAN_LOGISTICS_DOCUMENT_AVAILABLE"
LOGISTICS_ROLE = "factory_logistics"
FACTORY_SALES_ROLE = "factory_sales"
MERCHANT_DOCUMENT_ROLES = {
    "merchant_owner",
    "merchant_sales",
    "merchant_stock",
}
FACTORY_DOCUMENT_ROLES = {LOGISTICS_ROLE, FACTORY_SALES_ROLE}
VIEW_ROLES = FACTORY_DOCUMENT_ROLES | MERCHANT_DOCUMENT_ROLES
DOCUMENT_STATUSES = {
    OrderPlan.Status.SHIPPED,
    OrderPlan.Status.RECEIVED,
}
MAX_DOCUMENT_SIZE = 10 * 1024 * 1024


class OrderLogisticsError(Exception):
    pass


def _identity(user):
    identity = (
        UserOrganization.objects.select_related(
            "organization",
            "organization__organization_type",
            "role",
        )
        .filter(
            user=user,
            user__is_active=True,
            is_active=True,
            organization__is_active=True,
            role__is_active=True,
        )
        .first()
    )
    if not identity or identity.role.code not in VIEW_ROLES:
        raise OrderLogisticsError("当前账号没有查看物流单据的岗位权限。")
    organization_type = identity.organization.organization_type.code
    if (
        identity.role.code in FACTORY_DOCUMENT_ROLES
        and organization_type != "OWNER"
    ):
        raise OrderLogisticsError("当前厂家岗位与所属厂家不一致。")
    if (
        identity.role.code in MERCHANT_DOCUMENT_ROLES
        and organization_type != "MERCHANT"
    ):
        raise OrderLogisticsError("当前商家岗位与所属组织类型不一致。")
    return identity


def _authorized_plan(identity, plan_id, *, lock=False):
    plans = OrderPlan.objects.select_related(
        "factory_organization",
        "merchant_organization",
        "brand",
        "created_by",
        "shipped_by",
    )
    if lock:
        plans = plans.select_for_update()
    if identity.role.code == LOGISTICS_ROLE:
        plans = plans.filter(factory_organization=identity.organization)
    elif identity.role.code == FACTORY_SALES_ROLE:
        plans = plans.filter(
            factory_organization=identity.organization,
            created_by=identity.user,
        )
    else:
        plans = plans.filter(merchant_organization=identity.organization)
    plan = plans.filter(pk=plan_id).first()
    if not plan:
        raise OrderLogisticsError("订单计划不存在或不属于当前组织。")
    return plan


def _logistics_link(plan):
    return (
        "/pages/sales/order_logistics/order_logistics"
        f"?order_plan_id={plan.pk}"
    )


def _task_payload(plan):
    return {
        "order_plan_id": plan.pk,
        "plan_code": plan.plan_code,
        "merchant_name": plan.merchant_organization.name,
        "total_quantity": plan.total_quantity,
    }


def publish_logistics_upload_task(*, plan, actor_openid):
    logistics_openids = role_openids(
        plan.factory_organization,
        {LOGISTICS_ROLE},
    )
    if not logistics_openids:
        return None
    return publish_workitem(
        domain="SALES",
        type=UPLOAD_WORK_TYPE,
        biz_id=plan.plan_code,
        biz_ref=f"sales.OrderPlan:{plan.pk}",
        uniq_key=f"sales:order-plan:{plan.pk}:logistics-document-upload",
        title="订单已发货，待上传物流单据",
        summary=(
            f"{plan.merchant_organization.name}｜"
            f"{plan.brand.name}｜共{plan.total_quantity}台"
        ),
        link=_logistics_link(plan),
        recipients_openids=logistics_openids,
        need_action=True,
        action_mode="ANYONE_DO",
        payload=_task_payload(plan),
        actor_openid=actor_openid,
        mark_workitem_done_immediately=False,
    )


def _validate_document(uploaded_file):
    if not uploaded_file:
        raise OrderLogisticsError("请选择物流单据文件。")
    if uploaded_file.size <= 0 or uploaded_file.size > MAX_DOCUMENT_SIZE:
        raise OrderLogisticsError("物流单据不能为空且不能超过10MB。")

    header = uploaded_file.read(12)
    uploaded_file.seek(0)
    if header.startswith(b"\xff\xd8\xff"):
        suffix, file_type, content_type = ".jpg", "IMAGE", "image/jpeg"
    elif header.startswith(b"\x89PNG\r\n\x1a\n"):
        suffix, file_type, content_type = ".png", "IMAGE", "image/png"
    elif header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        suffix, file_type, content_type = ".webp", "IMAGE", "image/webp"
    elif header.startswith(b"%PDF-"):
        suffix, file_type, content_type = ".pdf", "PDF", "application/pdf"
    else:
        raise OrderLogisticsError("物流单据只支持JPG、PNG、WEBP或PDF格式。")

    original_name = Path(uploaded_file.name).name[:255]
    if not original_name.lower().endswith(suffix):
        original_name = f"{Path(original_name).stem[:240]}{suffix}"
    return file_type, content_type, original_name


def _complete_upload_task(plan, user, document):
    workitem = WorkItem.objects.select_for_update().filter(
        type=UPLOAD_WORK_TYPE,
        uniq_key=f"sales:order-plan:{plan.pk}:logistics-document-upload",
    ).first()
    if not workitem:
        publish_logistics_upload_task(
            plan=plan,
            actor_openid=plan.shipped_by.wx_openid if plan.shipped_by_id else "",
        )
        workitem = WorkItem.objects.select_for_update().filter(
            type=UPLOAD_WORK_TYPE,
            uniq_key=f"sales:order-plan:{plan.pk}:logistics-document-upload",
        ).first()
    if not workitem or workitem.status == "DONE":
        return False

    actor_task = workitem.user_tasks.select_for_update().filter(
        to_openid=user.wx_openid,
        need_action=True,
    ).exclude(state="DONE").first()
    if not actor_task:
        raise OrderLogisticsError("当前账号不是该物流单据任务的处理人。")

    now = timezone.now()
    payload = dict(workitem.payload or {})
    payload.update(
        {
            "document_id": document.pk,
            "completed_by_name": display_name(user),
            "completed_at": now.isoformat(),
        }
    )
    workitem.status = "DONE"
    workitem.done_by_openid = user.wx_openid or ""
    workitem.done_at = now
    workitem.last_activity_at = now
    workitem.payload = payload
    workitem.save(
        update_fields=(
            "status",
            "done_by_openid",
            "done_at",
            "last_activity_at",
            "payload",
            "updated_at",
        )
    )
    workitem.user_tasks.filter(to_openid=user.wx_openid).update(
        state="DONE",
        need_action=False,
        done_at=now,
        done_reason="SELF_ACTION",
        display_title_snapshot="物流单据已上传",
        display_subtitle_snapshot="由你完成",
        cursor_time=now,
        updated_at=now,
    )
    workitem.user_tasks.exclude(to_openid=user.wx_openid).update(
        state="DONE",
        need_action=False,
        done_at=now,
        done_reason="OTHER_ACTION",
        display_title_snapshot=f"已由{display_name(user)}上传物流单据",
        display_subtitle_snapshot="同组织无需重复完成首份单据任务",
        cursor_time=now,
        updated_at=now,
    )
    return True


def _sales_manager_openid(plan):
    return (
        UserOrganization.objects.filter(
            user=plan.created_by,
            organization=plan.factory_organization,
            role__code=FACTORY_SALES_ROLE,
            is_active=True,
            role__is_active=True,
            user__is_active=True,
        )
        .exclude(user__wx_openid__isnull=True)
        .exclude(user__wx_openid="")
        .values_list("user__wx_openid", flat=True)
        .first()
    )


def _notify_document_available(plan, document, user):
    recipient_openids = set(
        role_openids(
            plan.merchant_organization,
            MERCHANT_DOCUMENT_ROLES,
        )
    )
    sales_manager_openid = _sales_manager_openid(plan)
    if sales_manager_openid:
        recipient_openids.add(sales_manager_openid)
    if not recipient_openids:
        return False
    publish_workitem(
        domain="SALES",
        type=NOTICE_WORK_TYPE,
        biz_id=plan.plan_code,
        biz_ref=f"sales.OrderPlan:{plan.pk}",
        uniq_key=(
            f"sales:order-plan:{plan.pk}:"
            f"logistics-document:{document.pk}:available"
        ),
        title="厂家已上传物流单据",
        summary=(
            f"第{document.sequence_no}份｜{document.original_name}｜"
            f"{plan.factory_organization.name}"
        ),
        link=_logistics_link(plan),
        recipients_openids=tuple(sorted(recipient_openids)),
        need_action=False,
        action_mode="EACH_ACK",
        payload={
            **_task_payload(plan),
            "document_id": document.pk,
            "document_sequence_no": document.sequence_no,
        },
        actor_openid=user.wx_openid or "",
        mark_workitem_done_immediately=True,
    )
    return True


def _serialize_document(document):
    return {
        "id": document.pk,
        "sequence_no": document.sequence_no,
        "original_name": document.original_name,
        "file_type": document.file_type,
        "content_type": document.content_type,
        "file_size": document.file_size,
        "remark": document.remark,
        "uploaded_by_name": display_name(document.uploaded_by),
        "uploaded_at": document.uploaded_at.isoformat(),
        "preview_path": (
            f"/sales/order-plans/{document.order_plan_id}/logistics/"
            f"documents/{document.pk}/file/"
        ),
    }


def _documents(plan):
    prefetched = getattr(plan, "visible_logistics_documents", None)
    if prefetched is not None:
        return list(prefetched)
    return list(
        plan.logistics_documents.select_related("uploaded_by")
        .order_by("sequence_no", "id")
    )


def serialize_logistics_summary(plan, role_code):
    if role_code not in VIEW_ROLES:
        return None
    documents = _documents(plan)
    if documents:
        state = "DOCUMENTS_AVAILABLE"
        state_name = f"已上传{len(documents)}份物流单据"
    elif plan.status in DOCUMENT_STATUSES:
        state = "PENDING_UPLOAD"
        state_name = "待上传物流单据"
    else:
        state = "WAITING_SHIPMENT"
        state_name = "等待库房发货"
    latest = documents[-1] if documents else None
    return {
        "state": state,
        "state_name": state_name,
        "document_count": len(documents),
        "latest_uploaded_at": latest.uploaded_at.isoformat() if latest else None,
        "latest_uploaded_by_name": display_name(latest.uploaded_by) if latest else "",
        "can_upload": role_code == LOGISTICS_ROLE and plan.status in DOCUMENT_STATUSES,
        "can_view": bool(documents),
        "detail_link": _logistics_link(plan),
    }


def get_order_logistics_detail(*, user, plan_id):
    identity = _identity(user)
    plan = _authorized_plan(identity, plan_id)
    documents = _documents(plan)
    return {
        "order_plan": {
            "id": plan.pk,
            "plan_code": plan.plan_code,
            "status": plan.status,
            "status_name": plan.get_status_display(),
            "factory_name": plan.factory_organization.name,
            "merchant_name": plan.merchant_organization.name,
            "brand_name": plan.brand.name,
            "total_quantity": plan.total_quantity,
        },
        "viewer": {
            "role": identity.role.code,
            "role_name": identity.role.name,
        },
        "actions": {
            "can_upload": (
                identity.role.code == LOGISTICS_ROLE
                and plan.status in DOCUMENT_STATUSES
            ),
        },
        "documents": [_serialize_document(document) for document in documents],
        "non_blocking_notice": "物流单据不影响商家实物入库，可在入库前后查看。",
    }


@transaction.atomic
def upload_order_logistics_document(
    *, user, plan_id, client_request_id, document_file, remark=""
):
    identity = _identity(user)
    if identity.role.code != LOGISTICS_ROLE:
        raise OrderLogisticsError("只有厂家物流经理可以上传物流单据。")
    plan = _authorized_plan(identity, plan_id, lock=True)
    if plan.status not in DOCUMENT_STATUSES:
        raise OrderLogisticsError("厂家库管一键发货后才能上传物流单据。")

    request_id = str(client_request_id or "").strip()
    if not request_id or len(request_id) > 64:
        raise OrderLogisticsError("物流单据请求标识无效。")
    existing = (
        OrderPlanLogisticsDocument.objects.select_related(
            "order_plan", "uploaded_by"
        )
        .filter(order_plan=plan, client_request_id=request_id)
        .first()
    )
    if existing:
        return _serialize_document(existing), False

    file_type, content_type, original_name = _validate_document(document_file)
    sequence_no = (
        OrderPlanLogisticsDocument.objects.filter(order_plan=plan)
        .aggregate(value=Max("sequence_no"))["value"]
        or 0
    ) + 1
    document = OrderPlanLogisticsDocument.objects.create(
        order_plan=plan,
        sequence_no=sequence_no,
        client_request_id=request_id,
        document_file=document_file,
        original_name=original_name,
        file_type=file_type,
        content_type=content_type,
        file_size=document_file.size,
        remark=str(remark or "").strip()[:255],
        uploaded_by=user,
    )
    _complete_upload_task(plan, user, document)
    _notify_document_available(plan, document, user)
    return _serialize_document(document), True


def get_logistics_document_file(*, user, plan_id, document_id):
    identity = _identity(user)
    plan = _authorized_plan(identity, plan_id)
    document = OrderPlanLogisticsDocument.objects.filter(
        pk=document_id,
        order_plan=plan,
    ).first()
    if not document:
        raise OrderLogisticsError("物流单据不存在。")
    content_type = (
        document.content_type
        or mimetypes.guess_type(document.document_file.name)[0]
        or "application/octet-stream"
    )
    return document.document_file, document.original_name, content_type
