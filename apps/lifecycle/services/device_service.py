# apps/lifecycle/services/device_service.py

"""
Device Service（设备业务编排层）

职责划分：
1. 业务动作（不改状态）
   - SN生成
   - QR绑定
   - 基础信息维护

2. 调用状态机（不直接改status）
   - 出库
   - 安装
   - 激活

3. 写入事件（通过EventService）
"""

from account.services.auth_service import get_active_identity
from lifecycle.models import Device
from lifecycle.services.event_service import EventService
from lifecycle.services.state_machine import DeviceStateMachine


class DeviceService:
    """
    Device业务服务层（工业级分层）
    """

    # =========================================================
    # ① 业务类操作（不涉及状态流转）
    # =========================================================

    @staticmethod
    def generate_sn(device_id: int, sn: str, operator=None):
        """
        生成SN（设备身份标识）

        说明：
        - SN属于物理标识，不是状态
        - 不通过StateMachine
        """

        device = Device.objects.get(id=device_id)

        device.sn = sn
        device.save()

        identity = get_active_identity(operator) if operator else None
        EventService.create_event(
            device=device,
            event_type="CREATE",
            operator=operator,
            organization=identity.organization if identity else None,
            data={"action": "sn_generated"},
        )

        return device

    # =========================================================
    # ② 状态流转类操作（必须走StateMachine）
    # =========================================================

    @staticmethod
    def ship_device(device_id: int, operator=None):
        """
        出库
        """

        device = Device.objects.get(id=device_id)

        DeviceStateMachine.ship(
            device=device,
            operator=operator
        )

        return device

    @staticmethod
    def install_device(device_id: int, operator=None):
        """
        安装
        """

        device = Device.objects.get(id=device_id)

        DeviceStateMachine.install(
            device=device,
            operator=operator
        )

        return device

    @staticmethod
    def activate_device(device_id: int, operator=None):
        """
        激活（平台侧）
        """

        device = Device.objects.get(id=device_id)

        DeviceStateMachine.activate(
            device=device,
            operator=operator
        )

        return device
