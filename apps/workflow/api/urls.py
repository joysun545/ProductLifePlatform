from django.urls import path

from .views import (
    TaskGroupAPI,
    TaskSummaryAPI,
    TaskListAPI,
    TaskOpenAPI,
)


app_name = "workflow"


urlpatterns = [

    path(
        "task/summary/",
        TaskSummaryAPI.as_view(),
        name="task_summary"
    ),

    path(
        "task/list/",
        TaskListAPI.as_view(),
        name="task_list"
    ),

    path(
        "task/group/",
        TaskGroupAPI.as_view(),
        name="task_group"
    ),

    path(
        "task/open/",
        TaskOpenAPI.as_view(),
        name="task_open"
    ),

]
