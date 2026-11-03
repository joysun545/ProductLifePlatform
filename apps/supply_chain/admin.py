from django.contrib import admin

from supply_chain.models import (
    MaterialFaultCause,
    OrganizationMaterialLot,
    OrganizationMaterialStock,
    SupplierSalesResponsibility,
)


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


@admin.register(OrganizationMaterialStock)
class OrganizationMaterialStockAdmin(admin.ModelAdmin):
    list_display = (
        "organization",
        "source_factory",
        "supplier",
        "material_spec",
        "unit",
        "revision",
    )
    list_filter = ("source_factory", "organization")
    search_fields = (
        "organization__name",
        "source_factory__name",
        "supplier__name",
        "material_spec__code",
        "material_spec__name",
    )
    readonly_fields = tuple(field.name for field in OrganizationMaterialStock._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(OrganizationMaterialLot)
class OrganizationMaterialLotAdmin(admin.ModelAdmin):
    list_display = (
        "stock",
        "source_batch_code_snapshot",
        "received_milli",
        "on_hand_milli",
        "reserved_milli",
        "received_at",
    )
    list_filter = ("received_at",)
    search_fields = (
        "source_reference",
        "source_batch_code_snapshot",
        "stock__organization__name",
        "stock__material_spec__code",
    )
    readonly_fields = tuple(field.name for field in OrganizationMaterialLot._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
