import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [('lifecycle','0031_replacement_channel_activation'),('account','0012_senior_manager_delegation'),('product','0007_remove_productmodel_brand'), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.AddField(
                    model_name='replacementflow',
                    name='protocol_version',
                    field=models.PositiveSmallIntegerField(default=5),
                ),
        migrations.AddField(
                    model_name='replacementflow',
                    name='recovery',
                    field=models.JSONField(default=dict),
                ),
        migrations.AddField(
                    model_name='replacementflow',
                    name='service_confirmed_at',
                    field=models.DateTimeField(blank=True, null=True),
                ),
        migrations.AddField(
                    model_name='replacementflow',
                    name='service_mode',
                    field=models.CharField(blank=True, default='', max_length=8),
                ),
        migrations.CreateModel(
                    name='ReplacementTurnoverStock',
                    fields=[
                        ('id', models.BigAutoField(primary_key=True, serialize=False)),
                        ('status', models.CharField(default='AVAILABLE', max_length=16)),
                        ('stored_at', models.DateTimeField(auto_now_add=True)),
                        ('device', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='replacement_turnover_stock', to='lifecycle.device')),
                        ('factory', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='account.organization')),
                        ('flow', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='turnover_stock', to='lifecycle.replacementflow')),
                        ('stored_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
                    ],
                ),
        migrations.CreateModel(
                    name='ReplacementMaterialReturn',
                    fields=[
                        ('id', models.BigAutoField(primary_key=True, serialize=False)),
                        ('quantity', models.DecimalField(decimal_places=3, max_digits=14)),
                        ('status', models.CharField(default='NOTIFIED', max_length=16)),
                        ('snapshot', models.JSONField(default=dict)),
                        ('logistics', models.JSONField(default=dict)),
                        ('sent_at', models.DateTimeField(null=True)),
                        ('received_at', models.DateTimeField(null=True)),
                        ('bom_item', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='product.bomitem')),
                        ('flow', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='material_returns', to='lifecycle.replacementflow')),
                        ('received_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to=settings.AUTH_USER_MODEL)),
                        ('sent_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to=settings.AUTH_USER_MODEL)),
                        ('supplier', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='account.organization')),
                    ],
                    options={
                        'constraints': [models.UniqueConstraint(fields=('flow', 'bom_item'), name='replacement_unique_return_part'), models.CheckConstraint(condition=models.Q(('quantity__gt', 0)), name='replacement_return_quantity_positive')],
                    },
                ),
    ]
