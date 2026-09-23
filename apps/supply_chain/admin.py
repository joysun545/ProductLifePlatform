from django.contrib import admin

from supply_chain.models import MaterialFaultCause, SupplierSalesResponsibility


@admin.register(MaterialFaultCause)
class MaterialFaultCauseAdmin(admin.ModelAdmin):
    list_display = ("material", "code", "name", "grade", "is_active", "updated_at")
    list_filter = ("grade", "is_active", "material")
    search_fields = ("material__code", "material__name", "code", "name")
    raw_id_fields = ("material",)


@admin.register(SupplierSalesResponsibility)
class SupplierSalesResponsibilityAdmin(admin.ModelAdmin):
    list_display = (
        "supplier",
        "sales_identity",
        "material",
        "is_active",
        "updated_at",
    )
    list_filter = ("is_active", "supplier")
    search_fields = (
        "supplier__name",
        "sales_identity__user__username",
        "sales_identity__user__wx_nickname",
        "material__name",
        "material__code",
    )
    raw_id_fields = (
        "supplier",
        "sales_identity",
        "material",
        "assigned_by",
    )
