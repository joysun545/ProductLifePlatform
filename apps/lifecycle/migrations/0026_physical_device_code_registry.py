from django.db import migrations, models
import django.db.models.deletion


def backfill_already_bound_codes(apps, schema_editor):
    alias = schema_editor.connection.alias
    Device = apps.get_model("lifecycle", "Device")
    ActivationRecord = apps.get_model("lifecycle", "ActivationRecord")
    ProductionBatch = apps.get_model("manufacturing", "ProductionBatch")
    Organization = apps.get_model("account", "Organization")
    PhysicalDeviceCode = apps.get_model("lifecycle", "PhysicalDeviceCode")

    for device in Device.objects.using(alias).exclude(
        physical_device_code__isnull=True
    ).exclude(physical_device_code="").values(
        "id", "physical_device_code", "batch_id", "created_at"
    ).iterator():
        batch = ProductionBatch.objects.using(alias).filter(
            pk=device["batch_id"]
        ).values("manufacturer_id").first()
        if not batch:
            continue
        manufacturer = Organization.objects.using(alias).filter(
            pk=batch["manufacturer_id"]
        ).values("id", "factory_root_id", "organization_type__code").first()
        if not manufacturer:
            continue
        root_id = manufacturer["factory_root_id"]
        if not root_id and manufacturer["organization_type__code"] == "OWNER":
            root_id = manufacturer["id"]
        if not root_id:
            # Keep legacy device data intact; an operator can review an
            # organization with a missing tenant root separately.
            continue

        activation = ActivationRecord.objects.using(alias).filter(
            device_id=device["id"]
        ).values("activation_time", "operator_id").first()
        defaults = {
            "factory_root_id": root_id,
            "status": "BOUND",
            "device_id": device["id"],
            "generated_by_id": None,
            "bound_by_id": activation["operator_id"] if activation else None,
            "bound_at": activation["activation_time"] if activation else None,
        }
        PhysicalDeviceCode.objects.using(alias).get_or_create(
            code=device["physical_device_code"],
            defaults=defaults,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("lifecycle", "0025_activation_device_code_aftersales_presence"),
    ]

    operations = [
        migrations.CreateModel(
            name="PhysicalDeviceCode",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(max_length=64, unique=True, verbose_name="设备编码")),
                ("generation_batch_id", models.UUIDField(blank=True, db_index=True, null=True, verbose_name="生成批次号")),
                ("status", models.CharField(choices=[("GENERATED", "已生成待绑定"), ("BOUND", "已绑定设备"), ("DISABLED", "已停用")], db_index=True, default="GENERATED", max_length=16, verbose_name="编码状态")),
                ("generated_at", models.DateTimeField(auto_now_add=True, verbose_name="生成时间")),
                ("bound_at", models.DateTimeField(blank=True, null=True, verbose_name="绑定时间")),
                ("disabled_at", models.DateTimeField(blank=True, null=True, verbose_name="停用时间")),
                ("bound_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="bound_physical_device_codes", to="account.user", verbose_name="绑定操作人")),
                ("device", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="physical_device_code_record", to="lifecycle.device", verbose_name="绑定设备")),
                ("factory_root", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="issued_physical_device_codes", to="account.organization", verbose_name="发行厂家根组织")),
                ("generated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="generated_physical_device_codes", to="account.user", verbose_name="生成人员")),
            ],
            options={
                "verbose_name": "设备编码登记",
                "verbose_name_plural": "设备编码登记",
                "ordering": ("-generated_at", "-id"),
                "indexes": [models.Index(fields=["factory_root", "status"], name="life_device_code_root_status")],
            },
        ),
        migrations.RunPython(backfill_already_bound_codes, migrations.RunPython.noop),
    ]
