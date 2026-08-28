from django.apps import AppConfig


class QualityTraceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "quality_trace"
    verbose_name = "质量追溯与召回"

    def ready(self):
        from . import signals  # noqa: F401
