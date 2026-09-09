import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models
from django.db.models import F, Q


class Migration(migrations.Migration):

    dependencies = [
        ("account", "0002_seed_account_reference_data"),
        ("channel", "0001_initial"),
        ("product", "0006_productdocument_file_fields"),
        ("sales", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="OrderPlan",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("plan_code", models.CharField(db_index=True, max_length=64, unique=True, verbose_name="订单计划编码")),
                ("client_request_id", models.CharField(max_length=64, verbose_name="客户端请求标识")),
                ("region_snapshot", models.CharField(max_length=255, verbose_name="区域快照")),
                ("creator_role_code", models.CharField(default="factory_sales", max_length=64, verbose_name="创建岗位快照")),
                ("total_quantity", models.PositiveIntegerField(verbose_name="计划总数量")),
                ("remark", models.TextField(blank=True, default="", verbose_name="订单备注")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("MATCHING_PENDING", "待物料配套"),
                            ("MATCHING_COMPLETED", "配套完成"),
                            ("CANCELLED", "已取消"),
                        ],
                        db_index=True,
                        default="MATCHING_PENDING",
                        max_length=32,
                        verbose_name="计划状态",
                    ),
                ),
                ("submitted_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now, verbose_name="提交时间")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                (
                    "brand",
                    models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="order_plans", to="product.brand", verbose_name="品牌"),
                ),
                (
                    "channel_assignment",
                    models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="order_plans", to="channel.merchantchannelassignment", verbose_name="渠道授权"),
                ),
                (
                    "created_by",
                    models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="created_order_plans", to=settings.AUTH_USER_MODEL, verbose_name="创建销售经理"),
                ),
                (
                    "factory_organization",
                    models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="factory_order_plans", to="account.organization", verbose_name="厂家组织"),
                ),
                (
                    "merchant_organization",
                    models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="merchant_order_plans", to="account.organization", verbose_name="直接商家"),
                ),
                (
                    "merchant_relation",
                    models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="order_plans", to="account.organizationrelation", verbose_name="经销关系"),
                ),
            ],
            options={
                "verbose_name": "订单计划",
                "verbose_name_plural": "订单计划",
                "ordering": ("-submitted_at", "-id"),
                "constraints": [
                    models.UniqueConstraint(fields=("created_by", "client_request_id"), name="sales_unique_order_plan_client_request"),
                    models.CheckConstraint(condition=Q(total_quantity__gt=0), name="sales_order_plan_total_quantity_positive"),
                    models.CheckConstraint(condition=~Q(factory_organization=F("merchant_organization")), name="sales_order_plan_organizations_not_self"),
                ],
            },
        ),
        migrations.CreateModel(
            name="OrderPlanItem",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("quantity", models.PositiveIntegerField(verbose_name="计划数量")),
                ("order_batch_code", models.CharField(db_index=True, max_length=64, unique=True, verbose_name="订单批次编码")),
                ("lifecycle_code", models.CharField(db_index=True, max_length=64, unique=True, verbose_name="生命周期链路编码")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("MATCHING_PENDING", "待物料配套"),
                            ("MATCHING_COMPLETED", "配套完成"),
                            ("CANCELLED", "已取消"),
                        ],
                        db_index=True,
                        default="MATCHING_PENDING",
                        max_length=32,
                        verbose_name="明细状态",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                (
                    "category",
                    models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="order_plan_items", to="product.productcategory", verbose_name="产品品类"),
                ),
                (
                    "plan",
                    models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="items", to="sales.orderplan", verbose_name="订单计划"),
                ),
                (
                    "product_model",
                    models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="order_plan_items", to="product.productmodel", verbose_name="产品型号"),
                ),
            ],
            options={
                "verbose_name": "订单计划明细",
                "verbose_name_plural": "订单计划明细",
                "ordering": ("plan_id", "id"),
                "constraints": [
                    models.UniqueConstraint(fields=("plan", "product_model"), name="sales_unique_order_plan_product_model"),
                    models.CheckConstraint(condition=Q(quantity__gt=0), name="sales_order_plan_item_quantity_positive"),
                ],
            },
        ),
    ]
