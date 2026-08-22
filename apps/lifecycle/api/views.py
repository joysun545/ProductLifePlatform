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
    CreateGoodsTransferSerializer,
    FactoryReturnCreditAmountSerializer,
    FactoryReturnMatchingActionSerializer,
    FactoryReturnProductionActionSerializer,
    ScanGoodsTransferDeviceSerializer,
    FactoryReturnPostingSerializer,
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
from lifecycle.services.goods_transfer_service import (
    GoodsTransferError,
    confirm_goods_transfer_receipt,
    create_goods_transfer,
    get_goods_transfer_detail,
    get_goods_transfer_list,
    get_goods_transfer_options,
    scan_goods_transfer_device,
    submit_goods_transfer,
)
from lifecycle.services.goods_transfer_settlement_service import (
    GoodsTransferSettlementError,
    confirm_goods_transfer_payment,
    get_goods_transfer_settlement_detail,
    get_logistics_document_file as get_goods_transfer_logistics_document_file,
    get_payment_voucher_file as get_goods_transfer_payment_voucher_file,
    get_statement_file as get_goods_transfer_statement_file,
    submit_goods_transfer_statement,
    upload_goods_transfer_logistics_document,
    upload_goods_transfer_payment_voucher,
)
from lifecycle.services.goods_transfer_account_service import (
    GoodsTransferAccountError,
    allocate_goods_transfer_credit,
    confirm_goods_transfer_account_case,
    confirm_goods_transfer_account_payment,
    get_goods_transfer_account_payment_file,
    get_goods_transfer_account_statement_file,
    get_goods_transfer_accounts,
    submit_goods_transfer_account_case,
    upload_goods_transfer_account_payment,
)
from lifecycle.services.factory_return_service import (
    FactoryReturnProcessError,
    confirm_factory_return_pickup,
    get_factory_return_document_file,
    get_factory_return_inventory,
    get_factory_return_process,
    get_supplier_warranty_lists,
    get_supplier_warranty_dispatch,
    post_factory_return_inventory,
    process_factory_return_matching_item,
    process_factory_return_production_item,
    record_factory_return_credit_amount,
    receive_supplier_warranty_dispatch,
    upload_factory_return_document,
)


def _authenticated_flow_user(request):
    try:
        return authenticate_request(request), None
    except ApiAuthenticationError as exc:
        return None, Response(
            {"code": 401, "msg": str(exc), "data": {}},
            status=status.HTTP_401_UNAUTHORIZED,
        )


def _goods_transfer_error(exc):
    return Response(
        {"code": 1, "msg": str(exc), "data": {}},
        status=status.HTTP_409_CONFLICT,
    )


class GoodsTransferOptionsAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        user, error = _authenticated_flow_user(request)
        if error:
            return error
        try:
            data = get_goods_transfer_options(
                user=user,
                flow_type=request.GET.get("flow_type"),
            )
        except GoodsTransferError as exc:
            return _goods_transfer_error(exc)
        return Response({"code": 0, "msg": "success", "data": data})


class GoodsTransferListCreateAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        user, error = _authenticated_flow_user(request)
        if error:
            return error
        try:
            data = get_goods_transfer_list(
                user=user,
                limit=request.GET.get("limit", 50),
            )
        except GoodsTransferError as exc:
            return _goods_transfer_error(exc)
        return Response({"code": 0, "msg": "success", "data": data})

    def post(self, request):
        user, error = _authenticated_flow_user(request)
        if error:
            return error
        serializer = CreateGoodsTransferSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            data, created = create_goods_transfer(
                user=user,
                **serializer.validated_data,
            )
        except GoodsTransferError as exc:
            return _goods_transfer_error(exc)
        return Response(
            {
                "code": 0,
                "msg": "商品流转单已创建" if created else "商品流转单已存在",
                "data": data,
            },
            status=(
                status.HTTP_201_CREATED if created else status.HTTP_200_OK
            ),
        )


class GoodsTransferDetailAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request, transfer_id):
        user, error = _authenticated_flow_user(request)
        if error:
            return error
        try:
            data = get_goods_transfer_detail(
                user=user,
                transfer_id=transfer_id,
            )
        except GoodsTransferError as exc:
            return _goods_transfer_error(exc)
        return Response({"code": 0, "msg": "success", "data": data})


class GoodsTransferScanAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request, transfer_id):
        user, error = _authenticated_flow_user(request)
        if error:
            return error
        serializer = ScanGoodsTransferDeviceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            data = scan_goods_transfer_device(
                user=user,
                transfer_id=transfer_id,
                **serializer.validated_data,
            )
        except GoodsTransferError as exc:
            return _goods_transfer_error(exc)
        return Response({"code": 0, "msg": "设备已加入流转", "data": data})


class GoodsTransferSubmitAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request, transfer_id):
        user, error = _authenticated_flow_user(request)
        if error:
            return error
        try:
            data, changed = submit_goods_transfer(
                user=user,
                transfer_id=transfer_id,
                goods_list_document=request.FILES.get("goods_list_file"),
                goods_list_client_request_id=request.data.get(
                    "goods_list_client_request_id"
                ),
                return_remark=request.data.get("return_remark"),
            )
        except (GoodsTransferError, FactoryReturnProcessError) as exc:
            return _goods_transfer_error(exc)
        return Response({
            "code": 0,
            "msg": "商品流转已提交" if changed else "商品流转已提交",
            "data": data,
        })


class GoodsTransferReceiptAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request, transfer_id):
        user, error = _authenticated_flow_user(request)
        if error:
            return error
        try:
            data, changed = confirm_goods_transfer_receipt(
                user=user,
                transfer_id=transfer_id,
            )
        except GoodsTransferError as exc:
            return _goods_transfer_error(exc)
        return Response({
            "code": 0,
            "msg": "商品入库已确认" if changed else "商品已经入库",
            "data": data,
        })


def _factory_return_process_error(exc):
    return Response(
        {"code": 1, "msg": str(exc), "data": {}},
        status=status.HTTP_409_CONFLICT,
    )


class FactoryReturnProcessAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request, transfer_id):
        user, error = _authenticated_flow_user(request)
        if error:
            return error
        try:
            data = get_factory_return_process(
                user=user,
                transfer_id=transfer_id,
            )
        except FactoryReturnProcessError as exc:
            return _factory_return_process_error(exc)
        return Response({"code": 0, "msg": "success", "data": data})


class FactoryReturnPickupAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request, transfer_id):
        user, error = _authenticated_flow_user(request)
        if error:
            return error
        try:
            data, changed = confirm_factory_return_pickup(
                user=user,
                transfer_id=transfer_id,
            )
        except FactoryReturnProcessError as exc:
            return _factory_return_process_error(exc)
        return Response({
            "code": 0,
            "msg": "退货已确认取回" if changed else "退货已经取回",
            "data": data,
        })


class FactoryReturnPostingAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request, transfer_id):
        user, error = _authenticated_flow_user(request)
        if error:
            return error
        serializer = FactoryReturnPostingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            data, changed = post_factory_return_inventory(
                user=user,
                transfer_id=transfer_id,
                **serializer.validated_data,
            )
        except FactoryReturnProcessError as exc:
            return _factory_return_process_error(exc)
        return Response({
            "code": 0,
            "msg": "退货已分类入库" if changed else "退货已经分类入库",
            "data": data,
        })


class FactoryReturnCreditAmountAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request, transfer_id):
        user, error = _authenticated_flow_user(request)
        if error:
            return error
        serializer = FactoryReturnCreditAmountSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            data, changed = record_factory_return_credit_amount(
                user=user,
                transfer_id=transfer_id,
                amount=serializer.validated_data["amount"],
            )
        except FactoryReturnProcessError as exc:
            return _factory_return_process_error(exc)
        return Response({
            "code": 0,
            "msg": "退货冲抵金额已登记" if changed else "退货冲抵金额已经登记",
            "data": data,
        })


class FactoryReturnInventoryAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        user, error = _authenticated_flow_user(request)
        if error:
            return error
        try:
            data = get_factory_return_inventory(user=user)
        except FactoryReturnProcessError as exc:
            return _factory_return_process_error(exc)
        return Response({"code": 0, "msg": "success", "data": data})


class FactoryReturnProductionActionAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request, transfer_id, item_id):
        user, error = _authenticated_flow_user(request)
        if error:
            return error
        serializer = FactoryReturnProductionActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            data, changed = process_factory_return_production_item(
                user=user,
                transfer_id=transfer_id,
                item_id=item_id,
                **serializer.validated_data,
            )
        except FactoryReturnProcessError as exc:
            return _factory_return_process_error(exc)
        return Response({
            "code": 0,
            "msg": "生产处置已保存" if changed else "生产处置未变化",
            "data": data,
        })


class FactoryReturnMatchingActionAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request, transfer_id, item_id):
        user, error = _authenticated_flow_user(request)
        if error:
            return error
        serializer = FactoryReturnMatchingActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            data, changed = process_factory_return_matching_item(
                user=user,
                transfer_id=transfer_id,
                item_id=item_id,
                **serializer.validated_data,
            )
        except FactoryReturnProcessError as exc:
            return _factory_return_process_error(exc)
        return Response({
            "code": 0,
            "msg": "配套处置已保存" if changed else "配套处置未变化",
            "data": data,
        })


class SupplierWarrantyDispatchAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request, dispatch_id):
        user, error = _authenticated_flow_user(request)
        if error:
            return error
        try:
            data = get_supplier_warranty_dispatch(
                user=user,
                dispatch_id=dispatch_id,
            )
        except FactoryReturnProcessError as exc:
            return _factory_return_process_error(exc)
        return Response({"code": 0, "msg": "success", "data": data})


class SupplierWarrantyListsAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        user, error = _authenticated_flow_user(request)
        if error:
            return error
        try:
            data = get_supplier_warranty_lists(user=user)
        except FactoryReturnProcessError as exc:
            return _factory_return_process_error(exc)
        return Response({"code": 0, "msg": "success", "data": data})


class SupplierWarrantyDispatchReceiveAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request, dispatch_id):
        user, error = _authenticated_flow_user(request)
        if error:
            return error
        try:
            data, changed = receive_supplier_warranty_dispatch(
                user=user,
                dispatch_id=dispatch_id,
            )
        except FactoryReturnProcessError as exc:
            return _factory_return_process_error(exc)
        return Response({
            "code": 0,
            "msg": "三包物料通知已接收" if changed else "知晓任务无需重复确认",
            "data": data,
        })


class FactoryReturnDocumentUploadAPI(APIView):
    authentication_classes = []
    permission_classes = []
    document_kind = ""
    document_label = "退厂退货单据"

    def post(self, request, transfer_id):
        user, error = _authenticated_flow_user(request)
        if error:
            return error
        try:
            data, created = upload_factory_return_document(
                user=user,
                transfer_id=transfer_id,
                document_kind=self.document_kind,
                client_request_id=request.data.get("client_request_id"),
                document=request.FILES.get("document_file"),
                remark=request.data.get("remark", ""),
            )
        except FactoryReturnProcessError as exc:
            return _factory_return_process_error(exc)
        return Response({
            "code": 0,
            "msg": (
                f"{self.document_label}已上传"
                if created
                else f"{self.document_label}已存在"
            ),
            "data": data,
        })


class FactoryReturnLogisticsDocumentsAPI(FactoryReturnDocumentUploadAPI):
    document_kind = "LOGISTICS"
    document_label = "退货物流单据"


class FactoryReturnGoodsListDocumentsAPI(FactoryReturnDocumentUploadAPI):
    document_kind = "GOODS_LIST"
    document_label = "退货货品清单"


class FactoryReturnDocumentFileAPI(APIView):
    authentication_classes = []
    permission_classes = []
    document_kind = ""
    fallback_prefix = "factory-return-document"

    def get(self, request, transfer_id, document_id):
        user, error = _authenticated_flow_user(request)
        if error:
            return error
        try:
            field_file, original_name, content_type = (
                get_factory_return_document_file(
                    user=user,
                    transfer_id=transfer_id,
                    document_id=document_id,
                    document_kind=self.document_kind,
                )
            )
        except FactoryReturnProcessError as exc:
            return _factory_return_process_error(exc)
        return _private_file_response(
            field_file,
            original_name,
            content_type,
            f"{self.fallback_prefix}-{document_id}",
        )


class FactoryReturnLogisticsDocumentFileAPI(FactoryReturnDocumentFileAPI):
    document_kind = "LOGISTICS"
    fallback_prefix = "factory-return-logistics"


class FactoryReturnGoodsListDocumentFileAPI(FactoryReturnDocumentFileAPI):
    document_kind = "GOODS_LIST"
    fallback_prefix = "factory-return-goods-list"


def _goods_transfer_settlement_error(exc):
    return Response(
        {"code": 1, "msg": str(exc), "data": {}},
        status=status.HTTP_409_CONFLICT,
    )


def _private_file_response(field_file, original_name, content_type, fallback):
    response = FileResponse(
        field_file.open("rb"),
        content_type=content_type,
        as_attachment=False,
        filename=original_name or fallback,
    )
    response["Cache-Control"] = "private, no-store"
    return response


class GoodsTransferSettlementDetailAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request, transfer_id):
        user, error = _authenticated_flow_user(request)
        if error:
            return error
        try:
            data = get_goods_transfer_settlement_detail(
                user=user,
                transfer_id=transfer_id,
            )
        except GoodsTransferSettlementError as exc:
            return _goods_transfer_settlement_error(exc)
        return Response({"code": 0, "msg": "success", "data": data})


class GoodsTransferSettlementStatementAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request, transfer_id):
        user, error = _authenticated_flow_user(request)
        if error:
            return error
        try:
            data = submit_goods_transfer_statement(
                user=user,
                transfer_id=transfer_id,
                receivable_amount=request.data.get("receivable_amount"),
                image=request.FILES.get("statement_image"),
            )
        except GoodsTransferSettlementError as exc:
            return _goods_transfer_settlement_error(exc)
        return Response({"code": 0, "msg": "货品清单和应收金额已提交", "data": data})


class GoodsTransferSettlementLogisticsDocumentsAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request, transfer_id):
        user, error = _authenticated_flow_user(request)
        if error:
            return error
        try:
            data, created = upload_goods_transfer_logistics_document(
                user=user,
                transfer_id=transfer_id,
                client_request_id=request.data.get("client_request_id"),
                document=request.FILES.get("document_file"),
                remark=request.data.get("remark", ""),
            )
        except GoodsTransferSettlementError as exc:
            return _goods_transfer_settlement_error(exc)
        return Response({
            "code": 0,
            "msg": "物流单据已上传" if created else "该物流单据已经上传，请勿重复提交",
            "data": data,
        })


class GoodsTransferSettlementPaymentsAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request, transfer_id):
        user, error = _authenticated_flow_user(request)
        if error:
            return error
        try:
            data, created = upload_goods_transfer_payment_voucher(
                user=user,
                transfer_id=transfer_id,
                client_request_id=request.data.get("client_request_id"),
                image=request.FILES.get("voucher_image"),
            )
        except GoodsTransferSettlementError as exc:
            return _goods_transfer_settlement_error(exc)
        return Response({
            "code": 0,
            "msg": "付款凭证已上传" if created else "付款凭证已经上传，请勿重复提交",
            "data": data,
        })


class GoodsTransferSettlementPaymentConfirmAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request, transfer_id, payment_id):
        user, error = _authenticated_flow_user(request)
        if error:
            return error
        try:
            data, changed = confirm_goods_transfer_payment(
                user=user,
                transfer_id=transfer_id,
                payment_id=payment_id,
                amount=request.data.get("confirmed_amount"),
            )
        except GoodsTransferSettlementError as exc:
            return _goods_transfer_settlement_error(exc)
        return Response({
            "code": 0,
            "msg": "收款已确认" if changed else "该笔收款已经确认",
            "data": data,
        })


class GoodsTransferSettlementStatementFileAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request, transfer_id):
        user, error = _authenticated_flow_user(request)
        if error:
            return error
        try:
            file_value = get_goods_transfer_statement_file(
                user=user,
                transfer_id=transfer_id,
            )
        except GoodsTransferSettlementError as exc:
            return _goods_transfer_settlement_error(exc)
        return _private_file_response(*file_value, fallback="goods-statement.jpg")


class GoodsTransferSettlementLogisticsDocumentFileAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request, transfer_id, document_id):
        user, error = _authenticated_flow_user(request)
        if error:
            return error
        try:
            file_value = get_goods_transfer_logistics_document_file(
                user=user,
                transfer_id=transfer_id,
                document_id=document_id,
            )
        except GoodsTransferSettlementError as exc:
            return _goods_transfer_settlement_error(exc)
        return _private_file_response(*file_value, fallback="logistics-document")


class GoodsTransferSettlementPaymentVoucherFileAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request, transfer_id, payment_id):
        user, error = _authenticated_flow_user(request)
        if error:
            return error
        try:
            file_value = get_goods_transfer_payment_voucher_file(
                user=user,
                transfer_id=transfer_id,
                payment_id=payment_id,
            )
        except GoodsTransferSettlementError as exc:
            return _goods_transfer_settlement_error(exc)
        return _private_file_response(*file_value, fallback="payment-voucher.jpg")


class GoodsTransferAccountsAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request, transfer_id):
        user, error = _authenticated_flow_user(request)
        if error:
            return error
        try:
            data = get_goods_transfer_accounts(user=user, transfer_id=transfer_id)
        except GoodsTransferAccountError as exc:
            return _goods_transfer_settlement_error(exc)
        return Response({"code": 0, "msg": "success", "data": data})


class GoodsTransferAccountSubmitAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request, transfer_id, case_type):
        user, error = _authenticated_flow_user(request)
        if error:
            return error
        try:
            data = submit_goods_transfer_account_case(
                user=user,
                transfer_id=transfer_id,
                case_type=case_type,
                amount=request.data.get("amount"),
                no_amount=request.data.get("no_amount", False),
                remark=request.data.get("remark", ""),
                image=request.FILES.get("statement_image"),
            )
        except GoodsTransferAccountError as exc:
            return _goods_transfer_settlement_error(exc)
        return Response({"code": 0, "msg": "往来金额已提交，等待对方确认", "data": data})


class GoodsTransferAccountConfirmAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request, transfer_id, case_id):
        user, error = _authenticated_flow_user(request)
        if error:
            return error
        try:
            data = confirm_goods_transfer_account_case(
                user=user,
                transfer_id=transfer_id,
                case_id=case_id,
                action=request.data.get("action"),
                rejection_reason=request.data.get("rejection_reason", ""),
            )
        except GoodsTransferAccountError as exc:
            return _goods_transfer_settlement_error(exc)
        return Response({"code": 0, "msg": "往来金额已处理", "data": data})


class GoodsTransferAccountAllocationsAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request, transfer_id, case_id):
        user, error = _authenticated_flow_user(request)
        if error:
            return error
        try:
            data = allocate_goods_transfer_credit(
                user=user,
                transfer_id=transfer_id,
                case_id=case_id,
                target_kind=request.data.get("target_kind"),
                target_id=request.data.get("target_id"),
                amount=request.data.get("amount"),
            )
        except (GoodsTransferAccountError, TypeError, ValueError) as exc:
            return _goods_transfer_settlement_error(exc)
        return Response({"code": 0, "msg": "退货冲抵已关联应收批次", "data": data})


class GoodsTransferAccountPaymentsAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request, transfer_id, case_id):
        user, error = _authenticated_flow_user(request)
        if error:
            return error
        try:
            data, created = upload_goods_transfer_account_payment(
                user=user,
                transfer_id=transfer_id,
                case_id=case_id,
                client_request_id=request.data.get("client_request_id"),
                image=request.FILES.get("voucher_image"),
            )
        except GoodsTransferAccountError as exc:
            return _goods_transfer_settlement_error(exc)
        return Response({
            "code": 0,
            "msg": "付款凭证已上传" if created else "付款凭证已经上传，请勿重复提交",
            "data": data,
        })


class GoodsTransferAccountPaymentConfirmAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request, transfer_id, case_id, payment_id):
        user, error = _authenticated_flow_user(request)
        if error:
            return error
        try:
            data, changed = confirm_goods_transfer_account_payment(
                user=user,
                transfer_id=transfer_id,
                case_id=case_id,
                payment_id=payment_id,
                amount=request.data.get("confirmed_amount"),
            )
        except GoodsTransferAccountError as exc:
            return _goods_transfer_settlement_error(exc)
        return Response({
            "code": 0,
            "msg": "收款已确认" if changed else "该笔收款已经确认",
            "data": data,
        })


class GoodsTransferAccountStatementFileAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request, transfer_id, case_id):
        user, error = _authenticated_flow_user(request)
        if error:
            return error
        try:
            file_value = get_goods_transfer_account_statement_file(
                user=user,
                transfer_id=transfer_id,
                case_id=case_id,
            )
        except GoodsTransferAccountError as exc:
            return _goods_transfer_settlement_error(exc)
        return _private_file_response(*file_value, fallback="account-statement.jpg")


class GoodsTransferAccountPaymentVoucherFileAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request, transfer_id, case_id, payment_id):
        user, error = _authenticated_flow_user(request)
        if error:
            return error
        try:
            file_value = get_goods_transfer_account_payment_file(
                user=user,
                transfer_id=transfer_id,
                case_id=case_id,
                payment_id=payment_id,
            )
        except GoodsTransferAccountError as exc:
            return _goods_transfer_settlement_error(exc)
        return _private_file_response(*file_value, fallback="account-payment.jpg")


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


class CustomerDeviceListAPI(APIView):
    """返回当前用户负责人名下的全部设备。"""

    authentication_classes = []
    permission_classes = []

    def get(self, request):
        try:
            user = authenticate_request(request)
        except ApiAuthenticationError as exc:
            return Response(
                {"code": 401, "msg": str(exc), "data": {}},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        try:
            data = CustomerDeviceService.device_list(user=user)
        except (CustomerFlowPermissionError, CustomerFlowStateError) as exc:
            return _customer_flow_error_response(exc)
        return Response({"code": 0, "msg": "success", "data": data})


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
