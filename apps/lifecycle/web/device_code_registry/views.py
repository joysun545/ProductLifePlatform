import base64
import csv
import hashlib
from io import BytesIO, StringIO
import json
import re
import secrets
from datetime import timedelta
from zipfile import ZIP_DEFLATED, ZipFile

import qrcode
from django.db import transaction
from django.db.models import Count, Max
from django.http import HttpResponse, JsonResponse
from django.middleware.csrf import rotate_token
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt, csrf_protect, ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from account.models import User
from account.services.auth_service import (
    ApiAuthenticationError,
    authenticate_request,
    get_active_identity,
)
from lifecycle.models import (
    DeviceCodeRegistryLoginChallenge,
    PhysicalDeviceCode,
    PhysicalDeviceCodePackagingInfo,
)
from lifecycle.services.physical_device_code_service import (
    DEVICE_CODE_QR_PREFIX,
    PhysicalDeviceCodeError,
    PhysicalDeviceCodePermissionError,
    generate_device_code_batch,
    identity_can_issue_device_codes,
)


LOGIN_QR_PREFIX = "PLDP_DEVICE_CODE_LOGIN|"
LOGIN_CHALLENGE_SECONDS = 300
LOGIN_SESSION_SECONDS = 1800
LOGIN_SCOPE = "pldp.device_code_registry.v1"
GATE_SESSION_KEY = "pldp_device_code_registry_gate"
CHALLENGE_SESSION_KEY = "pldp_device_code_registry_challenge"
MAX_BATCH_SIZE = 1000


class DeviceCodeRegistryAccessError(Exception):
    pass


def _digest(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _qr_image_data_uri(value):
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=12,
        border=4,
    )
    qr.add_data(value)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _issuer_identity(user):
    identity = get_active_identity(user) if user and user.is_active else None
    if not identity_can_issue_device_codes(identity):
        raise DeviceCodeRegistryAccessError(
            "仅厂家生产经理、无生产分管高级经理时的厂家负责人，或分管生产经理的厂家高级经理可操作。"
        )
    return identity


def _gate(request):
    grant = request.session.get(GATE_SESSION_KEY) or {}
    if (
        grant.get("scope") != LOGIN_SCOPE
        or not isinstance(grant.get("expires_at"), (int, float))
        or grant["expires_at"] <= timezone.now().timestamp()
    ):
        raise DeviceCodeRegistryAccessError("登录已失效，请使用有权限的厂家角色重新扫码。")

    user = User.objects.filter(pk=grant.get("user_id"), is_active=True).first()
    identity = _issuer_identity(user)
    if (
        identity.pk != grant.get("identity_id")
        or identity.organization_id != grant.get("organization_id")
        or identity.role_id != grant.get("role_id")
    ):
        raise DeviceCodeRegistryAccessError("账号岗位或厂家组织已变更，请重新扫码登录。")
    return user, identity


def _error(message, status=400):
    return JsonResponse({"code": status, "msg": str(message), "data": {}}, status=status)


