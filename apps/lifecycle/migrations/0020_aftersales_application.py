from django.db import migrations, models
import django.db.models.deletion
import lifecycle.models.aftersales
import lifecycle.private_storage


class Migration(migrations.Migration):
    dependencies = [("lifecycle", "0019_vehicle_device_assignment")]
    operations = [
        migrations.CreateModel(
            name="AfterSalesApplication",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("case_no", models.CharField(max_length=48, unique=True, verbose_name="售后单号")),
                ("applicant_role_code", models.CharField(blank=True, default="", max_length=64)),
                ("applicant_role_name", models.CharField(blank=True, default="", max_length=64)),
                ("status", models.CharField(choices=[("DRAFT", "草稿"), ("SUBMITTED", "已提交"), ("ROUTING", "待分派"), ("PROCESSING", "处理中"), ("WAITING_CUSTOMER_CONFIRM", "待用户确认"), ("PENDING_FACTORY_CLOSE", "待厂家结案"), ("CLOSED", "已完成"), ("CANCELLED", "已取消")], default="DRAFT", max_length=32)),
                ("fault_description", models.TextField(blank=True, default="")),
                ("location_latitude", models.DecimalField(blank=True, decimal_places=7, max_digits=10, null=True)),
                ("location_longitude", models.DecimalField(blank=True, decimal_places=7, max_digits=10, null=True)),
                ("location_name", models.CharField(blank=True, default="", max_length=128)),
                ("location_address", models.CharField(blank=True, default="", max_length=255)),
                ("location_confirmed", models.BooleanField(default=False)),
                ("location_confirmed_at", models.DateTimeField(blank=True, null=True)),
                ("activation_snapshot", models.JSONField(blank=True, default=dict)),
                ("device_snapshot", models.JSONField(blank=True, default=dict)),
                ("bom_snapshot", models.JSONField(blank=True, default=dict)),
                ("warranty_snapshot", models.JSONField(blank=True, default=dict)),
                ("submitted_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("applicant", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="after_sales_applications", to="account.user")),
                ("customer_organization", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="customer_after_sales", to="account.organization")),
                ("device", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="after_sales_applications", to="lifecycle.device")),
                ("factory_organization", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="factory_after_sales", to="account.organization")),
            ],
            options={"ordering": ("-created_at", "-id")},
        ),
        migrations.CreateModel(
            name="AfterSalesEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_type", models.CharField(max_length=64)),
                ("actor_openid", models.CharField(blank=True, default="", max_length=128)),
                ("actor_role_code", models.CharField(blank=True, default="", max_length=64)),
                ("actor_role_name", models.CharField(blank=True, default="", max_length=64)),
                ("organization_id_snapshot", models.PositiveBigIntegerField(blank=True, null=True)),
                ("organization_name_snapshot", models.CharField(blank=True, default="", max_length=128)),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="after_sales_events", to="account.user")),
                ("application", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="events", to="lifecycle.aftersalesapplication")),
            ], options={"ordering": ("created_at", "id")},
        ),
        migrations.CreateModel(
            name="AfterSalesMedia",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("media_type", models.CharField(choices=[("IMAGE", "图片"), ("VIDEO", "视频")], max_length=16)),
                ("file", models.FileField(storage=lifecycle.private_storage.AfterSalesPrivateStorage(), upload_to=lifecycle.models.aftersales.aftersales_media_upload_to)),
                ("original_name", models.CharField(blank=True, default="", max_length=255)),
                ("content_type", models.CharField(blank=True, default="", max_length=128)),
                ("file_size", models.PositiveBigIntegerField(default=0)),
                ("duration_seconds", models.PositiveIntegerField(blank=True, null=True)),
                ("sequence", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("application", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="media", to="lifecycle.aftersalesapplication")),
                ("uploaded_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="after_sales_media", to="account.user")),
            ], options={"ordering": ("sequence", "id")},
        ),
        migrations.AddIndex(model_name="aftersalesapplication", index=models.Index(fields=["factory_organization", "status"], name="life_as_factory_status")),
        migrations.AddConstraint(model_name="aftersalesapplication", constraint=models.UniqueConstraint(condition=models.Q(("status__in", ("DRAFT", "SUBMITTED", "ROUTING", "PROCESSING", "WAITING_CUSTOMER_CONFIRM", "PENDING_FACTORY_CLOSE"))), fields=("device",), name="life_one_active_aftersale")),
    ]
