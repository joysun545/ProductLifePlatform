from abc import ABC, abstractmethod


class DeviceTelemetryAdapter(ABC):
    """第三方设备数据适配器协议。实现类不得改变生命周期核心状态。"""

    provider_code = ""

    @abstractmethod
    def fetch_snapshot(self, *, device, module_item, context):
        """返回 metrics/alerts/updated_at 等只读快照数据。"""
        raise NotImplementedError
