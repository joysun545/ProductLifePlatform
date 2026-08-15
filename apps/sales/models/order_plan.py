from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


class OrderPlan(models.Model):
    """厂家销售经理提交的一张多型号订单计划主单。"""

    class Status(models.TextChoices):
        MATCHING_PENDING = "MATCHING_PENDING", "待物料配套"
        MATCHING_COMPLETED = "MATCHING_COMPLETED", "配套完成"
        PRODUCTION_PENDING = "PRODUCTION_PENDING", "待生产完成"
        PRODUCTION_COMPLETED = "PRODUCTION_COMPLETED", "生产完成"
        SHIPPED = "SHIPPED", "已发货"
        RECEIVED = "RECEIVED", "商家已入库"
        CANCELLED = "CANCELLED", "已取消"

    plan_code = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        verbose_name="订单计划编码",
    )
    client_request_id = models.CharField(
        max_length=64,
        verbose_name="客户端请求标识",
    )
    factory_organization = models.ForeignKey(
        "account.Organization",
        on_delete=models.PROTECT,
        related_name="factory_order_plans",
        verbose_name="厂家组织",
    )
    merchant_organization = models.ForeignKey(
        "account.Organization",
        on_delete=models.PROTECT,
        related_name="merchant_order_plans",
        verbose_name="直接商家",
    )
    merchant_relation = models.ForeignKey(
        "account.OrganizationRelation",
        on_delete=models.PROTECT,
        related_name="order_plans",
        verbose_name="经销关系",
    )
    channel_assignment = models.ForeignKey(
        "channel.MerchantChannelAssignment",
        on_delete=models.PROTECT,
        related_name="order_plans",
        verbose_name="渠道授权",
    )
    brand = models.ForeignKey(
        "product.Brand",
        on_delete=models.PROTECT,
        related_name="order_plans",
        verbose_name="品牌",
    )
    region_snapshot = models.CharField(
        max_length=255,
        verbose_name="区域快照",
    )
    created_by = models.ForeignKey(
        "account.User",
        on_delete=models.PROTECT,
        related_name="created_order_plans",
        verbose_name="创建销售经理",
    )
    creator_role_code = models.CharField(
        max_length=64,
        default="factory_sales",
        verbose_name="创建岗位快照",
    )
    total_quantity = models.PositiveIntegerField(verbose_name="计划总数量")
    remark = models.TextField(blank=True, default="", verbose_name="订单备注")
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.MATCHING_PENDING,
        db_index=True,
        verbose_name="计划状态",
    )
    submitted_at = models.DateTimeField(
        default=timezone.now,
        db_index=True,
        verbose_name="提交时间",
    )
    shipped_at = models.DateTimeField(null=True, blank=True, verbose_name="发货时间")
    shipped_by = models.ForeignKey(
        "account.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="shipped_order_plans", verbose_name="出库操作人",
    )
    received_at = models.DateTimeField(null=True, blank=True, verbose_name="商家入库时间")
    received_by = models.ForeignKey(
        "account.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="received_order_plans", verbose_name="商家入库操作人",
    )
    accounted_at = models.DateTimeField(null=True, blank=True, verbose_name="入账时间")
    accounted_by = models.ForeignKey(
        "account.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="accounted_order_plans", verbose_name="入账操作人",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        ordering = ("-submitted_at", "-id")
        verbose_name = "订单计划"
        verbose_name_plural = "订单计划"
        constraints = [
            models.UniqueConstraint(
                fields=("created_by", "client_request_id"),
                name="sales_unique_order_plan_client_request",
            ),
            models.CheckConstraint(
                condition=Q(total_quantity__gt=0),
                name="sales_order_plan_total_quantity_positive",
            ),
            models.CheckConstraint(
                condition=~Q(factory_organization=F("merchant_organization")),
                name="sales_order_plan_organizations_not_self",
            ),
        ]

    def __str__(self):
        return f"{self.plan_code}-{self.merchant_organization.name}"

    def clean(self):
        super().clean()
        if self.merchant_relation_id:
            errors = {}
            if self.merchant_relation.source_organization_id != self.factory_organization_id:
                errors["factory_organization"] = "厂家组织必须与经销关系发起组织一致。"
            if self.merchant_relation.target_organization_id != self.merchant_organization_id:
                errors["merchant_organization"] = "直接商家必须与经销关系目标组织一致。"
            if self.merchant_relation.relation_type != "DISTRIBUTION":
                errors["merchant_relation"] = "订单计划只能使用经销关系。"
            if errors:
                raise ValidationError(errors)
        if self.channel_assignment_id:
            errors = {}
            if self.channel_assignment.relation_id != self.merchant_relation_id:
                errors["channel_assignment"] = "渠道授权与所选经销关系不一致。"
            if self.channel_assignment.brand_id != self.brand_id:
                errors["brand"] = "订单品牌必须与渠道授权品牌一致。"
            if errors:
                raise ValidationError(errors)


class OrderPlanItem(models.Model):
    """订单计划中的一个产品型号；一个型号固定对应一个订单批次。"""

    plan = models.ForeignKey(
        OrderPlan,
        on_delete=models.PROTECT,
        related_name="items",
        verbose_name="订单计划",
    )
    category = models.ForeignKey(
        "product.ProductCategory",
        on_delete=models.PROTECT,
        related_name="order_plan_items",
        verbose_name="产品品类",
    )
    product_model = models.ForeignKey(
        "product.ProductModel",
        on_delete=models.PROTECT,
        related_name="order_plan_items",
        verbose_name="产品型号",
    )
    quantity = models.PositiveIntegerField(verbose_name="计划数量")
    order_batch_code = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        verbose_name="订单批次编码",
    )
    lifecycle_code = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        verbose_name="生命周期链路编码",
    )
    status = models.CharField(
        max_length=32,
        choices=OrderPlan.Status.choices,
        default=OrderPlan.Status.MATCHING_PENDING,
        db_index=True,
        verbose_name="明细状态",
    )
    qrcode_generated_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="二维码生成完成时间",
    )
    qrcode_printed_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="二维码打印确认时间",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        ordering = ("plan_id", "id")
        verbose_name = "订单计划明细"
        verbose_name_plural = "订单计划明细"
        constraints = [
            models.UniqueConstraint(
                fields=("plan", "product_model"),
                name="sales_unique_order_plan_product_model",
            ),
            models.CheckConstraint(
                condition=Q(quantity__gt=0),
                name="sales_order_plan_item_quantity_positive",
            ),
        ]

    def __str__(self):
        return f"{self.order_batch_code}-{self.product_model.name}"

    def clean(self):
        super().clean()
        errors = {}
        if self.product_model_id and self.category_id:
            if self.product_model.category_id != self.category_id:
                errors["category"] = "产品型号与产品品类不一致。"
        if errors:
            raise ValidationError(errors)


