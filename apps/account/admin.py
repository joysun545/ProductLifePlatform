from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import (
    InvitationRecord,
    Organization,
    OrganizationRelation,
    OrganizationRole,
    OrganizationType,
    RoleInvitationRule,
    User,
    UserOrganization,
)


class UserOrganizationInline(admin.StackedInline):
    model = UserOrganization
    fk_name = "user"
    extra = 0
    max_num = 1
    can_delete = False
    fields = (
        "organization",
        "role",
        "inviter",
        "is_active",
        "created_at",
        "updated_at",
    )
    readonly_fields = ("created_at", "updated_at")
    autocomplete_fields = ("organization", "role", "inviter")


@admin.register(User)
class AccountUserAdmin(DjangoUserAdmin):
    list_display = (
        "id",
        "username",
        "wx_nickname",
        "phone",
        "identity_role",
        "identity_organization",
        "is_staff",
        "is_active",
        "created_at",
    )
    list_filter = ("is_staff", "is_superuser", "is_active", "created_at")
    search_fields = ("username", "wx_openid", "wx_nickname", "phone")
    ordering = ("-created_at", "id")
    readonly_fields = ("created_at",)
    inlines = (UserOrganizationInline,)

    fieldsets = DjangoUserAdmin.fieldsets + (
        (
            "微信业务账号",
            {
                "fields": (
                    "wx_openid",
                    "wx_nickname",
                    "wx_avatar",
                    "phone",
                    "created_at",
                )
            },
        ),
    )

    add_fieldsets = DjangoUserAdmin.add_fieldsets + (
        (
            "微信业务账号",
            {
                "classes": ("wide",),
                "fields": (
                    "wx_openid",
                    "wx_nickname",
                    "wx_avatar",
                    "phone",
                ),
            },
        ),
    )

    @admin.display(description="岗位")
    def identity_role(self, obj):
        identity = getattr(obj, "organization_identity", None)
        return identity.role.name if identity else "游客"

    @admin.display(description="组织")
    def identity_organization(self, obj):
        identity = getattr(obj, "organization_identity", None)
        return identity.organization.name if identity else "-"

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            "organization_identity__organization",
            "organization_identity__role",
        )


@admin.register(OrganizationType)
class OrganizationTypeAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "is_active", "created_at", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("code", "name")
    ordering = ("code",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "code",
        "organization_type",
        "region",
        "is_active",
        "created_at",
    )
    list_filter = ("organization_type", "is_active", "region")
    search_fields = ("name", "code", "region", "address")
    ordering = ("name", "id")
    readonly_fields = ("created_at", "updated_at")
    autocomplete_fields = ("organization_type",)


@admin.register(OrganizationRole)
class OrganizationRoleAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "name",
        "organization_type",
        "is_owner_role",
        "is_active",
    )
    list_filter = ("organization_type", "is_owner_role", "is_active")
    search_fields = ("code", "name", "organization_type__name")
    ordering = ("organization_type__code", "code")
    readonly_fields = ("created_at", "updated_at")
    autocomplete_fields = ("organization_type",)


@admin.register(RoleInvitationRule)
class RoleInvitationRuleAdmin(admin.ModelAdmin):
    list_display = (
        "inviter_role",
        "target_role",
        "scope",
        "relation_type",
        "is_active",
    )
    list_filter = ("scope", "relation_type", "is_active")
    search_fields = (
        "inviter_role__code",
        "inviter_role__name",
        "target_role__code",
        "target_role__name",
    )
    ordering = ("inviter_role__code", "target_role__code")
    readonly_fields = ("created_at", "updated_at")
    autocomplete_fields = ("inviter_role", "target_role")


@admin.register(UserOrganization)
class UserOrganizationAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "organization",
        "role",
        "inviter",
        "is_active",
        "created_at",
    )
    list_filter = (
        "is_active",
        "organization__organization_type",
        "role",
        "created_at",
    )
    search_fields = (
        "user__username",
        "user__wx_openid",
        "user__wx_nickname",
        "organization__name",
        "role__code",
        "role__name",
    )
    ordering = ("-created_at", "id")
    readonly_fields = ("created_at", "updated_at")
    autocomplete_fields = ("user", "organization", "role", "inviter")
    list_select_related = ("user", "organization", "role", "inviter")


@admin.register(InvitationRecord)
class InvitationRecordAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "inviter",
        "source_organization",
        "target_role",
        "scope",
        "status",
        "invitee",
        "expires_at",
        "created_at",
    )
    list_filter = ("status", "scope", "relation_type", "created_at")
    search_fields = (
        "token",
        "inviter__username",
        "inviter__wx_nickname",
        "invitee__username",
        "invitee__wx_nickname",
        "source_organization__name",
        "target_organization__name",
        "target_role__code",
    )
    ordering = ("-created_at", "id")
    list_select_related = (
        "inviter",
        "source_organization",
        "target_role",
        "target_organization",
        "invitee",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields)


@admin.register(OrganizationRelation)
class OrganizationRelationAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "source_organization",
        "relation_type",
        "target_organization",
        "is_active",
        "created_at",
    )
    list_filter = ("relation_type", "is_active", "created_at")
    search_fields = (
        "source_organization__name",
        "target_organization__name",
    )
    ordering = ("relation_type", "source_organization__name", "id")
    readonly_fields = ("created_at", "updated_at", "created_by_invitation")
    autocomplete_fields = ("source_organization", "target_organization")
    list_select_related = (
        "source_organization",
        "target_organization",
        "created_by_invitation",
    )
