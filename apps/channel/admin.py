from django.contrib import admin

from .models import MerchantChannelAssignment


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
