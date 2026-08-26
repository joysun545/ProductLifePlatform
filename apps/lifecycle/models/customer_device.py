import uuid

from django.db import models
from django.utils import timezone


class CustomerVehicle(models.Model):
    """用户负责人名下的车辆；设备和司机都通过车辆形成稳定子集。"""

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "有效"
        INACTIVE = "INACTIVE", "失效"

    customer_organization = models.ForeignKey(
        "account.Organization", on_delete=models.PROTECT,
        related_name="customer_vehicles", verbose_name="用户组织",
    )
    vehicle_no = models.CharField(max_length=64, verbose_name="车牌号或车辆编号")
    normalized_vehicle_no = models.CharField(max_length=64, verbose_name="标准化车辆编号")
    status = models.CharField(max_length=16, choices=Status.choices,
                              default=Status.ACTIVE, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("vehicle_no", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("customer_organization", "normalized_vehicle_no"),
                name="lc_unique_customer_vehicle",
            )
        ]

    def save(self, *args, **kwargs):
        self.vehicle_no = (self.vehicle_no or "").strip() or "未登记车辆"
        self.normalized_vehicle_no = "".join(self.vehicle_no.upper().split())
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.customer_organization.name}-{self.vehicle_no}"


class DriverVehicleInvitation(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "待接受"
        ACCEPTED = "ACCEPTED", "已接受"
        EXPIRED = "EXPIRED", "已过期"
        CANCELLED = "CANCELLED", "已取消"

    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    vehicle = models.ForeignKey(CustomerVehicle, on_delete=models.PROTECT,
                                related_name="driver_invitations")
    inviter = models.ForeignKey("account.User", on_delete=models.PROTECT,
                                related_name="sent_driver_vehicle_invitations")
    invitee = models.ForeignKey("account.User", null=True, blank=True,
                                on_delete=models.SET_NULL,
                                related_name="accepted_driver_vehicle_invitations")
    status = models.CharField(max_length=16, choices=Status.choices,
                              default=Status.PENDING, db_index=True)
    expires_at = models.DateTimeField(db_index=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class DriverVehicleBinding(models.Model):
    vehicle = models.ForeignKey(CustomerVehicle, on_delete=models.PROTECT,
                                related_name="driver_bindings")
    driver = models.ForeignKey("account.User", on_delete=models.PROTECT,
                               related_name="driver_vehicle_bindings")
    invitation = models.ForeignKey(DriverVehicleInvitation, null=True, blank=True,
                                   on_delete=models.SET_NULL,
                                   related_name="bindings")
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("vehicle", "driver"),
                                    name="lc_unique_vehicle_driver")
        ]


