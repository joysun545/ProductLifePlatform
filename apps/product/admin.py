from django.contrib import admin

from product.models import ProductDocument


@admin.register(ProductDocument)
class ProductDocumentAdmin(admin.ModelAdmin):
    list_display = (
        "product_model",
        "document_type",
        "version",
        "original_filename",
        "file_type",
        "is_active",
        "updated_at",
    )
    list_filter = ("document_type", "file_type", "is_active")
    search_fields = (
        "product_model__code",
        "product_model__name",
        "title",
        "original_filename",
    )
