# apps/lifecycle/utils/id_generator.py

import uuid
from django.utils import timezone


def gen_life_id():
    return f"PL-{timezone.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"


def gen_device_sn():
    return f"SN-{uuid.uuid4().hex[:10]}"


def gen_qrcode():
    return f"QR-{uuid.uuid4().hex[:12]}"
