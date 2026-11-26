"""Application and decision invariants; no Web portal grant is issued here."""
import hashlib
import uuid
import warnings
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, OperationalError, transaction
from django.utils import timezone

from account.models import Organization, OrganizationRole, UserOrganization
from workflow.models import UserTask, WorkItem

from .models import RootTrialApplication, RootTrialLicense
from .private_storage import private_storage

MAX_LICENSE_BYTES = 5 * 1024 * 1024
MAX_LICENSE_PIXELS = 24_000_000
REVIEW_TASK_TYPE = "FACTORY_TRIAL_REVIEW"
RESULT_TASK_TYPE = "FACTORY_TRIAL_RESULT"


class RootTrialError(Exception):
    def __init__(self, message, code="INVALID_REQUEST", http_status=400):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


def _account(user, *, require_wechat=True):
    pk = getattr(user, "pk", None)
    current = get_user_model().objects.filter(pk=pk).first() if pk else None
    if (not current or not current.is_active
            or (not (current.wx_openid or "").strip()
                and (require_wechat or not current.is_superuser))):
        raise RootTrialError("请使用有效微信账号登录后操作。", "ACCOUNT_UNAVAILABLE", 403)
    return current


def require_superadmin(user):
    current = _account(user, require_wechat=False)
    if not current.is_superuser:
        raise RootTrialError("仅项目超级管理员可以审核申请。", "ADMIN_REQUIRED", 403)
    return current


def _eligible_account(user):
    if user.is_superuser:
        raise RootTrialError("超级管理员无需申请厂家负责人身份。", "PLATFORM_ADMIN", 403)
    if UserOrganization.objects.filter(user_id=user.pk).exists():
        raise RootTrialError(
            "当前微信账号已有或保留过组织身份，不能自助申请新的根组织。",
            "IDENTITY_EXISTS", 403,
        )


def get_own_application(user):
    current = _account(user)
    return RootTrialApplication.objects.select_related(
        "license_upload", "organization", "identity", "reviewed_by",
    ).filter(applicant_id=current.pk).first()


def eligibility(user):
    try:
        current = _account(user)
        _eligible_account(current)
    except RootTrialError as exc:
        return {
            "eligible": False, "can_submit": False,
            "reason": str(exc), "code": exc.code,
            "application_status": "", "has_application": False,
        }
    latest = get_own_application(current)
    pending = bool(latest and latest.status == RootTrialApplication.Status.PENDING)
    return {
        "eligible": True, "can_submit": not pending,
        "reason": "申请已提交，等待超级管理员审核。" if pending else "",
        "code": "APPLICATION_PENDING" if pending else "ELIGIBLE",
        "application_status": latest.status if latest else "",
        "has_application": bool(latest),
    }


def _uuid(value, label):
    if isinstance(value, bool):
        raise RootTrialError(f"{label}格式错误。")
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise RootTrialError(f"{label}格式错误。") from None


def _application_id(value):
    if isinstance(value, bool):
        raise RootTrialError("申请编号格式错误。")
    text = str(value)
    if not text.isascii() or not text.isdecimal() or len(text) > 18 or int(text) < 1:
        raise RootTrialError("申请编号格式错误。")
    return int(text)


def _image_bytes(uploaded_file):
    if not uploaded_file:
        raise RootTrialError("请上传企业营业执照图片。", "LICENSE_REQUIRED")
    if getattr(uploaded_file, "size", 0) > MAX_LICENSE_BYTES:
        raise RootTrialError("营业执照图片不能超过5MB。", "LICENSE_TOO_LARGE")
    raw = uploaded_file.read(MAX_LICENSE_BYTES + 1)
    if not raw or len(raw) > MAX_LICENSE_BYTES:
        raise RootTrialError("营业执照图片为空或超过5MB。", "LICENSE_TOO_LARGE")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as source:
                if source.format not in {"JPEG", "PNG"}:
                    raise RootTrialError("营业执照仅支持JPG或PNG图片。", "LICENSE_FORMAT")
                if source.width * source.height > MAX_LICENSE_PIXELS:
                    raise RootTrialError("营业执照图片像素过大，请压缩后上传。", "LICENSE_TOO_LARGE")
                source.verify()
            with Image.open(BytesIO(raw)) as source:
                source.load()
                image = ImageOps.exif_transpose(source)
                output = BytesIO()
                # Normalize the actual pixels; metadata and appended bytes are discarded.
                if source.format == "JPEG":
                    image.convert("RGB").save(output, format="JPEG", quality=92)
                    content_type, extension = "image/jpeg", "jpg"
                else:
                    image.convert("RGBA" if "A" in image.getbands() else "RGB").save(
                        output, format="PNG",
                    )
                    content_type, extension = "image/png", "png"
    except RootTrialError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning):
        raise RootTrialError("营业执照文件不是有效图片，请重新上传。", "LICENSE_FORMAT") from None
    data = output.getvalue()
    if len(data) > MAX_LICENSE_BYTES:
        raise RootTrialError("图片处理后超过5MB，请压缩后上传。", "LICENSE_TOO_LARGE")
    return data, content_type, extension


