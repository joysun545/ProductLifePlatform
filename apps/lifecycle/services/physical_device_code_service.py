import secrets
import uuid

from django.db import IntegrityError, transaction

from account.models import SeniorManagerProfile
from account.services.auth_service import get_active_identity
from account.services.role_delegation_service import delegated_role_codes
from lifecycle.models import PhysicalDeviceCode, QRCode


DEVICE_CODE_QR_PREFIX = "PLDP_DEVICE|"
DEVICE_CODE_PRODUCTION_ROLE = "factory_production"
DEVICE_CODE_FACTORY_OWNER_ROLE = "factory_admin"
DEVICE_CODE_SENIOR_MANAGER_ROLE = "factory_senior_manager"


class PhysicalDeviceCodeError(Exception):
    """A manufacturer-issued device code is invalid or unavailable."""


class PhysicalDeviceCodePermissionError(PhysicalDeviceCodeError):
    """The current identity cannot issue physical device codes."""


def normalize_device_code(value):
    code = str(value or "").strip()
    if code.startswith(("PLDP_DEVICE|", "PLDP_DEVICE_CODE|")):
        code = code.partition("|")[2].strip()
    if not code or len(code) > 64 or any(ch.isspace() for ch in code):
        raise PhysicalDeviceCodeError("设备编码二维码内容无效")
    return code


def _device_factory_root(device):
    manufacturer = device.batch.manufacturer
    root = manufacturer.get_factory_root()
    if not root:
        raise PhysicalDeviceCodeError("设备所属厂家链路未配置，无法验证设备编码")
    return root


def _factory_has_production_delegate(factory_root):
    return SeniorManagerProfile.objects.filter(
        identity__organization=factory_root,
        identity__organization__is_active=True,
        identity__is_active=True,
        identity__user__is_active=True,
        identity__role__is_active=True,
        identity__role__code=DEVICE_CODE_SENIOR_MANAGER_ROLE,
        managed_roles__organization_type_id=factory_root.organization_type_id,
        managed_roles__organization_type__is_active=True,
        managed_roles__code=DEVICE_CODE_PRODUCTION_ROLE,
        managed_roles__is_active=True,
        managed_roles__is_owner_role=False,
    ).exists()


def identity_can_issue_device_codes(identity):
    """Enforce the production-delegation replacement rule for every surface."""
    if (
        not identity
        or not identity.is_active
        or not identity.organization.is_active
        or not identity.organization.organization_type.is_active
        or not identity.role.is_active
        or not identity.user.is_active
        or identity.organization.organization_type.code != "OWNER"
        or identity.role.organization_type_id != identity.organization.organization_type_id
    ):
        return False

    factory_root = identity.organization.get_factory_root()
    if not factory_root or factory_root.pk != identity.organization_id:
        return False

    role_code = identity.role.code
    if role_code == DEVICE_CODE_PRODUCTION_ROLE:
        return True
    if role_code == DEVICE_CODE_FACTORY_OWNER_ROLE:
        return not _factory_has_production_delegate(factory_root)
    if role_code == DEVICE_CODE_SENIOR_MANAGER_ROLE:
        return DEVICE_CODE_PRODUCTION_ROLE in delegated_role_codes(identity)
    return False


@transaction.atomic
def generate_device_code_batch(*, user, quantity):
    identity = get_active_identity(user)
    if not identity_can_issue_device_codes(identity):
        raise PhysicalDeviceCodePermissionError(
            "设备编码由厂家生产经理、厂家负责人，或分管生产经理的厂家高级经理生成"
        )
    factory_root = identity.organization.get_factory_root()
    if not isinstance(quantity, int) or quantity < 1 or quantity > 1000:
        raise PhysicalDeviceCodeError("单次生成数量须为1至1000个")

    batch_id = uuid.uuid4()
    codes = []
    seen = set()
    while len(codes) < quantity:
        code = f"DC-{secrets.token_hex(10).upper()}"
        if code in seen or PhysicalDeviceCode.objects.filter(code=code).exists():
            continue
        if QRCode.objects.filter(code=code).exists():
            continue
        seen.add(code)
        codes.append(code)

    records = [
        PhysicalDeviceCode(
            code=code,
            factory_root=factory_root,
            generation_batch_id=batch_id,
            generated_by=user,
        )
        for code in codes
    ]
    try:
        with transaction.atomic():
            PhysicalDeviceCode.objects.bulk_create(records)
    except IntegrityError as exc:
        raise PhysicalDeviceCodeError("设备编码生成冲突，请重新生成") from exc

    return {
        "generation_batch_id": str(batch_id),
        "factory_root": {"id": factory_root.pk, "name": factory_root.name},
        "count": len(codes),
        "codes": [
            {"code": code, "qr_payload": f"{DEVICE_CODE_QR_PREFIX}{code}"}
            for code in codes
        ],
    }


@transaction.atomic
def verify_device_code_for_activation(*, device, raw_code):
    code = normalize_device_code(raw_code)
    if QRCode.objects.filter(code=code).exists():
        raise PhysicalDeviceCodeError("扫描到的是包装生命周期二维码，请扫描厂家生成的设备编码二维码")

    entry = PhysicalDeviceCode.objects.select_for_update().filter(code=code).first()
    if not entry:
        raise PhysicalDeviceCodeError("该设备编码未在厂家编码库登记，不能绑定")

    factory_root = _device_factory_root(device)
    if entry.factory_root_id != factory_root.pk:
        raise PhysicalDeviceCodeError("该设备编码不属于此设备对应的厂家编码库")

    if entry.status == PhysicalDeviceCode.Status.DISABLED:
        raise PhysicalDeviceCodeError("该设备编码已停用，不能绑定")

    if entry.status == PhysicalDeviceCode.Status.BOUND:
        if entry.device_id == device.pk and device.physical_device_code == code:
            return entry, code
        raise PhysicalDeviceCodeError("该设备编码已绑定其他设备，不能重复使用")

    if entry.device_id:
        raise PhysicalDeviceCodeError("该设备编码登记状态异常，不能绑定")

    if device.physical_device_code and device.physical_device_code != code:
        raise PhysicalDeviceCodeError("该设备已经绑定其他设备编码")

    if PhysicalDeviceCode.objects.filter(device=device).exclude(pk=entry.pk).exists():
        raise PhysicalDeviceCodeError("该设备已经绑定其他设备编码")

    if QRCode.objects.filter(code=code).exists():
        raise PhysicalDeviceCodeError("设备编码不能与包装生命周期二维码相同")

    return entry, code


@transaction.atomic
def bind_device_code(*, entry, device, user, code, bound_at):
    """Bind a verified registry row and the device field in the activation transaction."""
    if entry.status == PhysicalDeviceCode.Status.BOUND:
        if entry.device_id == device.pk and device.physical_device_code == code:
            return
        raise PhysicalDeviceCodeError("该设备编码已绑定其他设备，不能重复使用")
    if entry.status != PhysicalDeviceCode.Status.GENERATED or entry.device_id:
        raise PhysicalDeviceCodeError("该设备编码已被使用或停用")

    try:
        with transaction.atomic():
            entry.status = PhysicalDeviceCode.Status.BOUND
            entry.device = device
            entry.bound_by = user
            entry.bound_at = bound_at
            entry.save(update_fields=("status", "device", "bound_by", "bound_at"))

            device.physical_device_code = code
            device.save(update_fields=("physical_device_code", "updated_at"))
    except IntegrityError as exc:
        raise PhysicalDeviceCodeError("该设备编码已经绑定到其他设备") from exc
