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


    product_categories = models.ManyToManyField(
        "product.ProductCategory",
        blank=True,
        related_name="materials",
        verbose_name="适用产品品类",
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


class MaterialFaultCause(models.Model):
    """Factory-independent fault-cause dictionary owned by one key material.

    A cause is a controlled quality fact, not a free-text service conclusion.
    The quality-rule version and every confirmed fault preserve a snapshot so
    later dictionary edits never rewrite historic quality evidence.
    """

    class Grade(models.TextChoices):
        A = "A", "A 级"
        B = "B", "B 级"
        C = "C", "C 级"

    material = models.ForeignKey(
        Material,
        on_delete=models.PROTECT,
        related_name="fault_causes",
        verbose_name="所属关键物料",
    )
    code = models.CharField(max_length=64, verbose_name="根因编码")
    name = models.CharField(max_length=128, verbose_name="根因名称")
    grade = models.CharField(max_length=1, choices=Grade.choices, verbose_name="根因等级")
    description = models.TextField(blank=True, default="", verbose_name="根因说明")
    is_active = models.BooleanField(default=True, verbose_name="启用")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "关键物料故障根因"
        verbose_name_plural = "关键物料故障根因"
        ordering = ("material_id", "grade", "code")
        constraints = [
            models.UniqueConstraint(
                fields=("material", "code"),
                name="supply_unique_material_fault_cause_code",
            ),
        ]

    def clean(self):
        super().clean()
        if self.material_id and not self.material.is_key_part:
            raise ValidationError({"material": "普通件不建立故障根因字典。"})

    def __str__(self):
        return f"{self.material.name}-{self.code}-{self.name}"






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


class SupplierMaterialBatch(models.Model):
    """Supplier's original production batch for one material specification.

    This is not a factory receipt lot and intentionally carries no delivered,
    remaining or total-production quantity.
    """

    supplier = models.ForeignKey(
        "account.Organization",
        on_delete=models.PROTECT,
        related_name="material_batches",
        verbose_name="供应商",
    )
    material = models.ForeignKey(
        Material,
        on_delete=models.PROTECT,
        related_name="supplier_batches",
        verbose_name="物料",
    )
    material_spec = models.ForeignKey(
        MaterialSpec,
        on_delete=models.PROTECT,
        related_name="supplier_batches",
        verbose_name="物料规格",
    )
    batch_code = models.CharField(max_length=128, verbose_name="供应商生产批次编码")
    normalized_batch_code = models.CharField(max_length=128, editable=False)
    production_date = models.DateField(null=True, blank=True, verbose_name="生产日期")
    remark = models.TextField(blank=True, default="", verbose_name="备注")
    created_by = models.ForeignKey(
        "account.User",
        on_delete=models.PROTECT,
        related_name="created_supplier_material_batches",
        verbose_name="首次录入账号",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-production_date", "-id")
        constraints = [
            models.UniqueConstraint(
                fields=("supplier", "material_spec", "normalized_batch_code"),
                name="supply_unique_supplier_spec_batch_code",
            )
        ]
        indexes = [
            models.Index(
                fields=("supplier", "material", "material_spec"),
                name="supply_batch_lookup",
            )
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.material_spec_id and self.material_spec.material_id != self.material_id:
            errors["material_spec"] = "物料规格与物料不一致。"
        if self.supplier_id and self.supplier.organization_type.code != "SUPPLIER":
            errors["supplier"] = "物料批次必须属于供应商组织。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.batch_code = str(self.batch_code or "").strip()
        self.normalized_batch_code = self.batch_code.upper()
        if not self.normalized_batch_code:
            raise ValidationError({"batch_code": "请填写供应商生产批次编码。"})
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.supplier.name}-{self.material_spec.name}-{self.batch_code}"


class SupplierResponsibilityScope(models.TextChoices):
    """供应商销售经理责任范围。"""

    MATERIAL = "MATERIAL", "按物料类型"


class SupplierSalesResponsibility(models.Model):
    """
    多名销售经理时，供应商负责人按物料类型指定唯一责任人。

    只有一名销售经理时不创建记录，该销售经理默认负责全部物料；
    没有销售经理时由供应商负责人独自负责全部物料。
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
    material = models.ForeignKey(
        Material,
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
        ordering = ("supplier_id", "material_id", "id")
        verbose_name = "供应商销售责任范围"
        verbose_name_plural = "供应商销售责任范围"
        constraints = [
            models.UniqueConstraint(
                fields=("supplier", "material"),
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
        if self.scope_type != SupplierResponsibilityScope.MATERIAL:
            errors["scope_type"] = "责任范围类型只能是按物料类型。"
        if not self.material_id:
            errors["material"] = "必须选择负责物料。"
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.supplier}-{self.sales_identity.user}-{self.material}"


# Physical warehouse stock is separate from supplier production-batch metadata.
from .material_inventory_models import (  # noqa: E402,F401
    FactoryMaterialStock, FactoryMaterialLot, MaterialWarrantyCase,
    MaterialWarehouseDocument,
)
