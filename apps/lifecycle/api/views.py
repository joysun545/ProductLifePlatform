# apps/lifecycle/api/views.py


from django.http import FileResponse, HttpResponse
from django.core.cache import cache
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status


from product.models import ProductModel
from manufacturing.models import ProductionBatch
from account.services.auth_service import (
    ApiAuthenticationError,
    authenticate_request,
    get_active_identity,
)
from common.services.wechat_miniprogram_code import (
    WeChatMiniProgramCodeError,
    generate_unlimited_miniprogram_code,
)


from lifecycle.services.lifecycle_service import LifecycleService

from .serializers import (
    AcceptInstallationInvitationSerializer,
    ActivateCustomerDeviceSerializer,
    CreateLifecycleSerializer,
    InstallDeviceSerializer,
    RefreshInstallationInvitationSerializer,
    ResolveQRCodeSerializer,
)


from lifecycle.services.scan_service import QRCodeResolveError, ScanService
from lifecycle.models import InstallationInvitation
from lifecycle.services.installation_service import (
    InstallationPermissionError,
    InstallationService,
    InstallationStateError,
)
from lifecycle.services.customer_service import (
    CustomerDeviceService,
    CustomerFlowPermissionError,
    CustomerFlowStateError,
)


class QRCodeResolveAPI(APIView):
    """识别设备二维码并返回与当前账号身份匹配的只读数据。"""

    authentication_classes = []
    permission_classes = []

    def post(self, request):
        serializer = ResolveQRCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            user = authenticate_request(request)
        except ApiAuthenticationError as exc:
            return Response(
                {"code": 401, "msg": str(exc), "data": {}},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        try:
            data = ScanService.resolve(
                qr_text=serializer.validated_data["qr_text"],
                user=user,
            )
        except QRCodeResolveError as exc:
            return Response(
                {"code": 1, "msg": str(exc), "data": {}},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response({"code": 0, "msg": "success", "data": data})


def _invitation_payload(request, invitation):
    image_path = reverse(
        "pldp:api:lifecycle:installation_invitation_qrcode",
        kwargs={"token": invitation.token},
    )
    return {
        "token": str(invitation.token),
        "status": invitation.status,
        "expires_at": invitation.expires_at.isoformat(),
        "remaining_seconds": max(
            0,
            int((invitation.expires_at - timezone.now()).total_seconds()),
        ),
        "qr_text": f"INSTALL_INVITE|{invitation.token}",
        "qr_image_url": request.build_absolute_uri(image_path),
        "target_role": invitation.target_role.code,
        "target_role_name": invitation.target_role.name,
    }


class InstallDeviceAPI(APIView):
    """直接库存组织的指定商家岗位完成装车登记。"""

    authentication_classes = []
    permission_classes = []

    def post(self, request):
        serializer = InstallDeviceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            operator = authenticate_request(request)
        except ApiAuthenticationError as exc:
            return Response(
                {"code": 401, "msg": str(exc), "data": {}},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        try:
            installation, invitation = InstallationService.install(
                device_id=serializer.validated_data["device_id"],
                operator=operator,
                location=serializer.validated_data.get("location", ""),
            )
            scan_result = ScanService.resolve_device(
                device=installation.device,
                user=operator,
            )
        except InstallationPermissionError as exc:
            return Response(
                {"code": 403, "msg": str(exc), "data": {}},
                status=status.HTTP_403_FORBIDDEN,
            )
        except InstallationStateError as exc:
            return Response(
                {"code": 409, "msg": str(exc), "data": {}},
                status=status.HTTP_409_CONFLICT,
            )

        return Response(
            {
                "code": 0,
                "msg": "装车登记成功",
                "data": {
                    "scan_result": scan_result,
                    "invitation": _invitation_payload(request, invitation),
                },
            },
            status=status.HTTP_201_CREATED,
        )


class RefreshInstallationInvitationAPI(APIView):
    """已安装待激活设备重新生成用户负责人临时邀请。"""

    authentication_classes = []
    permission_classes = []

    def post(self, request):
        serializer = RefreshInstallationInvitationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            operator = authenticate_request(request)
        except ApiAuthenticationError as exc:
            return Response(
                {"code": 401, "msg": str(exc), "data": {}},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        try:
            installation, invitation = (
                InstallationService.refresh_customer_invitation(
                    device_id=serializer.validated_data["device_id"],
                    operator=operator,
                )
            )
            scan_result = ScanService.resolve_device(
                device=installation.device,
                user=operator,
            )
        except InstallationPermissionError as exc:
            return Response(
                {"code": 403, "msg": str(exc), "data": {}},
                status=status.HTTP_403_FORBIDDEN,
            )
        except InstallationStateError as exc:
            return Response(
                {"code": 409, "msg": str(exc), "data": {}},
                status=status.HTTP_409_CONFLICT,
            )

        return Response(
            {
                "code": 0,
                "msg": "用户邀请二维码已更新",
                "data": {
                    "scan_result": scan_result,
                    "invitation": _invitation_payload(request, invitation),
                },
            }
        )


class InstallationInvitationQRCodeAPI(APIView):
    """输出微信扫一扫可直达邀请确认页的小程序码。"""

    authentication_classes = []
    permission_classes = []

    def get(self, request, token):
        invitation = InstallationInvitation.objects.filter(token=token).first()
        if not invitation:
            return Response(
                {"code": 404, "msg": "邀请不存在", "data": {}},
                status=status.HTTP_404_NOT_FOUND,
            )

        if (
            invitation.status == InstallationInvitation.Status.PENDING
            and invitation.expires_at <= timezone.now()
        ):
            invitation.status = InstallationInvitation.Status.EXPIRED
            invitation.save(update_fields=("status", "updated_at"))

        if invitation.status != InstallationInvitation.Status.PENDING:
            return Response(
                {"code": 410, "msg": "邀请二维码已失效", "data": {}},
                status=status.HTTP_410_GONE,
            )

        cache_key = f"lifecycle:installation-invitation:minicode:{invitation.token}"
        cached_image = cache.get(cache_key)

        if cached_image is None:
            try:
                image_data, image_type = generate_unlimited_miniprogram_code(
                    scene=invitation.token.hex,
                    page="pages/customer/invite/accept/accept",
                )
            except WeChatMiniProgramCodeError as exc:
                return Response(
                    {"code": 503, "msg": str(exc), "data": {}},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )

            remaining_seconds = max(
                1,
                int((invitation.expires_at - timezone.now()).total_seconds()),
            )
            cache.set(
                cache_key,
                (image_data, image_type),
                timeout=min(remaining_seconds, 10 * 60),
            )
        else:
            image_data, image_type = cached_image

        if not image_data:
            return Response(
                {"code": 503, "msg": "微信小程序码生成失败", "data": {}},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        response = HttpResponse(image_data, content_type=image_type)
        response["Cache-Control"] = "no-store, max-age=0"
        return response


def _customer_flow_error_response(exc):
    if isinstance(exc, CustomerFlowPermissionError):
        return Response(
            {"code": 403, "msg": str(exc), "data": {}},
            status=status.HTTP_403_FORBIDDEN,
        )
    return Response(
        {"code": 409, "msg": str(exc), "data": {}},
        status=status.HTTP_409_CONFLICT,
    )


class InstallationInvitationDetailAPI(APIView):
    """用户扫码后读取一次性装车邀请的确认信息。"""

    authentication_classes = []
    permission_classes = []

    def get(self, request, token):
        try:
            user = authenticate_request(request)
        except ApiAuthenticationError as exc:
            return Response(
                {"code": 401, "msg": str(exc), "data": {}},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        try:
            data = CustomerDeviceService.invitation_detail(
                token=token,
                user=user,
            )
        except (CustomerFlowPermissionError, CustomerFlowStateError) as exc:
            return _customer_flow_error_response(exc)
        return Response({"code": 0, "msg": "success", "data": data})


class AcceptInstallationInvitationAPI(APIView):
    """建立用户负责人身份并接收这台已经装车的设备。"""

    authentication_classes = []
    permission_classes = []

    def post(self, request, token):
        serializer = AcceptInstallationInvitationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user = authenticate_request(request)
        except ApiAuthenticationError as exc:
            return Response(
                {"code": 401, "msg": str(exc), "data": {}},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        try:
            invitation, identity = CustomerDeviceService.accept_invitation(
                token=token,
                user=user,
                customer_data=serializer.validated_data,
            )
        except (CustomerFlowPermissionError, CustomerFlowStateError) as exc:
            return _customer_flow_error_response(exc)

        return Response(
            {
                "code": 0,
                "msg": "接受邀请成功",
                "data": {
                    "device_id": invitation.device_id,
                    "role": identity.role.code,
                    "role_name": identity.role.name,
                    "organization_id": identity.organization_id,
                    "organization_name": identity.organization.name,
                },
            },
            status=status.HTTP_201_CREATED,
        )


class CustomerDeviceResultAPI(APIView):
    """用户负责人专用设备结果数据。"""

    authentication_classes = []
    permission_classes = []

    def get(self, request, device_id):
        try:
            user = authenticate_request(request)
        except ApiAuthenticationError as exc:
            return Response(
                {"code": 401, "msg": str(exc), "data": {}},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        try:
            data = CustomerDeviceService.device_result(
                device_id=device_id,
                user=user,
            )
        except (CustomerFlowPermissionError, CustomerFlowStateError) as exc:
            return _customer_flow_error_response(exc)
        return Response({"code": 0, "msg": "success", "data": data})


class ActivateCustomerDeviceAPI(APIView):
    """用户负责人确认车辆信息后激活已经装车的设备。"""

    authentication_classes = []
    permission_classes = []

    def post(self, request, device_id):
        serializer = ActivateCustomerDeviceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user = authenticate_request(request)
        except ApiAuthenticationError as exc:
            return Response(
                {"code": 401, "msg": str(exc), "data": {}},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        try:
            data = CustomerDeviceService.activate(
                device_id=device_id,
                user=user,
                vehicle_no=serializer.validated_data["vehicle_no"],
                location=serializer.validated_data.get("location", ""),
            )
        except (CustomerFlowPermissionError, CustomerFlowStateError) as exc:
            return _customer_flow_error_response(exc)
        return Response(
            {"code": 0, "msg": "设备激活成功", "data": data}
        )


def _authenticated_user(request):
    try:
        return authenticate_request(request), None
    except ApiAuthenticationError as exc:
        return None, Response(
            {"code": 401, "msg": str(exc), "data": {}},
            status=status.HTTP_401_UNAUTHORIZED,
        )


class CustomerDeviceDocumentsAPI(APIView):
    """返回当前设备型号的三篇必读资料及账号阅读状态。"""

    authentication_classes = []
    permission_classes = []

    def get(self, request, device_id):
        user, error = _authenticated_user(request)
        if error:
            return error
        try:
            data = CustomerDeviceService.document_list(
                device_id=device_id,
                user=user,
            )
        except (CustomerFlowPermissionError, CustomerFlowStateError) as exc:
            return _customer_flow_error_response(exc)
        return Response({"code": 0, "msg": "success", "data": data})


class CustomerDeviceDocumentDetailAPI(APIView):
    """读取单篇型号资料正文。"""

    authentication_classes = []
    permission_classes = []

    def get(self, request, device_id, document_id):
        user, error = _authenticated_user(request)
        if error:
            return error
        try:
            data = CustomerDeviceService.document_detail(
                device_id=device_id,
                document_id=document_id,
                user=user,
            )
        except (CustomerFlowPermissionError, CustomerFlowStateError) as exc:
            return _customer_flow_error_response(exc)
        return Response({"code": 0, "msg": "success", "data": data})


class CustomerDeviceDocumentFileAPI(APIView):
    """鉴权后下载DOCX、PDF或原始文本资料。"""

    authentication_classes = []
    permission_classes = []

    def get(self, request, device_id, document_id):
        user, error = _authenticated_user(request)
        if error:
            return error
        try:
            document = CustomerDeviceService.document_file(
                device_id=device_id,
                document_id=document_id,
                user=user,
            )
            response = FileResponse(
                document.document_file.open("rb"),
                as_attachment=False,
                filename=document.original_filename or document.document_file.name,
                content_type=document.mime_type or "application/octet-stream",
            )
        except (CustomerFlowPermissionError, CustomerFlowStateError) as exc:
            return _customer_flow_error_response(exc)
        except (FileNotFoundError, OSError):
            return Response(
                {"code": 404, "msg": "资料文件不存在，请联系厂家重新上传", "data": {}},
                status=status.HTTP_404_NOT_FOUND,
            )
        response["Cache-Control"] = "private, no-store"
        return response


class CustomerDeviceDocumentReadAPI(APIView):
    """确认当前账号已经阅读单篇资料的当前版本。"""

    authentication_classes = []
    permission_classes = []

    def post(self, request, device_id, document_id):
        user, error = _authenticated_user(request)
        if error:
            return error
        try:
            data = CustomerDeviceService.mark_document_read(
                device_id=device_id,
                document_id=document_id,
                user=user,
            )
        except (CustomerFlowPermissionError, CustomerFlowStateError) as exc:
            return _customer_flow_error_response(exc)
        return Response({"code": 0, "msg": "已记录阅读状态", "data": data})


class CustomerDeviceTelemetryAPI(APIView):
    """独立返回设备遥测能力；第三方异常不影响设备主链路。"""

    authentication_classes = []
    permission_classes = []

    def get(self, request, device_id):
        user, error = _authenticated_user(request)
        if error:
            return error
        try:
            data = CustomerDeviceService.telemetry_snapshot(
                device_id=device_id,
                user=user,
            )
        except (CustomerFlowPermissionError, CustomerFlowStateError) as exc:
            return _customer_flow_error_response(exc)
        return Response({"code": 0, "msg": "success", "data": data})




class LifecycleCreateAPI(APIView):
    """
    创建设备生命周期入口

    V2:

    ProductModel
          |
          ↓
       Device
          |
          ↓
    LifecycleEvent

    """


    authentication_classes = []
    permission_classes = []

    def post(self, request):

        try:
            operator = authenticate_request(request)
        except ApiAuthenticationError as exc:
            return Response(
                {"code": 401, "msg": str(exc), "data": {}},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        identity = get_active_identity(operator)
        if not identity or identity.role.code not in {
            "factory_admin",
            "factory_manager",
        }:
            return Response(
                {"code": 403, "msg": "当前岗位不能创建设备", "data": {}},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = CreateLifecycleSerializer(
            data=request.data
        )


        serializer.is_valid(
            raise_exception=True
        )


        data = serializer.validated_data



        product_model = get_object_or_404(ProductModel, id=data["product_model_id"])
        batch = get_object_or_404(ProductionBatch, id=data["batch_id"])
        if batch.product_model_id != product_model.id:
            return Response(
                {"code": 1, "msg": "生产批次与产品型号不一致", "data": {}},
                status=status.HTTP_400_BAD_REQUEST,
            )



        try:
            device = LifecycleService.create_device(
                product_model=product_model,
                operator=operator,
                organization=identity.organization,
                batch=batch,
                sn=data.get("sn"),
            )
        except ValueError as exc:
            return Response(
                {"code": 1, "msg": str(exc), "data": {}},
                status=status.HTTP_400_BAD_REQUEST,
            )



        return Response(

            {
                "code": 0,
                "sn": device.sn,
                "device_id": device.id,
                "message": "Device created successfully"
            },

            status=status.HTTP_201_CREATED

        )
