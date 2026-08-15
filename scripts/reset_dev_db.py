# scripts/reset_dev_db.py

from lifecycle.models import (
    Device,
    ProductLifecycle,
    DeviceEvent,
    HandoverEvent,
    TransportOrder,
    QRCode,
)

from sales.models import (
    Batch,
    SalesPlan,
)

from product.models import (
    BOMItem,
    BOM,
    ProductModel,
    Brand,
    ProductCategory,
)

print("=" * 50)
print("开始清空开发测试数据")
print("=" * 50)


# ===========================
# 生命周期
# ===========================

print("删除 DeviceEvent ...")
DeviceEvent.objects.all().delete()

print("删除 HandoverEvent ...")
HandoverEvent.objects.all().delete()

print("删除 TransportOrder ...")
TransportOrder.objects.all().delete()

print("删除 QRCode ...")
QRCode.objects.all().delete()

print("删除 Device ...")
Device.objects.all().delete()

print("删除 ProductLifecycle ...")
ProductLifecycle.objects.all().delete()


# ===========================
# 销售
# ===========================

print("删除 Batch ...")
Batch.objects.all().delete()

print("删除 SalesPlan ...")
SalesPlan.objects.all().delete()


# ===========================
# 产品
# ===========================

print("删除 BOMItem ...")
BOMItem.objects.all().delete()

print("删除 BOM ...")
BOM.objects.all().delete()

print("删除 ProductModel ...")
ProductModel.objects.all().delete()

print("删除 Brand ...")
Brand.objects.all().delete()

print("删除 ProductCategory ...")
ProductCategory.objects.all().delete()


print("=" * 50)
print("开发数据已全部清空！")
print("=" * 50)
