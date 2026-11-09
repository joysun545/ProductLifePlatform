"""Private recall attachments and proof of a successful upload."""
from pathlib import Path
from stat import S_ISREG
from uuid import uuid4

from django.conf import settings
from django.core import signing
from django.core.exceptions import SuspiciousFileOperation
from django.core.files.storage import FileSystemStorage
from django.utils import timezone

from .recall_service import QualityRecallError


UPLOAD_RECEIPT_SALT = "quality-recall-evidence-upload"


def _storage():
    return FileSystemStorage(
        location=getattr(
            settings, "QUALITY_RECALL_PRIVATE_ROOT",
            settings.BASE_DIR / "private_quality_recall",
        ),
        base_url=None,
    )


def save_recall_evidence(*, uploaded, content_type, identity):
    if uploaded.size <= 0:
        raise QualityRecallError("召回凭证文件不能为空，请重新上传。")
    suffix = Path(uploaded.name or "evidence").suffix.lower()[:12]
    key = f"recall_evidence/{timezone.now():%Y/%m}/{uuid4().hex}{suffix}"
    try:
        saved_key = _storage().save(key, uploaded)
    except OSError as exc:
        raise QualityRecallError("召回凭证保存失败，请重新上传。") from exc
    evidence = {
        "storage_key": saved_key,
        "original_name": uploaded.name,
        "content_type": content_type,
        "file_size": uploaded.size,
    }
    receipt = signing.dumps({
        "evidence": evidence,
        "user_id": identity.user_id,
        "identity_id": identity.pk,
        "organization_id": identity.organization_id,
        "factory_root_id": identity.organization.factory_root_id,
    }, salt=UPLOAD_RECEIPT_SALT)
    return {**evidence, "upload_receipt": receipt}


def confirmed_recall_evidence(*, submitted, identity, campaign):
    """Resolve transport proof only from this actor's signed, existing upload.

    The returned snapshot deliberately excludes the receipt. Callers forwarding
    proof to another service must retain the original submitted upload response,
    rather than treating a client-provided flag or a snapshot as verified proof.
    """
    if not isinstance(submitted, dict):
        raise QualityRecallError("交接凭证未通过上传校验，请重新上传。")
    token = submitted.get("upload_receipt")
    if not isinstance(token, str) or not token:
        raise QualityRecallError("交接凭证未通过上传校验，请重新上传。")
    try:
        receipt = signing.loads(token, salt=UPLOAD_RECEIPT_SALT)
    except (signing.BadSignature, ValueError, TypeError) as exc:
        raise QualityRecallError("交接凭证未通过上传校验，请重新上传。") from exc
    if not isinstance(receipt, dict) or not isinstance(receipt.get("evidence"), dict):
        raise QualityRecallError("交接凭证未通过上传校验，请重新上传。")
    evidence = receipt["evidence"]
    key = evidence.get("storage_key")
    size = evidence.get("file_size")
    if (
        not isinstance(key, str) or not key
        or type(size) is not int or size <= 0
        or not isinstance(evidence.get("original_name"), str)
        or not isinstance(evidence.get("content_type"), str)
    ):
        raise QualityRecallError("交接凭证未通过上传校验，请重新上传。")
    if (
        receipt.get("user_id") != identity.user_id
        or receipt.get("identity_id") != identity.pk
        or receipt.get("organization_id") != identity.organization_id
        or receipt.get("factory_root_id") != identity.organization.factory_root_id
        or receipt.get("factory_root_id") != campaign.factory_organization_id
        or submitted.get("storage_key") != key
    ):
        raise QualityRecallError("交接凭证与本次操作身份或链路不符，请重新上传。")

    try:
        info = Path(_storage().path(key)).stat()
    except (OSError, ValueError, SuspiciousFileOperation) as exc:
        raise QualityRecallError("交接凭证文件不存在或已失效，请重新上传。") from exc
    if not S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size != size:
        raise QualityRecallError("交接凭证文件不存在或已失效，请重新上传。")
    return dict(evidence)


def confirmed_dispatch_evidence(*, submitted, identity, campaign):
    """Compatibility name for callers using the original dispatch resolver."""
    return confirmed_recall_evidence(submitted=submitted, identity=identity, campaign=campaign)