def create_license_upload(user, uploaded_file):
    current = _account(user)
    _eligible_account(current)
    data, content_type, extension = _image_bytes(uploaded_file)
    media_id = uuid.uuid4()
    storage = private_storage()
    storage_key = storage.save(f"licenses/{current.pk}/{media_id.hex}.{extension}", ContentFile(data))
    try:
        if not storage.exists(storage_key):
            raise RootTrialError("营业执照未保存成功，请重新上传。", "LICENSE_MISSING", 409)
        return RootTrialLicense.objects.create(
            id=media_id, owner=current, storage_key=storage_key,
            original_name=f"营业执照.{extension}", content_type=content_type,
            size=len(data), sha256=hashlib.sha256(data).hexdigest(),
        )
    except Exception:
        storage.delete(storage_key)
        raise


def get_license_for_user(user, upload_id):
    current = _account(user, require_wechat=False)
    media_id = _uuid(upload_id, "营业执照编号")
    media = RootTrialLicense.objects.filter(pk=media_id).first()
    # Only submitted documents may be read by reviewers, preventing upload enumeration.
    reviewer = current.is_superuser and media and media.applications.exists()
    if not media or not (media.owner_id == current.pk or reviewer):
        raise RootTrialError("营业执照不存在或无权查看。", "NOT_FOUND", 404)
    return media


def open_license_file(media):
    storage = private_storage()
    try:
        handle = storage.open(media.storage_key, "rb")
        data = handle.read(MAX_LICENSE_BYTES + 1)
        if (len(data) != media.size or len(data) > MAX_LICENSE_BYTES
                or hashlib.sha256(data).hexdigest() != media.sha256):
            handle.close()
            raise RootTrialError("营业执照图片已缺失或变化，请重新上传。", "LICENSE_MISSING", 409)
        handle.seek(0)
        return handle
    except (FileNotFoundError, OSError):
        raise RootTrialError("营业执照图片已缺失，请重新上传。", "LICENSE_MISSING", 409) from None


def license_payload(media):
    return {
        "id": str(media.pk), "license_upload_id": str(media.pk),
        "url": f"/pldp/api/root-trial/licenses/{media.pk}/",
    }


def get_application_for_user(user, application_id):
    current = _account(user, require_wechat=False)
    pk = _application_id(application_id)
    query = RootTrialApplication.objects.select_related(
        "license_upload", "organization", "identity", "reviewed_by",
    ).filter(pk=pk)
    if not current.is_superuser:
        query = query.filter(applicant_id=current.pk)
    application = query.first()
    if not application:
        raise RootTrialError("申请不存在或无权查看。", "NOT_FOUND", 404)
    return application


def application_payload(application, user):
    current = _account(user, require_wechat=False)
    if current.pk != application.applicant_id and not current.is_superuser:
        raise RootTrialError("申请不存在或无权查看。", "NOT_FOUND", 404)
    organization = application.organization
    return {
        "id": application.pk, "request_id": str(application.request_id),
        "applicant_user_id": application.applicant_id,
        "applicant_name": application.applicant.wx_nickname or application.applicant.username,
        "organization_name": application.organization_name,
        "status": application.status, "status_label": application.get_status_display(),
        "license_upload_id": str(application.license_upload_id),
        "license_url": f"/pldp/api/root-trial/licenses/{application.license_upload_id}/",
        "review_reason": application.review_reason,
        "created_at": application.created_at.isoformat(),
        "reviewed_at": application.reviewed_at.isoformat() if application.reviewed_at else None,
        "organization_id": application.organization_id,
        "role_code": "factory_admin" if application.status == "APPROVED" else "",
        "can_review": bool(current.is_superuser and application.status == "PENDING"),
        "can_resubmit": bool(current.pk == application.applicant_id
                             and application.status == "REJECTED"
                             and eligibility(current)["eligible"]),
        "scope": "PLATFORM",
        "root_organization": {"id": organization.pk, "name": organization.name} if organization else None,
    }


def _request_matches(application, organization_name, media_id):
    return (application.organization_name == organization_name
            and application.license_upload_id == media_id)


def _existing_request(user_id, request_id, organization_name, media_id):
    application = RootTrialApplication.objects.filter(
        applicant_id=user_id, request_id=request_id,
    ).first()
    if application and not _request_matches(application, organization_name, media_id):
        raise RootTrialError(
            "这次提交的内容已改变，请使用新的提交编号。", "REQUEST_CONTENT_CHANGED", 409,
        )
    return application


