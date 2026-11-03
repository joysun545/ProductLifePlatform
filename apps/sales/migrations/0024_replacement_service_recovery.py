import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [('lifecycle','0032_replacement_service_recovery'),('sales','0023_order_plan_accessories')]
    operations = [

        migrations.AddField(
                    model_name='factoryfinishedgoodsinventoryitem',
                    name='replacement_flow',
                    field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='finished_stock', to='lifecycle.replacementflow'),
                ),
        migrations.AlterField(
                    model_name='factoryfinishedgoodsinventoryitem',
                    name='source_order_plan',
                    field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='cancelled_finished_goods_inventory_items', to='sales.orderplan', verbose_name='来源订单计划'),
                ),
        migrations.AlterField(
                    model_name='factoryfinishedgoodsinventoryitem',
                    name='source_subplan_item',
                    field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='finished_goods_inventory_items', to='sales.orderplanproductionsubplanitem', verbose_name='来源生产子计划产品项'),
                ),
        migrations.AddConstraint(model_name='factoryfinishedgoodsinventoryitem',constraint=models.CheckConstraint(condition=(
            models.Q(replacement_flow__isnull=False,source_order_plan__isnull=True,source_subplan_item__isnull=True)
            | models.Q(replacement_flow__isnull=True,source_order_plan__isnull=False,source_subplan_item__isnull=False)),name='sales_fg_valid_source')),
    ]
