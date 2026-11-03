# apps/lifecycle/models/__init__.py


from .device import Device
from .physical_device_code import PhysicalDeviceCode
from .device_code_registry_web import (
    DeviceCodeRegistryLoginChallenge,
    PhysicalDeviceCodePackagingInfo,
)

from .event import LifecycleEvent

from .state import DeviceState

from .qrcode import QRCode

from .activation import ActivationRecord

from .organization_device import OrganizationDevice

from .installation import InstallationInvitation, InstallationRecord

from .document_read import ProductDocumentRead

from .customer_device import (
    CustomerDeviceBinding,
    CustomerVehicle,
    DriverVehicleBinding,
    DriverVehicleInvitation,
    VehicleDeviceAssignment,
)

from .goods_transfer import GoodsTransfer, GoodsTransferItem

from .goods_transfer_settlement import (
    GoodsTransferLogisticsDocument,
    GoodsTransferPayment,
    GoodsTransferPaymentAllocation,
    GoodsTransferReceivableAccount,
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
from .aftersales import (
    AfterSalesApplication,
    AfterSalesDeviceCodeVerification,
    AfterSalesEvent,
    AfterSalesMedia,
    AfterSalesServiceStationApplication,
    AfterSalesServiceStationLiaison,
)


__all__ = [

    "Device",
    "PhysicalDeviceCode",
    "DeviceCodeRegistryLoginChallenge",
    "PhysicalDeviceCodePackagingInfo",

    "LifecycleEvent",

    "DeviceState",

    "QRCode",

    "ActivationRecord",

    "OrganizationDevice",

    "InstallationRecord",

    "InstallationInvitation",

    "ProductDocumentRead",

    "CustomerDeviceBinding",
    "CustomerVehicle",
    "DriverVehicleBinding",
    "DriverVehicleInvitation",
    "VehicleDeviceAssignment",

    "GoodsTransfer",

    "GoodsTransferItem",

    "GoodsTransferSettlement",

    "GoodsTransferLogisticsDocument",

    "GoodsTransferPayment",

    "GoodsTransferPaymentAllocation",

    "GoodsTransferReceivableAccount",

    "GoodsTransferAccountCase",

    "GoodsTransferAccountPayment",

    "GoodsTransferCreditAllocation",

    "FactoryReturnProcess",

    "FactoryReturnInventoryItem",

    "AfterSalesApplication",

    "AfterSalesDeviceCodeVerification",

    "AfterSalesEvent",

    "AfterSalesMedia",

    "AfterSalesServiceStationApplication",

    "AfterSalesServiceStationLiaison",

    "FactoryReturnMaterialReturnRecord",

    "FactoryReturnDocument",

    "FactoryReturnRepairMaterialRequirement",

    "DeviceMaterialFaultRecord",

    "DeviceMaterialWarrantyEvaluation",

    "FactoryReturnWarrantyDispatch",

    "FactoryReturnWarrantyDispatchItem",

]

from .replacement import ReplacementFlow, ReplacementLeg, ReplacementInventoryEntry

from .replacement import ReplacementTurnoverStock, ReplacementMaterialReturn
