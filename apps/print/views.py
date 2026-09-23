"""V2 shipment printing with a dedicated, scoped WeChat approval session."""
import base64
import hashlib
from io import BytesIO
import json
import re
import secrets
from datetime import timedelta

import qrcode
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt, csrf_protect, ensure_csrf_cookie
from django.middleware.csrf import rotate_token
from django.views.decorators.http import require_GET, require_POST

from account.models import User
from account.services.auth_service import (
    ApiAuthenticationError, authenticate_request, get_active_identity,
)
from print.models import PrintLoginChallenge
from sales.models import OrderPlanShipment
from sales.services.order_plan_material_flow_service import (
    OrderPlanMaterialFlowError,
    confirm_material_flow_shipment_qrcode_print,
    prepare_material_flow_shipment_qrcodes,
)

CHALLENGE_SECONDS = 300
SESSION_SECONDS = 1800
SESSION_SCOPE = "pldp.print.shipment.v2"
GATE_SESSION_KEY = "pldp_print_gate"
CHALLENGE_SESSION_KEY = "pldp_print_challenge"
PREPARED_SESSION_KEY = "pldp_print_prepared"


class PrintAccessError(Exception):
    pass


def _qr_image(value):
    buffer = BytesIO()
    qrcode.make(value).save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _digest(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _stock_identity(user):
    identity = get_active_identity(user) if user and user.is_active else None
    if (not identity or identity.role.code != "factory_stock"
            or identity.organization.organization_type.code != "OWNER"
            or identity.role.organization_type_id != identity.organization.organization_type_id):
        raise PrintAccessError("仅当前厂家成品库管账号可授权和操作打印中心。")
    return identity


def _gate(request):
    grant = request.session.get(GATE_SESSION_KEY) or {}
    if (grant.get("scope") != SESSION_SCOPE
            or not isinstance(grant.get("expires_at"), (int, float))
            or grant["expires_at"] <= timezone.now().timestamp()):
        raise PrintAccessError("打印登录已失效，请由厂家成品库管重新扫码授权。")
    user = User.objects.filter(pk=grant.get("user_id"), is_active=True).first()
    identity = _stock_identity(user)
    if (identity.pk != grant.get("identity_id")
            or identity.organization_id != grant.get("organization_id")
            or identity.role_id != grant.get("role_id")):
        raise PrintAccessError("账号组织或岗位已变更，请重新扫码授权。")
    return user, identity


def _error(exc, *, status=400):
    return JsonResponse({"code": status, "msg": str(exc), "data": {}}, status=status)


def _payload(request):
    try:
        data = json.loads(request.body or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OrderPlanMaterialFlowError("请求数据格式错误。") from exc
    if not isinstance(data, dict):
        raise OrderPlanMaterialFlowError("请求数据必须是对象。")
    return data


def _shipment(request, identity):
    data = _payload(request)
    shipment_id = data.get("shipment_id")
    if isinstance(shipment_id, bool) or not str(shipment_id or "").isdigit():
        raise OrderPlanMaterialFlowError("发货批次编号无效。")
    shipment = OrderPlanShipment.objects.select_related("order_plan").filter(
        pk=int(shipment_id), order_plan__factory_organization_id=identity.organization_id,
    ).first()
    if not shipment:
        raise OrderPlanMaterialFlowError("未找到本厂家发货批次。")
    return shipment


@never_cache
@ensure_csrf_cookie
@require_GET
def print_center(request):
    try:
        user, identity = _gate(request)
    except PrintAccessError:
        return render(request, "print/login.html")
    shipments = OrderPlanShipment.objects.select_related(
        "order_plan__merchant_organization", "production_subplan",
    ).filter(
        order_plan__factory_organization_id=identity.organization_id,
        status=OrderPlanShipment.Status.AUTHORIZED,
        qrcode_printed_at__isnull=True,
    ).order_by("order_plan_id", "sequence_no")
    return render(request, "print/print_center.html", {
        "shipments": shipments, "identity": identity,
        "grant_expires_at": request.session[GATE_SESSION_KEY]["expires_at"],
    })


@never_cache
@csrf_protect
@require_POST
def login_start(request):
    # The QR holds no user credentials. Only its originating browser can consume it.
    if not request.session.session_key:
        request.session.create()
    request.session.pop(GATE_SESSION_KEY, None)
    request.session.pop(PREPARED_SESSION_KEY, None)
    previous = request.session.pop(CHALLENGE_SESSION_KEY, None)
    if previous:
        PrintLoginChallenge.objects.filter(pk=previous).delete()
    token = secrets.token_hex(32)
    now = timezone.now()
    challenge = PrintLoginChallenge.objects.create(
        token_digest=_digest(token), session_digest=_digest(request.session.session_key),
        expires_at=now + timedelta(seconds=CHALLENGE_SECONDS),
    )
    PrintLoginChallenge.objects.filter(expires_at__lt=now-timedelta(days=1)).delete()
    request.session[CHALLENGE_SESSION_KEY] = challenge.pk
    return JsonResponse({"code": 0, "data": {
        "qr_text": "PLDP_PRINT_LOGIN|" + token,
        "qr_image": _qr_image("PLDP_PRINT_LOGIN|" + token),
        "expires_in": CHALLENGE_SECONDS,
    }})


@never_cache
@csrf_exempt
@require_POST
def login_approve(request):
    # This one endpoint is for the mini-program's Bearer token, never cookie auth.
    if not (request.headers.get("Authorization") or "").lower().startswith("bearer "):
        return _error("请先在小程序登录后扫码。", status=401)
    try:
        user = authenticate_request(request)
        identity = _stock_identity(user)
        token = _payload(request).get("token")
        if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token):
            raise OrderPlanMaterialFlowError("打印登录二维码无效。")
        now = timezone.now()
        # Conditional UPDATE is atomic even with multiple Gunicorn workers / SQLite.
        changed = PrintLoginChallenge.objects.filter(
            token_digest=_digest(token), expires_at__gt=now,
            approved_at__isnull=True, consumed_at__isnull=True,
        ).update(
            approved_by=user, identity_id=identity.pk, organization_id=identity.organization_id,
            role_id=identity.role_id, approved_at=now,
        )
        if changed != 1:
            raise OrderPlanMaterialFlowError("打印登录二维码已过期或已使用，请在电脑刷新二维码。")
        return JsonResponse({"code": 0, "data": {
            "organization_name": identity.organization.name,
            "role_name": identity.role.name, "expires_in": SESSION_SECONDS,
        }})
    except ApiAuthenticationError as exc:
        return _error(exc, status=401)
    except PrintAccessError as exc:
        return _error(exc, status=403)
    except OrderPlanMaterialFlowError as exc:
        return _error(exc)


@never_cache
@csrf_protect
@require_POST
def login_status(request):
    try:
        _gate(request)
        return JsonResponse({"code": 0, "data": {"status": "ready"}})
    except PrintAccessError:
        pass
    now = timezone.now()
    challenge = PrintLoginChallenge.objects.filter(
        pk=request.session.get(CHALLENGE_SESSION_KEY),
        session_digest=_digest(request.session.session_key), expires_at__gt=now,
        consumed_at__isnull=True,
    ).select_related("approved_by").first()
    if not challenge:
        return _error("打印登录二维码已过期，请刷新二维码。")
    if not challenge.approved_at:
        return JsonResponse({"code": 0, "data": {"status": "pending"}})
    try:
        identity = _stock_identity(challenge.approved_by)
        if (identity.pk != challenge.identity_id or identity.organization_id != challenge.organization_id
                or identity.role_id != challenge.role_id):
            raise PrintAccessError("扫码后账号岗位发生变更，请重新扫码。")
        changed = PrintLoginChallenge.objects.filter(
            pk=challenge.pk, consumed_at__isnull=True, expires_at__gt=now,
        ).update(consumed_at=now)
        if changed != 1:
            raise PrintAccessError("打印登录二维码已使用，请重新扫码。")
        request.session.cycle_key()
        rotate_token(request)
        request.session[GATE_SESSION_KEY] = {
            "scope": SESSION_SCOPE, "user_id": identity.user_id, "identity_id": identity.pk,
            "organization_id": identity.organization_id, "role_id": identity.role_id,
            "expires_at": (now + timedelta(seconds=SESSION_SECONDS)).timestamp(),
        }
        request.session.pop(CHALLENGE_SESSION_KEY, None)
        request.session.pop(PREPARED_SESSION_KEY, None)
        return JsonResponse({"code": 0, "data": {"status": "ready"}})
    except PrintAccessError as exc:
        return _error(exc, status=403)


@never_cache
@csrf_protect
@require_POST
def logout(request):
    for key in (GATE_SESSION_KEY, CHALLENGE_SESSION_KEY, PREPARED_SESSION_KEY):
        request.session.pop(key, None)
    request.session.cycle_key()
    return JsonResponse({"code": 0, "data": {}})


@never_cache
@csrf_protect
@require_POST
def prepare_qrcodes(request):
    try:
        user, identity = _gate(request)
        shipment = _shipment(request, identity)
        result = prepare_material_flow_shipment_qrcodes(
            user=user, plan_id=shipment.order_plan_id, shipment_id=shipment.pk,
        )
        for entry in result["qrcodes"]:
            entry["qr_image"] = _qr_image(entry["code"])
        # Confirmation from this browser requires an actual list preparation.
        prepared = request.session.get(PREPARED_SESSION_KEY, [])
        request.session[PREPARED_SESSION_KEY] = (
            [pk for pk in prepared if pk != shipment.pk] + [shipment.pk]
        )[-100:]
        return JsonResponse({"code": 0, "data": result})
    except PrintAccessError as exc:
        return _error(exc, status=403)
    except (ValueError, TypeError, OrderPlanMaterialFlowError) as exc:
        return _error(exc)


@never_cache
@csrf_protect
@require_POST
def confirm_print(request):
    try:
        user, identity = _gate(request)
        shipment = _shipment(request, identity)
        if shipment.pk not in request.session.get(PREPARED_SESSION_KEY, []):
            raise OrderPlanMaterialFlowError("请先在当前打印中心生成本批清单并实际打印贴包。")
        result, changed = confirm_material_flow_shipment_qrcode_print(
            user=user, plan_id=shipment.order_plan_id, shipment_id=shipment.pk,
        )
        request.session[PREPARED_SESSION_KEY] = [
            pk for pk in request.session.get(PREPARED_SESSION_KEY, []) if pk != shipment.pk
        ]
        return JsonResponse({"code": 0, "data": result})
    except PrintAccessError as exc:
        return _error(exc, status=403)
    except (ValueError, TypeError, OrderPlanMaterialFlowError) as exc:
        return _error(exc)
