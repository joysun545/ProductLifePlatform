from django.contrib import admin

from .models import (
    Batch,
    OrderPlan,
    OrderPlanItem,
    OrderPlanItemBOMConfirmation,
    OrderPlanFinance,
    OrderPlanLogisticsDocument,
    OrderPlanPayment,
    SalesPlan,
)


class OrderPlanItemInline(admin.TabularInline):
    model = OrderPlanItem
    extra = 0
    readonly_fields = ("order_batch_code", "lifecycle_code", "created_at")


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
    inlines = (OrderPlanItemInline,)


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
