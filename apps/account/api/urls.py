from django.urls import path

from . import views


app_name = "account"


urlpatterns = [
    path("login/", views.login, name="login"),
    path("profile/", views.profile, name="profile"),
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
    path(
        "invitations/create/",
        views.create_invitation,
        name="create_invitation",
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
