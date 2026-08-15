from django.utils import timezone

from lifecycle.models.qrcode import QRCode
from lifecycle.utils.id_generator import gen_qrcode


class QRCodeService:
    """设备二维码生成与绑定服务。"""

    @staticmethod
    def create_qrcode(device, created_by=None):
        code = gen_qrcode()
        while QRCode.objects.filter(code=code).exists():
            code = gen_qrcode()

        return QRCode.objects.create(
            code=code,
            device=device,
            status="ACTIVE",
            created_by=created_by,
            activated_at=timezone.now(),
        )
