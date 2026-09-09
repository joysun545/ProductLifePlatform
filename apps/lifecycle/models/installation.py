import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


class InstallationRecord(models.Model):
    """设备第一次完成装车的事实记录。"""

    class Status(models.TextChoices):
        INSTALLED = "INSTALLED", "已安装"
        CANCELLED = "CANCELLED", "已取消"

    device = models.OneToOneField(
        "lifecycle.Device",
        on_delete=models.PROTECT,
        related_name="installation",
        verbose_name="设备",
    )
    organization = models.ForeignKey(
        "account.Organization",
        on_delete=models.PROTECT,
        related_name="installation_records",
        verbose_name="安装组织",
    )
    operator = models.ForeignKey(
        "account.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="installation_records",
        verbose_name="安装操作账号",
    )
    lifecycle_event = models.OneToOneField(
        "lifecycle.LifecycleEvent",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="installation_record",
        verbose_name="安装生命周期事件",
    )
    installed_at = models.DateTimeField(
        default=timezone.now,
        db_index=True,
        verbose_name="安装时间",
    )
    location = models.CharField(
        max_length=128,
        blank=True,
        default="",
        verbose_name="安装地点",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.INSTALLED,
        db_index=True,
        verbose_name="记录状态",
    )
    remark = models.TextField(
        blank=True,
        default="",
        verbose_name="备注",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        ordering = ("-installed_at", "-id")
        verbose_name = "设备安装记录"
        verbose_name_plural = "设备安装记录"

    def __str__(self):
        return f"{self.device.sn}-{self.organization.name}"


class InstallationInvitation(models.Model):
    """装车后交给用户负责人的一次性临时邀请。"""

    class Status(models.TextChoices):
        PENDING = "PENDING", "待使用"
        ACCEPTED = "ACCEPTED", "已接受"
        CANCELLED = "CANCELLED", "已取消"
        EXPIRED = "EXPIRED", "已过期"

    token = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
        verbose_name="邀请令牌",
    )
    installation = models.ForeignKey(
        InstallationRecord,
        on_delete=models.PROTECT,
        related_name="customer_invitations",
        verbose_name="安装记录",
    )
    device = models.ForeignKey(
        "lifecycle.Device",
        on_delete=models.PROTECT,
        related_name="installation_invitations",
        verbose_name="设备",
    )
    inviter = models.ForeignKey(
        "account.User",
        on_delete=models.PROTECT,
        related_name="sent_installation_invitations",
        verbose_name="邀请账号",
    )
    source_organization = models.ForeignKey(
        "account.Organization",
        on_delete=models.PROTECT,
        related_name="installation_invitations",
        verbose_name="安装组织",
    )
    target_role = models.ForeignKey(
        "account.OrganizationRole",
        on_delete=models.PROTECT,
        related_name="installation_invitations",
        verbose_name="指定用户岗位",
    )
    target_organization = models.ForeignKey(
        "account.Organization",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="accepted_installation_invitations",
        verbose_name="接受邀请的用户组织",
    )
    invitee = models.ForeignKey(
        "account.User",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="received_installation_invitations",
        verbose_name="接受邀请账号",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
        verbose_name="邀请状态",
    )
    expires_at = models.DateTimeField(db_index=True, verbose_name="失效时间")
    accepted_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="接受时间",
    )
    customer_data = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="用户确认资料",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        ordering = ("-created_at", "-id")
        constraints = [
            models.CheckConstraint(
                condition=(
                    ~Q(status="ACCEPTED")
                    | (Q(invitee__isnull=False) & Q(accepted_at__isnull=False))
                ),
                name="lifecycle_install_invite_accepted_data",
            ),
            models.CheckConstraint(
                condition=(Q(invitee__isnull=True) | ~Q(invitee=F("inviter"))),
                name="lifecycle_install_invite_not_self",
            ),
        ]
        verbose_name = "安装用户临时邀请"
        verbose_name_plural = "安装用户临时邀请"

    @property
    def is_usable(self):
        return self.status == self.Status.PENDING and self.expires_at > timezone.now()

    def clean(self):
        super().clean()
        errors = {}

        if self.installation_id and self.device_id:
            if self.installation.device_id != self.device_id:
                errors["device"] = "邀请设备必须与安装记录中的设备一致。"

        if self.installation_id and self.source_organization_id:
            if (
                self.installation.organization_id
                != self.source_organization_id
            ):
                errors["source_organization"] = (
                    "邀请组织必须与安装记录中的组织一致。"
                )

        if self.target_role_id:
            if (
                self.target_role.organization_type.code != "CUSTOMER"
                or self.target_role.code != "customer_owner"
            ):
                errors["target_role"] = "安装邀请只能指定用户负责人岗位。"

        if (
            self.status == self.Status.ACCEPTED
            and (
                not self.invitee_id
                or not self.target_organization_id
                or not self.accepted_at
            )
        ):
            errors["status"] = (
                "已接受的邀请必须记录接受账号、用户组织和接受时间。"
            )

        if self.inviter_id and self.invitee_id:
            if self.inviter_id == self.invitee_id:
                errors["invitee"] = "邀请账号不能接受自己的邀请。"

        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.device.sn}-{self.status}"
