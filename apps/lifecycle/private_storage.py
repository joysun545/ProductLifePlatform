from pathlib import Path

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.utils.deconstruct import deconstructible


@deconstructible
class GoodsTransferPrivateStorage(FileSystemStorage):
    """Private commercial files served only through authenticated APIs."""

    def __init__(self):
        location = getattr(
            settings,
            "GOODS_TRANSFER_PRIVATE_ROOT",
            Path(settings.BASE_DIR) / "private_goods_transfer",
        )
        super().__init__(location=location, base_url=None)


goods_transfer_private_storage = GoodsTransferPrivateStorage()
