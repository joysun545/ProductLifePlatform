import django.core.files.storage
import django.db.models.deletion
from django.db import migrations, models

import sales.private_storage


class Migration(migrations.Migration):

    dependencies = [
        ("account", "0003_add_factory_production_role"),
        ("sales", "0006_orderplan_fulfillment_audit"),
    ]

    operations = [
        migrations.CreateModel(
            name="OrderPlanFinance",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("statement_image", models.FileField(blank=True, storage=sales.private_storage.OrderFinancePrivateStorage(), upload_to="statements/%Y/%m/", verbose_name="订单清单截图")),
                ("statement_original_name", models.CharField(blank=True, default="", max_length=255, verbose_name="清单原文件名")),
                ("receivable_amount", models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True, verbose_name="应收总额")),
                ("statement_submitted_at", models.DateTimeField(blank=True, db_index=True, null=True, verbose_name="清单提交时间")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("order_plan", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="finance", to="sales.orderplan", verbose_name="订单计划")),
                ("statement_submitted_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="submitted_order_finance_statements", to="account.user", verbose_name="清单提交人")),
            ],
            options={
                "verbose_name": "订单资金主账",
                "verbose_name_plural": "订单资金主账",
                "ordering": ("-created_at", "-id"),
            },
        ),
        migrations.CreateModel(
            name="OrderPlanPayment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sequence_no", models.PositiveIntegerField(verbose_name="付款批次")),
                ("client_request_id", models.CharField(max_length=64, verbose_name="客户端请求标识")),
                ("voucher_image", models.FileField(storage=sales.private_storage.OrderFinancePrivateStorage(), upload_to="payment_vouchers/%Y/%m/", verbose_name="付款凭证")),
                ("voucher_original_name", models.CharField(blank=True, default="", max_length=255, verbose_name="凭证原文件名")),
                ("uploaded_at", models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="凭证上传时间")),
                ("confirmed_amount", models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True, verbose_name="确认收款金额")),
                ("confirmed_at", models.DateTimeField(blank=True, db_index=True, null=True, verbose_name="收款确认时间")),
                ("status", models.CharField(choices=[("PENDING", "待确认收款"), ("CONFIRMED", "已确认收款")], db_index=True, default="PENDING", max_length=16, verbose_name="收款状态")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("confirmed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="confirmed_order_payments", to="account.user", verbose_name="收款确认人")),
                ("finance", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="payments", to="sales.orderplanfinance", verbose_name="订单资金主账")),
                ("uploaded_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="uploaded_order_payment_vouchers", to="account.user", verbose_name="凭证上传人")),
            ],
            options={
                "verbose_name": "订单付款记录",
                "verbose_name_plural": "订单付款记录",
                "ordering": ("sequence_no", "id"),
            },
        ),
        migrations.AddConstraint(
            model_name="orderplanfinance",
            constraint=models.CheckConstraint(condition=models.Q(("receivable_amount__isnull", True), ("receivable_amount__gt", 0), _connector="OR"), name="sales_finance_receivable_positive_or_null"),
        ),
        migrations.AddConstraint(
            model_name="orderplanpayment",
            constraint=models.UniqueConstraint(fields=("finance", "sequence_no"), name="sales_unique_finance_payment_sequence"),
        ),
        migrations.AddConstraint(
            model_name="orderplanpayment",
            constraint=models.UniqueConstraint(fields=("finance", "client_request_id"), name="sales_unique_finance_payment_request"),
        ),
        migrations.AddConstraint(
            model_name="orderplanpayment",
            constraint=models.CheckConstraint(condition=models.Q(("confirmed_amount__isnull", True), ("confirmed_amount__gt", 0), _connector="OR"), name="sales_payment_amount_positive_or_null"),
        ),
    ]
