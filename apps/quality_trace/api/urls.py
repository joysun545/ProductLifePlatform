from django.urls import path

from . import recall_views, views


app_name = "quality_trace"

urlpatterns = [
    path("recall-evidence/upload/", recall_views.evidence_upload, name="recall_evidence_upload"),
    path("overview/", views.overview, name="overview"),
    path("rules/", views.rules, name="rules"),
    path("rules/<int:rule_id>/", views.rule_detail, name="rule_detail"),
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
    path("recalls/", recall_views.campaigns, name="recall_campaigns"),
    path("recalls/<int:campaign_id>/", recall_views.campaign_detail, name="recall_campaign_detail"),
    path("recalls/<int:campaign_id>/launch/", recall_views.campaign_launch, name="recall_campaign_launch"),
    path("recalls/<int:campaign_id>/publish-tasks/", recall_views.campaign_publish_tasks, name="recall_publish_tasks"),
    path("recalls/<int:campaign_id>/progress/", recall_views.campaign_progress, name="recall_progress"),
    path("recalls/<int:campaign_id>/complete/", recall_views.campaign_complete, name="recall_campaign_complete"),
    path("recall-devices/<int:recall_device_id>/", recall_views.device_detail, name="recall_device_detail"),
    path("recall-devices/<int:recall_device_id>/initialize-handover/", recall_views.handover_initialize, name="recall_handover_initialize"),
    path("recall-devices/<int:recall_device_id>/next-transport/", recall_views.transport_create, name="recall_transport_create"),
    path("recall-devices/<int:recall_device_id>/unreachable-decision/", recall_views.unreachable_decide, name="recall_unreachable_decide"),
    path("recall-handovers/<int:handover_id>/acknowledge/", recall_views.handover_ack, name="recall_handover_ack"),
    path("recall-handovers/<int:handover_id>/schedule-pickup/", recall_views.handover_schedule, name="recall_handover_schedule"),
    path("recall-handovers/<int:handover_id>/evidence/", recall_views.handover_evidence, name="recall_handover_evidence"),
    path("recall-handovers/<int:handover_id>/receive/", recall_views.handover_receive, name="recall_handover_receive"),
    path("recall-transport-legs/<int:leg_id>/dispatch/", recall_views.transport_dispatch, name="recall_transport_dispatch"),
    path("recall-transport-legs/<int:leg_id>/receive/", recall_views.transport_receive, name="recall_transport_receive"),
    path("recall-transport-legs/<int:leg_id>/isolate/", recall_views.transport_isolate, name="recall_transport_isolate"),
]
