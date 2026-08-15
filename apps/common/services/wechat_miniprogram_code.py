"""调用微信服务端接口生成可从微信扫一扫直达的小程序码。"""

import hashlib

import requests
from django.conf import settings
from django.core.cache import cache


ACCESS_TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/token"
UNLIMITED_CODE_URL = "https://api.weixin.qq.com/wxa/getwxacodeunlimit"
ACCESS_TOKEN_CACHE_PREFIX = "pldp:wechat:access-token"


class WeChatMiniProgramCodeError(Exception):
    """微信小程序码生成失败。"""


def _setting(name, default=""):
    return str(getattr(settings, name, default) or "").strip()


def _access_token(*, force_refresh=False):
    appid = _setting("WECHAT_APPID")
    secret = _setting("WECHAT_SECRET")
    if not appid or not secret:
        raise WeChatMiniProgramCodeError("微信小程序 AppID 或 Secret 尚未配置")

    appid_digest = hashlib.sha256(appid.encode("utf-8")).hexdigest()[:16]
    cache_key = f"{ACCESS_TOKEN_CACHE_PREFIX}:{appid_digest}"
    if force_refresh:
        cache.delete(cache_key)
    cached_token = cache.get(cache_key)
    if cached_token:
        return cached_token

    try:
        response = requests.get(
            ACCESS_TOKEN_URL,
            params={
                "grant_type": "client_credential",
                "appid": appid,
                "secret": secret,
            },
            timeout=8,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise WeChatMiniProgramCodeError("获取微信接口令牌失败") from exc

    token = data.get("access_token")
    if not token:
        message = data.get("errmsg") or "微信接口未返回 access_token"
        raise WeChatMiniProgramCodeError(f"获取微信接口令牌失败：{message}")

    expires_in = int(data.get("expires_in") or 7200)
    cache.set(cache_key, token, timeout=max(60, expires_in - 300))
    return token


def generate_unlimited_miniprogram_code(
    *,
    scene,
    page,
    width=430,
):
    """返回 ``(图片字节, Content-Type)``。

    ``scene`` 只放 32 位邀请 UUID 十六进制值，具体业务数据仍由后端查询。
    开发阶段默认打开体验版；正式发布前把
    ``WECHAT_MINIPROGRAM_ENV_VERSION`` 配置为 ``release``。
    """

    scene = str(scene or "").strip()
    page = str(page or "").strip().lstrip("/")
    if not scene or len(scene) > 32:
        raise WeChatMiniProgramCodeError("小程序码 scene 必须为 1 至 32 个字符")
    if not page:
        raise WeChatMiniProgramCodeError("小程序码目标页面不能为空")

    env_version = _setting("WECHAT_MINIPROGRAM_ENV_VERSION", "trial").lower()
    if env_version not in {"develop", "trial", "release"}:
        raise WeChatMiniProgramCodeError(
            "WECHAT_MINIPROGRAM_ENV_VERSION 配置不正确"
        )

    payload = {
        "scene": scene,
        "page": page,
        "check_path": env_version == "release",
        "env_version": env_version,
        "width": int(width),
    }
    response = None
    for attempt in range(2):
        try:
            response = requests.post(
                UNLIMITED_CODE_URL,
                params={
                    "access_token": _access_token(force_refresh=attempt > 0)
                },
                json=payload,
                timeout=12,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise WeChatMiniProgramCodeError("请求微信小程序码接口失败") from exc

        content_type = (response.headers.get("Content-Type") or "").lower()
        if "json" not in content_type and not response.content.lstrip().startswith(b"{"):
            break

        try:
            data = response.json()
        except ValueError:
            data = {}
        if attempt == 0 and data.get("errcode") in {40001, 40014, 42001}:
            continue
        message = data.get("errmsg") or "微信接口未返回小程序码图片"
        raise WeChatMiniProgramCodeError(f"生成微信小程序码失败：{message}")

    if response is None or not response.content:
        raise WeChatMiniProgramCodeError("微信接口返回了空的小程序码图片")

    image_type = response.headers.get("Content-Type") or "image/jpeg"
    return response.content, image_type.split(";", 1)[0]