def _json_body(request):
    try:
        value = json.loads(request.body or b"{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("请求数据格式无效。") from exc
    if not isinstance(value, dict):
        raise ValueError("请求数据必须是对象。")
    return value


@never_cache
@ensure_csrf_cookie
@require_GET
def registry_page(request):
    identity = None
    try:
        _user, identity = _gate(request)
    except DeviceCodeRegistryAccessError:
        pass
    return render(request, "lifecycle/device_code_registry/index.html", {
        "authorized": identity is not None,
        "identity": identity,
        "urls": {
            "login_start": reverse("pldp:web:device_code_registry:login_start"),
            "login_status": reverse("pldp:web:device_code_registry:login_status"),
            "logout": reverse("pldp:web:device_code_registry:logout"),
            "batches": reverse("pldp:web:device_code_registry:batches"),
            "generate_batch": reverse("pldp:web:device_code_registry:generate_batch"),
            "packaging_template": reverse(
                "pldp:web:device_code_registry:save_packaging_info",
                kwargs={"batch_id": "00000000-0000-0000-0000-000000000000"},
            ),
            "qr_download_template": reverse(
                "pldp:web:device_code_registry:download_qr_package",
                kwargs={"batch_id": "00000000-0000-0000-0000-000000000000"},
            ),
            "codes_download_template": reverse(
                "pldp:web:device_code_registry:download_codes_package",
                kwargs={"batch_id": "00000000-0000-0000-0000-000000000000"},
            ),
        },
    })


@never_cache
@csrf_protect
@require_POST
def login_start(request):
    if not request.session.session_key:
        request.session.create()
    request.session.pop(GATE_SESSION_KEY, None)
    previous_id = request.session.pop(CHALLENGE_SESSION_KEY, None)
    if previous_id:
        DeviceCodeRegistryLoginChallenge.objects.filter(pk=previous_id).delete()

    token = secrets.token_hex(32)
    now = timezone.now()
    challenge = DeviceCodeRegistryLoginChallenge.objects.create(
        token_digest=_digest(token),
        session_digest=_digest(request.session.session_key),
        expires_at=now + timedelta(seconds=LOGIN_CHALLENGE_SECONDS),
    )
    request.session[CHALLENGE_SESSION_KEY] = challenge.pk
    DeviceCodeRegistryLoginChallenge.objects.filter(
        expires_at__lt=now - timedelta(days=1)
    ).delete()
    qr_text = LOGIN_QR_PREFIX + token
    return JsonResponse({"code": 0, "data": {
        "qr_text": qr_text,
        "qr_image": _qr_image_data_uri(qr_text),
        "expires_in": LOGIN_CHALLENGE_SECONDS,
    }})


@never_cache
@csrf_exempt
@require_POST
def login_approve(request):
    if not (request.headers.get("Authorization") or "").lower().startswith("bearer "):
        return _error("请先在小程序登录后扫码。", status=401)
    try:
        user = authenticate_request(request)
        identity = _issuer_identity(user)
        token = _json_body(request).get("token")
        if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token):
            raise ValueError("设备编码网页登录二维码无效。")

        now = timezone.now()
        changed = DeviceCodeRegistryLoginChallenge.objects.filter(
            token_digest=_digest(token),
            expires_at__gt=now,
            approved_at__isnull=True,
            consumed_at__isnull=True,
        ).update(
            approved_by=user,
            identity_id=identity.pk,
            organization_id=identity.organization_id,
            role_id=identity.role_id,
            approved_at=now,
        )
        if changed != 1:
            raise ValueError("登录二维码已过期或已使用，请刷新网页二维码。")
        return JsonResponse({"code": 0, "data": {
            "organization_name": identity.organization.name,
            "role_name": identity.role.name,
            "expires_in": LOGIN_SESSION_SECONDS,
        }})
    except ApiAuthenticationError as exc:
        return _error(exc, status=401)
    except DeviceCodeRegistryAccessError as exc:
        return _error(exc, status=403)
    except ValueError as exc:
        return _error(exc)


@never_cache
@csrf_protect
@require_POST
def login_status(request):
    try:
        _gate(request)
        return JsonResponse({"code": 0, "data": {"status": "ready"}})
    except DeviceCodeRegistryAccessError:
        pass

    session_key = request.session.session_key
    challenge = DeviceCodeRegistryLoginChallenge.objects.filter(
        pk=request.session.get(CHALLENGE_SESSION_KEY),
        session_digest=_digest(session_key or ""),
        expires_at__gt=timezone.now(),
        consumed_at__isnull=True,
    ).select_related("approved_by").first()
    if not challenge:
        return _error("登录二维码已过期，请刷新二维码。", status=410)
    if not challenge.approved_at:
        return JsonResponse({"code": 0, "data": {"status": "pending"}})

    try:
        identity = _issuer_identity(challenge.approved_by)
        if (
            identity.pk != challenge.identity_id
            or identity.organization_id != challenge.organization_id
            or identity.role_id != challenge.role_id
        ):
            raise DeviceCodeRegistryAccessError("扫码后账号岗位或组织发生变更，请重新扫码。")

        now = timezone.now()
        changed = DeviceCodeRegistryLoginChallenge.objects.filter(
            pk=challenge.pk,
            consumed_at__isnull=True,
            expires_at__gt=now,
        ).update(consumed_at=now)
        if changed != 1:
            raise DeviceCodeRegistryAccessError("登录二维码已使用，请重新扫码。")

        request.session.cycle_key()
        rotate_token(request)
        request.session[GATE_SESSION_KEY] = {
            "scope": LOGIN_SCOPE,
            "user_id": identity.user_id,
            "identity_id": identity.pk,
            "organization_id": identity.organization_id,
            "role_id": identity.role_id,
            "expires_at": (now + timedelta(seconds=LOGIN_SESSION_SECONDS)).timestamp(),
        }
        request.session.pop(CHALLENGE_SESSION_KEY, None)
        return JsonResponse({"code": 0, "data": {"status": "ready"}})
    except DeviceCodeRegistryAccessError as exc:
        return _error(exc, status=403)


