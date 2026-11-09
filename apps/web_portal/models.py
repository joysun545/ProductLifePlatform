from django.conf import settings
from django.db import models

class WebLoginChallenge(models.Model):
    token_digest = models.CharField(max_length=64, unique=True)
    browser_digest = models.CharField(max_length=64, db_index=True)
    expires_at = models.DateTimeField(db_index=True)
    approved_at = models.DateTimeField(null=True)
    consumed_at = models.DateTimeField(null=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.CASCADE)
    principal = models.JSONField(default=dict)
    binding = models.CharField(max_length=64, blank=True)
