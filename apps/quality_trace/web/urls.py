from django.urls import path

from . import views


app_name = "quality_trace"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
]