class OrderPlanItemBOMConfirmation(models.Model):
    """订单明细最终采用的BOM版本及其继承来源。"""

    order_plan_item = models.OneToOneField(
        OrderPlanItem,
        on_delete=models.PROTECT,
        related_name="bom_confirmation",
        verbose_name="订单计划明细",
    )
    source_bom = models.ForeignKey(
        "product.BOM",
        on_delete=models.PROTECT,
        related_name="order_plan_source_confirmations",
        verbose_name="默认继承BOM",
    )
    confirmed_bom = models.ForeignKey(
        "product.BOM",
        on_delete=models.PROTECT,
        related_name="order_plan_confirmations",
        verbose_name="订单确认BOM",
    )
    confirmed_by = models.ForeignKey(
        "account.User",
        on_delete=models.PROTECT,
        related_name="confirmed_order_plan_boms",
        verbose_name="确认配套经理",
    )
    has_changes = models.BooleanField(
        default=False,
        verbose_name="是否生成新版本",
    )
    confirmed_at = models.DateTimeField(
        default=timezone.now,
        db_index=True,
        verbose_name="确认时间",
    )

    class Meta:
        ordering = ("-confirmed_at", "-id")
        verbose_name = "订单明细BOM确认"
        verbose_name_plural = "订单明细BOM确认"

    def __str__(self):
        return (
            f"{self.order_plan_item.order_batch_code}-"
            f"{self.confirmed_bom.version}"
        )

    def clean(self):
        super().clean()
        errors = {}
        product_model_id = self.order_plan_item.product_model_id
        if self.source_bom_id and self.source_bom.product_model_id != product_model_id:
            errors["source_bom"] = "默认BOM与订单产品型号不一致。"
        if (
            self.confirmed_bom_id
            and self.confirmed_bom.product_model_id != product_model_id
        ):
            errors["confirmed_bom"] = "确认BOM与订单产品型号不一致。"
        if errors:
            raise ValidationError(errors)
