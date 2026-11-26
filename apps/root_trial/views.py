"""Bearer-only trial APIs and the narrowly scoped visitor QR handoff.

A trial request never approves a WebLoginChallenge or creates a browser grant.
Existing business users still pass through the original Web login implementation.
"""
import json
import re
from functools import wraps

from django.http import FileResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from account.services.auth_service import ApiAuthenticationError, authenticate_request
from web_portal import views as portal_views
from web_portal.models import WebLoginChallenge

from .models import RootTrialApplication
from .services import (
    RootTrialError,
    application_payload,
    create_license_upload,
    eligibility,
    get_application_for_user,
    get_license_for_user,
    get_own_application,
    license_payload,
    open_license_file,
    require_superadmin,
    review_application,
    submit_application,
)

MAX_ID = 9223372036854775807


def response(data=None, msg="", code=0, http=200):
    result = JsonResponse({"code": code, "msg": str(msg), "data": data if data is not None else {}}, status=http)
    result["Cache-Control"] = "no-store, private"
    return result


def error(message, status=400, error_code=""):
    return response({"error_code": error_code} if error_code else {}, message, code=status, http=status)


def _bearer_user(request):
    # authenticate_request also accepts query/form tokens for older APIs. These
    # endpoints deliberately require a header so credentials never enter URLs.
    if not (request.headers.get("Authorization") or "").strip().lower().startswith("bearer "):
        raise ApiAuthenticationError("请先在小程序中使用微信登录。")
    return authenticate_request(request)


def _body(request):
    try:
        value = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError) as exc:
        raise RootTrialError("请求格式错误。") from exc
    if not isinstance(value, dict):
        raise RootTrialError("请求格式错误。")
    return value


def _object_id(value):
    if type(value) is not int or not 0 < value <= MAX_ID:
        raise RootTrialError("记录编号无效。")
    return value


def api_errors(view):
    @wraps(view)
    def guarded(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except ApiAuthenticationError as exc:
            return error(exc, 401)
        except RootTrialError as exc:
            return error(exc, getattr(exc, "http_status", 400), getattr(exc, "code", ""))
    return guarded


@never_cache
@csrf_exempt
@require_POST
@api_errors
def approve_web_login(request):
    """Offer eligible visitors an application without weakening Web login."""
    user = _bearer_user(request)
    candidate = eligibility(user)
    if not candidate.get("eligible"):
        # Keep the original role matrix, WeChat binding, challenge consumption
        # and browser-session checks for every existing business identity.
        return portal_views.approve(request)
    token = _body(request).get("token")
    if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{64}", token):
        return error("登录二维码无效。")
    challenge = WebLoginChallenge.objects.filter(
        token_digest=portal_views.digest(token),
        expires_at__gt=timezone.now(),
        approved_at__isnull=True,
        consumed_at__isnull=True,
    ).first()
    if challenge is None:
        return error("二维码已过期或已授权，请在电脑端刷新。", 410)
    application = get_own_application(user)
    return response({
        "status": "trial_required",
        "trial_required": True,
        "application_path": "/pages/account/root_application/root_application?webToken=" + token,
        "eligibility": candidate,
        "application": application_payload(application, user) if application else None,
        "msg": "当前微信账号尚未加入组织，请提交根组织厂家负责人试用申请，审核通过后再确认电脑登录。",
    })


@never_cache
@require_GET
@api_errors
def context(request):
    user = _bearer_user(request)
    application = get_own_application(user)
    return response({
        "eligibility": eligibility(user),
        "application": application_payload(application, user) if application else None,
    })


@never_cache
@csrf_exempt
@require_POST
@api_errors
def upload_license(request):
    user = _bearer_user(request)
    files = request.FILES.getlist("file")
    if len(files) != 1:
        raise RootTrialError("请选择一张企业营业执照图片。")
    uploaded = create_license_upload(user, files[0])
    return response(license_payload(uploaded))


@never_cache
@csrf_exempt
@require_POST
@api_errors
def submit(request):
    user = _bearer_user(request)
    data = _body(request)
    upload_id = data.get("license_upload_id")
    application, created = submit_application(
        user,
        data.get("organization_name"),
        upload_id,
        data.get("request_id"),
    )
    return response({"application": application_payload(application, user), "created": created})


@never_cache
@require_GET
@api_errors
def applications(request):
    user = _bearer_user(request)
    require_superadmin(user)
    status = request.GET.get("status", "PENDING")
    if status not in ("PENDING", "APPROVED", "REJECTED", "ALL"):
        raise RootTrialError("申请状态无效。")
    # Cursor pagination keeps the review list bounded and stable as new requests
    # arrive; only small numeric identifiers are accepted before ORM lookup.
    try:
        limit = int(request.GET.get("limit", "50"))
    except (TypeError, ValueError) as exc:
        raise RootTrialError("分页参数无效。") from exc
    if not 1 <= limit <= 100:
        raise RootTrialError("分页数量应为1至100。")
    query = RootTrialApplication.objects.all()
    if status != "ALL":
        query = query.filter(status=status)
    before = request.GET.get("before_id")
    if before is not None:
        if not re.fullmatch(r"[1-9][0-9]{0,18}", before):
            raise RootTrialError("分页游标无效。")
        query = query.filter(pk__lt=_object_id(int(before)))
    rows = list(query.order_by("-pk")[:limit + 1])
    more = len(rows) > limit
    rows = rows[:limit]
    return response({
        "applications": [application_payload(row, user) for row in rows],
        "has_more": more,
        "next_before_id": rows[-1].pk if more and rows else None,
    })


@never_cache
@csrf_exempt
@require_http_methods(["GET", "POST"])
def application_collection(request):
    return submit(request) if request.method == "POST" else applications(request)


@never_cache
@require_GET
@api_errors
def detail(request, application_id):
    user = _bearer_user(request)
    application = get_application_for_user(user, _object_id(application_id))
    return response({"application": application_payload(application, user)})


@never_cache
@csrf_exempt
@require_POST
@api_errors
def review(request, application_id):
    user = _bearer_user(request)
    require_superadmin(user)
    data = _body(request)
    application, changed = review_application(
        user,
        _object_id(application_id),
        data.get("decision"),
        data.get("reason", ""),
    )
    return response({"application": application_payload(application, user), "changed": changed})


@never_cache
@require_GET
@api_errors
def license_file(request, upload_id):
    user = _bearer_user(request)
    uploaded = get_license_for_user(user, upload_id)
    handle = open_license_file(uploaded)
    result = FileResponse(handle, content_type=uploaded.content_type, as_attachment=True,
                          filename="business-license-" + str(uploaded.pk) + "." + (
                              "png" if uploaded.content_type == "image/png" else "jpg"))
    result["Cache-Control"] = "no-store, private"
    result["X-Content-Type-Options"] = "nosniff"
    result["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return result
