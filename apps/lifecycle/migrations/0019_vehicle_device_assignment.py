import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q
from django.utils import timezone


def backfill_assignments(apps, schema_editor):
    Binding = apps.get_model("lifecycle", "CustomerDeviceBinding")
    Assignment = apps.get_model("lifecycle", "VehicleDeviceAssignment")
    for binding in Binding.objects.exclude(vehicle_id=None).iterator():
        Assignment.objects.get_or_create(
            device_id=binding.device_id,
            is_current=True,
            defaults={
                "customer_organization_id": binding.customer_organization_id,
                "vehicle_id": binding.vehicle_id,
                "source_binding_id": binding.id,
                "assigned_by_id": binding.accepted_by_id,
                "reason": "LEGACY",
                "started_at": binding.accepted_at,
                "metadata": {"backfilled_from_customer_device_binding": True},
            },
        )


class Migration(migrations.Migration):
    dependencies = [("lifecycle", "0018_customer_vehicle_driver_share")]

    operations = [
        migrations.CreateModel(
            name="VehicleDeviceAssignment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("reason", models.CharField(choices=[("INITIAL_INSTALL", "首次安装"), ("ACTIVATION_UPDATE", "激活时确认车辆"), ("TEMPORARY_MOVE", "临时调装"), ("REINSTALL", "重新安装"), ("RETURN", "归还原车"), ("LEGACY", "历史数据回填")], default="INITIAL_INSTALL", max_length=24, verbose_name="归属原因")),
                ("started_at", models.DateTimeField(db_index=True, default=timezone.now)),
                ("ended_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("is_current", models.BooleanField(db_index=True, default=True)),
                ("note", models.CharField(blank=True, default="", max_length=255)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("assigned_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="vehicle_device_assignments", to=settings.AUTH_USER_MODEL, verbose_name="操作账号")),
                ("customer_organization", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="vehicle_device_assignments", to="account.organization", verbose_name="用户组织")),
                ("device", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="vehicle_assignments", to="lifecycle.device", verbose_name="设备")),
                ("source_binding", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="vehicle_assignment_history", to="lifecycle.customerdevicebinding", verbose_name="设备绑定来源")),
                ("vehicle", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="device_assignments", to="lifecycle.customervehicle", verbose_name="车辆")),
            ],
            options={"ordering": ("-started_at", "-id")},
        ),
        migrations.AddConstraint(
            model_name="vehicledeviceassignment",
            constraint=models.UniqueConstraint(condition=Q(is_current=True), fields=("device",), name="lc_unique_current_vehicle"),
        ),
        migrations.AddIndex(
            model_name="vehicledeviceassignment",
            index=models.Index(fields=["customer_organization", "vehicle", "is_current"], name="lc_vda_org_vehicle_idx"),
        ),
        migrations.RunPython(backfill_assignments, migrations.RunPython.noop),
    ]