@never_cache
@csrf_protect
@require_POST
def logout(request):
    request.session.pop(GATE_SESSION_KEY, None)
    request.session.pop(CHALLENGE_SESSION_KEY, None)
    request.session.cycle_key()
    return JsonResponse({"code": 0, "data": {}})


@never_cache
@csrf_protect
@require_GET
def list_batches(request):
    try:
        _user, identity = _gate(request)
        grouped = list(
            PhysicalDeviceCode.objects.filter(factory_root_id=identity.organization_id)
            .exclude(generation_batch_id__isnull=True)
            .values("generation_batch_id")
            .annotate(code_count=Count("id"), generated_at=Max("generated_at"))
            .order_by("-generated_at")[:50]
        )
        batch_ids = [row["generation_batch_id"] for row in grouped]
        metadata = {
            row.generation_batch_id: row
            for row in PhysicalDeviceCodePackagingInfo.objects.filter(
                factory_root_id=identity.organization_id,
                generation_batch_id__in=batch_ids,
            )
        }
        batches = []
        for row in grouped:
            batch_id = row["generation_batch_id"]
            info = metadata.get(batch_id)
            batches.append({
                "generation_batch_id": str(batch_id),
                "code_count": row["code_count"],
                "generated_at": row["generated_at"].isoformat() if row["generated_at"] else "",
                "packaging_info": ({
                    "batch_name": info.batch_name,
                    "maker_name": info.maker_name,
                    "label_spec": info.label_spec,
                    "notes": info.notes,
                    "updated_at": info.updated_at.isoformat(),
                } if info else None),
            })
        return JsonResponse({"code": 0, "data": {"batches": batches}})
    except DeviceCodeRegistryAccessError as exc:
        return _error(exc, status=403)


@never_cache
@csrf_protect
@require_POST
def generate_batch(request):
    try:
        user, _identity = _gate(request)
        data = _json_body(request)
        quantity = data.get("quantity")
        if isinstance(quantity, bool) or not isinstance(quantity, int):
            raise ValueError("数量必须是整数。")
        if quantity < 1 or quantity > MAX_BATCH_SIZE:
            raise ValueError("单次生成数量须为1至1000个。")

        result = generate_device_code_batch(user=user, quantity=quantity)
        result["codes"] = result["codes"][:30]
        result["codes_preview_truncated"] = quantity > 30
        return JsonResponse({"code": 0, "data": result}, status=201)
    except DeviceCodeRegistryAccessError as exc:
        return _error(exc, status=403)
    except PhysicalDeviceCodePermissionError as exc:
        return _error(exc, status=403)
    except PhysicalDeviceCodeError as exc:
        return _error(exc, status=409)
    except ValueError as exc:
        return _error(exc)


@never_cache
@csrf_protect
@require_POST
def save_packaging_info(request, batch_id):
    try:
        user, identity = _gate(request)
        data = _json_body(request)
        batch_name = str(data.get("batch_name") or "").strip()
        maker_name = str(data.get("maker_name") or "").strip()
        label_spec = str(data.get("label_spec") or "").strip()
        notes = str(data.get("notes") or "").strip()
        if not batch_name:
            raise ValueError("请填写打包批次名称。")
        if len(batch_name) > 120 or len(maker_name) > 120:
            raise ValueError("批次名称和制作单位不得超过120个字符。")
        if len(label_spec) > 4000 or len(notes) > 4000:
            raise ValueError("制作规格和备注不得超过4000个字符。")
        if not PhysicalDeviceCode.objects.filter(
            factory_root_id=identity.organization_id,
            generation_batch_id=batch_id,
        ).exists():
            raise ValueError("该批次不属于当前厂家，或批次不存在。")

        with transaction.atomic():
            info, created = PhysicalDeviceCodePackagingInfo.objects.get_or_create(
                generation_batch_id=batch_id,
                defaults={
                    "factory_root_id": identity.organization_id,
                    "batch_name": batch_name,
                    "maker_name": maker_name,
                    "label_spec": label_spec,
                    "notes": notes,
                    "created_by": user,
                    "updated_by": user,
                },
            )
            if not created:
                if info.factory_root_id != identity.organization_id:
                    raise ValueError("该打包批次不属于当前厂家。")
                info.batch_name = batch_name
                info.maker_name = maker_name
                info.label_spec = label_spec
                info.notes = notes
                info.updated_by = user
                info.save(update_fields=(
                    "batch_name", "maker_name", "label_spec", "notes", "updated_by", "updated_at",
                ))
        return JsonResponse({"code": 0, "data": {
            "generation_batch_id": str(batch_id),
            "batch_name": info.batch_name,
            "maker_name": info.maker_name,
            "label_spec": info.label_spec,
            "notes": info.notes,
        }})
    except DeviceCodeRegistryAccessError as exc:
        return _error(exc, status=403)
    except ValueError as exc:
        return _error(exc)


