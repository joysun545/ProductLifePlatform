from django.db import models
from django.utils import timezone


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
