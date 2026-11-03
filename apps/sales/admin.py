from django.contrib import admin

from .models import (
    Batch,
    OrderPlan,
    OrderPlanItem,
    OrderPlanItemBOMConfirmation,
    OrderPlanFinance,
    OrderPlanLogisticsDocument,
    OrderPlanPayment,
    OrderPlanReturnInventoryAllocation,
    OrderPlanAccessoryCompatibility,
    OrderPlanAccessoryItem,
    SalesPlan,
)


class OrderPlanItemInline(admin.TabularInline):
    model = OrderPlanItem
    extra = 0
    readonly_fields = (
        "order_batch_code",
        "lifecycle_code",
        "return_inventory_quantity",
        "production_quantity",
        "created_at",
    )


class OrderPlanAccessoryItemInline(admin.TabularInline):
    model = OrderPlanAccessoryItem
    extra = 0
    can_delete = False
    readonly_fields = (
        "category",
        "product_model",
        "material_spec",
        "quantity_milli",
        "unit_snapshot",
        "status",
        "created_at",
    )

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(OrderPlanAccessoryCompatibility)
class OrderPlanAccessoryCompatibilityAdmin(admin.ModelAdmin):
    list_display = (
        "factory_organization",
        "material_spec",
        "product_model",
        "is_active",
        "updated_at",
    )
    list_filter = ("factory_organization", "is_active", "product_model__category")
    search_fields = (
        "factory_organization__name",
        "material_spec__code",
        "material_spec__name",
        "material_spec__material__name",
        "product_model__code",
        "product_model__name",
    )
    readonly_fields = ("created_at", "updated_at")


@admin.register(OrderPlanReturnInventoryAllocation)
class OrderPlanReturnInventoryAllocationAdmin(admin.ModelAdmin):
    list_display = (
        "order_plan_item",
        "source_device",
        "source_bom",
        "status",
        "reserved_by",
        "reserved_at",
        "reissued_at",
    )
    list_filter = ("status", "source_bom")
    search_fields = (
        "order_plan_item__plan__plan_code",
        "order_plan_item__order_batch_code",
        "source_device__sn",
        "source_qr_code_snapshot",
    )
    readonly_fields = (
        "source_order_batch_snapshot",
        "source_lifecycle_snapshot",
        "source_qr_code_snapshot",
        "reserved_at",
        "ready_to_ship_at",
        "reissued_at",
        "created_at",
        "updated_at",
    )


@admin.register(OrderPlanItemBOMConfirmation)
class OrderPlanItemBOMConfirmationAdmin(admin.ModelAdmin):
    list_display = (
        "order_plan_item",
        "source_bom",
        "confirmed_bom",
        "has_changes",
        "confirmed_by",
        "confirmed_at",
    )
    list_filter = ("has_changes", "confirmed_at")
    search_fields = (
        "order_plan_item__plan__plan_code",
        "order_plan_item__product_model__name",
        "confirmed_by__wx_nickname",
    )
    readonly_fields = ("confirmed_at",)


@admin.register(OrderPlan)
class OrderPlanAdmin(admin.ModelAdmin):
    list_display = (
        "plan_code",
        "merchant_organization",
        "brand",
        "region_snapshot",
        "total_quantity",
        "status",
        "created_by",
        "shipment_authorized_by",
        "shipment_authorized_at",
        "submitted_at",
    )
    list_filter = ("status", "brand", "factory_organization")
    search_fields = (
        "plan_code",
        "merchant_organization__name",
        "region_snapshot",
        "created_by__wx_nickname",
    )
    readonly_fields = (
        "plan_code",
        "total_quantity",
        "submitted_at",
        "shipment_authorized_at",
    )
    inlines = (OrderPlanItemInline, OrderPlanAccessoryItemInline)


admin.site.register(SalesPlan)
admin.site.register(Batch)


@admin.register(OrderPlanFinance)
class OrderPlanFinanceAdmin(admin.ModelAdmin):
    list_display = (
        "order_plan",
        "receivable_amount",
        "statement_submitted_by",
        "statement_submitted_at",
    )
    search_fields = ("order_plan__plan_code", "order_plan__merchant_organization__name")
    readonly_fields = ("created_at", "updated_at")


@admin.register(OrderPlanPayment)
class OrderPlanPaymentAdmin(admin.ModelAdmin):
    list_display = (
        "finance",
        "sequence_no",
        "status",
        "confirmed_amount",
        "uploaded_by",
        "confirmed_by",
        "uploaded_at",
        "confirmed_at",
    )
    list_filter = ("status", "uploaded_at", "confirmed_at")
    search_fields = ("finance__order_plan__plan_code", "uploaded_by__wx_nickname")
    readonly_fields = ("uploaded_at", "created_at", "updated_at")


@admin.register(OrderPlanLogisticsDocument)
class OrderPlanLogisticsDocumentAdmin(admin.ModelAdmin):
    list_display = (
        "order_plan",
        "sequence_no",
        "original_name",
        "file_type",
        "uploaded_by",
        "uploaded_at",
    )
    list_filter = ("file_type", "uploaded_at")
    search_fields = (
        "order_plan__plan_code",
        "order_plan__merchant_organization__name",
        "original_name",
        "uploaded_by__wx_nickname",
    )
    readonly_fields = ("uploaded_at", "created_at", "updated_at")
