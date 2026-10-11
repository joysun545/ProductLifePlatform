import json
import re
import uuid

from django.db import IntegrityError
from django.core.files.storage import default_storage
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from account.services.auth_service import (
    ApiAuthenticationError,
    _wx_code_to_openid,
    authenticate_request,
    build_user_payload,
    get_or_create_wechat_user,
    issue_access_token,
)
from account.services.invite_service import (
    InvitationError,
    accept_staff_invitation,
    create_platform_factory_invitation,
    create_staff_invitation,
    get_available_invitation_roles,
    get_invitation_detail,
)
from account.services.subordinate_service import (
    SubordinateManagementError,
    get_direct_subordinates,
    update_direct_subordinate_after_sales_qualification,
)
from account.services.merchant_catalog_service import (
    MerchantCatalogError,
    get_merchant_catalog_options,
    save_merchant_catalog_scope,
)
from account.services.appearance_service import (
    AppearanceError,
    appearance_options,
    serialize_appearance,
    update_operator_appearance,
)
from account.services.auth_service import get_active_identity


def _request_data(request):
    content_type = (request.content_type or "").lower()
    if "application/json" in content_type and request.body:
        try:
            data = json.loads(request.body.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
    return request.POST.dict()


def _method_error(method):
    return JsonResponse({"code": 1, "msg": f"仅支持{method}"}, status=200)


def _authenticated_user(request):
    try:
        return authenticate_request(request), None
    except ApiAuthenticationError as exc:
        return None, JsonResponse(
            {"code": 401, "msg": str(exc)},
            status=401,
        )


@csrf_exempt
def login(request):
    if request.method != "POST":
        return _method_error("POST")

    code = (_request_data(request).get("code") or "").strip()
    if not code:
        return JsonResponse({"code": 1, "msg": "code 缺失"}, status=200)

    try:
        openid = _wx_code_to_openid(code)
        user = get_or_create_wechat_user(openid)
    except Exception as exc:
        return JsonResponse(
            {"code": 2, "msg": f"微信登录失败：{exc}"},
            status=200,
        )

    response_data = build_user_payload(user)
    response_data.update(
        {
            "code": 0,
            "access_token": issue_access_token(user),
        }
    )
    return JsonResponse(response_data, status=200)


def _validate_profile_data(data):
    nickname = str(data.get("nickname") or "").strip()
    phone = str(data.get("phone") or "").strip()

    if not nickname:
        return None, "微信昵称不能为空"
    if len(nickname) > 128:
        return None, "微信昵称不能超过128个字符"
    if phone and not re.fullmatch(r"\d{6,20}", phone):
        return None, "电话号码只能填写6至20位数字"

    return {"nickname": nickname, "phone": phone}, None


def _save_profile_avatar(request):
    avatar = request.FILES.get("avatar")
    if not avatar:
        return None, None

    if avatar.size > 3 * 1024 * 1024:
        return None, "头像文件不能超过3MB"

    header = avatar.read(12)
    avatar.seek(0)
    if header.startswith(b"\xff\xd8\xff"):
        suffix = ".jpg"
    elif header.startswith(b"\x89PNG\r\n\x1a\n"):
        suffix = ".png"
    elif header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        suffix = ".webp"
    else:
        return None, "头像只支持JPG、PNG或WEBP格式"

    saved_path = default_storage.save(
        f"account/avatars/{uuid.uuid4().hex}{suffix}",
        avatar,
    )
    return request.build_absolute_uri(default_storage.url(saved_path)), None


def _editable_merchant_organization(user):
    """Return the merchant organization only for its active owner account."""

    identity = get_active_identity(user)
    if (
        identity
        and identity.role.code == "merchant_owner"
        and identity.organization.organization_type.code == "MERCHANT"
    ):
        return identity.organization
    return None


def _profile_payload(user):
    payload = build_user_payload(user)
    organization = _editable_merchant_organization(user)
    payload["can_edit_organization_address"] = organization is not None
    payload["organization_address"] = organization.address if organization else ""
    return payload


@csrf_exempt
def profile(request):
    """读取或修改当前登录微信账号的可编辑资料。"""

    if request.method not in {"GET", "POST"}:
        return _method_error("GET或POST")

    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response

    if request.method == "GET":
        return JsonResponse(
            {"code": 0, "data": _profile_payload(user)},
            status=200,
        )

    data = _request_data(request)
    profile_data, validation_error = _validate_profile_data(data)
    if validation_error:
        return JsonResponse({"code": 1, "msg": validation_error}, status=200)

    organization = None
    organization_address = ""
    if "organization_address" in data:
        organization = _editable_merchant_organization(user)
        if not organization:
            return JsonResponse(
                {"code": 1, "msg": "仅商家负责人可维护本组织官方地址"},
                status=200,
            )
        organization_address = str(data.get("organization_address") or "").strip()
        if len(organization_address) > 255:
            return JsonResponse(
                {"code": 1, "msg": "组织官方地址不能超过255个字符"},
                status=200,
            )

    avatar_url, avatar_error = _save_profile_avatar(request)
    if avatar_error:
        return JsonResponse({"code": 1, "msg": avatar_error}, status=200)

    user.wx_nickname = profile_data["nickname"] or None
    user.phone = profile_data["phone"] or None
    update_fields = ["wx_nickname", "phone"]
    if avatar_url:
        user.wx_avatar = avatar_url
        update_fields.append("wx_avatar")
    user.save(update_fields=update_fields)
    if organization is not None:
        organization.address = organization_address
        organization.save(update_fields=("address", "updated_at"))

    return JsonResponse(
        {
            "code": 0,
            "msg": "账号资料已保存",
            "data": _profile_payload(user),
        },
        status=200,
    )


@csrf_exempt
def appearance(request):
    """Read the resolved theme or update the OWNER-wide selection."""

    if request.method not in {"GET", "POST"}:
        return _method_error("GET或POST")

    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response
    identity = get_active_identity(user)

    can_manage = bool(
        identity
        and identity.organization.organization_type.code == "OWNER"
        and identity.role.is_owner_role
    )
    if request.method == "GET":
        return JsonResponse(
            {
                "code": 0,
                "data": {
                    "appearance": serialize_appearance(identity),
                    "options": appearance_options(),
                    "can_manage": can_manage,
                },
            },
            status=200,
        )

    data = _request_data(request)
    try:
        selected = update_operator_appearance(
            identity=identity,
            color_scheme=data.get("color_scheme"),
            style_scheme=data.get("style_scheme"),
        )
    except AppearanceError as exc:
        return JsonResponse({"code": 1, "msg": str(exc)}, status=200)

    return JsonResponse(
        {
            "code": 0,
            "msg": "全局界面主题已更新",
            "data": {"appearance": selected, "can_manage": True},
        },
        status=200,
    )


@csrf_exempt
def invitation_roles(request):
    if request.method != "GET":
        return _method_error("GET")

    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response

    try:
        items = get_available_invitation_roles(user)
    except InvitationError as exc:
        return JsonResponse({"code": 1, "msg": str(exc)}, status=200)

    return JsonResponse({"code": 0, "data": {"items": items}}, status=200)


def subordinates(request):
    """列出当前身份亲自邀请且仍有效的直接下级账号。"""

    if request.method != "GET":
        return _method_error("GET")

    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response

    try:
        data = get_direct_subordinates(user)
    except SubordinateManagementError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)

    return JsonResponse({"code": 0, "msg": "", "data": data}, status=200)


