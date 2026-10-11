from django.contrib import admin

from .models import (
    MerchantChannelAssignment,
    MerchantProductCategoryScope,
    MerchantProductModelScope,
)


@admin.register(MerchantChannelAssignment)
class MerchantChannelAssignmentAdmin(admin.ModelAdmin):
    list_display = (
        "relation",
        "brand",
        "region",
        "assigned_by",
        "is_active",
        "updated_at",
    )
    list_filter = ("is_active", "brand")
    search_fields = (
        "relation__source_organization__name",
        "relation__target_organization__name",
        "brand__name",
        "region",
        "assigned_by__wx_nickname",
    )
    raw_id_fields = ("relation", "brand", "assigned_by")


@admin.register(MerchantProductCategoryScope)
class MerchantProductCategoryScopeAdmin(admin.ModelAdmin):
    list_display = ("relation", "brand", "category", "assigned_by", "updated_at")
    list_filter = ("brand", "category")
    search_fields = (
        "relation__source_organization__name",
        "relation__target_organization__name",
        "brand__name",
        "category__name",
    )
    raw_id_fields = ("relation", "brand", "category", "assigned_by")


@admin.register(MerchantProductModelScope)
class MerchantProductModelScopeAdmin(admin.ModelAdmin):
    list_display = ("category_scope", "product_model", "created_at")
    list_filter = ("category_scope__brand", "category_scope__category")
    search_fields = (
        "category_scope__relation__target_organization__name",
        "category_scope__brand__name",
        "product_model__code",
        "product_model__name",
    )
    raw_id_fields = ("category_scope", "product_model")
