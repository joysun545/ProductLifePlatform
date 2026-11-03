import uuid

from django.core.exceptions import ValidationError
from django.db import models


class PhysicalDeviceCode(models.Model):
    """Manufacturer-issued physical QR codes awaiting device binding."""

    class Status(models.TextChoices):
        GENERATED = "GENERATED", "已生成待绑定"
        BOUND = "BOUND", "已绑定设备"
        DISABLED = "DISABLED", "已停用"

    code = models.CharField(
        max_length=64,
        unique=True,
        verbose_name="设备编码",
    )
    factory_root = models.ForeignKey(
        "account.Organization",
        on_delete=models.PROTECT,
        related_name="issued_physical_device_codes",
        verbose_name="发行厂家根组织",
    )
    generation_batch_id = models.UUIDField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="生成批次号",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.GENERATED,
        db_index=True,
        verbose_name="编码状态",
    )
    device = models.OneToOneField(
        "lifecycle.Device",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="physical_device_code_record",
        verbose_name="绑定设备",
    )
    generated_by = models.ForeignKey(
        "account.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="generated_physical_device_codes",
        verbose_name="生成人员",
    )
    bound_by = models.ForeignKey(
        "account.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="bound_physical_device_codes",
        verbose_name="绑定操作人",
    )
    generated_at = models.DateTimeField(auto_now_add=True, verbose_name="生成时间")
    bound_at = models.DateTimeField(null=True, blank=True, verbose_name="绑定时间")
    disabled_at = models.DateTimeField(null=True, blank=True, verbose_name="停用时间")

    class Meta:
        ordering = ("-generated_at", "-id")
        indexes = [
            models.Index(
                fields=("factory_root", "status"),
                name="life_device_code_root_status",
            ),
        ]
        verbose_name = "设备编码登记"
        verbose_name_plural = "设备编码登记"

    @property
    def qr_payload(self):
        return f"PLDP_DEVICE|{self.code}"

    def clean(self):
        super().clean()
        if self.factory_root_id and self.factory_root.organization_type.code != "OWNER":
            raise ValidationError({"factory_root": "设备编码必须归属厂家根组织。"})
        if self.device_id:
            manufacturer = self.device.batch.manufacturer
            root = manufacturer.get_factory_root()
            if not root or root.pk != self.factory_root_id:
                raise ValidationError({"device": "编码发行厂家与设备所属厂家不一致。"})
            if self.device.physical_device_code not in (None, "", self.code):
                raise ValidationError({"device": "设备已绑定其他设备编码。"})
        if self.status == self.Status.GENERATED and self.device_id:
            raise ValidationError({"device": "待绑定编码不能已有绑定设备。"})
        if self.status == self.Status.BOUND and not self.device_id:
            raise ValidationError({"device": "已绑定编码必须关联设备。"})

    def __str__(self):
        return self.code