@csrf_exempt
def update_subordinate_after_sales_qualification(request):
    if request.method != "POST":
        return _method_error("POST")

    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response

    data = _request_data(request)
    organization_id = data.get("organization_id")
    provides_after_sales = data.get("provides_after_sales")
    if type(provides_after_sales) is not bool:
        return JsonResponse(
            {"code": 1, "msg": "售后服务资格必须是布尔值"},
            status=200,
        )

    try:
        result = update_direct_subordinate_after_sales_qualification(
            user=user,
            organization_id=organization_id,
            provides_after_sales=provides_after_sales,
        )
    except SubordinateManagementError as exc:
        return JsonResponse({"code": 1, "msg": str(exc)}, status=200)

    return JsonResponse(
        {"code": 0, "msg": "售后服务资格已更新", "data": result},
        status=200,
    )


@csrf_exempt
def subordinate_merchant_catalog(request):
    if request.method not in {"GET", "POST"}:
        return _method_error("GET或POST")

    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response

    data = _request_data(request) if request.method == "POST" else request.GET
    organization_id = data.get("organization_id")
    try:
        if request.method == "GET":
            result = get_merchant_catalog_options(
                user=user,
                organization_id=organization_id,
            )
            message = "经营范围已加载"
        else:
            result = save_merchant_catalog_scope(
                user=user,
                organization_id=organization_id,
                catalog=data.get("catalog"),
            )
            message = "商家品牌、品类及型号范围已保存"
    except MerchantCatalogError as exc:
        return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)

    return JsonResponse({"code": 0, "msg": message, "data": result}, status=200)


