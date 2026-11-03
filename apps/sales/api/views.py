import json
import logging
from uuid import uuid4

from django.http import FileResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from account.services.auth_service import ApiAuthenticationError, authenticate_request
from sales.services.order_plan_list_service import OrderPlanListError, get_order_plan_list
from sales.services.order_plan_material_flow_service import (
    OrderPlanMaterialFlowError,
    authorize_material_flow_shipment,
    cancel_material_flow_order_plan,
    confirm_material_flow_accounting,
    confirm_material_flow_merchant_receipt,
    confirm_material_flow_production,
    confirm_material_flow_shipment_logistics,
    confirm_material_flow_shipments_logistics,
    upload_material_flow_logistics_document,
    get_material_flow_logistics_document_file,
    confirm_material_flow_shipment_outbound,
    confirm_material_flow_shipment_qrcode_print,
    confirm_supplier_material_delivery,
    create_material_flow_order_plan,
    create_material_flow_shipment,
    get_material_flow_bom_detail,
    get_material_flow_detail,
    get_material_flow_options,
    get_order_delivery_receipt_form,
    receive_all_order_delivery_materials,
    release_material_ready_for_production,
    warehouse_review_material_plan,
)
from sales.services.order_plan_service import (
    OrderPlanError,
    configure_direct_merchant,
    get_order_plan_options,
    get_order_plan_reissue_options,
)


logger = logging.getLogger(__name__)


def _data(request):
    if "application/json" in (request.content_type or "").lower() and request.body:
        try:
            value = json.loads(request.body.decode("utf-8"))
            return value if isinstance(value, dict) else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
    return request.POST.dict()


def _user(request):
    try:
        return authenticate_request(request), None
    except ApiAuthenticationError as exc:
        return None, JsonResponse({"code": 401, "msg": str(exc), "data": {}}, status=401)


def _method(method):
    return JsonResponse({"code": 1, "msg": f"仅支持{method}", "data": {}}, status=200)


def _result(value):
    return value[0] if isinstance(value, tuple) else value or {}


def _optional_bool(value):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return value if isinstance(value, bool) else str(value).strip().lower() in {"1", "true", "yes", "on"}


def _v2_error(exc):
    return JsonResponse({"code": 1, "msg": str(exc), "data": {}}, status=200)


def order_plan_options(request):
    if request.method != "GET": return _method("GET")
    user, error = _user(request)
    if error: return error
    try:
        return JsonResponse({"code": 0, "msg": "", "data": get_order_plan_options(user)}, status=200)
    except OrderPlanError as exc:
        return _v2_error(exc)


def order_plan_reissue_options(request):
    if request.method != "GET": return _method("GET")
    user, error = _user(request)
    if error: return error
    try:
        result = get_order_plan_reissue_options(
            user=user, merchant_id=request.GET.get("merchant_id"), product_model_id=request.GET.get("product_model_id")
        )
        return JsonResponse({"code": 0, "msg": "", "data": result}, status=200)
    except OrderPlanError as exc:
        return _v2_error(exc)


def order_plan_list(request):
    if request.method != "GET": return _method("GET")
    user, error = _user(request)
    if error: return error
    try:
        result = get_order_plan_list(user=user, status=request.GET.get("status", ""), limit=request.GET.get("limit", 50))
        return JsonResponse({"code": 0, "msg": "", "data": result}, status=200)
    except OrderPlanListError as exc:
        return _v2_error(exc)


@csrf_exempt
def configure_merchant_channel(request):
    if request.method != "POST": return _method("POST")
    user, error = _user(request)
    if error: return error
    data = _data(request)
    try:
        result = configure_direct_merchant(
            user=user, merchant_id=data.get("merchant_id"), brand_id=data.get("brand_id"), region=data.get("region")
        )
        return JsonResponse({"code": 0, "msg": "商家区域和品牌已保存", "data": result}, status=200)
    except OrderPlanError as exc:
        return _v2_error(exc)


def material_flow_options(request):
    if request.method != "GET": return _method("GET")
    user, error = _user(request)
    if error: return error
    try:
        result = get_material_flow_options(user, merchant_id=request.GET.get("merchant_id"))
        return JsonResponse({"code": 0, "msg": "", "data": result}, status=200)
    except OrderPlanMaterialFlowError as exc:
        return _v2_error(exc)


