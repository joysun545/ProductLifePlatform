"""Short-lived, browser-bound print-center login approvals."""
from django.conf import settings
from django.db import models


class PrintLoginChallenge(models.Model):
    token_digest = models.CharField(max_length=64, unique=True)
    session_digest = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="print_login_approvals",
    )
    identity_id = models.PositiveBigIntegerField(null=True, blank=True)
    organization_id = models.PositiveBigIntegerField(null=True, blank=True)
    role_id = models.PositiveBigIntegerField(null=True, blank=True)
