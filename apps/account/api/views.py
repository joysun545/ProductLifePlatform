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
    create_staff_invitation,
    get_available_invitation_roles,
    get_invitation_detail,
)
from account.services.subordinate_service import (
    SubordinateManagementError,
    get_direct_subordinates,
)


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
            {"code": 0, "data": build_user_payload(user)},
            status=200,
        )

    profile_data, validation_error = _validate_profile_data(_request_data(request))
    if validation_error:
        return JsonResponse({"code": 1, "msg": validation_error}, status=200)

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

    return JsonResponse(
        {
            "code": 0,
            "msg": "账号资料已保存",
            "data": build_user_payload(user),
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
        result = create_staff_invitation(inviter=user, rule_id=rule_id)
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
