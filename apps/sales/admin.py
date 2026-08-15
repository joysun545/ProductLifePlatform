from django.contrib import admin

from .models import (
    Batch,
    OrderPlan,
    OrderPlanItem,
    OrderPlanItemBOMConfirmation,
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
        "submitted_at",
    )
    list_filter = ("status", "brand", "factory_organization")
    search_fields = (
        "plan_code",
        "merchant_organization__name",
        "region_snapshot",
        "created_by__wx_nickname",
    )
    readonly_fields = ("plan_code", "total_quantity", "submitted_at")
    inlines = (OrderPlanItemInline,)


admin.site.register(SalesPlan)
admin.site.register(Batch)