class CustomerDeviceBinding(models.Model):
    """终端用户组织接收设备的稳定事实关系。

    岗位身份仍由 UserOrganization 管理；本表只回答“哪个用户组织通过哪次
    安装邀请接收了哪台设备”，从而支持同一用户组织绑定多台设备。
    """

    class AcceptanceMode(models.TextChoices):
        NEW_CUSTOMER = "NEW_CUSTOMER", "新用户首次接收"
        EXISTING_CUSTOMER = "EXISTING_CUSTOMER", "已有用户添加设备"
        LEGACY = "LEGACY", "历史数据回填"

    class CustomerType(models.TextChoices):
        PERSONAL = "PERSONAL", "个人用户"
        FLEET = "FLEET", "企业或车队用户"

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "有效"
        INACTIVE = "INACTIVE", "失效"

    device = models.OneToOneField(
        "lifecycle.Device",
        on_delete=models.PROTECT,
        related_name="customer_binding",
        verbose_name="设备",
    )
    customer_organization = models.ForeignKey(
        "account.Organization",
        on_delete=models.PROTECT,
        related_name="customer_device_bindings",
        verbose_name="终端用户组织",
    )
    source_organization = models.ForeignKey(
        "account.Organization",
        on_delete=models.PROTECT,
        related_name="customer_device_sources",
        verbose_name="设备来源组织",
    )
    invitation = models.OneToOneField(
        "lifecycle.InstallationInvitation",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="customer_device_binding",
        verbose_name="安装邀请",
    )
    accepted_by = models.ForeignKey(
        "account.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="accepted_customer_devices",
        verbose_name="接收账号",
    )
    acceptance_mode = models.CharField(
        max_length=24,
        choices=AcceptanceMode.choices,
        default=AcceptanceMode.NEW_CUSTOMER,
        verbose_name="接收方式",
    )
    customer_type = models.CharField(
        max_length=16,
        choices=CustomerType.choices,
        default=CustomerType.PERSONAL,
        verbose_name="用户类型快照",
    )
    vehicle_no = models.CharField(
        max_length=64,
        blank=True,
        default="",
        verbose_name="车辆编号快照",
    )
    vehicle = models.ForeignKey(
        CustomerVehicle, null=True, blank=True, on_delete=models.PROTECT,
        related_name="device_bindings", verbose_name="绑定车辆",
    )
    accepted_at = models.DateTimeField(
        default=timezone.now,
        db_index=True,
        verbose_name="接收时间",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.ACTIVE,
        db_index=True,
        verbose_name="绑定状态",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        ordering = ("-accepted_at", "-id")
        indexes = [
            models.Index(
                fields=("customer_organization", "status"),
                name="lc_cdb_org_status_idx",
            ),
            models.Index(
                fields=("source_organization", "accepted_at"),
                name="lc_cdb_source_idx",
            ),
        ]
        verbose_name = "终端用户设备绑定"
        verbose_name_plural = "终端用户设备绑定"

    def __str__(self):
        return f"{self.customer_organization.name}-{self.device.sn}"


class VehicleDeviceAssignment(models.Model):
    """设备与车辆的时态归属事实。

    CustomerDeviceBinding.vehicle 继续保存当前车辆，供现有页面快速查询；本表只追加
    历史事实，为以后临时跨车调装、复装和归还提供无损扩展点。
    """

    class Reason(models.TextChoices):
        INITIAL_INSTALL = "INITIAL_INSTALL", "首次安装"
        ACTIVATION_UPDATE = "ACTIVATION_UPDATE", "激活时确认车辆"
        TEMPORARY_MOVE = "TEMPORARY_MOVE", "临时调装"
        REINSTALL = "REINSTALL", "重新安装"
        RETURN = "RETURN", "归还原车"
        LEGACY = "LEGACY", "历史数据回填"

    device = models.ForeignKey(
        "lifecycle.Device", on_delete=models.PROTECT,
        related_name="vehicle_assignments", verbose_name="设备",
    )
    customer_organization = models.ForeignKey(
        "account.Organization", on_delete=models.PROTECT,
        related_name="vehicle_device_assignments", verbose_name="用户组织",
    )
    vehicle = models.ForeignKey(
        CustomerVehicle, on_delete=models.PROTECT,
        related_name="device_assignments", verbose_name="车辆",
    )
    source_binding = models.ForeignKey(
        CustomerDeviceBinding, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="vehicle_assignment_history", verbose_name="设备绑定来源",
    )
    assigned_by = models.ForeignKey(
        "account.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="vehicle_device_assignments", verbose_name="操作账号",
    )
    reason = models.CharField(
        max_length=24, choices=Reason.choices,
        default=Reason.INITIAL_INSTALL, verbose_name="归属原因",
    )
    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    ended_at = models.DateTimeField(null=True, blank=True, db_index=True)
    is_current = models.BooleanField(default=True, db_index=True)
    note = models.CharField(max_length=255, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-started_at", "-id")
        constraints = [
            models.UniqueConstraint(
                fields=("device",), condition=models.Q(is_current=True),
                name="lc_unique_current_vehicle",
            )
        ]
        indexes = [
            models.Index(
                fields=("customer_organization", "vehicle", "is_current"),
                name="lc_vda_org_vehicle_idx",
            )
        ]

    def __str__(self):
        return f"{self.device.sn}-{self.vehicle.vehicle_no}"
