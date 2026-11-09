from django.test import TestCase
from django.urls import reverse


class QualityTraceDevelopmentAccessTests(TestCase):
    def test_dashboard_redirects_anonymous_browser_to_login(self):
        response = self.client.get(reverse("pldp:web:quality_trace:dashboard"))

        self.assertRedirects(response, "/?login=1", fetch_redirect_response=False)