def _platform_payload(application):
    return {
        "scope": "PLATFORM", "application_id": application.pk,
        "proposed_root_organization_name": application.organization_name,
        "root_organization_id": application.organization_id,
        "factory_root_id": application.organization_id,
    }


def _publish_review_task(application):
    now = timezone.now()
    workitem = WorkItem.objects.create(
        domain="SYSTEM", type=REVIEW_TASK_TYPE, biz_id=str(application.pk),
        biz_ref=f"root_trial.RootTrialApplication:{application.pk}",
        uniq_key=f"root-trial-review:{application.pk}",
        title=f"根组织厂家负责人申请：{application.organization_name}",
        summary="请核验申请组织名称及营业执照，审核通过后建立独立根组织。",
        link=f"/pages/account/root_application_review/root_application_review?application_id={application.pk}",
        payload=_platform_payload(application), action_mode="ANYONE_DO", status="OPEN",
        actor_openid=application.applicant.wx_openid or "", last_activity_at=now,
    )
    recipients = get_user_model().objects.filter(
        is_active=True, is_superuser=True,
    ).exclude(wx_openid__isnull=True).exclude(wx_openid="").values_list("wx_openid", flat=True)
    # Platform reviews deliberately do not inherit organization-role delegation.
    UserTask.objects.bulk_create([
        UserTask(
            workitem=workitem, to_openid=openid, to_role_snapshot="platform_admin",
            need_action=True, state="NEW", cursor_time=now, badge_group="MY",
            display_title_snapshot=workitem.title,
            display_subtitle_snapshot=workitem.summary,
        ) for openid in set(recipients) if openid.strip()
    ])


def submit_application(user, organization_name, license_upload_id, request_id):
    current = _account(user)
    if not isinstance(organization_name, str):
        raise RootTrialError("请填写企业营业执照上的组织名称。")
    name = organization_name.strip()
    if not name or len(name) > 128 or any(ord(char) < 32 for char in name):
        raise RootTrialError("组织名称不能为空，不能含控制字符，且不得超过128字。")
    request_uuid = _uuid(request_id, "提交编号")
    media_id = _uuid(license_upload_id, "营业执照编号")
    try:
        with transaction.atomic():
            current = get_user_model().objects.select_for_update().get(pk=current.pk)
            _account(current)
            existing = _existing_request(current.pk, request_uuid, name, media_id)
            if existing:
                return existing, False
            _eligible_account(current)
            pending = RootTrialApplication.objects.filter(
                applicant=current, status="PENDING",
            ).first()
            if pending:
                if _request_matches(pending, name, media_id):
                    return pending, False
                raise RootTrialError("已有待审核申请，请等待审核结果。", "APPLICATION_PENDING", 409)
            media = RootTrialLicense.objects.filter(pk=media_id, owner=current).first()
            if not media:
                raise RootTrialError("请先完成本人营业执照图片上传。", "LICENSE_REQUIRED", 400)
            with open_license_file(media):
                pass
            application = RootTrialApplication.objects.create(
                applicant=current, organization_name=name, license_upload=media,
                request_id=request_uuid,
            )
            _publish_review_task(application)
            return application, True
    except IntegrityError:
        existing = _existing_request(current.pk, request_uuid, name, media_id)
        if existing:
            return existing, False
        raise RootTrialError("申请状态已变化，请刷新后重试。", "APPLICATION_CONFLICT", 409) from None
    except OperationalError as exc:
        if "locked" in str(exc).lower():
            raise RootTrialError("申请正在处理中，请稍后原样重试。", "REQUEST_BUSY", 409) from None
        raise


def _finish_review_task(application, reviewer, now):
    workitem = WorkItem.objects.filter(uniq_key=f"root-trial-review:{application.pk}").first()
    if not workitem:
        return
    workitem.status = "DONE"
    workitem.done_by_openid = reviewer.wx_openid or ""
    workitem.done_at = now
    workitem.last_activity_at = now
    workitem.payload = _platform_payload(application)
    workitem.save(update_fields=(
        "status", "done_by_openid", "done_at", "last_activity_at", "payload", "updated_at",
    ))
    tasks = UserTask.objects.filter(workitem=workitem)
    tasks.update(state="DONE", done_at=now, done_reason="OTHER_ACTION", cursor_time=now)
    tasks.filter(to_openid=reviewer.wx_openid).update(done_reason="SELF_ACTION")