@csrf_exempt
def create_invitation(request):
    if request.method != "POST":
        return _method_error("POST")

    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response

    data = _request_data(request)
    rule_id = data.get("rule_id")
    if not rule_id:
        return JsonResponse({"code": 1, "msg": "缺少邀请规则"}, status=200)

    try:
        result = create_staff_invitation(
            inviter=user,
            rule_id=rule_id,
            senior_manager_job_title=data.get("senior_manager_job_title", ""),
            senior_manager_managed_role_codes=data.get(
                "senior_manager_managed_role_codes", []
            ),
        )
    except (InvitationError, ValueError, TypeError) as exc:
        return JsonResponse({"code": 1, "msg": str(exc)}, status=200)

    return JsonResponse({"code": 0, "data": result}, status=200)


@csrf_exempt
def create_platform_factory_invitation_view(request):
    """Create a shareable invite for one independent factory-root owner."""
    if request.method != "POST":
        return _method_error("POST")
    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response

    data = _request_data(request)
    try:
        result = create_platform_factory_invitation(
            inviter=user,
            organization_name=data.get("organization_name", ""),
            region=data.get("region", ""),
            address=data.get("address", ""),
        )
    except (InvitationError, ValueError, TypeError) as exc:
        return JsonResponse({"code": 1, "msg": str(exc)}, status=200)
    return JsonResponse({"code": 0, "data": result}, status=200)


@csrf_exempt
def invitation_detail(request):
    if request.method != "GET":
        return _method_error("GET")

    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response

    token = (request.GET.get("token") or "").strip()
    if not token:
        return JsonResponse({"code": 1, "msg": "邀请token缺失"}, status=200)

    try:
        result = get_invitation_detail(token=token, viewer=user)
    except InvitationError as exc:
        return JsonResponse({"code": 1, "msg": str(exc)}, status=200)

    return JsonResponse({"code": 0, "data": result}, status=200)


@csrf_exempt
def accept_invitation(request):
    if request.method != "POST":
        return _method_error("POST")

    user, error_response = _authenticated_user(request)
    if error_response:
        return error_response

    data = _request_data(request)
    token = (data.get("token") or "").strip()
    if not token:
        return JsonResponse({"code": 1, "msg": "邀请token缺失"}, status=200)

    try:
        result = accept_staff_invitation(
            invitee=user,
            token=token,
            nickname=data.get("nickname", ""),
            avatar=data.get("avatar", ""),
            phone=data.get("phone", ""),
            organization_name=data.get("organization_name", ""),
            region=data.get("region", ""),
            address=data.get("address", ""),
        )
    except InvitationError as exc:
        return JsonResponse({"code": 1, "msg": str(exc)}, status=200)
    except IntegrityError:
        return JsonResponse(
            {"code": 1, "msg": "账号身份或组织关系发生冲突，请重新登录后重试"},
            status=200,
        )

    return JsonResponse(
        {"code": 0, "msg": "加入成功", "data": result},
        status=200,
    )
