from django.test import TestCase
from django.urls import reverse


class QualityTraceDevelopmentAccessTests(TestCase):
    def test_dashboard_is_available_without_web_login_in_development(self):
        response = self.client.get(reverse("pldp:web:quality_trace:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "开发模式：当前未启用扫码临时会话门禁。")
