from product.models import BOM

from .registry import get_adapter


CAPABILITY_MARKERS = {"telemetry", "communication", "device_telemetry"}
COMMUNICATION_KEYWORDS = ("通讯模组", "通信模组", "通讯模块", "通信模块")


def _normalized_capabilities(parameters):
    raw = parameters.get("capabilities", []) if isinstance(parameters, dict) else []
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",")]
    if not isinstance(raw, (list, tuple, set)):
        return set()
    return {str(value).strip().lower() for value in raw if str(value).strip()}


def _is_communication_item(item):
    parameters = item.material_spec.parameters if item.material_spec else {}
    parameters = parameters if isinstance(parameters, dict) else {}
    if _normalized_capabilities(parameters) & CAPABILITY_MARKERS:
        return True
    if any(
        parameters.get(key) is True
        for key in (
            "communication_module",
            "is_communication_module",
            "telemetry_enabled",
        )
    ):
        return True
    searchable = " ".join(
        value
        for value in (
            item.material.name,
            item.material.description,
            item.material_spec.name if item.material_spec else "",
            item.material_spec.description if item.material_spec else "",
        )
        if value
    )
    return any(keyword in searchable for keyword in COMMUNICATION_KEYWORDS)


class DeviceTelemetryService:
    """识别 BOM 通讯能力，并隔离调用可插拔的第三方数据适配器。"""

    @staticmethod
    def _bom(device):
        batch = getattr(device, "batch", None)
        if batch and getattr(batch, "bom_id", None):
            return batch.bom
        return (
            BOM.objects.filter(product_model=device.product_model, is_active=True)
            .order_by("-version_no", "-id")
            .first()
        )

    @classmethod
    def capability(cls, *, device):
        bom = cls._bom(device)
        if not bom:
            return None
        items = bom.items.select_related("material", "material_spec").order_by("id")
        return next((item for item in items if _is_communication_item(item)), None)

    @classmethod
    def snapshot(cls, *, device, user=None):
        module_item = cls.capability(device=device)
        if not module_item:
            return {
                "capable": False,
                "configured": False,
                "status": "NOT_SUPPORTED",
                "provider": "",
                "metrics": [],
                "alerts": [],
                "message": "该设备型号未配置通讯模组",
            }

        parameters = (
            module_item.material_spec.parameters
            if module_item.material_spec and isinstance(module_item.material_spec.parameters, dict)
            else {}
        )
        provider_code = str(parameters.get("telemetry_provider") or "").strip()
        module = {
            "material_code": module_item.material.code,
            "material_name": module_item.material.name,
            "spec_code": module_item.material_spec.code if module_item.material_spec else "",
            "spec_name": module_item.material_spec.name if module_item.material_spec else "",
        }
        adapter = get_adapter(provider_code)
        if not provider_code or not adapter:
            return {
                "capable": True,
                "configured": False,
                "status": "NOT_CONFIGURED",
                "provider": provider_code,
                "module": module,
                "metrics": [],
                "alerts": [],
                "message": "该设备支持远程数据，第三方数据服务尚未接入",
            }

        try:
            payload = adapter.fetch_snapshot(
                device=device,
                module_item=module_item,
                context={"user": user},
            ) or {}
        except Exception:
            return {
                "capable": True,
                "configured": True,
                "status": "TEMPORARILY_UNAVAILABLE",
                "provider": provider_code,
                "module": module,
                "metrics": [],
                "alerts": [],
                "message": "设备数据服务暂时不可用，不影响其他功能",
            }

        return {
            "capable": True,
            "configured": True,
            "status": "AVAILABLE",
            "provider": provider_code,
            "module": module,
            "metrics": payload.get("metrics", []),
            "alerts": payload.get("alerts", []),
            "updated_at": payload.get("updated_at"),
            "message": payload.get("message", "设备数据已更新"),
        }