def _download_context(batch_id, identity):
    info = PhysicalDeviceCodePackagingInfo.objects.filter(
        generation_batch_id=batch_id,
        factory_root_id=identity.organization_id,
    ).first()
    if not info:
        raise ValueError("请先录入并保存该批次的第三方制作信息。")
    codes = list(
        PhysicalDeviceCode.objects.filter(
            generation_batch_id=batch_id,
            factory_root_id=identity.organization_id,
        ).exclude(status=PhysicalDeviceCode.Status.DISABLED).order_by("generated_at", "id")
    )
    if not codes:
        raise ValueError("该批次没有可交付的设备编码。")
    return info, codes


def _manifest(info, codes):
    return {
        "批次名称": info.batch_name,
        "第三方制作单位": info.maker_name,
        "制作规格": info.label_spec,
        "制作备注": info.notes,
        "设备编码数量": len(codes),
        "厂家组织": info.factory_root.name,
        "编码批次ID": str(info.generation_batch_id),
        "生成时间": info.created_at.isoformat(),
        "信息更新时间": info.updated_at.isoformat(),
    }


def _csv_text(rows, *, qr_payload=False):
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["设备编码", "二维码内容"] if qr_payload else ["设备编码"])
    for row in rows:
        if qr_payload:
            writer.writerow([row.code, row.qr_payload])
        else:
            writer.writerow([row.code])
    return "\ufeff" + output.getvalue()


def _zip_response(filename, builder):
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        builder(archive)
    response = HttpResponse(buffer.getvalue(), content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Cache-Control"] = "private, no-store"
    return response


@never_cache
@csrf_protect
@require_GET
def download_qr_package(request, batch_id):
    try:
        _user, identity = _gate(request)
        info, codes = _download_context(batch_id, identity)
        manifest = json.dumps(_manifest(info, codes), ensure_ascii=False, indent=2)

        def build(archive):
            archive.writestr("打包信息.json", manifest)
            archive.writestr("设备编码对应表.csv", _csv_text(codes, qr_payload=True))
            for row in codes:
                qr = qrcode.QRCode(
                    version=None,
                    error_correction=qrcode.constants.ERROR_CORRECT_M,
                    box_size=20,
                    border=4,
                )
                qr.add_data(row.qr_payload)
                qr.make(fit=True)
                image = qr.make_image(fill_color="black", back_color="white")
                image_buffer = BytesIO()
                image.save(image_buffer, format="PNG")
                archive.writestr(f"二维码图片/{row.code}.png", image_buffer.getvalue())

        return _zip_response(f"device_codes_{batch_id}_qr_images.zip", build)
    except DeviceCodeRegistryAccessError as exc:
        return _error(exc, status=403)
    except ValueError as exc:
        return _error(exc, status=409)


@never_cache
@csrf_protect
@require_GET
def download_codes_package(request, batch_id):
    try:
        _user, identity = _gate(request)
        _info, codes = _download_context(batch_id, identity)

        def build(archive):
            archive.writestr("设备编码.txt", "\n".join(row.code for row in codes) + "\n")
            archive.writestr("设备编码.csv", _csv_text(codes, qr_payload=False))

        return _zip_response(f"device_codes_{batch_id}_plain_codes.zip", build)
    except DeviceCodeRegistryAccessError as exc:
        return _error(exc, status=403)
    except ValueError as exc:
        return _error(exc, status=409)
