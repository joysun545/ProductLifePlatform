import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("account", "0003_add_factory_production_role"),
        ("lifecycle", "0004_productdocumentread"),
    ]

    operations = [
        migrations.CreateModel(
            name="GoodsTransfer",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("flow_no", models.CharField(db_index=True, max_length=64, unique=True)),
                ("client_request_id", models.CharField(max_length=64, unique=True)),
                ("flow_type", models.CharField(choices=[("SHIPMENT", "向下级发货"), ("RETURN", "向上级退货"), ("TRANSFER", "下级之间调货")], db_index=True, max_length=16)),
                ("status", models.CharField(choices=[("SCANNING", "扫码中"), ("PENDING_RECEIPT", "待确认入库"), ("COMPLETED", "已完成"), ("CANCELLED", "已取消")], db_index=True, default="SCANNING", max_length=24)),
                ("remark", models.CharField(blank=True, default="", max_length=255)),
                ("receipt_required", models.BooleanField(default=False)),
                ("factory_lifecycle_terminated", models.BooleanField(default=False)),
                ("submitted_at", models.DateTimeField(blank=True, null=True)),
                ("received_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("from_organization", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="outgoing_goods_transfers", to="account.organization")),
                ("initiated_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="initiated_goods_transfers", to=settings.AUTH_USER_MODEL)),
                ("instruction_organization", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="instructed_goods_transfers", to="account.organization")),
                ("received_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="received_goods_transfers", to=settings.AUTH_USER_MODEL)),
                ("source_account", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="goods_transfers_as_source_account", to=settings.AUTH_USER_MODEL)),
                ("target_account", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="goods_transfers_as_target_account", to=settings.AUTH_USER_MODEL)),
                ("to_organization", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="incoming_goods_transfers", to="account.organization")),
            ],
            options={"ordering": ("-created_at", "-id")},
        ),
        migrations.CreateModel(
            name="GoodsTransferItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("SCANNED", "已扫码"), ("PENDING_RECEIPT", "待确认入库"), ("COMPLETED", "已完成")], db_index=True, default="SCANNED", max_length=24)),
                ("scanned_at", models.DateTimeField(auto_now_add=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("device", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="goods_transfer_items", to="lifecycle.device")),
                ("lifecycle_event", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="goods_transfer_items", to="lifecycle.lifecycleevent")),
                ("receipt_event", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="received_goods_transfer_items", to="lifecycle.lifecycleevent")),
                ("scanned_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="scanned_goods_transfer_items", to=settings.AUTH_USER_MODEL)),
                ("transfer", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="items", to="lifecycle.goodstransfer")),
            ],
            options={"ordering": ("scanned_at", "id")},
        ),
        migrations.AddIndex(
            model_name="goodstransfer",
            index=models.Index(fields=["instruction_organization", "status"], name="lifecycle_g_instruc_9c5872_idx"),
        ),
        migrations.AddIndex(
            model_name="goodstransfer",
            index=models.Index(fields=["from_organization", "status"], name="lifecycle_g_from_or_629109_idx"),
        ),
        migrations.AddIndex(
            model_name="goodstransfer",
            index=models.Index(fields=["to_organization", "status"], name="lifecycle_g_to_orga_dccdea_idx"),
        ),
        migrations.AddConstraint(
            model_name="goodstransfer",
            constraint=models.CheckConstraint(
                condition=~models.Q(
                    from_organization=models.F("to_organization")
                ),
                name="lifecycle_goods_transfer_orgs_differ",
            ),
        ),
        migrations.AddConstraint(
            model_name="goodstransferitem",
            constraint=models.UniqueConstraint(fields=("transfer", "device"), name="lifecycle_unique_goods_transfer_device"),
        ),
        migrations.AlterField(
            model_name="devicestate",
            name="status",
            field=models.CharField(choices=[("CREATED", "已创建"), ("PRODUCED", "已生产"), ("IN_STOCK", "库存中"), ("SHIPPED", "已发货"), ("INSTALLED", "已安装"), ("ACTIVATED", "已激活"), ("RUNNING", "运行中"), ("REPAIRING", "维修中"), ("SCRAPPED", "已报废"), ("RETURNED_TO_FACTORY", "已退回厂家")], db_index=True, default="CREATED", max_length=32, verbose_name="设备状态"),
        ),
        migrations.AlterField(
            model_name="organizationdevice",
            name="status",
            field=models.CharField(choices=[("IN_STOCK", "库存"), ("INSTALLED", "已安装"), ("SOLD", "已销售"), ("RETURNED", "退货中"), ("TRANSFERRED", "流转中"), ("RETURNED_TO_FACTORY", "已退回厂家")], db_index=True, default="IN_STOCK", max_length=32, verbose_name="设备状态"),
        ),
    ]
