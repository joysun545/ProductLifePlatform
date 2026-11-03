from django.conf import settings
from django.db import models


class DeviceCodeRegistryLoginChallenge(models.Model):
    """Browser-bound, one-use WeChat approval for the device-code web page."""

    token_digest = models.CharField(max_length=64, unique=True)
    session_digest = models.CharField(max_length=64, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="device_code_registry_login_approvals",
    )
    identity_id = models.PositiveBigIntegerField(null=True, blank=True)
    organization_id = models.PositiveBigIntegerField(null=True, blank=True)
    role_id = models.PositiveBigIntegerField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(
                fields=("session_digest", "expires_at"),
                name="life_dcr_login_sess_exp",
            ),
        ]
        verbose_name = "设备编码网页登录挑战"
        verbose_name_plural = "设备编码网页登录挑战"


class PhysicalDeviceCodePackagingInfo(models.Model):
    """Third-party manufacturing instructions attached to a code batch."""

    generation_batch_id = models.UUIDField(unique=True)
    factory_root = models.ForeignKey(
        "account.Organization",
        on_delete=models.PROTECT,
        related_name="physical_device_code_packaging_info",
    )
    batch_name = models.CharField(max_length=120)
    maker_name = models.CharField(max_length=120, blank=True)
    label_spec = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_device_code_packaging_info",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="updated_device_code_packaging_info",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-updated_at", "-id")
        verbose_name = "设备编码第三方制作信息"
        verbose_name_plural = "设备编码第三方制作信息"
