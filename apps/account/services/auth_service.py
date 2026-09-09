import hashlib

import requests
from django.conf import settings
from django.core import signing
from django.core.signing import BadSignature, SignatureExpired

from account.models import User, UserOrganization


ACCESS_TOKEN_SALT = "account.api-access.v1"
DEFAULT_ACCESS_TOKEN_MAX_AGE = 7 * 24 * 60 * 60


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


def issue_access_token(user: User) -> str:
    """签发可跨Gunicorn进程验证的服务端签名令牌。"""

    return signing.dumps(
        {"user_id": user.pk},
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

    user_id = payload.get("user_id")
    user = User.objects.filter(pk=user_id, is_active=True).first()
    if not user:
        raise ApiAuthenticationError("账号不存在或已停用")

    return user


def get_active_identity(user: User):
    """返回账号唯一且有效的组织身份；游客返回None。"""

    return (
        UserOrganization.objects.select_related(
            "organization",
            "organization__organization_type",
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


def build_user_payload(user: User) -> dict:
    """生成登录和接受邀请后共用的账号身份数据。"""

    identity = get_active_identity(user)

    if identity:
        organization = identity.organization
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
    else:
        role_code = "tourist"
        role_name = "游客"
        company_name = ""
        region = ""
        organization_id = None
        organization_code = ""
        organization_type_code = ""
        organization_type_name = ""

    return {
        "openid": user.wx_openid or "",
        "role": role_code,
        "role_name": role_name,
        "nickname": user.wx_nickname or "微信用户",
        "avatar_url": user.wx_avatar or "",
        "real_name": "",
        "phone": user.phone or "",
        "company_name": company_name,
        "region": region,
        "organization_id": organization_id,
        "organization_code": organization_code,
        "organization_type": organization_type_code,
        "organization_type_name": organization_type_name,
        "brand": "",
        "brand_id": "",
        "category_id": "",
    }
