from pathlib import Path

from django.conf import settings
from django.core.files.storage import FileSystemStorage


class RootTrialPrivateStorage(FileSystemStorage):
    """Physically outside MEDIA_ROOT and without any public URL."""

    def __init__(self):
        location = getattr(
            settings, "ROOT_TRIAL_PRIVATE_ROOT",
            Path(settings.BASE_DIR).parent / f".{Path(settings.BASE_DIR).name}_private" / "root_trial",
        )
        super().__init__(
            location=location, base_url=None,
            file_permissions_mode=0o600, directory_permissions_mode=0o700,
        )

    def url(self, name):
        raise ValueError("营业执照只能通过已鉴权的私有接口访问。")


def private_storage():
    # Construct per operation so deployment/test setting changes are respected.
    return RootTrialPrivateStorage()
