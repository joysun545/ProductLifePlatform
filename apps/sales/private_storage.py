from pathlib import Path

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.utils.deconstruct import deconstructible


@deconstructible
class OrderFinancePrivateStorage(FileSystemStorage):
    """Private files that must never be exposed by the public media location."""

    def __init__(self):
        location = getattr(
            settings,
            "ORDER_FINANCE_PRIVATE_ROOT",
            Path(settings.BASE_DIR) / "private_order_finance",
        )
        super().__init__(location=location, base_url=None)


order_finance_private_storage = OrderFinancePrivateStorage()


@deconstructible
class OrderLogisticsPrivateStorage(FileSystemStorage):
    """Private logistics documents served only through authenticated APIs."""

    def __init__(self):
        location = getattr(
            settings,
            "ORDER_LOGISTICS_PRIVATE_ROOT",
            Path(settings.BASE_DIR) / "private_order_logistics",
        )
        super().__init__(location=location, base_url=None)


order_logistics_private_storage = OrderLogisticsPrivateStorage()
