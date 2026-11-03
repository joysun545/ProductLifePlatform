import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [('account', '0012_senior_manager_delegation'), ('lifecycle','0030_repair_escalation'), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(
                    name='ReplacementFlow',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('mode', models.CharField(choices=[('LOCAL', '组织库存换新'), ('FACTORY', '厂家直接换新')], max_length=16)),
                        ('chain', models.JSONField(default=list)),
                        ('activation_method', models.CharField(max_length=24)),
                        ('first_activation_at', models.DateTimeField()),
                        ('activation_snapshot', models.JSONField(default=dict)),
                        ('swapped_at', models.DateTimeField(blank=True, null=True)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('application', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='replacement_flow', to='lifecycle.aftersalesapplication')),
                        ('execution_organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='account.organization')),
                    ],
                ),
        migrations.CreateModel(
                    name='ReplacementLeg',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('direction', models.CharField(choices=[('OLD', '旧机返厂'), ('SUPPLY', '换新补发'), ('USER', '新机交付用户')], max_length=16)),
                        ('sequence', models.PositiveIntegerField()),
                        ('status', models.CharField(choices=[('PENDING', '待发出'), ('SENT', '已发出待签收'), ('RECEIVED', '已签收')], db_index=True, default='PENDING', max_length=16)),
                        ('shipment_id', models.PositiveBigIntegerField(blank=True, null=True)),
                        ('logistics', models.JSONField(default=dict)),
                        ('sent_at', models.DateTimeField(blank=True, null=True)),
                        ('received_at', models.DateTimeField(blank=True, null=True)),
                        ('receipt_note', models.TextField(blank=True)),
                        ('device', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to='lifecycle.device')),
                        ('flow', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='legs', to='lifecycle.replacementflow')),
                        ('from_organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='account.organization')),
                        ('received_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to=settings.AUTH_USER_MODEL)),
                        ('sent_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to=settings.AUTH_USER_MODEL)),
                        ('to_organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='account.organization')),
                    ],
                    options={
                        'ordering': ['direction', 'sequence'],
                    },
                ),
        migrations.CreateModel(
                    name='ReplacementInventoryEntry',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('kind', models.CharField(max_length=24)),
                        ('quantity', models.SmallIntegerField()),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('actor', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
                        ('device', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='lifecycle.device')),
                        ('flow', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='inventory_entries', to='lifecycle.replacementflow')),
                        ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='account.organization')),
                        ('leg', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='lifecycle.replacementleg')),
                    ],
                ),
        migrations.AddConstraint(
                    model_name='replacementleg',
                    constraint=models.UniqueConstraint(fields=('flow', 'direction', 'sequence'), name='replacement_unique_leg'),
                ),
        migrations.AddConstraint(
                    model_name='replacementinventoryentry',
                    constraint=models.UniqueConstraint(fields=('leg', 'organization', 'kind'), name='replacement_unique_stock_fact'),
                ),
    ]