def material_flow_bom_detail(request):
    if request.method != "GET": return _method("GET")
    user, error = _user(request)
    if error: return error
    try:
        result = get_material_flow_bom_detail(
            user=user, category_id=request.GET.get("category_id"), product_model_id=request.GET.get("product_model_id"),
            source_bom_id=request.GET.get("source_bom_id")
        )
        return JsonResponse({"code": 0, "msg": "", "data": result}, status=200)
    except OrderPlanMaterialFlowError as exc:
        return _v2_error(exc)


@csrf_exempt
def create_material_flow_plan(request):
    if request.method != "POST": return _method("POST")
    user, error = _user(request)
    if error: return error
    data = _data(request)
    try:
        result, created = create_material_flow_order_plan(
            user=user, merchant_id=data.get("merchant_id"), client_request_id=data.get("client_request_id"),
            items=data.get("items"), accessories=data.get("accessories"),
            remark=data.get("remark", "")
        )
        return JsonResponse({"code": 0, "msg": "订单计划创建成功" if created else "订单计划已创建，请勿重复提交", "data": result}, status=200)
    except OrderPlanMaterialFlowError as exc:
        return _v2_error(exc)
    except Exception:
        trace_id = uuid4().hex[:12]
        logger.exception(
            "material-flow order-plan creation failed trace_id=%s user_id=%s client_request_id=%r",
            trace_id,
            getattr(user, "pk", None),
            data.get("client_request_id"),
        )
        return JsonResponse(
            {
                "code": 1,
                "msg": f"订单计划创建异常，请稍后重试（追踪号：{trace_id}）",
                "data": {"trace_id": trace_id},
            },
            status=500,
        )


def material_flow_detail(request, plan_id):
    if request.method != "GET": return _method("GET")
    user, error = _user(request)
    if error: return error
    try:
        return JsonResponse({"code": 0, "msg": "", "data": get_material_flow_detail(user=user, plan_id=plan_id)}, status=200)
    except OrderPlanMaterialFlowError as exc:
        return _v2_error(exc)


def _post_v2(request, *, action, success_message, argument_builder):
    if request.method != "POST": return _method("POST")
    user, error = _user(request)
    if error: return error
    data = _data(request)
    try:
        result = action(user=user, **argument_builder(data))
        return JsonResponse({"code": 0, "msg": success_message, "data": _result(result)}, status=200)
    except OrderPlanMaterialFlowError as exc:
        return _v2_error(exc)


@csrf_exempt
def material_flow_warehouse_review(request, plan_id):
    return _post_v2(request, action=warehouse_review_material_plan, success_message="物料备料已核对，配送计划已生成", argument_builder=lambda _: {"plan_id": plan_id})


@csrf_exempt
def material_flow_supplier_confirm(request, plan_id):
    return _post_v2(request, action=confirm_supplier_material_delivery, success_message="物料配送已确认", argument_builder=lambda data: {"plan_id": plan_id, "supplier_id": data.get("supplier_id"), "delivery_id": data.get("delivery_id")})


@csrf_exempt
def material_flow_order_delivery_receipt(request, plan_id):
    user, error = _user(request)
    if error:
        return error
    try:
        if request.method == "GET":
            result = get_order_delivery_receipt_form(user=user, plan_id=plan_id)
            return JsonResponse({"code": 0, "msg": "", "data": result}, status=200)
        if request.method == "POST":
            result = receive_all_order_delivery_materials(user=user, plan_id=plan_id)
            return JsonResponse({"code": 0, "msg": "订单配送物料已全部入库", "data": _result(result)}, status=200)
        return _method("GET 或 POST")
    except OrderPlanMaterialFlowError as exc:
        return _v2_error(exc)


@csrf_exempt
def material_flow_material_ready(request, plan_id):
    return _post_v2(request, action=release_material_ready_for_production, success_message="所需物料全部到位，已推送生产", argument_builder=lambda _: {"plan_id": plan_id})


@csrf_exempt
def material_flow_production_confirm(request, plan_id):
    return _post_v2(request, action=confirm_material_flow_production, success_message="生产完成已确认", argument_builder=lambda data: {"plan_id": plan_id, "subplan_id": data.get("subplan_id")})


@csrf_exempt
def material_flow_shipment_create(request, plan_id):
    return _post_v2(request, action=create_material_flow_shipment, success_message="发货批次已创建", argument_builder=lambda data: {"plan_id": plan_id, "client_request_id": data.get("client_request_id"), "shipment_id": data.get("shipment_id"), "production_subplan_id": data.get("production_subplan_id"), "items": data.get("items")})


@csrf_exempt
def material_flow_shipment_authorize(request, plan_id):
    return _post_v2(request, action=authorize_material_flow_shipment, success_message="本批发货已授权，已推送成品库管打码", argument_builder=lambda data: {"plan_id": plan_id, "shipment_id": data.get("shipment_id")})


