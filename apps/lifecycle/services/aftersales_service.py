import calendar
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from account.models import Organization, OrganizationRelation, OrganizationRelationType, UserOrganization
from account.services.auth_service import get_active_identity
from lifecycle.models import (
    AfterSalesApplication,
    AfterSalesEvent,
    AfterSalesMedia,
    Device,
    LifecycleEvent,
)
from lifecycle.services.customer_service import CustomerDeviceService
from supply_chain.models import MaterialSpecSupplier
from supply_chain.services.supplier_responsibility_service import (
    get_supplier_task_recipients,
)
from workflow.models import UserTask, WorkItem
from workflow.services.workflow_service import publish_workitem


class AfterSalesError(Exception):
    """A business validation error for the canonical after-sales application."""


def _add_months(value, months):
    month = value.month - 1 + months
    year = value.year + month // 12
    month = month % 12 + 1
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


def _user_name(user):
    if not user:
        return ""
    return user.wx_nickname or user.get_full_name() or user.username


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


class AfterSalesService:
    """One master record for every after-sales request and its execution path.

    No dispatch, repair, logistics or third-party root model is created here.
    The current position is stored on AfterSalesApplication; immutable process
    facts are appended to AfterSalesEvent and evidence is kept in
    AfterSalesMedia with a purpose.
    """

    APPLICATION_MEDIA_LIMIT = 9
    OTHER_MEDIA_LIMIT = 9
    IMAGE_MAX_BYTES = 20 * 1024 * 1024
    VIDEO_MAX_BYTES = 200 * 1024 * 1024
    DOCUMENT_MAX_BYTES = 30 * 1024 * 1024
    IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
    VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".3gp"}
    DOCUMENT_SUFFIXES = {
        ".pdf", ".jpg", ".jpeg", ".png", ".webp", ".heic",
        ".mp4", ".mov", ".m4v", ".3gp",
    }
    ACTIVE_STATUSES = (
        AfterSalesApplication.Status.DRAFT,
        AfterSalesApplication.Status.SUBMITTED,
        AfterSalesApplication.Status.ROUTING,
        AfterSalesApplication.Status.PROCESSING,
        AfterSalesApplication.Status.WAITING_CUSTOMER_CONFIRM,
        AfterSalesApplication.Status.PENDING_FACTORY_CLOSE,
    )
    FINAL_COMPONENT_FAULT = AfterSalesApplication.FaultType.COMPONENT_SUSPECTED
    APPLICANT_WORKORDER_STEP = "applicant-workorder"
    APPLICANT_WORKORDER_TYPE = "AFTERSALES_APPLICANT_WORKORDER"
    FACTORY_WORKORDER_STEP = "factory-workorder"
    FACTORY_WORKORDER_TYPE = "AFTERSALES_FACTORY_WORKORDER"
    ASSIGNEE_WORKORDER_STEP = "assignee-workorder"
    ASSIGNEE_WORKORDER_TYPE = "AFTERSALES_ASSIGNEE_WORKORDER"
    # Versions before the stable applicant workorder used one WorkItem for
    # every notification/action.  These types are only used to adopt the
    # earliest historical applicant task for a case; immutable after-sales
    # events remain the audit trail of every update.
    LEGACY_APPLICANT_WORKITEM_TYPES = (
        "AFTERSALES_APPLICATION_SUBMITTED",
        "AFTERSALES_PROGRESS_NOTICE",
        "AFTERSALES_APPLICANT_SHIPMENT",
        "AFTERSALES_APPLICANT_CONFIRM",
    )
    SUPPLIER_QUALITY_NOTICE_TYPE = "AFTERSALES_SUPPLIER_QUALITY_NOTICE"

    @staticmethod
    def _identity(user):
        return get_active_identity(user)

    @classmethod
    def _event(cls, application, event_type, *, user=None, identity=None, payload=None):
        identity = identity if identity is not None else (
            cls._identity(user) if user else None
        )
        AfterSalesEvent.objects.create(
            application=application,
            event_type=event_type,
            actor=user,
            actor_openid=(user.wx_openid or "") if user else "",
            actor_role_code=identity.role.code if identity else "",
            actor_role_name=identity.role.name if identity else "",
            organization_id_snapshot=identity.organization_id if identity else None,
            organization_name_snapshot=identity.organization.name if identity else "",
            payload=payload or {},
        )

    @staticmethod
    def _option_rows(choices):
        return [{"value": value, "label": label} for value, label in choices]

    @classmethod
    def _snapshot(cls, device):
        activation = getattr(device, "activation", None)
        bom = device.batch.bom if device.batch_id and device.batch.bom_id else None
        items = []
        warranty_months = []
        if bom:
            for item in bom.items.select_related(
                "material", "material_spec", "supplier"
            ):
                months = None
                if item.material_spec_id and item.supplier_id:
                    relation = MaterialSpecSupplier.objects.filter(
                        material_spec_id=item.material_spec_id,
                        supplier_id=item.supplier_id,
                    ).first()
                    months = relation.warranty_months if relation else None
                if item.material.is_key_part and months:
                    warranty_months.append(months)
                items.append(
                    {
                        "bom_item_id": item.id,
                        "material_id": item.material_id,
                        "material_name": item.material.name,
                        "is_key_part": item.material.is_key_part,
                        "specification_id": item.material_spec_id,
                        "specification_name": (
                            item.material_spec.name if item.material_spec else ""
                        ),
                        "supplier_id": item.supplier_id,
                        "supplier_name": (
                            item.supplier.name if item.supplier else "供应商待确认"
                        ),
                        "quantity": float(item.quantity),
                        "warranty_months": months,
                    }
                )
        activation_date = activation.activation_time.date() if activation else None
        overall_months = min(warranty_months) if warranty_months else None
        warranty_end = (
            _add_months(activation_date, overall_months)
            if activation_date and overall_months
            else None
        )
        in_warranty = bool(warranty_end and timezone.localdate() <= warranty_end)
        warranty_summary = (
            f"至 {warranty_end.isoformat()}（{'质保期内' if in_warranty else '已过保'}）"
            if warranty_end
            else "暂无明确质保期限"
        )
        try:
            qr_code = device.qrcode.code
        except ObjectDoesNotExist:
            qr_code = ""
        return {
            "device": {
                "id": device.id,
                "sn": device.sn,
                "qr_code": qr_code,
                "product_model": device.product_model.name,
                "product_model_id": device.product_model_id,
                "production_batch": device.batch.batch_no if device.batch_id else "",
            },
            "activation": {
                "activated_at": (
                    activation.activation_time.isoformat() if activation else ""
                ),
                "vehicle_no": activation.vehicle_no if activation else "",
            },
            "bom": {
                "id": bom.id if bom else None,
                "version": bom.version if bom else "",
                "items": items,
                "key_materials": [item for item in items if item["is_key_part"]],
            },
            "warranty": {
                "rule": "KEY_MATERIAL_MINIMUM",
                "months": overall_months,
                "start_date": activation_date.isoformat() if activation_date else "",
                "end_date": warranty_end.isoformat() if warranty_end else "",
                "in_warranty": in_warranty,
                "summary": warranty_summary,
            },
        }

    @classmethod
    def _validate_location(cls, application):
        if not application.location_confirmed:
            raise AfterSalesError("请先确认真实位置")
        if (
            application.location_latitude is None
            or application.location_longitude is None
        ):
            raise AfterSalesError("真实位置缺少经纬度，请重新定位")
        try:
            latitude = Decimal(str(application.location_latitude))
            longitude = Decimal(str(application.location_longitude))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise AfterSalesError("位置坐标格式不正确") from exc
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise AfterSalesError("位置坐标超出有效范围")
        if not (application.location_name or application.location_address):
            raise AfterSalesError("请确认位置名称或详细地址")

    @staticmethod
    def _coordinate(value, *, axis):
        """Normalize untrusted API coordinates before saving a DecimalField."""
        text = str(value or "").strip()
        if not text:
            return None
        try:
            coordinate = Decimal(text)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise AfterSalesError(f"{axis}坐标格式不正确") from exc
        if not coordinate.is_finite():
            raise AfterSalesError(f"{axis}坐标格式不正确")
        minimum, maximum = (-90, 90) if axis == "纬度" else (-180, 180)
        if not minimum <= coordinate <= maximum:
            raise AfterSalesError(f"{axis}坐标超出有效范围")
        return coordinate.quantize(Decimal("0.0000001"))

    @classmethod
    def context(cls, *, device_id, user):
        identity, device, _ = CustomerDeviceService._owned_device_context(
            device_id=device_id, user=user
        )
        active = AfterSalesApplication.objects.filter(
            device=device, status__in=cls.ACTIVE_STATUSES
        ).first()
        snapshot = cls._snapshot(device)
        if active and active.applicant_id != user.id:
            active_data = {
                "id": active.id,
                "case_no": active.case_no,
                "status": active.status,
                "status_name": active.get_status_display(),
                "owned_by_another_applicant": True,
            }
        else:
            active_data = cls.serialize(active, detail=True) if active else None
        return {
            **snapshot,
            "applicant": {
                "role_code": identity.role.code,
                "role_name": identity.role.name,
                "organization": identity.organization.name,
            },
            "active_application": active_data,
        }

    @classmethod
    @transaction.atomic
    def save_draft(cls, *, device_id, user, data):
        identity, device, _ = CustomerDeviceService._owned_device_context(
            device_id=device_id, user=user
        )
        # A select_for_update on an empty application queryset cannot prevent
        # two first-draft requests from racing.  Lock the stable Device row so
        # the one-active-application constraint remains a user-facing rule,
        # not an IntegrityError response.
        device = Device.objects.select_for_update().get(pk=device.pk)
        factory = device.batch.manufacturer if device.batch_id else None
        if not factory:
            raise AfterSalesError("设备缺少原生产厂家，暂不能申请售后")
        application = (
            AfterSalesApplication.objects.select_for_update()
            .filter(device=device, status__in=cls.ACTIVE_STATUSES)
            .first()
        )
        if application and application.status != AfterSalesApplication.Status.DRAFT:
            raise AfterSalesError("该设备已有进行中的售后申请")
        if application and application.applicant_id != user.id:
            raise AfterSalesError("该设备已有其他账号创建的售后草稿")

        snapshot = cls._snapshot(device)
        created = application is None
        if created:
            application = AfterSalesApplication(
                case_no=f"AS-{timezone.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6].upper()}",
                device=device,
                customer_organization=identity.organization,
                factory_organization=factory,
                applicant=user,
                applicant_role_code=identity.role.code,
                applicant_role_name=identity.role.name,
            )

        application.fault_description = str(
            data.get("fault_description") or ""
        ).strip()
        application.location_name = str(data.get("location_name") or "").strip()
        application.location_address = str(
            data.get("location_address") or ""
        ).strip()
        application.location_latitude = cls._coordinate(
            data.get("location_latitude"), axis="纬度"
        )
        application.location_longitude = cls._coordinate(
            data.get("location_longitude"), axis="经度"
        )
        application.location_confirmed = _as_bool(data.get("location_confirmed"))
        application.location_confirmed_at = (
            timezone.now() if application.location_confirmed else None
        )
        application.device_snapshot = snapshot["device"]
        application.activation_snapshot = snapshot["activation"]
        application.bom_snapshot = snapshot["bom"]
        application.warranty_snapshot = snapshot["warranty"]
        application.save()
        cls._event(
            application,
            "DRAFT_CREATED" if created else "DRAFT_UPDATED",
            user=user,
            identity=identity,
        )
        return cls.serialize(application), created

    @classmethod
    def _is_factory_sales(cls, application, user, identity=None):
        identity = identity if identity is not None else cls._identity(user)
        return bool(
            identity
            and identity.organization_id == application.factory_organization_id
            and identity.role.code == "factory_sales"
        )

    @classmethod
    def _is_applicant(cls, application, user):
        return application.applicant_id == user.id

    @classmethod
    def _is_assignee(cls, application, user):
        return bool(application.assigned_user_id and application.assigned_user_id == user.id)

    @classmethod
    def can_view_application(cls, *, application, user):
        identity = cls._identity(user)
        return (
            cls._is_applicant(application, user)
            or cls._is_assignee(application, user)
            or cls._is_factory_sales(application, user, identity)
        )

    @classmethod
    def _require_factory_sales(cls, application, user):
        identity = cls._identity(user)
        if not cls._is_factory_sales(application, user, identity):
            raise AfterSalesError("仅对应厂家销售经理可以处理该售后申请")
        return identity

    @classmethod
    def _require_applicant(cls, application, user):
        if not cls._is_applicant(application, user):
            raise AfterSalesError("仅售后发起人可以执行此操作")
        return cls._identity(user)

    @classmethod
    def _require_assignee(cls, application, user):
        if not cls._is_assignee(application, user):
            raise AfterSalesError("仅被指定的维修执行人可以执行此操作")
        return cls._identity(user)

    @classmethod
    def _contact_snapshot(cls, *, user, organization, role_code="", role_name="", fallback_address=""):
        return {
            "user_id": user.id,
            "name": _user_name(user),
            "phone": user.phone or "",
            "openid": user.wx_openid or "",
            "organization_id": organization.id if organization else None,
            "organization_name": organization.name if organization else "",
            "organization_region": organization.region if organization else "",
            "organization_address": (
                (organization.address if organization else "") or fallback_address or ""
            ),
            "role_code": role_code,
            "role_name": role_name,
            "assigned_at": timezone.now().isoformat(),
        }

    @classmethod
    def _candidate_rows(cls, application):
        """Return only server-derived candidates for dispatch.

        A first-level merchant is a direct DISTRIBUTION relation whose source
        is the manufacturer itself.  No descendant merchant is inferred.
        """
        rows = []
        seen = set()

        def add(user, organization, route, role_code, role_name, fallback_address=""):
            if not user or user.id in seen or not user.is_active or not user.wx_openid:
                return
            seen.add(user.id)
            snapshot = cls._contact_snapshot(
                user=user,
                organization=organization,
                role_code=role_code,
                role_name=role_name,
                fallback_address=fallback_address,
            )
            requires_address = route in {
                AfterSalesApplication.DispatchRoute.MERCHANT_ELECTRICIAN,
                AfterSalesApplication.DispatchRoute.FACTORY_AFTERSALES,
            }
            available = not requires_address or bool(snapshot["organization_address"])
            rows.append(
                {
                    "user": user,
                    "organization": organization,
                    "route": route,
                    "snapshot": snapshot,
                    "available": available,
                    "unavailable_reason": (
                        "寄修路径要求先维护维修组织详细地址"
                        if not available
                        else ""
                    ),
                }
            )

        try:
            installation = application.device.installation
        except ObjectDoesNotExist:
            installation = None
        if installation and installation.operator_id:
            add(
                installation.operator,
                installation.organization,
                AfterSalesApplication.DispatchRoute.ORIGINAL_INSTALLER,
                "installation_operator",
                "原装车操作人员",
                fallback_address=installation.location,
            )

        direct_merchant_ids = OrganizationRelation.objects.filter(
            source_organization_id=application.factory_organization_id,
            relation_type=OrganizationRelationType.DISTRIBUTION,
            is_active=True,
            target_organization__is_active=True,
        ).values_list("target_organization_id", flat=True)
        for identity in UserOrganization.objects.select_related(
            "user", "organization", "role"
        ).filter(
            organization_id__in=direct_merchant_ids,
            role__code="merchant_electrician",
            is_active=True,
            user__is_active=True,
            organization__is_active=True,
            role__is_active=True,
        ):
            add(
                identity.user,
                identity.organization,
                AfterSalesApplication.DispatchRoute.MERCHANT_ELECTRICIAN,
                identity.role.code,
                identity.role.name,
            )

        for identity in UserOrganization.objects.select_related(
            "user", "organization", "role"
        ).filter(
            organization_id=application.factory_organization_id,
            role__code="factory_aftersales",
            is_active=True,
            user__is_active=True,
            organization__is_active=True,
            role__is_active=True,
        ):
            add(
                identity.user,
                identity.organization,
                AfterSalesApplication.DispatchRoute.FACTORY_AFTERSALES,
                identity.role.code,
                identity.role.name,
            )
        return rows

    @classmethod
    def _public_candidate(cls, row):
        snapshot = row["snapshot"]
        return {
            "user_id": snapshot["user_id"],
            "name": snapshot["name"],
            "phone": snapshot["phone"],
            "organization_id": snapshot["organization_id"],
            "organization": snapshot["organization_name"],
            "region": snapshot["organization_region"],
            "address": snapshot["organization_address"],
            "role_code": snapshot["role_code"],
            "role_name": snapshot["role_name"],
            "route": row["route"],
            "available": row["available"],
            "unavailable_reason": row["unavailable_reason"],
        }

    @classmethod
    def triage_options(cls, *, application_id, user):
        application = (
            AfterSalesApplication.objects.select_related("device")
            .filter(pk=application_id)
            .first()
        )
        if not application:
            raise AfterSalesError("售后申请不存在")
        cls._require_factory_sales(application, user)
        if application.status != AfterSalesApplication.Status.SUBMITTED:
            raise AfterSalesError("该售后申请当前不能重新判断或派单")
        return {
            "routes": cls._option_rows(AfterSalesApplication.DispatchRoute.choices),
            "candidates": [
                cls._public_candidate(row) for row in cls._candidate_rows(application)
            ],
            "preliminary_fault_types": cls._option_rows(
                AfterSalesApplication.FaultType.choices
            ),
            "final_fault_types": cls._option_rows(
                AfterSalesApplication.FaultType.choices
            ),
            "solution_types": cls._option_rows(
                AfterSalesApplication.SolutionType.choices
            ),
            "final_material_options": cls._material_options(application, key_only=True),
        }

    @classmethod
    def _media_purpose_allowed(cls, application, user, purpose):
        if (
            purpose == AfterSalesMedia.Purpose.APPLICATION_EVIDENCE
            and cls._is_applicant(application, user)
            and application.status != AfterSalesApplication.Status.CLOSED
        ):
            return True
        if (
            purpose == AfterSalesMedia.Purpose.APPLICANT_SHIPMENT_DOCUMENT
            and cls._is_applicant(application, user)
            and application.execution_stage
            == AfterSalesApplication.ExecutionStage.AWAITING_APPLICANT_SHIPMENT
        ):
            return True
        if (
            purpose == AfterSalesMedia.Purpose.REPAIR_EVIDENCE
            and cls._is_assignee(application, user)
            and application.execution_stage
            in {
                AfterSalesApplication.ExecutionStage.CONTACT_INSTALLER,
                AfterSalesApplication.ExecutionStage.REPAIRING,
            }
        ):
            return True
        if (
            purpose
            == AfterSalesMedia.Purpose.REPAIR_RETURN_SHIPMENT_DOCUMENT
            and cls._is_assignee(application, user)
            and application.execution_stage
            == AfterSalesApplication.ExecutionStage.AWAITING_RETURN_SHIPMENT
        ):
            return True
        if (
            purpose
            in {
                AfterSalesMedia.Purpose.THIRD_PARTY_MATERIAL_SHIPMENT_DOCUMENT,
                AfterSalesMedia.Purpose.THIRD_PARTY_FEE_VOUCHER,
                AfterSalesMedia.Purpose.THIRD_PARTY_FAULT_MATERIAL_RETURN_DOCUMENT,
            }
            and cls._is_factory_sales(application, user)
            and application.dispatch_route
            == AfterSalesApplication.DispatchRoute.THIRD_PARTY
            and application.execution_stage
            == AfterSalesApplication.ExecutionStage.THIRD_PARTY_REPAIR
        ):
            return True
        return False

    @classmethod
    def _validate_uploaded_file(cls, *, uploaded_file, media_type):
        if not uploaded_file:
            raise AfterSalesError("请选择需要上传的附件")
        suffix = Path(uploaded_file.name or "").suffix.lower()
        content_type = str(getattr(uploaded_file, "content_type", "") or "").lower()
        if media_type == AfterSalesMedia.MediaType.IMAGE:
            if uploaded_file.size > cls.IMAGE_MAX_BYTES:
                raise AfterSalesError("单张图片不能超过20MB")
            if suffix and suffix not in cls.IMAGE_SUFFIXES:
                raise AfterSalesError("图片格式仅支持 JPG、PNG、WEBP 或 HEIC")
            if content_type and not content_type.startswith("image/"):
                raise AfterSalesError("上传文件不是有效图片")
            return
        if media_type == AfterSalesMedia.MediaType.VIDEO:
            if uploaded_file.size > cls.VIDEO_MAX_BYTES:
                raise AfterSalesError("单个视频不能超过200MB")
            if suffix and suffix not in cls.VIDEO_SUFFIXES:
                raise AfterSalesError("视频格式仅支持 MP4、MOV、M4V 或 3GP")
            if content_type and not (
                content_type.startswith("video/") or content_type == "application/octet-stream"
            ):
                raise AfterSalesError("上传文件不是有效视频")
            return
        if media_type == AfterSalesMedia.MediaType.DOCUMENT:
            if uploaded_file.size > cls.DOCUMENT_MAX_BYTES:
                raise AfterSalesError("单个单据不能超过30MB")
            if suffix and suffix not in cls.DOCUMENT_SUFFIXES:
                raise AfterSalesError("单据格式仅支持 PDF、图片或视频")
            if content_type and not (
                content_type.startswith("application/pdf")
                or content_type.startswith("image/")
                or content_type.startswith("video/")
                or content_type == "application/octet-stream"
            ):
                raise AfterSalesError("上传文件不是有效单据")
            return
        raise AfterSalesError("附件类型必须是图片、视频或单据")

    @classmethod
    @transaction.atomic
    def upload_media(
        cls,
        *,
        application_id,
        user,
        uploaded_file,
        media_type,
        purpose=None,
        duration_seconds=None,
        caption="",
    ):
        application = (
            AfterSalesApplication.objects.select_for_update()
            .filter(pk=application_id)
            .first()
        )
        if not application:
            raise AfterSalesError("售后申请不存在")
        media_type = str(media_type or "").upper()
        purpose = str(
            purpose or AfterSalesMedia.Purpose.APPLICATION_EVIDENCE
        ).upper()
        valid_types = {value for value, _ in AfterSalesMedia.MediaType.choices}
        valid_purposes = {value for value, _ in AfterSalesMedia.Purpose.choices}
        if media_type not in valid_types:
            raise AfterSalesError("附件类型必须是图片、视频或单据")
        if purpose not in valid_purposes:
            raise AfterSalesError("附件用途不正确")
        if not cls._media_purpose_allowed(application, user, purpose):
            raise AfterSalesError("当前身份或处理阶段不能上传该类附件")
        max_count = (
            cls.APPLICATION_MEDIA_LIMIT
            if purpose == AfterSalesMedia.Purpose.APPLICATION_EVIDENCE
            else cls.OTHER_MEDIA_LIMIT
        )
        if application.media.filter(purpose=purpose).count() >= max_count:
            raise AfterSalesError(f"该类附件最多上传{max_count}个")
        cls._validate_uploaded_file(uploaded_file=uploaded_file, media_type=media_type)
        try:
            duration_seconds = int(duration_seconds) if duration_seconds else None
        except (TypeError, ValueError):
            duration_seconds = None
        caption = str(caption or "").strip()
        if len(caption) > 255:
            raise AfterSalesError("附件说明不能超过255个字")
        media = AfterSalesMedia.objects.create(
            application=application,
            media_type=media_type,
            purpose=purpose,
            file=uploaded_file,
            original_name=uploaded_file.name,
            caption=caption,
            content_type=getattr(uploaded_file, "content_type", ""),
            file_size=uploaded_file.size,
            duration_seconds=duration_seconds,
            sequence=application.media.count(),
            uploaded_by=user,
        )
        cls._event(
            application,
            "MEDIA_UPLOADED",
            user=user,
            payload={
                "media_id": media.id,
                "media_type": media_type,
                "purpose": purpose,
                "caption": caption,
            },
        )
        return cls.serialize_media(media)

    @classmethod
    def _factory_sales_openids(cls, application):
        return list(
            UserOrganization.objects.filter(
                organization_id=application.factory_organization_id,
                role__code="factory_sales",
                is_active=True,
                user__is_active=True,
                role__is_active=True,
            )
            .exclude(user__wx_openid="")
            .values_list("user__wx_openid", flat=True)
        )

    @staticmethod
    def _link(application):
        return f"/pages/aftersale/detail/detail?application_id={application.id}"

    @classmethod
    def _summary(cls, application):
        device = application.device_snapshot or {}
        return f"{device.get('product_model', '')}｜{device.get('sn', '')}"

    @classmethod
    def _task_key(cls, application, step):
        return f"aftersales:{application.id}:{step}:v{application.dispatch_version}"

    @classmethod
    def _applicant_workorder_key(cls, application):
        """One stable inbox workorder for the applicant and one case.

        Dispatch tasks intentionally keep a versioned key because they are
        separate operational responsibilities.  The applicant, however,
        should see a single service order whose latest status is pushed back
        onto the same UserTask.
        """

        return f"aftersales:{application.id}:{cls.APPLICANT_WORKORDER_STEP}"

    @classmethod
    def _task_payload(cls, application, *, stage="", extra=None):
        payload = {
            "application_id": application.id,
            "case_no": application.case_no,
            "route": application.dispatch_route,
            "stage": stage or application.execution_stage,
            "dispatch_version": application.dispatch_version,
            "inbox_group_key": f"aftersales:{application.case_no}",
            "inbox_group_kind": "AFTERSALES",
            "inbox_group_label": "售后工单",
        }
        if extra:
            payload.update(extra)
        return payload

    @classmethod
    def _publish_task(
        cls,
        application,
        *,
        step,
        task_type,
        title,
        summary,
        recipients_openids,
        actor_openid="",
        action_mode="EACH_ACK",
        need_action=True,
        mark_workitem_done_immediately=False,
        payload=None,
    ):
        return publish_workitem(
            domain="AFTERSALE",
            type=task_type,
            biz_id=str(application.id),
            biz_ref=f"AfterSalesApplication:{application.id}",
            uniq_key=cls._task_key(application, step),
            title=title,
            summary=summary,
            link=cls._link(application),
            recipients_openids=recipients_openids,
            need_action=need_action,
            action_mode=action_mode,
            payload=cls._task_payload(application, extra=payload),
            actor_openid=actor_openid,
            mark_workitem_done_immediately=mark_workitem_done_immediately,
        )

    @classmethod
    def _adopt_legacy_applicant_workorder(cls, application, applicant_openid):
        """Reuse the original applicant task for already-deployed cases.

        Historical rows are not deleted or merged.  When a previously created
        case receives its first update after this release, its earliest
        applicant-facing WorkItem is simply given the stable workorder key.
        This preserves that WorkItem/UserTask primary key while later pushes
        update it in place.  Other historical rows remain available through
        the existing grouped inbox and the immutable AfterSalesEvent history.
        """

        workorder_key = cls._applicant_workorder_key(application)
        if WorkItem.objects.select_for_update().filter(uniq_key=workorder_key).exists():
            return
        if not applicant_openid:
            return
        legacy = (
            WorkItem.objects.select_for_update()
            .filter(
                domain="AFTERSALE",
                biz_ref=f"AfterSalesApplication:{application.id}",
                type__in=cls.LEGACY_APPLICANT_WORKITEM_TYPES,
                user_tasks__to_openid=applicant_openid,
            )
            .order_by("created_at", "id")
            .first()
        )
        if not legacy:
            return
        legacy.uniq_key = workorder_key
        legacy.save(update_fields=("uniq_key", "updated_at"))

    @classmethod
    def _publish_applicant_workorder(
        cls,
        application,
        *,
        topic,
        title,
        summary,
        actor_openid="",
        need_action=False,
        payload=None,
    ):
        """Push the latest applicant-facing progress onto one inbox task."""

        openid = application.applicant.wx_openid if application.applicant_id else ""
        if not openid:
            return None
        cls._adopt_legacy_applicant_workorder(application, openid)
        extra = {
            "applicant_workorder": True,
            "applicant_update_topic": topic,
        }
        if payload:
            extra.update(payload)
        return publish_workitem(
            domain="AFTERSALE",
            type=cls.APPLICANT_WORKORDER_TYPE,
            biz_id=str(application.id),
            biz_ref=f"AfterSalesApplication:{application.id}",
            uniq_key=cls._applicant_workorder_key(application),
            title=title,
            summary=summary,
            link=cls._link(application),
            recipients_openids=[openid],
            need_action=need_action,
            action_mode="EACH_ACK",
            payload=cls._task_payload(application, extra=extra),
            actor_openid=actor_openid,
            # A pure status update remains visible as unread but does not keep
            # an operational WorkItem open.  A required applicant action does.
            mark_workitem_done_immediately=not need_action,
        )

    @classmethod
    def _publish_applicant_notice(cls, application, *, topic, title, summary, actor_openid=""):
        return cls._publish_applicant_workorder(
            application,
            topic=topic,
            title=title,
            summary=summary,
            actor_openid=actor_openid,
        )

    @classmethod
    def _publish_factory_sales_notice(cls, application, *, topic, title, summary, actor_openid=""):
        return cls._publish_factory_workorder(
            application,
            topic=topic,
            title=title,
            summary=summary,
            actor_openid=actor_openid,
            need_action=False,
        )

    @classmethod
    def _publish_factory_workorder(
        cls, application, *, topic, title, summary, actor_openid="", need_action=False,
    ):
        return publish_workitem(
            domain="AFTERSALE",
            type=cls.FACTORY_WORKORDER_TYPE,
            biz_id=str(application.id),
            biz_ref=f"AfterSalesApplication:{application.id}",
            uniq_key=f"aftersales:{application.id}:{cls.FACTORY_WORKORDER_STEP}",
            title=title,
            summary=summary,
            link=cls._link(application),
            recipients_openids=cls._factory_sales_openids(application),
            need_action=need_action,
            action_mode="ANYONE_DO",
            payload=cls._task_payload(
                application,
                extra={"factory_workorder": True, "factory_update_topic": topic},
            ),
            actor_openid=actor_openid,
            mark_workitem_done_immediately=not need_action,
        )

    @classmethod
    def _publish_assignee_workorder(
        cls, application, *, topic, title, summary, actor_openid="", need_action=False,
    ):
        if not application.assigned_user_id or not application.assigned_user.wx_openid:
            return None
        return publish_workitem(
            domain="AFTERSALE",
            type=cls.ASSIGNEE_WORKORDER_TYPE,
            biz_id=str(application.id),
            biz_ref=f"AfterSalesApplication:{application.id}",
            uniq_key=(
                f"aftersales:{application.id}:{cls.ASSIGNEE_WORKORDER_STEP}:"
                f"{application.assigned_user_id}"
            ),
            title=title,
            summary=summary,
            link=cls._link(application),
            recipients_openids=[application.assigned_user.wx_openid],
            need_action=need_action,
            action_mode="EACH_ACK",
            payload=cls._task_payload(
                application,
                extra={"assignee_workorder": True, "assignee_update_topic": topic},
            ),
            actor_openid=actor_openid,
            mark_workitem_done_immediately=not need_action,
        )

    @classmethod
    def _complete_workitems(cls, *, keys, actor_openid=""):
        """Complete specific operational work items in the same transaction."""

        now = timezone.now()
        for workitem in WorkItem.objects.select_for_update().filter(
            uniq_key__in=keys, status="OPEN"
        ):
            tasks = UserTask.objects.select_for_update().filter(
                workitem=workitem, state__in=("NEW", "READ")
            )
            if actor_openid:
                tasks.filter(to_openid=actor_openid).update(
                    state="DONE",
                    done_at=now,
                    done_reason="SELF_ACTION",
                    cursor_time=now,
                )
                tasks.exclude(to_openid=actor_openid).update(
                    state="DONE",
                    done_at=now,
                    done_reason="OTHER_ACTION",
                    cursor_time=now,
                )
            else:
                tasks.update(
                    state="DONE",
                    done_at=now,
                    done_reason="OTHER_ACTION",
                    cursor_time=now,
                )
            workitem.status = "DONE"
            workitem.done_at = now
            workitem.done_by_openid = actor_openid
            workitem.last_activity_at = now
            workitem.save(
                update_fields=(
                    "status",
                    "done_at",
                    "done_by_openid",
                    "last_activity_at",
                    "updated_at",
                )
                )

    @classmethod
    def _complete_tasks(cls, application, *, steps, actor_openid=""):
        """Complete versioned operational tasks, retaining legacy review support."""

        keys = [cls._task_key(application, step) for step in steps]
        # The pre-recovery submit service used this one legacy task key.  Keep
        # it completable so the existing submitted AS record remains tidy.
        if "review" in steps and application.dispatch_version == 0:
            keys.append(f"aftersales:{application.id}:factory-sales")
        cls._complete_workitems(keys=keys, actor_openid=actor_openid)

    @classmethod
    def _complete_applicant_workorder(cls, application, *, actor_openid=""):
        """Finish the current applicant action without creating a replacement."""

        keys = [
            cls._applicant_workorder_key(application),
            # A case can be paused at an applicant action when this release is
            # installed.  Completing its old versioned task prevents a stale
            # action card from surviving the transition.
            cls._task_key(application, "applicant-shipment"),
            cls._task_key(application, "applicant-confirm"),
        ]
        cls._complete_workitems(keys=keys, actor_openid=actor_openid)

    @classmethod
    def _complete_factory_workorder(cls, application, *, actor_openid=""):
        cls._complete_workitems(
            keys=[f"aftersales:{application.id}:{cls.FACTORY_WORKORDER_STEP}"],
            actor_openid=actor_openid,
        )

    @classmethod
    def _complete_assignee_workorder(cls, application, *, actor_openid=""):
        if not application.assigned_user_id:
            return
        cls._complete_workitems(
            keys=[
                f"aftersales:{application.id}:{cls.ASSIGNEE_WORKORDER_STEP}:"
                f"{application.assigned_user_id}"
            ],
            actor_openid=actor_openid,
        )

    @classmethod
    @transaction.atomic
    def submit(cls, *, application_id, user):
        identity = CustomerDeviceService._customer_identity(user)
        application = (
            AfterSalesApplication.objects.select_for_update()
            .select_related("applicant", "factory_organization")
            .filter(pk=application_id)
            .first()
        )
        if (
            not application
            or application.applicant_id != user.id
            or application.status != AfterSalesApplication.Status.DRAFT
        ):
            raise AfterSalesError("当前售后申请不能提交")
        if not application.fault_description:
            raise AfterSalesError("请填写故障描述")
        cls._validate_location(application)
        if not application.media.filter(
            purpose=AfterSalesMedia.Purpose.APPLICATION_EVIDENCE,
            media_type__in=(
                AfterSalesMedia.MediaType.IMAGE,
                AfterSalesMedia.MediaType.VIDEO,
            ),
        ).exists():
            raise AfterSalesError("请至少上传一张图片或一段视频")
        recipients = cls._factory_sales_openids(application)
        if not recipients:
            raise AfterSalesError("厂家尚未配置销售经理，暂不能提交")
        application.status = AfterSalesApplication.Status.SUBMITTED
        application.execution_stage = AfterSalesApplication.ExecutionStage.NONE
        application.submitted_at = timezone.now()
        application.save(
            update_fields=(
                "status",
                "execution_stage",
                "submitted_at",
                "updated_at",
            )
        )
        cls._publish_factory_workorder(
            application,
            topic="review-required",
            title="收到新的售后申请，待判断并分派",
            summary=cls._summary(application),
            actor_openid=user.wx_openid or "",
            need_action=True,
        )
        cls._publish_applicant_workorder(
            application,
            topic="submitted",
            title="售后申请已提交",
            summary=f"{cls._summary(application)}｜等待厂家销售经理判断",
            actor_openid=user.wx_openid or "",
        )
        cls._event(application, "SUBMITTED", user=user, identity=identity)
        return cls.serialize(application)

    @classmethod
    def _valid_choice(cls, value, choices, message):
        valid_values = {item[0] for item in choices}
        value = str(value or "").strip().upper()
        if value not in valid_values:
            raise AfterSalesError(message)
        return value

    @classmethod
    def _material_options(cls, application, *, key_only):
        items = list((application.bom_snapshot or {}).get("items") or [])
        if key_only:
            items = [item for item in items if item.get("is_key_part")]
        return [
            {
                "bom_item_id": item.get("bom_item_id"),
                "material_id": item.get("material_id"),
                "material_name": item.get("material_name") or item.get("material") or "",
                "specification_id": item.get("specification_id"),
                "specification_name": item.get("specification_name") or "",
                "supplier_id": item.get("supplier_id"),
                "supplier_name": item.get("supplier_name") or "",
                "is_key_part": bool(item.get("is_key_part")),
                "quantity": item.get("quantity"),
            }
            for item in items
        ]

    @classmethod
    def _normalize_materials(cls, application, raw_materials, *, key_only):
        if raw_materials in (None, ""):
            raw_materials = []
        if isinstance(raw_materials, str):
            raw_materials = [item.strip() for item in raw_materials.split(",") if item.strip()]
        if not isinstance(raw_materials, list):
            raise AfterSalesError("涉及物料必须为列表")
        allowed = cls._material_options(application, key_only=key_only)
        by_bom_item = {
            str(item["bom_item_id"]): item
            for item in allowed
            if item.get("bom_item_id") not in (None, "")
        }
        by_material = {
            str(item["material_id"]): item
            for item in allowed
            if item.get("material_id") not in (None, "")
        }
        selected = []
        seen = set()
        for raw in raw_materials:
            if isinstance(raw, dict):
                bom_item_id = raw.get("bom_item_id")
                material_id = raw.get("material_id")
            else:
                bom_item_id = raw
                material_id = raw
            item = (
                by_bom_item.get(str(bom_item_id))
                if bom_item_id not in (None, "")
                else None
            ) or (
                by_material.get(str(material_id))
                if material_id not in (None, "")
                else None
            )
            if not item:
                raise AfterSalesError("所选物料不在该设备提交时冻结的BOM范围内")
            key = str(item.get("bom_item_id") or item.get("material_id"))
            if key not in seen:
                selected.append(item)
                seen.add(key)
        return selected

    @classmethod
    def _validate_final_data(cls, application, data):
        fault_type = cls._valid_choice(
            data.get("final_fault_type"),
            AfterSalesApplication.FaultType.choices,
            "请选择最终故障类型",
        )
        materials = cls._normalize_materials(
            application,
            data.get("final_fault_materials"),
            key_only=True,
        )
        if fault_type == cls.FINAL_COMPONENT_FAULT and not materials:
            raise AfterSalesError("疑似关键配套物料故障时，至少选择一项关键BOM物料")
        solution_type = cls._valid_choice(
            data.get("final_solution_type"),
            AfterSalesApplication.SolutionType.choices,
            "请选择最终解决方案类型",
        )
        solution_note = str(data.get("final_solution_note") or "").strip()
        if not solution_note:
            raise AfterSalesError("请填写最终解决方案说明")
        return fault_type, materials, solution_type, solution_note

    @classmethod
    def _third_party_snapshot(cls, data):
        name = str(data.get("third_party_name") or "").strip()
        contact_name = str(data.get("third_party_contact_name") or "").strip()
        phone = str(data.get("third_party_phone") or "").strip()
        address = str(data.get("third_party_address") or "").strip()
        if not name or not phone or not address:
            raise AfterSalesError("第三方维修路径需填写维修店名称、联系电话和详细地址")
        return {
            "name": name,
            "contact_name": contact_name,
            "phone": phone,
            "address": address,
            "assigned_at": timezone.now().isoformat(),
        }

    @classmethod
    def _applicant_instruction(cls, application):
        snapshot = application.assignee_snapshot or {}
        route = application.dispatch_route
        if route == AfterSalesApplication.DispatchRoute.ORIGINAL_INSTALLER:
            return {
                "type": "CONTACT_INSTALLER",
                "title": "请联系原装车操作人员处理",
                "message": f"请联系原装车操作人员 {snapshot.get('name') or ''} 处理。",
                "contact": snapshot,
                "repair_plan": application.repair_plan,
            }
        if route in {
            AfterSalesApplication.DispatchRoute.MERCHANT_ELECTRICIAN,
            AfterSalesApplication.DispatchRoute.FACTORY_AFTERSALES,
        }:
            return {
                "type": "SHIP_TO_REPAIRER",
                "title": "请按以下地址寄送设备维修",
                "message": (
                    f"请将设备寄送至 {snapshot.get('organization_name') or ''}"
                    f"（{snapshot.get('name') or ''}）"
                ),
                "contact": snapshot,
                "repair_plan": application.repair_plan,
            }
        if route == AfterSalesApplication.DispatchRoute.REMOTE:
            return {
                "type": "REMOTE_GUIDANCE",
                "title": "厂家远程排障方案",
                "message": application.repair_plan,
                "repair_plan": application.repair_plan,
            }
        if route == AfterSalesApplication.DispatchRoute.THIRD_PARTY:
            return {
                "type": "THIRD_PARTY_REPAIR",
                "title": "厂家已安排第三方紧急维修",
                "message": application.repair_plan,
                "contact": application.third_party_snapshot or {},
                "repair_plan": application.repair_plan,
            }
        return {}

    @classmethod
    @transaction.atomic
    def triage(cls, *, application_id, user, data):
        application = (
            AfterSalesApplication.objects.select_for_update()
            .select_related("device", "applicant", "factory_organization")
            .filter(pk=application_id)
            .first()
        )
        if not application:
            raise AfterSalesError("售后申请不存在")
        identity = cls._require_factory_sales(application, user)
        if application.status != AfterSalesApplication.Status.SUBMITTED:
            raise AfterSalesError("该售后申请已经完成判断或派单")

        preliminary_fault_type = cls._valid_choice(
            data.get("preliminary_fault_type") or data.get("fault_type"),
            AfterSalesApplication.FaultType.choices,
            "请选择初步故障认定",
        )
        diagnosis_note = str(
            data.get("diagnosis_note") or data.get("judgment") or ""
        ).strip()
        repair_plan = str(data.get("repair_plan") or "").strip()
        if not diagnosis_note or not repair_plan:
            raise AfterSalesError("故障认定依据和维修方案均为必填")
        route = cls._valid_choice(
            data.get("dispatch_route"),
            AfterSalesApplication.DispatchRoute.choices,
            "请选择有效的售后处理路径",
        )

        assigned_row = None
        if route in {
            AfterSalesApplication.DispatchRoute.ORIGINAL_INSTALLER,
            AfterSalesApplication.DispatchRoute.MERCHANT_ELECTRICIAN,
            AfterSalesApplication.DispatchRoute.FACTORY_AFTERSALES,
        }:
            requested_id = str(data.get("assigned_user_id") or "")
            for row in cls._candidate_rows(application):
                if row["route"] == route and str(row["user"].id) == requested_id:
                    assigned_row = row
                    break
            if not assigned_row:
                raise AfterSalesError("请选择该处理路径下的有效维修执行人")
            if not assigned_row["available"]:
                raise AfterSalesError(assigned_row["unavailable_reason"])

        application.dispatch_route = route
        application.preliminary_fault_type = preliminary_fault_type
        application.diagnosis_note = diagnosis_note
        application.repair_plan = repair_plan
        application.triaged_by = user
        application.triaged_at = timezone.now()
        application.assigned_user = assigned_row["user"] if assigned_row else None
        application.assigned_organization = (
            assigned_row["organization"] if assigned_row else None
        )
        application.assignee_snapshot = (
            assigned_row["snapshot"] if assigned_row else {}
        )
        application.third_party_snapshot = (
            cls._third_party_snapshot(data)
            if route == AfterSalesApplication.DispatchRoute.THIRD_PARTY
            else {}
        )
        cls._complete_tasks(
            application,
            steps=["review"],
            actor_openid=user.wx_openid or "",
        )
        cls._complete_factory_workorder(
            application,
            actor_openid=user.wx_openid or "",
        )
        application.dispatch_version += 1
        cls._event(
            application,
            "TRIAGED",
            user=user,
            identity=identity,
            payload={
                "preliminary_fault_type": preliminary_fault_type,
                "diagnosis_note": diagnosis_note,
                "repair_plan": repair_plan,
                "dispatch_route": route,
                "assigned_user_id": application.assigned_user_id,
                "assigned_organization_id": application.assigned_organization_id,
                "dispatch_version": application.dispatch_version,
                "third_party": application.third_party_snapshot,
            },
        )

        if route == AfterSalesApplication.DispatchRoute.REMOTE:
            application.status = AfterSalesApplication.Status.SUBMITTED
            application.execution_stage = AfterSalesApplication.ExecutionStage.NONE
            cls._close_application(
                application,
                user=user,
                identity=identity,
                data=data,
                event_type="REMOTE_RESOLVED_AND_CLOSED",
            )
            return cls.serialize(application)

        if route == AfterSalesApplication.DispatchRoute.ORIGINAL_INSTALLER:
            application.status = AfterSalesApplication.Status.PROCESSING
            application.execution_stage = (
                AfterSalesApplication.ExecutionStage.CONTACT_INSTALLER
            )
            application.save()
            cls._publish_assignee_workorder(
                application,
                topic="repair-required",
                title="售后维修任务待处理",
                summary=f"{cls._summary(application)}｜{repair_plan}",
                actor_openid=user.wx_openid or "",
                need_action=True,
            )
            cls._publish_applicant_notice(
                application,
                topic="installer-assigned",
                title="厂家已指派原装车操作人员处理",
                summary=f"{cls._summary(application)}｜请联系 {application.assignee_snapshot.get('name') or ''}",
                actor_openid=user.wx_openid or "",
            )
            cls._event(
                application,
                "DISPATCHED",
                user=user,
                identity=identity,
                payload={"next_stage": application.execution_stage},
            )
            return cls.serialize(application)

        if route in {
            AfterSalesApplication.DispatchRoute.MERCHANT_ELECTRICIAN,
            AfterSalesApplication.DispatchRoute.FACTORY_AFTERSALES,
        }:
            application.status = AfterSalesApplication.Status.ROUTING
            application.execution_stage = (
                AfterSalesApplication.ExecutionStage.AWAITING_APPLICANT_SHIPMENT
            )
            application.save()
            cls._publish_applicant_workorder(
                application,
                topic="applicant-shipment-required",
                title="请寄送设备至指定维修人",
                summary=(
                    f"{cls._summary(application)}｜寄送至 "
                    f"{application.assignee_snapshot.get('organization_name') or ''}，"
                    "请按服务单地址上传物流单据后确认寄送"
                ),
                actor_openid=user.wx_openid or "",
                need_action=True,
            )
            cls._publish_assignee_workorder(
                application,
                topic="awaiting-applicant-shipment",
                title="已接受售后指派，等待发起人寄送设备",
                summary=f"{cls._summary(application)}｜发起人尚未确认寄送",
                actor_openid=user.wx_openid or "",
                need_action=False,
            )
            cls._event(
                application,
                "DISPATCHED",
                user=user,
                identity=identity,
                payload={"next_stage": application.execution_stage},
            )
            return cls.serialize(application)

        application.status = AfterSalesApplication.Status.PROCESSING
        application.execution_stage = (
            AfterSalesApplication.ExecutionStage.THIRD_PARTY_REPAIR
        )
        application.save()
        recipients = cls._factory_sales_openids(application)
        cls._publish_factory_workorder(
            application,
            topic="third-party-material-shipment-required",
            title="第三方紧急维修：登记备件寄送",
            summary=cls._summary(application),
            actor_openid=user.wx_openid or "",
            need_action=True,
        )
        cls._publish_applicant_notice(
            application,
            topic="third-party-assigned",
            title="厂家已安排第三方紧急维修",
            summary=f"{cls._summary(application)}｜请留意厂家后续处理通知",
            actor_openid=user.wx_openid or "",
        )
        cls._event(
            application,
            "DISPATCHED",
            user=user,
            identity=identity,
            payload={"next_stage": application.execution_stage},
        )
        return cls.serialize(application)

    @classmethod
    def _require_media(cls, application, purpose, message):
        if not application.media.filter(purpose=purpose).exists():
            raise AfterSalesError(message)

    @staticmethod
    def _has_event(application, event_type):
        return AfterSalesEvent.objects.filter(
            application_id=application.id,
            event_type=event_type,
        ).exists()

    @classmethod
    def _logistics_payload(cls, data, *, prefix=""):
        company = str(
            data.get(f"{prefix}logistics_company") or data.get("logistics_company") or ""
        ).strip()
        tracking_no = str(
            data.get(f"{prefix}tracking_no") or data.get("tracking_no") or ""
        ).strip()
        remark = str(
            data.get(f"{prefix}remark") or data.get("remark") or ""
        ).strip()
        if not company or not tracking_no:
            raise AfterSalesError("请填写物流公司和物流单号")
        return {
            "logistics_company": company,
            "tracking_no": tracking_no,
            "remark": remark,
        }

    @classmethod
    @transaction.atomic
    def applicant_shipment(cls, *, application_id, user, data):
        application = (
            AfterSalesApplication.objects.select_for_update()
            .select_related("applicant", "assigned_user")
            .filter(pk=application_id)
            .first()
        )
        if not application:
            raise AfterSalesError("售后申请不存在")
        identity = cls._require_applicant(application, user)
        if application.execution_stage != (
            AfterSalesApplication.ExecutionStage.AWAITING_APPLICANT_SHIPMENT
        ):
            raise AfterSalesError("当前售后申请不在待寄送设备阶段")
        cls._require_media(
            application,
            AfterSalesMedia.Purpose.APPLICANT_SHIPMENT_DOCUMENT,
            "请先上传寄送物流单据",
        )
        logistics = cls._logistics_payload(data)
        application.execution_stage = (
            AfterSalesApplication.ExecutionStage.AWAITING_REPAIR_RECEIPT
        )
        application.save(update_fields=("execution_stage", "updated_at"))
        cls._complete_applicant_workorder(
            application,
            actor_openid=user.wx_openid or "",
        )
        cls._event(
            application,
            "APPLICANT_SHIPMENT_CONFIRMED",
            user=user,
            identity=identity,
            payload=logistics,
        )
        cls._publish_assignee_workorder(
            application,
            topic="repair-receipt-required",
            title="售后寄修设备待确认收件",
            summary=f"{cls._summary(application)}｜{logistics['logistics_company']} {logistics['tracking_no']}",
            actor_openid=user.wx_openid or "",
            need_action=True,
        )
        return cls.serialize(application)

    @classmethod
    @transaction.atomic
    def repair_receipt(cls, *, application_id, user, data):
        application = (
            AfterSalesApplication.objects.select_for_update()
            .select_related("assigned_organization")
            .filter(pk=application_id)
            .first()
        )
        if not application:
            raise AfterSalesError("售后申请不存在")
        identity = cls._require_assignee(application, user)
        if application.execution_stage != (
            AfterSalesApplication.ExecutionStage.AWAITING_REPAIR_RECEIPT
        ):
            raise AfterSalesError("当前售后申请不在待确认收件阶段")
        receipt_note = str(data.get("receipt_note") or "").strip()
        application.status = AfterSalesApplication.Status.PROCESSING
        application.execution_stage = AfterSalesApplication.ExecutionStage.REPAIRING
        application.save(update_fields=("status", "execution_stage", "updated_at"))
        cls._complete_tasks(
            application,
            steps=["repair-receipt"],
            actor_openid=user.wx_openid or "",
        )
        cls._event(
            application,
            "REPAIRER_RECEIPT_CONFIRMED",
            user=user,
            identity=identity,
            payload={"receipt_note": receipt_note},
        )
        cls._publish_assignee_workorder(
            application,
            topic="repair-required",
            title="售后维修任务待处理",
            summary=cls._summary(application),
            actor_openid=user.wx_openid or "",
            need_action=True,
        )
        cls._publish_applicant_notice(
            application,
            topic="repairer-received",
            title="维修人已确认收到寄修设备",
            summary=cls._summary(application),
            actor_openid=user.wx_openid or "",
        )
        return cls.serialize(application)

    @classmethod
    def _parse_actual_time(cls, value):
        if not value:
            return timezone.now(), ""
        text = str(value).strip()
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
        return parsed, text

    @classmethod
    @transaction.atomic
    def repair_complete(cls, *, application_id, user, data):
        application = (
            AfterSalesApplication.objects.select_for_update()
            .select_related("device", "assigned_organization", "applicant")
            .filter(pk=application_id)
            .first()
        )
        if not application:
            raise AfterSalesError("售后申请不存在")
        identity = cls._require_assignee(application, user)
        if application.execution_stage not in {
            AfterSalesApplication.ExecutionStage.CONTACT_INSTALLER,
            AfterSalesApplication.ExecutionStage.REPAIRING,
        }:
            raise AfterSalesError("当前售后申请不在可确认维修完成阶段")
        repair_summary = str(data.get("repair_summary") or "").strip()
        if not repair_summary:
            raise AfterSalesError("请填写处理说明")
        if "repair_material_replaced" not in data:
            raise AfterSalesError("请确认是否更换物料")
        material_replaced = _as_bool(data.get("repair_material_replaced"))
        repair_materials = cls._normalize_materials(
            application,
            data.get("repair_materials"),
            key_only=False,
        )
        if material_replaced and not repair_materials:
            raise AfterSalesError("已更换物料时，请选择实际更换的BOM物料")
        if not material_replaced:
            repair_materials = []
        cls._require_media(
            application,
            AfterSalesMedia.Purpose.REPAIR_EVIDENCE,
            "请先上传维修前后图片或测试视频",
        )
        try:
            actual_time, actual_time_text = cls._parse_actual_time(
                data.get("actual_processed_at")
            )
        except (TypeError, ValueError) as exc:
            raise AfterSalesError("实际处理时间格式不正确") from exc
        application.repair_summary = repair_summary
        application.repair_material_replaced = material_replaced
        application.repair_materials = repair_materials
        application.repair_completed_by = user
        application.repair_completed_at = actual_time
        if application.dispatch_route == (
            AfterSalesApplication.DispatchRoute.ORIGINAL_INSTALLER
        ):
            application.status = AfterSalesApplication.Status.WAITING_CUSTOMER_CONFIRM
            application.execution_stage = (
                AfterSalesApplication.ExecutionStage.AWAITING_APPLICANT_CONFIRM
            )
            application.applicant_confirm_due_at = timezone.now() + timedelta(days=7)
            next_step = "applicant-confirm"
        else:
            application.status = AfterSalesApplication.Status.PROCESSING
            application.execution_stage = (
                AfterSalesApplication.ExecutionStage.AWAITING_RETURN_SHIPMENT
            )
            next_step = "repair-return-shipment"
        application.save()
        cls._complete_tasks(
            application,
            steps=["repair-execution"],
            actor_openid=user.wx_openid or "",
        )
        cls._event(
            application,
            "REPAIR_COMPLETED",
            user=user,
            identity=identity,
            payload={
                "repair_summary": repair_summary,
                "repair_material_replaced": material_replaced,
                "repair_materials": repair_materials,
                "actual_processed_at": actual_time_text or actual_time.isoformat(),
            },
        )
        LifecycleEvent.objects.create(
            device=application.device,
            event_type="REPAIR",
            operator=user,
            organization=application.assigned_organization,
            data={
                "application_id": application.id,
                "case_no": application.case_no,
                "route": application.dispatch_route,
                "repair_summary": repair_summary,
                "repair_material_replaced": material_replaced,
                "repair_materials": repair_materials,
            },
        )
        cls._publish_factory_sales_notice(
            application,
            topic="repair-completed",
            title="维修执行人已确认处理完成",
            summary=cls._summary(application),
            actor_openid=user.wx_openid or "",
        )
        if next_step == "applicant-confirm":
            cls._complete_assignee_workorder(
                application,
                actor_openid=user.wx_openid or "",
            )
            cls._publish_applicant_workorder(
                application,
                topic="applicant-confirm-required",
                title="请确认售后处理结果",
                summary=(
                    f"{cls._summary(application)}｜原装车操作人员已确认维修完成，"
                    "请确认处理结果"
                ),
                actor_openid=user.wx_openid or "",
                need_action=True,
            )
        else:
            cls._publish_assignee_workorder(
                application,
                topic="return-shipment-required",
                title="请上传返件物流单据并确认寄回",
                summary=cls._summary(application),
                actor_openid=user.wx_openid or "",
                need_action=True,
            )
        return cls.serialize(application)

    @classmethod
    @transaction.atomic
    def return_shipment(cls, *, application_id, user, data):
        application = (
            AfterSalesApplication.objects.select_for_update()
            .select_related("applicant")
            .filter(pk=application_id)
            .first()
        )
        if not application:
            raise AfterSalesError("售后申请不存在")
        identity = cls._require_assignee(application, user)
        if application.execution_stage != (
            AfterSalesApplication.ExecutionStage.AWAITING_RETURN_SHIPMENT
        ):
            raise AfterSalesError("当前售后申请不在待返件阶段")
        cls._require_media(
            application,
            AfterSalesMedia.Purpose.REPAIR_RETURN_SHIPMENT_DOCUMENT,
            "请先上传返件物流单据",
        )
        logistics = cls._logistics_payload(data)
        application.status = AfterSalesApplication.Status.WAITING_CUSTOMER_CONFIRM
        application.execution_stage = (
            AfterSalesApplication.ExecutionStage.AWAITING_APPLICANT_CONFIRM
        )
        application.applicant_confirm_due_at = timezone.now() + timedelta(days=7)
        application.save()
        cls._complete_assignee_workorder(
            application,
            actor_openid=user.wx_openid or "",
        )
        cls._complete_tasks(
            application,
            steps=["repair-return-shipment"],
            actor_openid=user.wx_openid or "",
        )
        cls._event(
            application,
            "RETURN_SHIPMENT_CONFIRMED",
            user=user,
            identity=identity,
            payload=logistics,
        )
        cls._publish_applicant_workorder(
            application,
            topic="applicant-confirm-required",
            title="维修设备已寄回，请确认售后结果",
            summary=f"{cls._summary(application)}｜{logistics['logistics_company']} {logistics['tracking_no']}",
            actor_openid=user.wx_openid or "",
            need_action=True,
        )
        cls._publish_factory_sales_notice(
            application,
            topic="return-shipped",
            title="维修设备已寄回，待发起人确认",
            summary=f"{cls._summary(application)}｜{logistics['logistics_company']} {logistics['tracking_no']}",
            actor_openid=user.wx_openid or "",
        )
        return cls.serialize(application)

    @classmethod
    def _send_factory_close_task(cls, application, *, actor_openid="", title="售后待最终归因结案"):
        recipients = cls._factory_sales_openids(application)
        cls._publish_factory_workorder(
            application,
            topic="factory-close-required",
            title=title,
            summary=cls._summary(application),
            actor_openid=actor_openid,
            need_action=True,
        )

    @classmethod
    @transaction.atomic
    def applicant_confirm(cls, *, application_id, user, data):
        application = (
            AfterSalesApplication.objects.select_for_update()
            .select_related("applicant")
            .filter(pk=application_id)
            .first()
        )
        if not application:
            raise AfterSalesError("售后申请不存在")
        identity = cls._require_applicant(application, user)
        if application.execution_stage != (
            AfterSalesApplication.ExecutionStage.AWAITING_APPLICANT_CONFIRM
        ):
            raise AfterSalesError("当前售后申请不在待确认结果阶段")
        confirmed = _as_bool(data.get("confirmed", True))
        feedback = str(data.get("feedback") or "").strip()
        application.status = AfterSalesApplication.Status.PENDING_FACTORY_CLOSE
        application.execution_stage = (
            AfterSalesApplication.ExecutionStage.FINAL_DIAGNOSIS
        )
        application.applicant_confirmed_at = timezone.now()
        application.applicant_confirm_mode = (
            AfterSalesApplication.ApplicantConfirmMode.MANUAL
        )
        application.save()
        cls._complete_applicant_workorder(
            application,
            actor_openid=user.wx_openid or "",
        )
        cls._event(
            application,
            "APPLICANT_CONFIRMED" if confirmed else "APPLICANT_FEEDBACK_NOT_RESOLVED",
            user=user,
            identity=identity,
            payload={"confirmed": confirmed, "feedback": feedback},
        )
        cls._send_factory_close_task(
            application,
            actor_openid=user.wx_openid or "",
            title=(
                "售后待最终归因结案"
                if confirmed
                else "用户反馈问题未完全解决，待厂家复核结案"
            ),
        )
        return cls.serialize(application)

    @classmethod
    @transaction.atomic
    def third_party_material_shipment(cls, *, application_id, user, data):
        application = (
            AfterSalesApplication.objects.select_for_update().filter(pk=application_id).first()
        )
        if not application:
            raise AfterSalesError("售后申请不存在")
        identity = cls._require_factory_sales(application, user)
        if (
            application.dispatch_route != AfterSalesApplication.DispatchRoute.THIRD_PARTY
            or application.execution_stage
            != AfterSalesApplication.ExecutionStage.THIRD_PARTY_REPAIR
        ):
            raise AfterSalesError("当前售后申请不在第三方维修备件处理阶段")
        if cls._has_event(application, "THIRD_PARTY_MATERIAL_SHIPMENT_CONFIRMED"):
            raise AfterSalesError("第三方维修备件寄送已经登记")
        cls._require_media(
            application,
            AfterSalesMedia.Purpose.THIRD_PARTY_MATERIAL_SHIPMENT_DOCUMENT,
            "请先上传备件寄送物流单据",
        )
        logistics = cls._logistics_payload(data)
        materials = cls._normalize_materials(
            application, data.get("materials"), key_only=False
        )
        if not materials:
            raise AfterSalesError("请记录寄送给第三方的配套物料")
        cls._complete_tasks(
            application,
            steps=["third-party-material-shipment"],
            actor_openid=user.wx_openid or "",
        )
        cls._event(
            application,
            "THIRD_PARTY_MATERIAL_SHIPMENT_CONFIRMED",
            user=user,
            identity=identity,
            payload={**logistics, "materials": materials},
        )
        cls._publish_factory_workorder(
            application,
            topic="third-party-expense-required",
            title="第三方紧急维修：登记费用凭证",
            summary=cls._summary(application),
            actor_openid=user.wx_openid or "",
            need_action=True,
        )
        cls._publish_applicant_notice(
            application,
            topic="third-party-material-shipped",
            title="厂家已完成第三方维修备件寄送登记",
            summary=cls._summary(application),
            actor_openid=user.wx_openid or "",
        )
        return cls.serialize(application)

    @classmethod
    @transaction.atomic
    def third_party_expense(cls, *, application_id, user, data):
        application = (
            AfterSalesApplication.objects.select_for_update().filter(pk=application_id).first()
        )
        if not application:
            raise AfterSalesError("售后申请不存在")
        identity = cls._require_factory_sales(application, user)
        if (
            application.dispatch_route != AfterSalesApplication.DispatchRoute.THIRD_PARTY
            or application.execution_stage
            != AfterSalesApplication.ExecutionStage.THIRD_PARTY_REPAIR
        ):
            raise AfterSalesError("当前售后申请不在第三方维修费用登记阶段")
        if not cls._has_event(application, "THIRD_PARTY_MATERIAL_SHIPMENT_CONFIRMED"):
            raise AfterSalesError("请先完成第三方维修备件寄送登记")
        if cls._has_event(application, "THIRD_PARTY_EXPENSE_RECORDED"):
            raise AfterSalesError("第三方维修费用已经登记")
        cls._require_media(
            application,
            AfterSalesMedia.Purpose.THIRD_PARTY_FEE_VOUCHER,
            "请先上传第三方维修费用凭证",
        )
        try:
            amount = Decimal(str(data.get("amount") or "")).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise AfterSalesError("请填写有效的第三方维修费用") from exc
        if not amount.is_finite() or amount <= 0:
            raise AfterSalesError("第三方维修费用必须大于0")
        application.third_party_cost_amount = amount
        application.save(update_fields=("third_party_cost_amount", "updated_at"))
        cls._complete_tasks(
            application,
            steps=["third-party-expense"],
            actor_openid=user.wx_openid or "",
        )
        cls._event(
            application,
            "THIRD_PARTY_EXPENSE_RECORDED",
            user=user,
            identity=identity,
            payload={"amount": str(amount), "remark": str(data.get("remark") or "").strip()},
        )
        cls._publish_factory_workorder(
            application,
            topic="third-party-fault-material-return-required",
            title="第三方紧急维修：登记故障物料返还",
            summary=cls._summary(application),
            actor_openid=user.wx_openid or "",
            need_action=True,
        )
        return cls.serialize(application)

    @classmethod
    @transaction.atomic
    def third_party_fault_material_return(cls, *, application_id, user, data):
        application = (
            AfterSalesApplication.objects.select_for_update().filter(pk=application_id).first()
        )
        if not application:
            raise AfterSalesError("售后申请不存在")
        identity = cls._require_factory_sales(application, user)
        if (
            application.dispatch_route != AfterSalesApplication.DispatchRoute.THIRD_PARTY
            or application.execution_stage
            != AfterSalesApplication.ExecutionStage.THIRD_PARTY_REPAIR
        ):
            raise AfterSalesError("当前售后申请不在第三方故障物料返还阶段")
        if not cls._has_event(application, "THIRD_PARTY_EXPENSE_RECORDED"):
            raise AfterSalesError("请先完成第三方维修费用登记")
        if cls._has_event(application, "THIRD_PARTY_FAULT_MATERIAL_RETURN_CONFIRMED"):
            raise AfterSalesError("第三方故障物料返还已经登记")
        cls._require_media(
            application,
            AfterSalesMedia.Purpose.THIRD_PARTY_FAULT_MATERIAL_RETURN_DOCUMENT,
            "请先上传故障物料返还物流单据",
        )
        logistics = cls._logistics_payload(data)
        cls._complete_tasks(
            application,
            steps=["third-party-fault-material-return"],
            actor_openid=user.wx_openid or "",
        )
        application.status = AfterSalesApplication.Status.PENDING_FACTORY_CLOSE
        application.execution_stage = (
            AfterSalesApplication.ExecutionStage.FINAL_DIAGNOSIS
        )
        application.save(update_fields=("status", "execution_stage", "updated_at"))
        cls._event(
            application,
            "THIRD_PARTY_FAULT_MATERIAL_RETURN_CONFIRMED",
            user=user,
            identity=identity,
            payload=logistics,
        )
        cls._send_factory_close_task(
            application,
            actor_openid=user.wx_openid or "",
        )
        cls._publish_applicant_notice(
            application,
            topic="third-party-ready-to-close",
            title="第三方维修资料已完成，厂家将进行最终结案",
            summary=cls._summary(application),
            actor_openid=user.wx_openid or "",
        )
        return cls.serialize(application)

    @classmethod
    def _close_application(
        cls,
        application,
        *,
        user,
        identity,
        data,
        event_type="FACTORY_CLOSED",
    ):
        fault_type, materials, solution_type, solution_note = cls._validate_final_data(
            application, data
        )
        application.final_fault_type = fault_type
        application.final_fault_materials = materials
        application.final_solution_type = solution_type
        application.final_solution_note = solution_note
        application.status = AfterSalesApplication.Status.CLOSED
        application.execution_stage = AfterSalesApplication.ExecutionStage.CLOSED
        application.closed_by = user
        application.closed_at = timezone.now()
        application.save()
        cls._event(
            application,
            event_type,
            user=user,
            identity=identity,
            payload={
                "final_fault_type": fault_type,
                "final_fault_materials": materials,
                "final_solution_type": solution_type,
                "final_solution_note": solution_note,
            },
        )
        cls._publish_supplier_quality_notices(
            application,
            actor_openid=user.wx_openid or "",
        )
        is_remote_guidance = event_type == "REMOTE_RESOLVED_AND_CLOSED"
        cls._publish_applicant_notice(
            application,
            topic="factory-closed",
            title=(
                "厂家远程指导已完成并结案"
                if is_remote_guidance
                else "售后服务已完成并结案"
            ),
            summary=(
                f"{cls._summary(application)}｜已完成远程指导，可查看处理方案与结案结果"
                if is_remote_guidance
                else cls._summary(application)
            ),
            actor_openid=user.wx_openid or "",
        )

    @staticmethod
    def _material_match_key(material):
        bom_item_id = material.get("bom_item_id")
        if bom_item_id not in (None, ""):
            return ("bom_item", str(bom_item_id))
        material_id = material.get("material_id")
        return ("material", str(material_id)) if material_id not in (None, "") else None

    @classmethod
    def _supplier_fault_replacement_materials(cls, application):
        """Return actual replaced materials confirmed as the final root cause."""
        if (
            application.final_fault_type != cls.FINAL_COMPONENT_FAULT
            or application.final_solution_type
            != AfterSalesApplication.SolutionType.COMPONENT_REPLACEMENT
            or application.repair_material_replaced is not True
        ):
            return []
        repaired_keys = {
            key
            for material in (application.repair_materials or [])
            if (key := cls._material_match_key(material))
        }
        return [
            material
            for material in (application.final_fault_materials or [])
            if cls._material_match_key(material) in repaired_keys
            and material.get("supplier_id")
        ]

    @classmethod
    def _supplier_notice_key(cls, application, supplier_id):
        return f"aftersales:{application.id}:supplier-quality:{supplier_id}"

    @classmethod
    def _supplier_notice_key_for_id(cls, application_id, supplier_id):
        return f"aftersales:{application_id}:supplier-quality:{supplier_id}"

    @classmethod
    def _supplier_notice_link(cls, application, supplier_id):
        return (
            "/pages/supply_chain/aftersale_quality_notice/"
            "aftersale_quality_notice"
            f"?application_id={application.id}&supplier_id={supplier_id}"
        )

    @classmethod
    def _publish_supplier_quality_notices(cls, application, *, actor_openid=""):
        grouped = {}
        for material in cls._supplier_fault_replacement_materials(application):
            grouped.setdefault(int(material["supplier_id"]), []).append(material)

        for supplier_id, materials in grouped.items():
            supplier = Organization.objects.filter(
                pk=supplier_id,
                organization_type__code="SUPPLIER",
                is_active=True,
            ).first()
            if not supplier:
                continue
            material_ids = sorted({
                int(item["material_id"])
                for item in materials
                if item.get("material_id") not in (None, "")
            })
            recipient_data = get_supplier_task_recipients(
                supplier=supplier,
                material_ids=material_ids,
            )
            recipient_scopes = dict(recipient_data.get("recipient_scopes") or {})
            recipients = list(recipient_data.get("openids") or [])

            # Old deployments may not yet have material/category responsibility
            # assignments.  Supplier sales still need the quality notification;
            # owners remain included by the shared recipient resolver.
            if not recipient_data.get("sales_openids"):
                fallback_sales = (
                    UserOrganization.objects.select_related("user")
                    .filter(
                        organization=supplier,
                        role__code="supplier_sales",
                        role__is_active=True,
                        user__is_active=True,
                        is_active=True,
                    )
                    .exclude(user__wx_openid__isnull=True)
                    .exclude(user__wx_openid="")
                )
                for identity in fallback_sales:
                    openid = identity.user.wx_openid
                    recipients.append(openid)
                    recipient_scopes[openid] = {
                        "role": "supplier_sales",
                        "identity_id": identity.pk,
                        "name": _user_name(identity.user),
                        "material_ids": material_ids,
                    }
            recipients = list(dict.fromkeys(recipients))
            if not recipients:
                continue
            material_names = "、".join(
                dict.fromkeys(item.get("material_name") or "物料" for item in materials)
            )
            publish_workitem(
                domain="AFTERSALE",
                type=cls.SUPPLIER_QUALITY_NOTICE_TYPE,
                biz_id=str(application.id),
                biz_ref=f"AfterSalesApplication:{application.id}",
                uniq_key=cls._supplier_notice_key(application, supplier_id),
                title="产品售后确认物料故障并完成更换",
                summary=f"{cls._summary(application)}｜涉及：{material_names}",
                link=cls._supplier_notice_link(application, supplier_id),
                recipients_openids=recipients,
                need_action=False,
                action_mode="EACH_ACK",
                payload={
                    "application_id": application.id,
                    "case_no": application.case_no,
                    "supplier_id": supplier_id,
                    "supplier_name": supplier.name,
                    "material_ids": material_ids,
                    "materials": materials,
                    "recipient_scopes": recipient_scopes,
                    "notice_kind": "SUPPLIER_QUALITY",
                },
                actor_openid=actor_openid,
                mark_workitem_done_immediately=True,
            )

    @classmethod
    def supplier_quality_notice_detail(
        cls, *, application_id, supplier_id, user
    ):
        identity = cls._identity(user)
        if (
            not identity
            or identity.organization_id != supplier_id
            or identity.role.code not in {"supplier_owner", "supplier_sales"}
        ):
            raise AfterSalesError("无权查看该供应商质量通知")
        workitem = WorkItem.objects.filter(
            uniq_key=cls._supplier_notice_key_for_id(application_id, supplier_id),
            type=cls.SUPPLIER_QUALITY_NOTICE_TYPE,
            user_tasks__to_openid=user.wx_openid or "",
        ).distinct().first()
        if not workitem:
            raise AfterSalesError("供应商质量通知不存在或未向当前账号送达")
        application = (
            AfterSalesApplication.objects.select_related(
                "factory_organization", "device", "device__product_model"
            )
            .filter(pk=application_id, status=AfterSalesApplication.Status.CLOSED)
            .first()
        )
        if not application:
            raise AfterSalesError("对应售后结案记录不存在")
        scope = (workitem.payload.get("recipient_scopes") or {}).get(
            user.wx_openid or "", {}
        )
        allowed_material_ids = {str(item) for item in scope.get("material_ids") or []}
        materials = list(workitem.payload.get("materials") or [])
        if identity.role.code == "supplier_sales":
            materials = [
                item for item in materials
                if str(item.get("material_id")) in allowed_material_ids
            ]
        device = application.device_snapshot or {}
        bom = application.bom_snapshot or {}
        return {
            "notice_type": "SUPPLIER_QUALITY",
            "notice_title": "售后物料质量知晓通知",
            "case_no": application.case_no,
            "factory_name": application.factory_organization.name,
            "supplier": {"id": supplier_id, "name": identity.organization.name},
            "device": {
                "sn": device.get("sn") or application.device.sn,
                "product_model": device.get("product_model")
                or application.device.product_model.name,
                "production_batch": device.get("production_batch")
                or device.get("batch_no")
                or "",
                "bom_version": bom.get("version") or bom.get("version_no") or "",
            },
            "materials": materials,
            "final": {
                "fault_type": application.final_fault_type,
                "fault_type_name": application.get_final_fault_type_display(),
                "solution_type": application.final_solution_type,
                "solution_type_name": application.get_final_solution_type_display(),
                "solution_note": application.final_solution_note,
            },
            "repair": {
                "summary": application.repair_summary,
                "material_replaced": application.repair_material_replaced is True,
            },
            "closed_at": application.closed_at.isoformat() if application.closed_at else "",
            "viewer": {"role_code": identity.role.code, "role_name": identity.role.name},
            "privacy_note": "本通知已隐藏售后发起人、联系方式、地址、定位及现场附件。",
        }

    @classmethod
    def list_supplier_quality_notices(cls, *, user, limit=100):
        identity = cls._identity(user)
        if (
            not identity
            or identity.role.code not in {"supplier_owner", "supplier_sales"}
            or identity.organization.organization_type.code != "SUPPLIER"
        ):
            raise AfterSalesError("当前账号没有供应商质量通知列表权限")
        try:
            limit = max(1, min(int(limit), 200))
        except (TypeError, ValueError):
            limit = 100

        # UserTask is the authorization source.  A supplier cannot discover a
        # case merely by guessing its application id.
        candidate_tasks = list(
            UserTask.objects.select_related("workitem")
            .filter(
                to_openid=user.wx_openid or "",
                workitem__type=cls.SUPPLIER_QUALITY_NOTICE_TYPE,
            )
            .order_by("-cursor_time", "-id")[:400]
        )
        supplier_id = identity.organization_id
        tasks = [
            task for task in candidate_tasks
            if int((task.workitem.payload or {}).get("supplier_id") or 0)
            == supplier_id
        ][:limit]
        application_ids = {
            int((task.workitem.payload or {}).get("application_id") or 0)
            for task in tasks
        }
        applications = {
            item.id: item
            for item in AfterSalesApplication.objects.select_related(
                "factory_organization", "device", "device__product_model"
            ).filter(id__in=application_ids, status=AfterSalesApplication.Status.CLOSED)
        }
        rows = []
        for task in tasks:
            payload = task.workitem.payload or {}
            application_id = int(payload.get("application_id") or 0)
            application = applications.get(application_id)
            if not application:
                continue
            scope = (payload.get("recipient_scopes") or {}).get(
                user.wx_openid or "", {}
            )
            allowed_ids = {str(value) for value in scope.get("material_ids") or []}
            materials = list(payload.get("materials") or [])
            if identity.role.code == "supplier_sales":
                materials = [
                    item for item in materials
                    if str(item.get("material_id")) in allowed_ids
                ]
            device = application.device_snapshot or {}
            rows.append({
                "task_id": task.id,
                "application_id": application.id,
                "supplier_id": supplier_id,
                "case_no": application.case_no,
                "title": task.display_title_snapshot or task.workitem.title,
                "summary": task.display_subtitle_snapshot or task.workitem.summary,
                "state": task.state,
                "is_unread": task.state == "NEW",
                "factory_name": application.factory_organization.name,
                "product_model": device.get("product_model")
                or application.device.product_model.name,
                "sn": device.get("sn") or application.device.sn,
                "materials": materials,
                "material_names": list(dict.fromkeys(
                    item.get("material_name") or "物料" for item in materials
                )),
                "closed_at": application.closed_at.isoformat()
                if application.closed_at else "",
                "detail_link": cls._supplier_notice_link(application, supplier_id),
            })
        return {"items": rows, "count": len(rows)}

    @classmethod
    @transaction.atomic
    def factory_close(cls, *, application_id, user, data):
        application = (
            AfterSalesApplication.objects.select_for_update().filter(pk=application_id).first()
        )
        if not application:
            raise AfterSalesError("售后申请不存在")
        identity = cls._require_factory_sales(application, user)
        if (
            application.status != AfterSalesApplication.Status.PENDING_FACTORY_CLOSE
            or application.execution_stage
            != AfterSalesApplication.ExecutionStage.FINAL_DIAGNOSIS
        ):
            raise AfterSalesError("当前售后申请暂不能最终结案")
        if application.dispatch_route == AfterSalesApplication.DispatchRoute.THIRD_PARTY:
            if not application.third_party_cost_amount:
                raise AfterSalesError("第三方维修费用尚未登记")
            if not cls._has_event(application, "THIRD_PARTY_MATERIAL_SHIPMENT_CONFIRMED"):
                raise AfterSalesError("第三方维修备件寄送记录缺失")
            if not cls._has_event(application, "THIRD_PARTY_EXPENSE_RECORDED"):
                raise AfterSalesError("第三方维修费用记录缺失")
            if not cls._has_event(application, "THIRD_PARTY_FAULT_MATERIAL_RETURN_CONFIRMED"):
                raise AfterSalesError("第三方故障物料返还记录缺失")
            cls._require_media(
                application,
                AfterSalesMedia.Purpose.THIRD_PARTY_MATERIAL_SHIPMENT_DOCUMENT,
                "第三方备件寄送单据缺失",
            )
            cls._require_media(
                application,
                AfterSalesMedia.Purpose.THIRD_PARTY_FEE_VOUCHER,
                "第三方维修费用凭证缺失",
            )
            cls._require_media(
                application,
                AfterSalesMedia.Purpose.THIRD_PARTY_FAULT_MATERIAL_RETURN_DOCUMENT,
                "故障物料返还单据缺失",
            )
        cls._complete_tasks(
            application,
            steps=["factory-close"],
            actor_openid=user.wx_openid or "",
        )
        cls._complete_factory_workorder(
            application,
            actor_openid=user.wx_openid or "",
        )
        cls._close_application(application, user=user, identity=identity, data=data)
        return cls.serialize(application)

    @classmethod
    @transaction.atomic
    def process_confirmation_timeouts(cls):
        """Default-confirm seven days after repair completion.

        Intended for a server-side hourly/daily management command.  It never
        depends on a mini-program page being opened.
        """
        now = timezone.now()
        applications = list(
            AfterSalesApplication.objects.select_for_update()
            .select_related("applicant")
            .filter(
                status=AfterSalesApplication.Status.WAITING_CUSTOMER_CONFIRM,
                execution_stage=AfterSalesApplication.ExecutionStage.AWAITING_APPLICANT_CONFIRM,
                applicant_confirm_due_at__isnull=False,
                applicant_confirm_due_at__lte=now,
            )
        )
        processed = []
        for application in applications:
            application.status = AfterSalesApplication.Status.PENDING_FACTORY_CLOSE
            application.execution_stage = (
                AfterSalesApplication.ExecutionStage.FINAL_DIAGNOSIS
            )
            application.applicant_confirmed_at = now
            application.applicant_confirm_mode = (
                AfterSalesApplication.ApplicantConfirmMode.TIMEOUT
            )
            application.save()
            cls._complete_applicant_workorder(application)
            cls._event(
                application,
                "APPLICANT_CONFIRM_TIMEOUT",
                payload={"due_at": application.applicant_confirm_due_at.isoformat()},
            )
            cls._send_factory_close_task(
                application,
                title="售后七天未确认，待厂家最终归因结案",
            )
            cls._publish_applicant_notice(
                application,
                topic="applicant-confirm-timeout",
                title="售后结果已按七天未确认规则转厂家结案",
                summary=cls._summary(application),
            )
            processed.append(application.id)
        return processed

    @classmethod
    def _history(cls, application):
        return [
            {
                "id": event.id,
                "event_type": event.event_type,
                "actor": _user_name(event.actor) if event.actor else "系统",
                "actor_role_code": event.actor_role_code,
                "actor_role_name": event.actor_role_name,
                "organization": event.organization_name_snapshot,
                "payload": event.payload or {},
                "created_at": event.created_at.isoformat(),
            }
            for event in application.events.all()
        ]

    @classmethod
    def _lifecycle_history(cls, application):
        event_types = {
            "INSTALL", "ACTIVATE", "FACTORY_OUT", "SHIP", "RECEIVE",
            "RETURN", "TRANSFER", "AFTERSALE_TRANSFER", "MAINTAIN", "REPAIR",
        }
        return [
            {
                "id": item.id,
                "event_type": item.event_type,
                "event_type_name": dict(LifecycleEvent.EVENT_TYPE_CHOICES).get(
                    item.event_type, item.event_type
                ),
                "organization": item.organization.name if item.organization else "",
                "from_organization": (
                    item.from_organization.name if item.from_organization else ""
                ),
                "to_organization": (
                    item.to_organization.name if item.to_organization else ""
                ),
                "operator": _user_name(item.operator) if item.operator else "",
                "data": item.data or {},
                "created_at": item.created_at.isoformat(),
            }
            for item in LifecycleEvent.objects.filter(
                device_id=application.device_id, event_type__in=event_types
            ).select_related(
                "organization", "from_organization", "to_organization", "operator"
            ).order_by("-created_at", "-id")[:30]
        ]

    @classmethod
    def _viewer_actions(cls, application, user, identity):
        applicant = cls._is_applicant(application, user)
        assignee = cls._is_assignee(application, user)
        factory_sales = cls._is_factory_sales(application, user, identity)
        stage = application.execution_stage
        third_party_stage = (
            application.dispatch_route == AfterSalesApplication.DispatchRoute.THIRD_PARTY
            and stage == AfterSalesApplication.ExecutionStage.THIRD_PARTY_REPAIR
        )
        third_party_material_done = cls._has_event(
            application, "THIRD_PARTY_MATERIAL_SHIPMENT_CONFIRMED"
        ) if third_party_stage else False
        third_party_expense_done = cls._has_event(
            application, "THIRD_PARTY_EXPENSE_RECORDED"
        ) if third_party_stage else False
        actions = {
            "can_triage": factory_sales
            and application.status == AfterSalesApplication.Status.SUBMITTED,
            "can_applicant_shipment": applicant
            and stage == AfterSalesApplication.ExecutionStage.AWAITING_APPLICANT_SHIPMENT,
            "can_repair_receipt": assignee
            and stage == AfterSalesApplication.ExecutionStage.AWAITING_REPAIR_RECEIPT,
            "can_repair_complete": assignee
            and stage
            in {
                AfterSalesApplication.ExecutionStage.CONTACT_INSTALLER,
                AfterSalesApplication.ExecutionStage.REPAIRING,
            },
            "can_return_shipment": assignee
            and stage == AfterSalesApplication.ExecutionStage.AWAITING_RETURN_SHIPMENT,
            "can_applicant_confirm": applicant
            and stage == AfterSalesApplication.ExecutionStage.AWAITING_APPLICANT_CONFIRM,
            "can_factory_close": factory_sales
            and stage == AfterSalesApplication.ExecutionStage.FINAL_DIAGNOSIS
            and application.status == AfterSalesApplication.Status.PENDING_FACTORY_CLOSE,
            "can_third_party_material_shipment": factory_sales and third_party_stage
            and not third_party_material_done,
            "can_third_party_expense": factory_sales and third_party_stage
            and third_party_material_done and not third_party_expense_done,
            "can_third_party_fault_material_return": factory_sales and third_party_stage
            and third_party_expense_done,
        }
        actions["allowed_media_purposes"] = [
            value
            for value, _ in AfterSalesMedia.Purpose.choices
            if cls._media_purpose_allowed(application, user, value)
        ]
        return actions

    @classmethod
    def _viewer_stage(cls, application, *, applicant, assignee, factory_sales, actions):
        """Describe the current node from this viewer's perspective."""

        stage = application.execution_stage
        action_titles = (
            ("can_triage", "待您认定并分派", "请完成故障认定并选择处理路径"),
            ("can_applicant_shipment", "待您寄送设备", "请寄送设备并上传物流单据"),
            ("can_repair_receipt", "待您确认收件", "请确认已收到寄修设备"),
            ("can_repair_complete", "待您完成维修", "请完成维修并上传处理记录"),
            ("can_return_shipment", "待您寄回设备", "请寄回设备并上传物流单据"),
            ("can_applicant_confirm", "待您确认处理结果", "请确认本次售后处理结果"),
            ("can_factory_close", "待您完成最终归因", "请完成最终故障归因并结案"),
            (
                "can_third_party_material_shipment",
                "待您登记备件寄送",
                "请登记第三方维修备件寄送",
            ),
            ("can_third_party_expense", "待您登记维修费用", "请登记第三方维修费用凭证"),
            (
                "can_third_party_fault_material_return",
                "待您登记故障物料返还",
                "请登记故障物料返还",
            ),
        )
        for key, badge, title in action_titles:
            if actions.get(key):
                return {
                    "mode": "ACTION_REQUIRED",
                    "badge": badge,
                    "title": title,
                    "message": "当前步骤正在由您完成，请在下方提交相应操作。",
                }

        if stage == AfterSalesApplication.ExecutionStage.CLOSED:
            return {
                "mode": "COMPLETED",
                "badge": "售后服务已完成",
                "title": "本次售后服务已完成",
                "message": application.final_solution_note or "可查看完整处理记录与结案结果。",
            }

        waiting = {
            AfterSalesApplication.ExecutionStage.NONE: (
                "等待厂家销售经理判断并指派", "厂家尚未完成故障认定。"
            ),
            AfterSalesApplication.ExecutionStage.AWAITING_APPLICANT_SHIPMENT: (
                "等待售后发起人寄送设备", "发起人尚未上传物流单据并确认寄送。"
            ),
            AfterSalesApplication.ExecutionStage.AWAITING_REPAIR_RECEIPT: (
                "等待维修人确认收件", "设备已寄出，等待维修执行人确认收到。"
            ),
            AfterSalesApplication.ExecutionStage.REPAIRING: (
                "等待维修人完成维修", "维修执行人正在处理故障。"
            ),
            AfterSalesApplication.ExecutionStage.AWAITING_RETURN_SHIPMENT: (
                "等待维修人寄回设备", "维修已经完成，等待维修人登记返件物流。"
            ),
            AfterSalesApplication.ExecutionStage.AWAITING_APPLICANT_CONFIRM: (
                "等待售后发起人确认结果", "设备处理结果等待发起人确认。"
            ),
            AfterSalesApplication.ExecutionStage.FINAL_DIAGNOSIS: (
                "等待厂家完成最终归因", "厂家销售经理将确认故障物料、类型和解决方案。"
            ),
        }
        title, message = waiting.get(stage, ("售后处理中", "请查看最新处理记录。"))

        # The original installer route gives the applicant a contact plan,
        # while the assignee performs the repair. It is progress information,
        # not a false "waiting for yourself" prompt.
        if (
            stage == AfterSalesApplication.ExecutionStage.CONTACT_INSTALLER
            and applicant
        ):
            return {
                "mode": "PROGRESS",
                "badge": "厂家已安排处理",
                "title": "厂家已安排原装车操作人员处理",
                "message": application.repair_plan,
            }

        # A viewer who is involved but has no current action sees the actual
        # counterparty that owns this node.
        if applicant or assignee or factory_sales:
            return {
                "mode": "WAITING",
                "badge": title,
                "title": title,
                "message": message,
            }
        return {
            "mode": "PROGRESS",
            "badge": "售后处理中",
            "title": "售后处理中",
            "message": "",
        }

    @classmethod
    def detail(cls, *, application_id, user):
        application = (
            AfterSalesApplication.objects.select_related(
                "device",
                "applicant",
                "customer_organization",
                "factory_organization",
                "assigned_user",
                "assigned_organization",
                "triaged_by",
                "repair_completed_by",
                "closed_by",
            )
            .prefetch_related("media", "events")
            .filter(pk=application_id)
            .first()
        )
        if not application:
            raise AfterSalesError("售后申请不存在")
        if not cls.can_view_application(application=application, user=user):
            raise AfterSalesError("无权查看该售后申请")
        identity = cls._identity(user)
        data = cls.serialize(application, detail=True)
        is_applicant = cls._is_applicant(application, user)
        is_assignee = cls._is_assignee(application, user)
        is_factory_sales = cls._is_factory_sales(application, user, identity)
        data["viewer"] = {
            "is_applicant": is_applicant,
            "is_assignee": is_assignee,
            "is_factory_sales": is_factory_sales,
        }
        data["actions"] = cls._viewer_actions(application, user, identity)
        data["viewer_stage"] = cls._viewer_stage(
            application,
            applicant=is_applicant,
            assignee=is_assignee,
            factory_sales=is_factory_sales,
            actions=data["actions"],
        )
        data["applicant_instruction"] = (
            cls._applicant_instruction(application) if is_applicant else {}
        )
        data["preliminary_fault_types"] = cls._option_rows(
            AfterSalesApplication.FaultType.choices
        )
        data["final_fault_types"] = cls._option_rows(
            AfterSalesApplication.FaultType.choices
        )
        data["solution_types"] = cls._option_rows(
            AfterSalesApplication.SolutionType.choices
        )
        data["final_material_options"] = cls._material_options(
            application, key_only=True
        )
        data["history"] = cls._history(application)
        data["lifecycle_history"] = cls._lifecycle_history(application)
        if data["actions"]["can_triage"]:
            data["triage_options"] = cls.triage_options(
                application_id=application.id, user=user
            )
        return data

    @classmethod
    def list_applications(cls, *, user, limit=50):
        try:
            limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            limit = 50
        identity = cls._identity(user)
        queryset = AfterSalesApplication.objects.select_related(
            "device__product_model", "applicant", "factory_organization", "assigned_user"
        )
        if identity and identity.role.code == "factory_sales":
            queryset = queryset.filter(
                Q(factory_organization_id=identity.organization_id)
                | Q(applicant_id=user.id)
                | Q(assigned_user_id=user.id)
            )
        else:
            queryset = queryset.filter(
                Q(applicant_id=user.id) | Q(assigned_user_id=user.id)
            )
        return [
            {
                "id": application.id,
                "case_no": application.case_no,
                "status": application.status,
                "status_name": application.get_status_display(),
                "execution_stage": application.execution_stage,
                "execution_stage_name": application.get_execution_stage_display(),
                "sn": application.device.sn,
                "product_model": application.device.product_model.name,
                "route": application.dispatch_route,
                "route_name": (
                    application.get_dispatch_route_display()
                    if application.dispatch_route
                    else ""
                ),
                "applicant": _user_name(application.applicant),
                "assigned_user": _user_name(application.assigned_user),
                "created_at": application.created_at.isoformat(),
                "updated_at": application.updated_at.isoformat(),
            }
            for application in queryset.order_by("-created_at", "-id")[:limit]
        ]

    @classmethod
    def get_media_file(cls, *, media_id, user):
        media = (
            AfterSalesMedia.objects.select_related("application")
            .filter(pk=media_id)
            .first()
        )
        if not media:
            raise AfterSalesError("售后附件不存在")
        if not cls.can_view_application(application=media.application, user=user):
            raise AfterSalesError("无权读取该售后附件")
        return media

    @staticmethod
    def serialize_media(media):
        return {
            "id": media.id,
            "media_type": media.media_type,
            "media_type_name": media.get_media_type_display(),
            "purpose": media.purpose,
            "purpose_name": media.get_purpose_display(),
            "original_name": media.original_name,
            "caption": media.caption,
            "file_size": media.file_size,
            "duration_seconds": media.duration_seconds,
            "uploaded_by": _user_name(media.uploaded_by),
            "created_at": media.created_at.isoformat(),
            "url": f"/api/lifecycle/after-sales/media/{media.id}/file/",
        }

    @classmethod
    def serialize(cls, application, detail=False):
        if not application:
            return None
        data = {
            "id": application.id,
            "case_no": application.case_no,
            "status": application.status,
            "status_name": application.get_status_display(),
            "execution_stage": application.execution_stage,
            "execution_stage_name": application.get_execution_stage_display(),
            "fault_description": application.fault_description,
            "location": {
                "name": application.location_name,
                "address": application.location_address,
                "latitude": (
                    str(application.location_latitude)
                    if application.location_latitude is not None
                    else ""
                ),
                "longitude": (
                    str(application.location_longitude)
                    if application.location_longitude is not None
                    else ""
                ),
                "confirmed": application.location_confirmed,
                "confirmed_at": (
                    application.location_confirmed_at.isoformat()
                    if application.location_confirmed_at
                    else ""
                ),
            },
            "device": application.device_snapshot or {},
            "activation": application.activation_snapshot or {},
            "bom": application.bom_snapshot or {},
            "warranty": application.warranty_snapshot or {},
            "submitted_at": (
                application.submitted_at.isoformat() if application.submitted_at else ""
            ),
            "dispatch": {
                "route": application.dispatch_route,
                "route_name": (
                    application.get_dispatch_route_display()
                    if application.dispatch_route
                    else ""
                ),
                "version": application.dispatch_version,
                "preliminary_fault_type": application.preliminary_fault_type,
                "preliminary_fault_type_name": (
                    application.get_preliminary_fault_type_display()
                    if application.preliminary_fault_type
                    else ""
                ),
                "diagnosis_note": application.diagnosis_note,
                "repair_plan": application.repair_plan,
                "triaged_by": _user_name(application.triaged_by),
                "triaged_at": (
                    application.triaged_at.isoformat() if application.triaged_at else ""
                ),
                "assignee": application.assignee_snapshot or {},
                "third_party": application.third_party_snapshot or {},
            },
            "repair": {
                "summary": application.repair_summary,
                "material_replaced": application.repair_material_replaced,
                "materials": application.repair_materials or [],
                "completed_by": _user_name(application.repair_completed_by),
                "completed_at": (
                    application.repair_completed_at.isoformat()
                    if application.repair_completed_at
                    else ""
                ),
            },
            "confirmation": {
                "due_at": (
                    application.applicant_confirm_due_at.isoformat()
                    if application.applicant_confirm_due_at
                    else ""
                ),
                "confirmed_at": (
                    application.applicant_confirmed_at.isoformat()
                    if application.applicant_confirmed_at
                    else ""
                ),
                "mode": application.applicant_confirm_mode,
                "mode_name": (
                    application.get_applicant_confirm_mode_display()
                    if application.applicant_confirm_mode
                    else ""
                ),
            },
            "final": {
                "fault_type": application.final_fault_type,
                "fault_type_name": (
                    application.get_final_fault_type_display()
                    if application.final_fault_type
                    else ""
                ),
                "materials": application.final_fault_materials or [],
                "solution_type": application.final_solution_type,
                "solution_type_name": (
                    application.get_final_solution_type_display()
                    if application.final_solution_type
                    else ""
                ),
                "solution_note": application.final_solution_note,
                "closed_by": _user_name(application.closed_by),
                "closed_at": application.closed_at.isoformat() if application.closed_at else "",
            },
            "applicant": {
                "name": _user_name(application.applicant),
                "organization": application.customer_organization.name,
                "role_code": application.applicant_role_code,
                "role_name": application.applicant_role_name,
            },
        }
        if detail:
            data["media"] = [
                cls.serialize_media(item) for item in application.media.all()
            ]
        return data
