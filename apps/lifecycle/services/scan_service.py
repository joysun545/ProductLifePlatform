from urllib.parse import parse_qs, unquote, urlparse

from django.core.exceptions import ObjectDoesNotExist

from account.services.auth_service import get_active_identity
from common.domain.device_ownership import (
    evaluate_device_access,
    resolve_device_ownership,
)
from common.domain.device_ownership.types import organization_payload
from lifecycle.models import InstallationInvitation, QRCode


class QRCodeResolveError(Exception):
    """二维码不能被识别或当前不可使用。"""


class ScanService:
    """设备二维码识别以及页面数据装配，不拥有属主和权限规则。"""

    @staticmethod
    def extract_code(qr_text: str) -> str:
        raw = unquote((qr_text or "").strip())
        if not raw or len(raw) > 1024:
            raise QRCodeResolveError("二维码内容无效")

        if raw.startswith("PLDP_DEVICE|"):
            code = raw.partition("|")[2].strip()
        elif raw.startswith(("http://", "https://")):
            parsed = urlparse(raw)
            query = parse_qs(parsed.query)
            code = ""
            for key in ("code", "qr_code", "qr"):
                values = query.get(key)
                if values and values[0].strip():
                    code = values[0].strip()
                    break
            if not code:
                code = parsed.path.rstrip("/").rsplit("/", 1)[-1].strip()
        else:
            code = raw

        if not code or len(code) > 128 or any(ch.isspace() for ch in code):
            raise QRCodeResolveError("二维码编号无效")
        return code

    @staticmethod
    def _qrcode_queryset():
        return QRCode.objects.select_related(
            "device__product_model__category",
            "device__batch__bom",
            "device__batch__manufacturer__organization_type",
            "device__state__current_organization__organization_type",
            "device__state__last_event",
            "device__organization_record__organization__organization_type",
            "device__organization_record__last_event",
            "device__installation__organization__organization_type",
            "device__installation__operator",
        )

    @classmethod
    def resolve(cls, *, qr_text: str, user) -> dict:
        code = cls.extract_code(qr_text)
        qrcode = cls._qrcode_queryset().filter(code=code).first()
        cls._validate_qrcode(qrcode)
        return cls._build_result(qrcode=qrcode, user=user)

    @classmethod
    def resolve_device(cls, *, device, user) -> dict:
        qrcode = cls._qrcode_queryset().filter(device=device).first()
        cls._validate_qrcode(qrcode)
        return cls._build_result(qrcode=qrcode, user=user)

    @staticmethod
    def _validate_qrcode(qrcode):
        if not qrcode:
            raise QRCodeResolveError("未找到对应的设备二维码")
        if qrcode.status == "DISABLED":
            raise QRCodeResolveError("该二维码已禁用")
        if qrcode.status == "REPLACED":
            raise QRCodeResolveError("该二维码已被替换")
        if qrcode.status != "ACTIVE":
            raise QRCodeResolveError("该二维码尚未启用")
        if not qrcode.device_id:
            raise QRCodeResolveError("该二维码尚未绑定设备")

    @classmethod
    def _build_result(cls, *, qrcode, user):
        device = qrcode.device
        identity = get_active_identity(user)
        ownership = resolve_device_ownership(
            device=device,
            viewer_identity=identity,
        )
        access = evaluate_device_access(
            device=device,
            identity=identity,
            ownership=ownership,
        )

        ownership_payload = ownership.as_dict()
        payload = {
            "qr_code": qrcode.code,
            "view_mode": access.view_mode,
            "role": identity.role.code if identity else "tourist",
            "viewer_relation": ownership.viewer_relation,
            "viewer_relation_name": ownership_payload["viewer_relation_name"],
            "viewer_relations": ownership_payload["viewer_relations"],
            "viewer_relation_names": ownership_payload[
                "viewer_relation_names"
            ],
            "capabilities": list(access.capabilities),
            "device": cls._public_device(device),
        }

        if access.allows("view_region_owner"):
            regional_owner = ownership.regional_owner
            payload["region"] = {
                "name": regional_owner.region if regional_owner else "",
                "owner": organization_payload(regional_owner),
            }

        if access.allows("view_device_detail"):
            payload["device"].update(cls._device_detail(device))
            payload["customer_invitation"] = cls._customer_invitation(device)

        if access.allows("view_ownership"):
            payload["ownership"] = ownership_payload

        if access.allows("view_traceability"):
            payload["traceability"] = cls._traceability(device)

        return payload

    @staticmethod
    def _related_object(instance, name):
        try:
            return getattr(instance, name)
        except ObjectDoesNotExist:
            return None

    @classmethod
    def _public_device(cls, device):
        product_model = device.product_model
        state = cls._related_object(device, "state")
        return {
            "id": device.pk,
            "sn": device.sn,
            "manufacturer": device.batch.manufacturer.name,
            "product_model": {
                "id": product_model.pk,
                "code": product_model.code,
                "name": product_model.name,
                "category": product_model.category.name,
            },
            "status_summary": {
                "status": state.status if state else "CREATED",
                "status_name": state.get_status_display() if state else "已创建",
                "install_status": (
                    state.install_status if state else "NOT_INSTALLED"
                ),
                "install_status_name": (
                    state.get_install_status_display() if state else "未安装"
                ),
                "activation_status": (
                    state.activation_status if state else "NOT_ACTIVATED"
                ),
                "activation_status_name": (
                    state.get_activation_status_display() if state else "未激活"
                ),
            },
        }

    @classmethod
    def _device_detail(cls, device):
        state = cls._related_object(device, "state")
        organization_record = cls._related_object(device, "organization_record")
        installation = cls._related_object(device, "installation")
        current_organization = None
        if organization_record:
            current_organization = organization_record.organization
        elif state:
            current_organization = state.current_organization

        return {
            "batch": {
                "id": device.batch_id,
                "batch_no": device.batch.batch_no,
                "production_date": device.batch.production_date.isoformat(),
            },
            "state": {
                "status": state.status if state else "CREATED",
                "status_name": state.get_status_display() if state else "已创建",
                "activation_status": (
                    state.activation_status if state else "NOT_ACTIVATED"
                ),
                "activation_status_name": (
                    state.get_activation_status_display() if state else "未激活"
                ),
                "install_status": (
                    state.install_status if state else "NOT_INSTALLED"
                ),
                "install_status_name": (
                    state.get_install_status_display() if state else "未安装"
                ),
                "last_event_time": (
                    state.last_event_time.isoformat()
                    if state and state.last_event_time
                    else ""
                ),
            },
            "current_organization": organization_payload(current_organization),
            "installation": {
                "installed_at": (
                    installation.installed_at.isoformat() if installation else ""
                ),
                "organization": (
                    organization_payload(installation.organization)
                    if installation
                    else None
                ),
                "operator_name": cls._installation_operator_name(installation),
                "location": installation.location if installation else "",
            },
        }

    @staticmethod
    def _installation_operator_name(installation):
        if not installation or not installation.operator:
            return ""
        return (
            installation.operator.wx_nickname
            or installation.operator.username
        )

    @staticmethod
    def _customer_invitation(device):
        invitation = (
            InstallationInvitation.objects.filter(device=device)
            .order_by("-created_at", "-id")
            .first()
        )
        if not invitation:
            return None
        return {
            "status": invitation.status,
            "accepted_at": (
                invitation.accepted_at.isoformat()
                if invitation.accepted_at
                else ""
            ),
        }

    @staticmethod
    def _traceability(device):
        bom = device.batch.bom
        items = (
            bom.items.select_related("material", "material_spec", "supplier")
            .order_by("material__code", "id")
        )
        return {
            "bom": {
                "id": bom.pk,
                "version": bom.version,
                "version_no": bom.version_no,
            },
            "items": [
                {
                    "material_code": item.material.code,
                    "material_name": item.material.name,
                    "spec_code": item.material_spec.code if item.material_spec else "",
                    "spec_name": item.material_spec.name if item.material_spec else "",
                    "supplier_id": item.supplier_id,
                    "supplier_name": item.supplier.name if item.supplier else "",
                    "quantity": item.quantity,
                    "remark": item.remark,
                }
                for item in items
            ],
        }
