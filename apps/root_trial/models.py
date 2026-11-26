import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class RootTrialLicense(models.Model):
    """Private, validated evidence; storage keys are never public URLs."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="root_trial_licenses",
    )
    storage_key = models.CharField(max_length=255, unique=True, editable=False)
    original_name = models.CharField(max_length=128)
    content_type = models.CharField(max_length=64)
    size = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "试用申请营业执照"
        verbose_name_plural = verbose_name

    def __str__(self):
        return str(self.pk)


class RootTrialApplication(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "待审核"
        APPROVED = "APPROVED", "已通过"
        REJECTED = "REJECTED", "未通过"

    applicant = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="root_trial_applications",
    )
    request_id = models.UUIDField(editable=False)
    organization_name = models.CharField(max_length=128)
    license_upload = models.ForeignKey(
        RootTrialLicense, on_delete=models.PROTECT, related_name="applications",
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True,
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name="reviewed_root_trial_applications",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_reason = models.TextField(blank=True, default="")
    organization = models.ForeignKey(
        "account.Organization", on_delete=models.PROTECT, null=True, blank=True,
        related_name="root_trial_applications",
    )
    identity = models.ForeignKey(
        "account.UserOrganization", on_delete=models.PROTECT, null=True, blank=True,
        related_name="root_trial_applications",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at", "-pk")
        verbose_name = "根组织厂家负责人试用申请"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(
                fields=("applicant", "request_id"), name="rt_applicant_request_unique",
            ),
            models.UniqueConstraint(
                fields=("applicant",), condition=Q(status="PENDING"),
                name="rt_one_pending_per_applicant",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status="PENDING", reviewed_by__isnull=True,
                      reviewed_at__isnull=True, organization__isnull=True,
                      identity__isnull=True)
                    | Q(status="REJECTED", reviewed_by__isnull=False,
                        reviewed_at__isnull=False, organization__isnull=True,
                        identity__isnull=True)
                    | Q(status="APPROVED", reviewed_by__isnull=False,
                        reviewed_at__isnull=False, organization__isnull=False,
                        identity__isnull=False)
                ),
                name="rt_decision_has_consistent_state",
            ),
        ]

    def __str__(self):
        return f"{self.organization_name} · {self.get_status_display()}"
