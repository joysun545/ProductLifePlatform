"""The production URL tree imports apps.print; Django registers print."""
from importlib import import_module

from django.apps import apps
from django.test import SimpleTestCase, override_settings
from django.urls import include, path, resolve, reverse


# Match the nesting and apps.print import in the production traceback.
web_patterns = [path('print/', include('apps.print.urls'))]
pldp_patterns = [path('web/', include((web_patterns, 'web'), namespace='web'))]
urlpatterns = [path('pldp/', include((pldp_patterns, 'pldp'), namespace='pldp'))]


class PrintImportCompatibilityTests(SimpleTestCase):
    def test_both_import_paths_use_the_registered_model_and_view(self):
        canonical = import_module('print.views')
        legacy = import_module('apps.print.views')
        legacy_urls = import_module('apps.print.urls')
        self.assertIs(canonical.PrintLoginChallenge, apps.get_model('print', 'PrintLoginChallenge'))
        self.assertIs(legacy.PrintLoginChallenge, canonical.PrintLoginChallenge)
        self.assertIs(legacy_urls.views, canonical)
        self.assertEqual(canonical.PrintLoginChallenge.__module__, 'print.models')

    @override_settings(ROOT_URLCONF=__name__)
    def test_production_nested_url_tree_resolves_and_redirects_to_portal(self):
        url = reverse('pldp:web:print:center')
        self.assertEqual(url, '/pldp/web/print/')
        self.assertIs(resolve(url).func, import_module('print.views').print_center)
        response = self.client.get(url)
        self.assertRedirects(response, '/?login=1', fetch_redirect_response=False)
