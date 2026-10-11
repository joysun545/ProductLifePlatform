from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from account.services.auth_service import ApiAuthenticationError, authenticate_request
from lifecycle.services.after_sales_station_service import (
    AfterSalesServiceStationError,
    AfterSalesServiceStationPermissionError,
    get_service_station_application_context,
    get_service_station_organization_badge,
    get_service_station_review_context,
    review_service_station_application,
    submit_service_station_application,
    update_approved_service_station_receiving_address,
)


def _authenticated_user(request):
    try:
        return authenticate_request(request), None
    except ApiAuthenticationError as exc:
        return None, Response(
            {"code": 401, "msg": str(exc), "data": {}},
            status=status.HTTP_401_UNAUTHORIZED,
        )


class AfterSalesServiceStationOrganizationBadgeAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        user, error = _authenticated_user(request)
        if error:
            return error
        data = get_service_station_organization_badge(user=user)
        return Response({"code": 0, "msg": "success", "data": data})


class AfterSalesServiceStationApplicationAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        user, error = _authenticated_user(request)
        if error:
            return error
        data = get_service_station_application_context(user=user)
        return Response({"code": 0, "msg": "success", "data": data})

    def post(self, request):
        user, error = _authenticated_user(request)
        if error:
            return error
        try:
            if request.data.get("action") == "update_receiving_address":
                data = update_approved_service_station_receiving_address(
                    user=user,
                    data=request.data,
                )
                message = "官方售后服务收件地址已更新"
                response_status = status.HTTP_200_OK
            else:
                data = submit_service_station_application(user=user, data=request.data)
                message = "售后服务站申请已提交"
                response_status = status.HTTP_201_CREATED
        except AfterSalesServiceStationError as exc:
            return Response(
                {"code": 1, "msg": str(exc), "data": {}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            {"code": 0, "msg": message, "data": data},
            status=response_status,
        )


class AfterSalesServiceStationReviewAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request, application_id):
        user, error = _authenticated_user(request)
        if error:
            return error
        try:
            data = get_service_station_review_context(
                user=user,
                application_id=application_id,
            )
        except AfterSalesServiceStationPermissionError as exc:
            return Response(
                {"code": 1, "msg": str(exc), "data": {}},
                status=status.HTTP_403_FORBIDDEN,
            )
        except AfterSalesServiceStationError as exc:
            return Response(
                {"code": 1, "msg": str(exc), "data": {}},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response({"code": 0, "msg": "success", "data": data})

    def post(self, request, application_id):
        user, error = _authenticated_user(request)
        if error:
            return error
        try:
            data = review_service_station_application(
                user=user,
                application_id=application_id,
                action=request.data.get("action"),
                review_note=request.data.get("review_note", ""),
            )
        except AfterSalesServiceStationPermissionError as exc:
            return Response(
                {"code": 1, "msg": str(exc), "data": {}},
                status=status.HTTP_403_FORBIDDEN,
            )
        except AfterSalesServiceStationError as exc:
            return Response(
                {"code": 1, "msg": str(exc), "data": {}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            {"code": 0, "msg": "审核已完成", "data": data},
            status=status.HTTP_200_OK,
        )