def _publish_result_task(application, reviewer, now):
    approved = application.status == "APPROVED"
    workitem = WorkItem.objects.create(
        domain="SYSTEM", type=RESULT_TASK_TYPE, biz_id=str(application.pk),
        biz_ref=f"root_trial.RootTrialApplication:{application.pk}",
        uniq_key=f"root-trial-result:{application.pk}",
        title="根组织厂家负责人申请已通过" if approved else "根组织厂家负责人申请未通过",
        summary=(f"{application.organization_name}已开通，请刷新身份后进入工作台。"
                 if approved else f"{application.organization_name}：{application.review_reason}"),
        link="/pages/account/root_application/root_application",
        payload=_platform_payload(application), action_mode="EACH_ACK", status="DONE",
        actor_openid=reviewer.wx_openid or "", done_by_openid=reviewer.wx_openid or "",
        done_at=now, last_activity_at=now,
    )
    UserTask.objects.create(
        workitem=workitem, to_openid=application.applicant.wx_openid,
        to_role_snapshot="factory_admin" if approved else "tourist",
        need_action=False, state="NEW", cursor_time=now, badge_group="MY",
        display_title_snapshot=workitem.title, display_subtitle_snapshot=workitem.summary[:255],
    )


def review_application(reviewer, application_id, decision, reason=""):
    current_reviewer = require_superadmin(reviewer)
    pk = _application_id(application_id)
    if not isinstance(decision, str) or decision not in {"APPROVE", "REJECT"}:
        raise RootTrialError("请选择通过或拒绝申请。", "INVALID_DECISION")
    if not isinstance(reason, str) or len(reason.strip()) > 1000:
        raise RootTrialError("审核说明不能超过1000字。", "INVALID_REASON")
    reason = reason.strip()
    if decision == "REJECT" and not reason:
        raise RootTrialError("请填写未通过原因，以便申请人修改。", "REASON_REQUIRED")
    target_status = "APPROVED" if decision == "APPROVE" else "REJECTED"
    try:
        with transaction.atomic():
            applicant_id = RootTrialApplication.objects.filter(pk=pk).values_list("applicant_id", flat=True).first()
            if not applicant_id:
                raise RootTrialError("申请不存在。", "NOT_FOUND", 404)
            # Stable lock order also covers a reviewer who is the applicant of another request.
            locked_users = {u.pk: u for u in get_user_model().objects.select_for_update().filter(
                pk__in={applicant_id, current_reviewer.pk},
            ).order_by("pk")}
            current_reviewer = require_superadmin(locked_users.get(current_reviewer.pk))
            application = RootTrialApplication.objects.select_for_update().select_related(
                "license_upload", "organization", "identity",
            ).get(pk=pk)
            if application.status != "PENDING":
                if application.status == target_status:
                    return application, False
                raise RootTrialError("该申请已完成审核，请刷新查看最终结果。", "ALREADY_REVIEWED", 409)
            applicant = locked_users.get(applicant_id)
            now = timezone.now()
            if decision == "APPROVE":
                applicant = _account(applicant)
                _eligible_account(applicant)
                if application.license_upload.owner_id != applicant.pk:
                    raise RootTrialError("营业执照归属与申请人不一致。", "LICENSE_REQUIRED", 409)
                with open_license_file(application.license_upload):
                    pass
                role = OrganizationRole.objects.select_related("organization_type").filter(
                    organization_type__code="OWNER", organization_type__is_active=True,
                    code="factory_admin", is_owner_role=True, is_active=True,
                ).first()
                if not role:
                    raise RootTrialError("厂家负责人岗位配置不可用，请先恢复配置。", "ROLE_UNAVAILABLE", 409)
                organization = Organization.objects.create(
                    name=application.organization_name,
                    code=f"OWNER_{uuid.uuid4().hex}", organization_type=role.organization_type,
                )
                organization.full_clean()
                identity = UserOrganization(
                    user=applicant, organization=organization, role=role,
                    inviter=current_reviewer, is_active=True,
                )
                identity.full_clean()
                identity.save()
                application.organization = organization
                application.identity = identity
            application.status = target_status
            application.reviewed_by = current_reviewer
            application.reviewed_at = now
            application.review_reason = reason
            application.save(update_fields=(
                "status", "reviewed_by", "reviewed_at", "review_reason",
                "organization", "identity", "updated_at",
            ))
            _finish_review_task(application, current_reviewer, now)
            _publish_result_task(application, current_reviewer, now)
            return application, True
    except IntegrityError:
        # A competing invitation/identity creation must never leave an orphan root.
        raise RootTrialError("账号或申请状态已变化，请刷新后重试。", "APPLICATION_CONFLICT", 409) from None
    except ValidationError as exc:
        raise RootTrialError("申请未能开通，请核对组织及岗位配置。", "CONFIGURATION_CONFLICT", 409) from exc
    except OperationalError as exc:
        if "locked" in str(exc).lower():
            raise RootTrialError("申请正在审核，请稍后刷新结果。", "REQUEST_BUSY", 409) from None
        raise
