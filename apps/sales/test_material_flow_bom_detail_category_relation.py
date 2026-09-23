from types import SimpleNamespace
from unittest.mock import Mock

from django.test import SimpleTestCase

from sales.services.order_plan_material_flow_service import _material_queryset


class MaterialFlowBomDetailCategoryRelationTests(SimpleTestCase):
    def test_category_filter_uses_material_category_many_to_many_relation(self):
        """The BOM editor must query the current Material.product_categories relation."""
        bom = Mock()
        bom.items.values_list.return_value = []

        queryset = _material_queryset(
            SimpleNamespace(category_id=1),
            bom,
        )

        self.assertIn("product_categories", str(queryset.query))
