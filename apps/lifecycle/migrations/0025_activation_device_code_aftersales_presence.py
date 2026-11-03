import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("lifecycle", "0024_device_physical_device_code"),
    ]

    operations = [
        migrations.AddField(
            model_name="activationrecord",
            name="activation_method",
            field=models.CharField(
                choices=[
                    ("VEHICLE_PLATE", "车牌号"),
                    ("DEVICE_CODE", "设备编码二维码"),
                ],
                default="VEHICLE_PLATE",
                max_length=24,
                verbose_name="激活绑定方式",
            ),
        ),
        migrations.AddField(
            model_name="activationrecord",
            name="physical_device_code_snapshot",
            field=models.CharField(
                blank=True,
                default="",
                max_length=64,
                verbose_name="激活时设备编码快照",
            ),
        ),
        migrations.AddField(
            model_name="aftersalesapplication",
            name="device_presence_verified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="aftersalesapplication",
            name="device_presence_code_snapshot",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AlterField(
            model_name="aftersalesmedia",
            name="purpose",
            field=models.CharField(
                choices=[
                    ("APPLICATION_EVIDENCE", "申请故障证据"),
                    ("PLATE_PRESENCE_EVIDENCE", "车牌实时照片"),
                    ("APPLICANT_SHIPMENT_DOCUMENT", "发起人寄送物流单据"),
                    ("REPAIR_EVIDENCE", "维修过程或测试证据"),
                    ("REPAIR_RETURN_SHIPMENT_DOCUMENT", "维修返件物流单据"),
                    ("THIRD_PARTY_MATERIAL_SHIPMENT_DOCUMENT", "第三方备件寄送单据"),
                    ("THIRD_PARTY_FEE_VOUCHER", "第三方维修费用凭证"),
                    ("THIRD_PARTY_FAULT_MATERIAL_RETURN_DOCUMENT", "第三方故障物料返还单据"),
                ],
                default="APPLICATION_EVIDENCE",
                max_length=64,
            ),
        ),
        migrations.CreateModel(
            name="AfterSalesDeviceCodeVerification",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("token", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("used_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("device", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="after_sales_code_verifications", to="lifecycle.device")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="after_sales_code_verifications", to=settings.AUTH_USER_MODEL)),
                ("device_code", models.CharField(max_length=64)),
            ],
            options={
                "ordering": ("-created_at", "-id"),
                "indexes": [
                    models.Index(fields=["device", "user", "expires_at"], name="life_as_code_verify_idx"),
                ],
            },
        ),
    ]
