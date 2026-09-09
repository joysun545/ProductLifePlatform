_ADAPTERS = {}


def register_adapter(adapter_class):
    provider_code = str(getattr(adapter_class, "provider_code", "") or "").strip()
    if not provider_code:
        raise ValueError("设备数据适配器必须声明 provider_code")
    _ADAPTERS[provider_code] = adapter_class
    return adapter_class


def get_adapter(provider_code):
    adapter_class = _ADAPTERS.get(str(provider_code or "").strip())
    return adapter_class() if adapter_class else None
