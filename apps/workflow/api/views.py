from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from account.services.auth_service import (
    ApiAuthenticationError,
    authenticate_request,
)

from workflow.services.task_query import (
    TaskCursorError,
    get_task_group,
    get_task_summary,
    get_task_list,
)

from workflow.services.task_action import (
    TaskActionError,
    open_task,
)


def authenticated_openid(request):
    """只使用Bearer令牌确定任务所属账号，不信任前端提交的OpenID。"""

    try:
        user = authenticate_request(request)
    except ApiAuthenticationError as exc:
        return None, Response(
            {"code": 401, "msg": str(exc)},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    if not user.wx_openid:
        return None, Response(
            {"code": 1, "msg": "当前账号不是微信业务账号"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    return user.wx_openid, None


class TaskSummaryAPI(APIView):

    def get(self, request):
        openid, error_response = authenticated_openid(request)
        if error_response:
            return error_response

        data = get_task_summary(openid)

        return Response({
            "code": 0,
            "data": data
        })


class TaskListAPI(APIView):

    def get(self, request):
        openid, error_response = authenticated_openid(request)
        if error_response:
            return error_response
        limit = request.GET.get("limit", 50)

        try:
            data = get_task_list(
                openid,
                limit=limit,
                cursor=request.GET.get("cursor"),
                task_filter=request.GET.get("filter", "all"),
            )
        except TaskCursorError as exc:
            return Response(
                {"code": 1, "msg": str(exc), "data": {"refresh_required": True}},
                status=exc.http_status,
            )

        return Response({
            "code": 0,
            "data": data
        })


class TaskGroupAPI(APIView):

    def get(self, request):
        openid, error_response = authenticated_openid(request)
        if error_response:
            return error_response

        data = get_task_group(openid, request.GET.get("group_id"))
        if data is None:
            return Response(
                {"code": 1, "msg": "任务组不存在或无权查看", "data": {}},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response({
            "code": 0,
            "data": data,
        })


class TaskOpenAPI(APIView):

    def post(self, request):
        openid, error_response = authenticated_openid(request)
        if error_response:
            return error_response
        usertask_id = request.data.get("usertask_id")
        try:
            result = open_task(openid, usertask_id)
        except TaskActionError as exc:
            return Response(
                {"code": 1, "msg": str(exc)},
                status=status.HTTP_403_FORBIDDEN,
            )

        return Response({
            "code": 0,
            "data": result
        })
