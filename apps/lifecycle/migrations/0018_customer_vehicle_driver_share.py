import uuid

import django.db.models.deletion
from django.db import migrations, models


def backfill_customer_vehicles(apps, schema_editor):
    CustomerVehicle = apps.get_model("lifecycle", "CustomerVehicle")
    CustomerDeviceBinding = apps.get_model("lifecycle", "CustomerDeviceBinding")
    for binding in CustomerDeviceBinding.objects.all().iterator():
        vehicle_no = (binding.vehicle_no or "").strip() or "未登记车辆"
        normalized = "".join(vehicle_no.upper().split())
        vehicle, _ = CustomerVehicle.objects.get_or_create(
            customer_organization_id=binding.customer_organization_id,
            normalized_vehicle_no=normalized,
            defaults={"vehicle_no": vehicle_no, "status": "ACTIVE"},
        )
        binding.vehicle_id = vehicle.id
        binding.save(update_fields=("vehicle",))


class Migration(migrations.Migration):
    dependencies = [("lifecycle", "0017_customer_device_binding")]

    operations = [
        migrations.CreateModel(
            name="CustomerVehicle",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("vehicle_no", models.CharField(max_length=64, verbose_name="车牌号或车辆编号")),
                ("normalized_vehicle_no", models.CharField(max_length=64, verbose_name="标准化车辆编号")),
                ("status", models.CharField(choices=[("ACTIVE", "有效"), ("INACTIVE", "失效")], db_index=True, default="ACTIVE", max_length=16)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("customer_organization", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="customer_vehicles", to="account.organization", verbose_name="用户组织")),
            ],
            options={"ordering": ("vehicle_no", "id")},
        ),
        migrations.AddConstraint(
            model_name="customervehicle",
            constraint=models.UniqueConstraint(fields=("customer_organization", "normalized_vehicle_no"), name="lc_unique_customer_vehicle"),
        ),
        migrations.CreateModel(
            name="DriverVehicleInvitation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("token", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("status", models.CharField(choices=[("PENDING", "待接受"), ("ACCEPTED", "已接受"), ("EXPIRED", "已过期"), ("CANCELLED", "已取消")], db_index=True, default="PENDING", max_length=16)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("accepted_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("invitee", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="accepted_driver_vehicle_invitations", to="account.user")),
                ("inviter", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="sent_driver_vehicle_invitations", to="account.user")),
                ("vehicle", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="driver_invitations", to="lifecycle.customervehicle")),
            ],
        ),
        migrations.CreateModel(
            name="DriverVehicleBinding",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("driver", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="driver_vehicle_bindings", to="account.user")),
                ("invitation", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="bindings", to="lifecycle.drivervehicleinvitation")),
                ("vehicle", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="driver_bindings", to="lifecycle.customervehicle")),
            ],
        ),
        migrations.AddConstraint(
            model_name="drivervehiclebinding",
            constraint=models.UniqueConstraint(fields=("vehicle", "driver"), name="lc_unique_vehicle_driver"),
        ),
        migrations.AddField(
            model_name="customerdevicebinding",
            name="vehicle",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="device_bindings", to="lifecycle.customervehicle", verbose_name="绑定车辆"),
        ),
        migrations.RunPython(backfill_customer_vehicles, migrations.RunPython.noop),
    ]
