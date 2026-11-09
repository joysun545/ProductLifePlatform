from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion

class Migration(migrations.Migration):
    initial = True
    dependencies = [migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [migrations.CreateModel(name='WebLoginChallenge', fields=[
        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
        ('token_digest', models.CharField(max_length=64, unique=True)),
        ('browser_digest', models.CharField(max_length=64, db_index=True)),
        ('expires_at', models.DateTimeField(db_index=True)),
        ('approved_at', models.DateTimeField(null=True)),
        ('consumed_at', models.DateTimeField(null=True)),
        ('principal', models.JSONField(default=dict)),
        ('binding', models.CharField(max_length=64, blank=True)),
        ('user', models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
    ])]
