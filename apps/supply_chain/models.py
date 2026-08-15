# apps/supply_chain/models.py

from django.db import models
from django.core.exceptions import ValidationError
from django.db.models import Q



class Material(models.Model):
    """
    物料基础信息。

    描述物料功能类别。

    示例：

    控制器
    化油器
    点火器
    油箱
    电芯
    """


    code = models.CharField(
        max_length=64,
        unique=True,
        verbose_name="物料编码"
    )


    name = models.CharField(
        max_length=128,
        verbose_name="物料名称"
    )


    product_category = models.ForeignKey(
        "product.ProductCategory",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="materials",
        verbose_name="所属产品分类"
    )


    is_key_part = models.BooleanField(
        default=False,
        verbose_name="是否关键件"
    )


    description = models.TextField(
        blank=True,
        default="",
        verbose_name="物料描述"
    )


    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="创建时间"
    )


    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name="更新时间"
    )


    class Meta:

        verbose_name = "物料"

        verbose_name_plural = "物料"

        ordering = [
            "code"
        ]


    def __str__(self):

        return f"{self.code}-{self.name}"






class MaterialSpec(models.Model):
    """
    物料规格型号。

    描述同一种物料不同规格版本。

    示例：

    控制器

        蓝牙遥控版

        普通版

        高功率版
    """


    material = models.ForeignKey(
        Material,
        on_delete=models.CASCADE,
        related_name="specs",
        verbose_name="所属物料"
    )


    code = models.CharField(
        max_length=64,
        unique=True,
        verbose_name="规格编码"
    )


    name = models.CharField(
        max_length=128,
        verbose_name="规格名称"
    )


    performance_description = models.CharField(
        max_length=256,
        blank=True,
        default="",
        verbose_name="性能描述"
    )


    parameters = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="规格参数"
    )


    description = models.TextField(
        blank=True,
        default="",
        verbose_name="规格说明"
    )


    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="创建时间"
    )


    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name="更新时间"
    )


    class Meta:

        verbose_name = "物料规格"

        verbose_name_plural = "物料规格"

        ordering = [
            "code"
        ]


    def __str__(self):

        return (
            f"{self.material.name}-"
            f"{self.name}"
        )








class MaterialSpecSupplier(models.Model):
    """
    物料规格供应关系。

    描述：

    哪个供应商可以提供哪个规格物料。


    示例：

    蓝牙控制器

        A供应商

        B供应商

    """


    material_spec = models.ForeignKey(
        MaterialSpec,
        on_delete=models.CASCADE,
        related_name="suppliers",
        verbose_name="物料规格"
    )


    supplier = models.ForeignKey(
        "account.Organization",
        on_delete=models.PROTECT,
        related_name="supplied_material_specs",
        verbose_name="供应商"
    )


    warranty_months = models.PositiveIntegerField(
        default=12,
        verbose_name="供应商质保(月)"
    )


    reference_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name="参考价格"
    )


    remark = models.TextField(
        blank=True,
        default="",
        verbose_name="备注"
    )


    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="创建时间"
    )


    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name="更新时间"
    )


    class Meta:

        verbose_name = "物料规格供应关系"

        verbose_name_plural = "物料规格供应关系"


        constraints = [

            models.UniqueConstraint(
                fields=[
                    "material_spec",
                    "supplier"
                ],
                name="unique_material_spec_supplier"
            )

        ]


        indexes = [

            models.Index(
                fields=[
                    "material_spec"
                ]
            ),

            models.Index(
                fields=[
                    "supplier"
                ]
            ),

        ]


    def __str__(self):

        return (
            f"{self.material_spec}-"
            f"{self.supplier}"
        )


class SupplierResponsibilityScope(models.TextChoices):
    """供应商销售经理责任范围的两种配置粒度。"""

    CATEGORY = "CATEGORY", "按产品品类"
    MATERIAL = "MATERIAL", "按物料名称"


class SupplierSalesResponsibility(models.Model):
    """
    供应商负责人对本组织销售经理划分的物料责任范围。

    同一品类或同一物料在一个供应商组织内只有一个直接责任销售经理。
    当品类范围与具体物料范围重叠时，具体物料配置优先。
    """

    supplier = models.ForeignKey(
        "account.Organization",
        on_delete=models.PROTECT,
        related_name="sales_responsibilities",
        verbose_name="供应商组织",
    )
    sales_identity = models.ForeignKey(
        "account.UserOrganization",
        on_delete=models.PROTECT,
        related_name="supplier_sales_responsibilities",
        verbose_name="负责销售经理身份",
    )
    scope_type = models.CharField(
        max_length=16,
        choices=SupplierResponsibilityScope.choices,
        db_index=True,
        verbose_name="责任范围类型",
    )
    product_category = models.ForeignKey(
        "product.ProductCategory",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="supplier_sales_responsibilities",
        verbose_name="负责产品品类",
    )
    material = models.ForeignKey(
        Material,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="supplier_sales_responsibilities",
        verbose_name="负责物料",
    )
    assigned_by = models.ForeignKey(
        "account.User",
        on_delete=models.PROTECT,
        related_name="assigned_supplier_sales_responsibilities",
        verbose_name="分配负责人账号",
    )
    is_active = models.BooleanField(default=True, verbose_name="是否有效")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        ordering = ("supplier_id", "scope_type", "id")
        verbose_name = "供应商销售责任范围"
        verbose_name_plural = "供应商销售责任范围"
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(
                        scope_type=SupplierResponsibilityScope.CATEGORY,
                        product_category__isnull=False,
                        material__isnull=True,
                    )
                    | Q(
                        scope_type=SupplierResponsibilityScope.MATERIAL,
                        product_category__isnull=True,
                        material__isnull=False,
                    )
                ),
                name="supply_chain_supplier_responsibility_scope_consistent",
            ),
            models.UniqueConstraint(
                fields=("supplier", "product_category"),
                condition=Q(scope_type=SupplierResponsibilityScope.CATEGORY),
                name="supply_chain_unique_supplier_category_responsibility",
            ),
            models.UniqueConstraint(
                fields=("supplier", "material"),
                condition=Q(scope_type=SupplierResponsibilityScope.MATERIAL),
                name="supply_chain_unique_supplier_material_responsibility",
            ),
        ]
        indexes = [
            models.Index(
                fields=("supplier", "scope_type", "is_active"),
                name="supply_chai_supplie_2f12f2_idx",
            ),
            models.Index(
                fields=("sales_identity", "is_active"),
                name="supply_chai_sales_i_6fd16a_idx",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        supplier = self.supplier if self.supplier_id else None
        sales_identity = self.sales_identity if self.sales_identity_id else None
        if supplier and supplier.organization_type.code != "SUPPLIER":
            errors["supplier"] = "责任范围只能配置给供应商组织。"
        if sales_identity:
            if sales_identity.organization_id != self.supplier_id:
                errors["sales_identity"] = "销售经理必须属于当前供应商组织。"
            elif sales_identity.role.code != "supplier_sales":
                errors["sales_identity"] = "只能给供应商销售经理分配责任范围。"
        if self.scope_type == SupplierResponsibilityScope.CATEGORY:
            if not self.product_category_id or self.material_id:
                errors["scope_type"] = "按品类分配时必须且只能选择产品品类。"
        elif self.scope_type == SupplierResponsibilityScope.MATERIAL:
            if not self.material_id or self.product_category_id:
                errors["scope_type"] = "按物料分配时必须且只能选择物料。"
        else:
            errors["scope_type"] = "责任范围类型无效。"
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        target = self.product_category or self.material
        return f"{self.supplier}-{self.sales_identity.user}-{target}"