@csrf_exempt
def material_flow_shipment_qrcode_print(request, plan_id):
    return _post_v2(request, action=confirm_material_flow_shipment_qrcode_print, success_message="二维码已生成并确认打印", argument_builder=lambda data: {"plan_id": plan_id, "shipment_id": data.get("shipment_id")})


@csrf_exempt
def material_flow_shipment_logistics_confirm(request, plan_id):
    return _post_v2(request, action=confirm_material_flow_shipment_logistics, success_message="本批已出库，已推送成品库管确认发货", argument_builder=lambda data: {"plan_id": plan_id, "shipment_id": data.get("shipment_id"), "logistics_company": data.get("logistics_company"), "tracking_no": data.get("tracking_no"), "logistics_document_id": data.get("logistics_document_id")})


@csrf_exempt
def material_flow_shipments_logistics_confirm(request, plan_id):
    return _post_v2(
        request, action=confirm_material_flow_shipments_logistics,
        success_message="选中批次已出库，已推送成品库管确认发货",
        argument_builder=lambda data: {
            "plan_id": plan_id, "shipment_ids": data.get("shipment_ids"),
            "logistics_company": data.get("logistics_company"), "tracking_no": data.get("tracking_no"),
            "logistics_document_id": data.get("logistics_document_id"),
        },
    )


@csrf_exempt
def material_flow_shipment_logistics_document(request, plan_id):
    return _post_v2(
        request, action=upload_material_flow_logistics_document,
        success_message="物流凭证已上传",
        argument_builder=lambda data: {
            "plan_id": plan_id, "client_request_id": data.get("client_request_id"),
            "document": request.FILES.get("document"),
        },
    )


def material_flow_shipment_logistics_document_file(request, plan_id, document_id):
    if request.method != "GET":
        return _method("GET")
    user, error = _user(request)
    if error:
        return error
    try:
        document = get_material_flow_logistics_document_file(user=user, plan_id=plan_id, document_id=document_id)
        response = FileResponse(document.document_file.open("rb"), content_type=document.content_type or "application/octet-stream")
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response
    except OrderPlanMaterialFlowError as exc:
        return _v2_error(exc)
    except OSError:
        return _v2_error(OrderPlanMaterialFlowError("物流凭证文件暂不可读取，请联系管理员核对。"))


@csrf_exempt
def material_flow_shipment_outbound(request, plan_id):
    return _post_v2(request, action=confirm_material_flow_shipment_outbound, success_message="本批已发货，已通知商家及销售经理", argument_builder=lambda data: {"plan_id": plan_id, "shipment_id": data.get("shipment_id")})


@csrf_exempt
def material_flow_shipment_receipt(request, plan_id):
    return _post_v2(request, action=confirm_material_flow_merchant_receipt, success_message="商家入库已确认", argument_builder=lambda data: {"plan_id": plan_id, "shipment_id": data.get("shipment_id")})


@csrf_exempt
def material_flow_accounting_confirm(request, plan_id):
    return _post_v2(request, action=confirm_material_flow_accounting, success_message="订单已入账", argument_builder=lambda data: {"plan_id": plan_id, "shipment_id": data.get("shipment_id")})


@csrf_exempt
def material_flow_cancel(request, plan_id):
    return _post_v2(request, action=cancel_material_flow_order_plan, success_message="订单计划已取消", argument_builder=lambda data: {"plan_id": plan_id, "assembly_started_offline": _optional_bool(data.get("assembly_started_offline")), "reason": str(data.get("reason") or "")})


@csrf_exempt
def replacement_supply_plans(request,plan_id=None):
    user,error=_user(request)
    if error:return error
    from sales.services import replacement_supply_service as R
    try:
        if request.method=='GET':result=R.overview(user,plan_id,request.GET.get('order_id'))
        elif request.method=='POST':
            data=_data(request)
            if plan_id:
                from sales.models import ReplacementSupplyPlan
                plan=ReplacementSupplyPlan.objects.filter(pk=plan_id).first()
                if not plan:raise OrderPlanMaterialFlowError('补发计划不存在')
                if data.get('action')!='CANCEL':raise OrderPlanMaterialFlowError('未知补发计划操作')
                result={'plan':R.cancel(plan,user)}
            else:result={'plan':R.create(user,data.get('merchant_id'),data.get('client_request_id'))}
        else:return _method('GET/POST')
        return JsonResponse({'code':0,'msg':'ok','data':result})
    except OrderPlanMaterialFlowError as exc:return _v2_error(exc)
