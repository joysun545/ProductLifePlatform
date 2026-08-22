# apps/lifecycle/models/__init__.py


from .device import Device

from .event import LifecycleEvent

from .state import DeviceState

from .qrcode import QRCode

from .activation import ActivationRecord

from .organization_device import OrganizationDevice

from .installation import InstallationInvitation, InstallationRecord

from .document_read import ProductDocumentRead

from .customer_device import CustomerDeviceBinding

from .goods_transfer import GoodsTransfer, GoodsTransferItem

from .goods_transfer_settlement import (
    GoodsTransferLogisticsDocument,
    GoodsTransferPayment,
    GoodsTransferSettlement,
)

from .goods_transfer_accounts import (
    GoodsTransferAccountCase,
    GoodsTransferAccountPayment,
    GoodsTransferCreditAllocation,
)

from .factory_return import (
    DeviceMaterialFaultRecord,
    DeviceMaterialWarrantyEvaluation,
    FactoryReturnDocument,
    FactoryReturnInventoryItem,
    FactoryReturnMaterialReturnRecord,
    FactoryReturnProcess,
    FactoryReturnRepairMaterialRequirement,
    FactoryReturnWarrantyDispatch,
    FactoryReturnWarrantyDispatchItem,
)


__all__ = [

    "Device",

    "LifecycleEvent",

    "DeviceState",

    "QRCode",

    "ActivationRecord",

    "OrganizationDevice",

    "InstallationRecord",

    "InstallationInvitation",

    "ProductDocumentRead",

    "CustomerDeviceBinding",

    "GoodsTransfer",

    "GoodsTransferItem",

    "GoodsTransferSettlement",

    "GoodsTransferLogisticsDocument",

    "GoodsTransferPayment",

    "GoodsTransferAccountCase",

    "GoodsTransferAccountPayment",

    "GoodsTransferCreditAllocation",

    "FactoryReturnProcess",

    "FactoryReturnInventoryItem",

    "FactoryReturnMaterialReturnRecord",

    "FactoryReturnDocument",

    "FactoryReturnRepairMaterialRequirement",

    "DeviceMaterialFaultRecord",

    "DeviceMaterialWarrantyEvaluation",

    "FactoryReturnWarrantyDispatch",

    "FactoryReturnWarrantyDispatchItem",

]
