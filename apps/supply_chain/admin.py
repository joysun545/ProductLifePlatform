from django.contrib import admin

from supply_chain.models import SupplierSalesResponsibility


@admin.register(SupplierSalesResponsibility)
class SupplierSalesResponsibilityAdmin(admin.ModelAdmin):
    list_display = (
        "supplier",
        "sales_identity",
        "scope_type",
        "product_category",
        "material",
        "is_active",
        "updated_at",
    )
    list_filter = ("scope_type", "is_active", "supplier")
    search_fields = (
        "supplier__name",
        "sales_identity__user__username",
        "sales_identity__user__wx_nickname",
        "product_category__name",
        "material__name",
        "material__code",
    )
    raw_id_fields = (
        "supplier",
        "sales_identity",
        "product_category",
        "material",
        "assigned_by",
    )
