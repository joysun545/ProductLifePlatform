"""Server-derived sending-leg evidence scopes; no logistics facts are created here."""
import uuid

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from lifecycle.models import AfterSalesMedia, ReplacementLeg

LIMIT = 9
PURPOSE = "REPAIR_RETURN_SHIPMENT_DOCUMENT"
FT_ACTION = "FACTORY_TRANSPORT_SEND"
OLD_ACTION = "SEND_REPLACEMENT_LEG"


def error(message):
    from lifecycle.services.aftersales_service import AfterSalesError
    raise AfterSalesError(message)


def transport_id(app, transport):
    value = transport.get("transport_id")
    if value:
        try:
            return str(uuid.UUID(str(value)))
        except (ValueError, TypeError, AttributeError):
            error("返厂物流标识无效，请核对原物流记录")
    # Existing transports already have an immutable creation timestamp. Derive
    # one stable identifier without guessing attachment ownership or rewriting
    # any historical logistics fact.
    created = transport.get("created_at")
    if not created or not parse_datetime(str(created)):
        error("返厂物流缺少有效创建记录，请核对")
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"pldp:factory-transport:{app.pk}:{created}"))


def ft_scope(app, transport, direction, index):
    return f"ft:{transport_id(app, transport)}:{direction}:{index}:send"


def old_scope(leg):
    return f"old:{leg.pk}:send"


def _integer(value, label):
    if isinstance(value, bool):
        error(label + "无效")
    if isinstance(value, int):
        result = value
    else:
        text = str(value or "")
        if not text.isascii() or not text.isdigit() or len(text) > 19:
            error(label + "无效")
        result = int(text)
    if not 0 <= result <= 9223372036854775807:
        error(label + "无效")
    return result


def _time(value):
    result = parse_datetime(value) if isinstance(value, str) else value
    if not result:
        error("本段物流缺少有效开始时间")
    if timezone.is_naive(result):
        result = timezone.make_aware(result)
    return result


def resolve(app, user, data, *, require_pending=True):
    from lifecycle.services.aftersales_service import AfterSalesService as S
    from lifecycle.services import factory_transport_service as F
    from lifecycle.services import replacement_flow_service as B

    action = str(data.get("evidence_action") or "")
    if action not in {FT_ACTION, OLD_ACTION}:
        error("请选择本次物流段后上传凭证")
    if not S.can_view_application(application=app, user=user):
        error("无权上传该售后物流凭证")
    if app.status == "CANCELLED":
        error("当前售后单不能上传物流凭证")
    kind = str(data.get("evidence_kind") or "")
    if action == FT_ACTION:
        if app.status == "CLOSED":
            error("当前售后单不能上传返厂物流凭证")
        t = F.get(app)
        if not t or str(data.get("transport_id") or "") != transport_id(app, t):
            error("返厂物流标识已变化，请刷新后上传")
        direction = str(data.get("direction") or "")
        index = _integer(data.get("index"), "返厂交接段")
        if direction not in {"inbound", "outbound"} or not 0 <= index < len(t[direction]):
            error("返厂交接段不存在")
        leg = t[direction][index]
        if kind != "LOGISTICS":
            error("本段请上传物流凭证")
        if require_pending and leg["status"] != "PENDING":
            error("本段已发出或已接收，不能继续上传")
        if index and t[direction][index - 1]["status"] != "RECEIVED":
            error("前段尚未接收，不能上传本段凭证")
        if direction == "outbound" and not t.get("attribution"):
            error("请先由厂家售后完成处理归因")
        if not F.actor(app, user, leg["from"]):
            error("仅本段发送岗位可以上传凭证")
        return {"key": ft_scope(app, t, direction, index), "kind": kind,
                "after": _time(leg.get("ready_at")), "action": action}
    if F.get(app):
        error("本单使用厂家物流返件流程，请按对应交接段上传")
    leg_id = _integer(data.get("leg_id"), "旧机交接段")
    leg = ReplacementLeg.objects.select_related("flow__application", "from_organization", "to_organization").filter(
        pk=leg_id, flow__application=app, direction="OLD").first()
    if not leg or kind not in {"LOGISTICS", "DEVICE"}:
        error("请选择有效旧机段及物流或设备图片用途")
    if require_pending and not B.can_send(leg, user):
        error("当前账号或前段状态不允许上传本段退旧凭证")
    if not require_pending:
        # An idempotent read of one's own upload never authorizes a new send.
        sender = B._handoff_user(leg, user, leg.from_organization)
        if leg.from_organization_id == app.customer_organization_id:
            sender = user.pk == app.applicant_id
        if not sender:
            error("仅原上传的本段发送岗位可以恢复凭证")
    after = leg.flow.created_at
    previous = leg.flow.legs.filter(direction="OLD", sequence__lt=leg.sequence).order_by("-sequence").first()
    if previous and previous.received_at:
        after = max(after, previous.received_at)
    return {"key": old_scope(leg), "kind": kind, "after": after, "action": action}


def validate_type(scope, media_type):
    if scope["action"] == OLD_ACTION:
        if scope["kind"] == "DEVICE" and media_type != "IMAGE":
            error("旧机设备凭证必须为图片")
        if scope["kind"] == "LOGISTICS" and media_type not in {"IMAGE", "DOCUMENT"}:
            error("退旧物流单须为图片或单据")


def requires_scope(app, user, purpose):
    if purpose != PURPOSE:
        return False
    from lifecycle.services import factory_transport_service as F
    from lifecycle.services import replacement_flow_service as B
    t = F.get(app)
    if t:
        # Post-delivery warranty returns intentionally retain their existing
        # contract. Only the actual factory sending legs use this scope.
        return t.get("state") != "DELIVERED"
    return any(B.can_send(leg, user) for leg in ReplacementLeg.objects.filter(
        flow__application=app, direction="OLD").select_related("flow__application", "from_organization", "to_organization"))


def describe(app, user, key):
    from lifecycle.services.aftersales_service import AfterSalesService as S
    rows = app.media.filter(evidence_scope_key=key)
    count = rows.count()
    return {"evidence_scope_key": key, "evidence_count": count,
            "evidence_remaining": max(0, LIMIT - count),
            "evidence_media": [S.serialize_media(row) for row in rows.filter(uploaded_by=user)]}


def require_ids(app, user, ids, *, key, kind, after):
    if (not isinstance(ids, list) or not ids or len(ids) > LIMIT
            or any(type(value) is not int or value <= 0 for value in ids)
            or len(set(ids)) != len(ids)):
        error("请提交本段1至9个不同的有效凭证")
    rows = list(app.media.filter(pk__in=ids, purpose=PURPOSE, evidence_scope_key=key,
        evidence_kind=kind, uploaded_by=user, created_at__gte=after))
    if {row.pk for row in rows} != set(ids):
        error("凭证不属于本段、当前账号或本次交接，请重新上传")
    # Scoped sending proofs must still be real private files when custody is
    # committed. A database row alone cannot certify a missing/truncated file.
    for row in rows:
        try:
            valid = bool(row.file.name and row.file_size > 0
                and row.file.storage.exists(row.file.name)
                and row.file.storage.size(row.file.name) == row.file_size)
        except Exception:
            valid = False
        if not valid:
            error("本段凭证文件不存在、为空或大小不一致，请重新核对附件")
    return rows
