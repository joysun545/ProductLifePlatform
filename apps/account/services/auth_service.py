import hashlib

import requests
from django.conf import settings
from django.core import signing
from django.core.signing import BadSignature, SignatureExpired
from django.utils.crypto import constant_time_compare, salted_hmac

from account.models import OrganizationRole, SeniorManagerProfile, User, UserOrganization
from account.services.role_delegation_service import (
    SENIOR_MANAGER_ROLE_CODES,
    delegated_role_codes,
)


ACCESS_TOKEN_SALT = "account.api-access.v1"
DEFAULT_ACCESS_TOKEN_MAX_AGE = 7 * 24 * 60 * 60
WECHAT_BINDING_SALT = "account.api-access.wechat-binding.v1"

# The approved 2026-09-15 handovers keep the database user/identity IDs
# while exchanging their WeChat bindings. Preserve the earlier four-slot guard
# and include the six slots in the subsequent child-merchant handover, the
# dedicated tourist accounts and the factory material-stock handovers.
# Legacy tokens contain only user_id, so affected IDs must sign in again;
# unrelated legacy sessions remain valid. New tokens are binding-aware.
LEGACY_TOKEN_RELOGIN_USER_IDS = frozenset({1, 2, 3, 4, 7, 8, 9, 24, 26, 27, 33, 35, 49, 51, 55, 57, 59})


class ApiAuthenticationError(Exception):
    """API访问令牌无效。"""


def _wx_code_to_openid(code: str) -> str:
    code = (code or "").strip()
    if not code:
        raise ValueError("code 缺失")

    response = requests.get(
        "https://api.weixin.qq.com/sns/jscode2session",
        params={
            "appid": settings.WECHAT_APPID,
            "secret": settings.WECHAT_SECRET,
            "js_code": code,
            "grant_type": "authorization_code",
        },
        timeout=8,
    )
    response.raise_for_status()

    data = response.json()
    openid = data.get("openid")
    if not openid:
        raise ValueError(f"微信登录失败：{data.get('errmsg') or data}")

    return openid


def get_or_create_wechat_user(openid: str) -> User:
    """根据OpenID获取或创建微信业务账号。"""

    username = "wx_" + hashlib.sha256(openid.encode("utf-8")).hexdigest()
    user, created = User.objects.get_or_create(
        wx_openid=openid,
        defaults={"username": username},
    )

    if created:
        user.set_unusable_password()
        user.save(update_fields=("password",))

    return user


def _wechat_token_binding(user: User) -> str:
    """Bind the token without exposing a raw OpenID or an unkeyed fingerprint."""

    value = f"{user.pk}:{user.wx_openid or ''}"
    return salted_hmac(
        WECHAT_BINDING_SALT, value, algorithm="sha256"
    ).hexdigest()


def issue_access_token(user: User) -> str:
    """签发可跨Gunicorn进程验证的服务端签名令牌。"""

    return signing.dumps(
        {"user_id": user.pk, "wechat_binding": _wechat_token_binding(user)},
        salt=ACCESS_TOKEN_SALT,
        compress=True,
    )


def authenticate_request(request) -> User:
    """从Authorization头中识别当前微信业务账号。"""

    authorization = (request.headers.get("Authorization") or "").strip()
    token = ""

    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()

    if not token:
        token = (
            request.POST.get("access_token")
            or request.GET.get("access_token")
            or ""
        ).strip()

    if not token:
        raise ApiAuthenticationError("登录状态缺失，请重新登录")

    max_age = getattr(
        settings,
        "API_ACCESS_TOKEN_MAX_AGE",
        DEFAULT_ACCESS_TOKEN_MAX_AGE,
    )

    try:
        payload = signing.loads(
            token,
            salt=ACCESS_TOKEN_SALT,
            max_age=max_age,
        )
    except SignatureExpired as exc:
        raise ApiAuthenticationError("登录状态已过期，请重新登录") from exc
    except BadSignature as exc:
        raise ApiAuthenticationError("登录状态无效，请重新登录") from exc

    if not isinstance(payload, dict):
        raise ApiAuthenticationError("登录状态无效，请重新登录")

    user_id = payload.get("user_id")
    # Reject bool, containers, strings and out-of-range IDs before ORM lookup.
    if type(user_id) is not int or not (0 < user_id <= 9223372036854775807):
        raise ApiAuthenticationError("登录状态无效，请重新登录")

    user = User.objects.filter(pk=user_id, is_active=True).first()
    if not user:
        raise ApiAuthenticationError("账号不存在或已停用")

    if "wechat_binding" not in payload:
        if user.pk in LEGACY_TOKEN_RELOGIN_USER_IDS:
            raise ApiAuthenticationError("微信身份已更新，请重新登录")
    else:
        binding = payload["wechat_binding"]
        if (
            not user.wx_openid
            or not isinstance(binding, str)
            or len(binding) != 64
            or not constant_time_compare(binding, _wechat_token_binding(user))
        ):
            raise ApiAuthenticationError("微信身份已更新，请重新登录")

    return user


