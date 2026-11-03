import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("lifecycle", "0026_physical_device_code_registry"),
    ]

    operations = [
        migrations.CreateModel(
            name="DeviceCodeRegistryLoginChallenge",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("token_digest", models.CharField(max_length=64, unique=True)),
                ("session_digest", models.CharField(db_index=True, max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("approved_at", models.DateTimeField(blank=True, null=True)),
                ("consumed_at", models.DateTimeField(blank=True, null=True)),
                ("identity_id", models.PositiveBigIntegerField(blank=True, null=True)),
                ("organization_id", models.PositiveBigIntegerField(blank=True, null=True)),
                ("role_id", models.PositiveBigIntegerField(blank=True, null=True)),
                ("approved_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="device_code_registry_login_approvals", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "verbose_name": "设备编码网页登录挑战",
                "verbose_name_plural": "设备编码网页登录挑战",
            },
        ),
        migrations.CreateModel(
            name="PhysicalDeviceCodePackagingInfo",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("generation_batch_id", models.UUIDField(unique=True)),
                ("batch_name", models.CharField(max_length=120)),
                ("maker_name", models.CharField(blank=True, max_length=120)),
                ("label_spec", models.TextField(blank=True)),
                ("notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="created_device_code_packaging_info", to=settings.AUTH_USER_MODEL)),
                ("factory_root", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="physical_device_code_packaging_info", to="account.organization")),
                ("updated_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="updated_device_code_packaging_info", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "verbose_name": "设备编码第三方制作信息",
                "verbose_name_plural": "设备编码第三方制作信息",
                "ordering": ("-updated_at", "-id"),
            },
        ),
        migrations.AddIndex(
            model_name="devicecoderegistryloginchallenge",
            index=models.Index(fields=["session_digest", "expires_at"], name="life_dcr_login_sess_exp"),
        ),
    ]
