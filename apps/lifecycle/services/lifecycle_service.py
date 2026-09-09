from django.db import transaction

from lifecycle.models import Device, DeviceState, OrganizationDevice
from lifecycle.utils.id_generator import gen_device_sn

from .event_service import EventService
from .qrcode_service import QRCodeService


class LifecycleService:
    """设备、二维码、当前状态及初始事件的原子化创建入口。"""

    @staticmethod
    @transaction.atomic
    def create_device(
        product_model,
        operator=None,
        organization=None,
        batch=None,
        sn=None,
    ):
        if batch is None:
            raise ValueError("创建设备必须指定生产批次")
        if batch.product_model_id != product_model.pk:
            raise ValueError("生产批次与产品型号不一致")

        device_sn = (sn or "").strip() or gen_device_sn()
        while Device.objects.filter(sn=device_sn).exists():
            if sn:
                raise ValueError("设备序列号已存在")
            device_sn = gen_device_sn()

        device = Device.objects.create(
            sn=device_sn,
            product_model=product_model,
            batch=batch,
        )
        qrcode = QRCodeService.create_qrcode(device, created_by=operator)

        state = DeviceState.objects.create(
            device=device,
            current_organization=organization,
            status="IN_STOCK" if organization else "CREATED",
        )

        organization_record = None
        if organization:
            organization_record = OrganizationDevice.objects.create(
                device=device,
                organization=organization,
                status="IN_STOCK",
            )

        event = EventService.create_event(
            device=device,
            event_type="CREATE",
            operator=operator,
            organization=organization,
            data={"qr_code": qrcode.code},
        )

        state.last_event = event
        state.last_event_time = event.created_at
        state.save(update_fields=("last_event", "last_event_time", "updated_at"))

        if organization_record:
            organization_record.last_event = event
            organization_record.save(update_fields=("last_event", "updated_at"))

        return device