def get_active_identity(user: User):
    """返回账号唯一且有效的组织身份；游客返回None。"""
    identity = (
        UserOrganization.objects.select_related(
            "organization",
            "organization__organization_type",
            "organization__factory_root",
            "role",
        )
        .filter(
            user=user,
            is_active=True,
            organization__is_active=True,
            organization__organization_type__is_active=True,
            role__is_active=True,
        )
        .first()
    )
    # A suspended root makes its existing business identities ineffective
    # without changing their own status or historical organization links.
    if identity:
        factory_root = identity.organization.get_factory_root()
        if not factory_root or not factory_root.is_active:
            return None
    return identity


def build_user_payload(user: User) -> dict:
    """生成登录和接受邀请后共用的账号身份数据。"""

    identity = get_active_identity(user)

    if identity:
        organization = identity.organization
        factory_root = organization.get_factory_root()
        role = identity.role
        organization_type = organization.organization_type

        role_code = role.code
        role_name = role.name
        company_name = organization.name
        region = organization.region or ""
        organization_id = organization.pk
        organization_code = organization.code
        organization_type_code = organization_type.code
        organization_type_name = organization_type.name
        factory_root_id = factory_root.pk if factory_root else None
        factory_root_name = factory_root.name if factory_root else ""
    else:
        is_platform_admin = bool(user.is_active and user.is_superuser)
        role_code = "platform_admin" if is_platform_admin else "tourist"
        role_name = "超级管理员" if is_platform_admin else "游客"
        company_name = ""
        region = ""
        organization_id = None
        organization_code = ""
        organization_type_code = ""
        organization_type_name = ""
        factory_root_id = None
        factory_root_name = ""

    manager_title = ""
    delegated_codes = []
    delegated_names = []
    if identity and role_code in SENIOR_MANAGER_ROLE_CODES:
        delegated_codes = sorted(delegated_role_codes(identity))
        manager_profile = SeniorManagerProfile.objects.filter(
            identity=identity
        ).first()
        manager_title = manager_profile.job_title if manager_profile else ""
        delegated_names = list(
            OrganizationRole.objects.filter(
                organization_type_id=identity.role.organization_type_id,
                code__in=delegated_codes,
                is_active=True,
            )
            .order_by("id")
            .values_list("name", flat=True)
        )

    from account.services.appearance_service import serialize_appearance

    return {
        "openid": user.wx_openid or "",
        "role": role_code,
        "role_name": role_name,
        "nickname": user.wx_nickname or "微信用户",
        "avatar_url": user.wx_avatar or "",
        "real_name": "",
        "phone": user.phone or "",
        "company_name": company_name,
        "organization_name": company_name,
        "region": region,
        "organization_id": organization_id,
        "factory_root_id": factory_root_id,
        "factory_root_name": factory_root_name,
        "organization_code": organization_code,
        "organization_type": organization_type_code,
        "organization_type_name": organization_type_name,
        "senior_manager_job_title": manager_title,
        "delegated_role_codes": delegated_codes,
        "delegated_role_names": delegated_names,
        "brand": "",
        "brand_id": "",
        "category_id": "",
        "is_platform_admin": bool(user.is_active and user.is_superuser),
        "appearance": serialize_appearance(identity),
    }
