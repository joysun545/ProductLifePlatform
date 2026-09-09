from django.urls import path

from . import views


app_name = "data_statistics"

urlpatterns = [
    path("dashboard/", views.dashboard, name="dashboard"),
    path("material/", views.material, name="material"),
    path("flow/", views.flow, name="flow"),
    path("aftersales/", views.aftersales, name="aftersales"),
    path("quality/", views.quality, name="quality"),
    path("metric-definitions/", views.metric_definitions, name="metric_definitions"),
    path("metric-trends/<str:metric_code>/", views.metric_trends, name="metric_trends"),
]
