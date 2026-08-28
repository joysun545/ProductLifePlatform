from django.urls import path

from . import views


app_name = "quality_trace"

urlpatterns = [
    path("overview/", views.overview, name="overview"),
    path("rules/", views.rules, name="rules"),
    path("rules/evaluate/", views.evaluate_rules, name="evaluate_rules"),
    path("metric-snapshots/", views.metric_snapshots, name="metric_snapshots"),
    path("alerts/", views.alerts, name="alerts"),
    path("alerts/<int:alert_id>/", views.alert_detail, name="alert_detail"),
    path("alerts/<int:alert_id>/assess/", views.alert_assess, name="alert_assess"),
    path("alerts/<int:alert_id>/open-case/", views.alert_open_case, name="alert_open_case"),
    path("cases/", views.quality_cases, name="quality_cases"),
    path("cases/<int:case_id>/", views.quality_case_detail, name="quality_case_detail"),
    path("cases/<int:case_id>/temporary-hold/", views.temporary_hold, name="temporary_hold"),
    path("cases/<int:case_id>/formal-hold/", views.formal_hold, name="formal_hold"),
    path("cases/<int:case_id>/recall/", views.recall_start, name="recall_start"),
    path("control-actions/<int:action_id>/revoke/", views.revoke_hold, name="revoke_hold"),
    path("control-actions/<int:action_id>/release/", views.formal_hold_release, name="formal_hold_release"),
    path("control-actions/<int:action_id>/complete-recall/", views.recall_complete, name="recall_complete"),
    path("gates/check/", views.gate_check, name="gate_check"),
    path("gates/decisions/", views.gate_decisions, name="gate_decisions"),
    path(
        "supplier-batches/<int:batch_id>/impact/",
        views.supplier_batch_impact,
        name="supplier_batch_impact",
    ),
]
