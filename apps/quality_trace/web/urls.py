from django.urls import path

from . import views


app_name = "quality_trace"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("root-causes/", views.root_causes, name="root_causes"),
    path("rule-simulation/", views.rule_simulation, name="rule_simulation"),
    path("rule-publish/", views.rule_publish, name="rule_publish"),
    path("rules/", views.rule_list, name="rule_list"),
]
