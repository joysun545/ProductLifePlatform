from django.urls import path

from . import views


app_name = "account"


urlpatterns = [
    path("login/", views.login, name="login"),
    path("profile/", views.profile, name="profile"),
    path("appearance/", views.appearance, name="appearance"),
    path(
        "invitation_roles/",
        views.invitation_roles,
        name="invitation_roles",
    ),
    path(
        "subordinates/",
        views.subordinates,
        name="subordinates",
    ),
    path("subordinates/managed-roles/", views.subordinate_managed_roles, name="subordinate_managed_roles"),
    path(
        "subordinates/after-sales-qualification/",
        views.update_subordinate_after_sales_qualification,
        name="update_subordinate_after_sales_qualification",
    ),
    path(
        "subordinates/merchant-catalog/",
        views.subordinate_merchant_catalog,
        name="subordinate_merchant_catalog",
    ),
    path(
        "invitations/create/",
        views.create_invitation,
        name="create_invitation",
    ),
    path(
        "platform/factory-invitations/create/",
        views.create_platform_factory_invitation_view,
        name="create_platform_factory_invitation",
    ),
    path(
        "invitations/detail/",
        views.invitation_detail,
        name="invitation_detail",
    ),
    path(
        "invitations/accept/",
        views.accept_invitation,
        name="accept_invitation",
    ),
]
