from django.urls import path
# The deployed URL tree imports apps.print.urls, while INSTALLED_APPS uses
# print. Always use the registered application's canonical module path.
from print import views

app_name = "print"
urlpatterns = [
    path("", views.print_center, name="center"),
    path("api/login/start/", views.login_start, name="login_start"),
    path("api/login/approve/", views.login_approve, name="login_approve"),
    path("api/login/status/", views.login_status, name="login_status"),
    path("api/logout/", views.logout, name="logout"),
    path("api/prepare/", views.prepare_qrcodes, name="prepare"),
    path("api/confirm/", views.confirm_print, name="confirm"),
]
