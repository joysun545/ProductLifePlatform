import uuid

from django.db import transaction
from django.utils import timezone

from account.models import (
    Organization,
    OrganizationRelation,
    OrganizationRelationType,
    OrganizationType,
    UserOrganization,
)
from account.services.auth_service import get_active_identity
from common.domain.device_ownership import resolve_device_ownership
from lifecycle.models import (
    ActivationRecord,
    Device,
    DeviceState,
    InstallationInvitation,
    LifecycleEvent,
    OrganizationDevice,
    ProductDocumentRead,
)
from lifecycle.services.scan_service import ScanService
from integration.services.device_telemetry import DeviceTelemetryService
from product.models import ProductDocument


class CustomerFlowError(Exception):
    """用户负责人接受邀请或激活链路不能继续。"""


class CustomerFlowPermissionError(CustomerFlowError):
    """当前账号无权执行该用户操作。"""


class CustomerFlowStateError(CustomerFlowError):
    """邀请或设备当前状态不允许继续。"""


class CustomerDeviceService:
    @staticmethod
    def _expire(invitation):
        if (
            invitation.status == InstallationInvitation.Status.PENDING
            and invitation.expires_at <= timezone.now()
        ):
            invitation.status = InstallationInvitation.Status.EXPIRED
            invitation.save(update_fields=("status", "updated_at"))

    @classmethod
    def invitation_detail(cls, *, token, user):
        invitation = (
            InstallationInvitation.objects.select_related(
                "device__product_model__category",
                "device__batch__manufacturer",
                "installation",
                "inviter",
                "source_organization",
                "target_role",
                "invitee",
                "target_organization",
            )
            .filter(token=token)
            .first()
        )
        if not invitation:
            raise CustomerFlowStateError("邀请不存在")

        cls._expire(invitation)
        identity = get_active_identity(user)
        accepted_by_me = bool(
            invitation.status == InstallationInvitation.Status.ACCEPTED
            and invitation.invitee_id == user.id
        )

        if invitation.status == InstallationInvitation.Status.ACCEPTED:
            if not accepted_by_me:
                raise CustomerFlowStateError("该邀请已经被其他账号使用")
        elif invitation.status != InstallationInvitation.Status.PENDING:
            raise CustomerFlowStateError("邀请二维码已失效")

        blocked_reason = ""
        if identity and not (
            identity.organization.organization_type.code == "CUSTOMER"
            and identity.role.code == "customer_owner"
        ):
            blocked_reason = "当前微信已有其他业务岗位，请使用独立微信账号接受"

        customer_data = invitation.customer_data or {}
        viewer = {
            "has_identity": bool(identity),
            "role": identity.role.code if identity else "tourist",
            "organization_id": identity.organization_id if identity else None,
            "organization_name": identity.organization.name if identity else "",
            "phone": user.phone or "",
            "nickname": user.wx_nickname or user.username,
            "region": identity.organization.region if identity else "",
        }

        return {
            "token": str(invitation.token),
            "status": invitation.status,
            "accepted_by_me": accepted_by_me,
            "acceptance_allowed": not blocked_reason,
            "blocked_reason": blocked_reason,
            "remaining_seconds": max(
                0,
                int((invitation.expires_at - timezone.now()).total_seconds()),
            ),
            "expires_at": invitation.expires_at.isoformat(),
            "device": {
                "id": invitation.device_id,
                "sn": invitation.device.sn,
                "product_model": invitation.device.product_model.name,
                "product_model_code": invitation.device.product_model.code,
                "category": invitation.device.product_model.category.name,
                "manufacturer": invitation.device.batch.manufacturer.name,
            },
            "installation": {
                "installed_at": invitation.installation.installed_at.isoformat(),
                "location": invitation.installation.location,
            },
            "inviter": {
                "nickname": (
                    invitation.inviter.wx_nickname
                    or invitation.inviter.username
                ),
                "organization": invitation.source_organization.name,
            },
            "target_role": {
                "code": invitation.target_role.code,
                "name": invitation.target_role.name,
            },
            "viewer": viewer,
            "customer_data": customer_data,
        }

    @staticmethod
    def _customer_organization(*, user, identity, data):
        if identity:
            if not (
                identity.organization.organization_type.code == "CUSTOMER"
                and identity.role.code == "customer_owner"
            ):
                raise CustomerFlowPermissionError(
                    "当前微信已有其他业务岗位，请使用独立微信账号接受"
                )
            organization = identity.organization
            changed = False
            if data["region"] and not organization.region:
                organization.region = data["region"]
                changed = True
            if data.get("address") and not organization.address:
                organization.address = data["address"]
                changed = True
            if changed:
                organization.save()
            return organization, identity

        customer_type = OrganizationType.objects.filter(
            code="CUSTOMER",
            is_active=True,
        ).first()
        if not customer_type:
            raise CustomerFlowStateError("系统尚未配置用户组织类型")

        if data["customer_type"] == "FLEET":
            organization_name = data["customer_name"].strip()
            if not organization_name:
                raise CustomerFlowStateError("企业或车队用户必须填写名称")
        else:
            base_name = data["contact_name"].strip() or (
                user.wx_nickname or "个人用户"
            )
            organization_name = f"{base_name}的个人用户"

        organization = Organization.objects.create(
            name=organization_name,
            code=f"CUSTOMER-{uuid.uuid4().hex[:12].upper()}",
            organization_type=customer_type,
            region=data["region"],
            address=data.get("address", ""),
        )
        identity = UserOrganization.objects.create(
            user=user,
            organization=organization,
            role=data["target_role"],
            inviter=data["inviter"],
        )
        return organization, identity

    @staticmethod
    def _ensure_single_parent(*, source_organization, customer_organization):
        conflicting = (
            OrganizationRelation.objects.select_for_update()
            .filter(
                target_organization=customer_organization,
                relation_type=OrganizationRelationType.DISTRIBUTION,
                is_active=True,
            )
            .exclude(source_organization=source_organization)
            .exists()
        )
        if conflicting:
            raise CustomerFlowStateError(
                "当前用户组织已经属于其他销售链路，不能通过本邀请加入"
            )

        relation, _ = OrganizationRelation.objects.get_or_create(
            source_organization=source_organization,
            target_organization=customer_organization,
            relation_type=OrganizationRelationType.DISTRIBUTION,
            defaults={"is_active": True},
        )
        if not relation.is_active:
            relation.is_active = True
            relation.save(update_fields=("is_active", "updated_at"))
        return relation

    @classmethod
    @transaction.atomic
    def accept_invitation(cls, *, token, user, customer_data):
        invitation = (
            InstallationInvitation.objects.select_for_update()
            .select_related(
                "device__state",
                "device__organization_record__organization",
                "installation",
                "source_organization",
                "target_role__organization_type",
                "inviter",
                "invitee",
                "target_organization",
            )
            .filter(token=token)
            .first()
        )
        if not invitation:
            raise CustomerFlowStateError("邀请不存在")

        cls._expire(invitation)
        if invitation.status == InstallationInvitation.Status.ACCEPTED:
            if invitation.invitee_id == user.id:
                return invitation, get_active_identity(user)
            raise CustomerFlowStateError("该邀请已经被其他账号使用")
        if invitation.status != InstallationInvitation.Status.PENDING:
            raise CustomerFlowStateError("邀请二维码已失效")
        if invitation.inviter_id == user.id:
            raise CustomerFlowPermissionError("邀请人不能接受自己的邀请")

        device = Device.objects.select_for_update().get(pk=invitation.device_id)
        state = DeviceState.objects.select_for_update().get(device=device)
        owner_record = (
            OrganizationDevice.objects.select_for_update()
            .select_related("organization")
            .get(device=device)
        )

        if invitation.installation.status != "INSTALLED":
            raise CustomerFlowStateError("设备安装记录已经失效")
        if state.install_status != "INSTALLED":
            raise CustomerFlowStateError("设备尚未完成装车")
        if state.activation_status == "ACTIVATED":
            raise CustomerFlowStateError("设备已经激活")
        if owner_record.organization_id != invitation.source_organization_id:
            raise CustomerFlowStateError("设备已经不属于邀请方组织")

        identity = get_active_identity(user)
        profile_data = dict(customer_data)
        profile_data["target_role"] = invitation.target_role
        profile_data["inviter"] = invitation.inviter
        customer_org, identity = cls._customer_organization(
            user=user,
            identity=identity,
            data=profile_data,
        )
        cls._ensure_single_parent(
            source_organization=invitation.source_organization,
            customer_organization=customer_org,
        )

        now = timezone.now()
        event = LifecycleEvent.objects.create(
            device=device,
            event_type="TRANSFER",
            operator=user,
            organization=invitation.source_organization,
            from_organization=invitation.source_organization,
            to_organization=customer_org,
            data={
                "purpose": "customer_acceptance",
                "installation_invitation_id": invitation.id,
                "vehicle_no": customer_data["vehicle_no"],
            },
        )

        owner_record.organization = customer_org
        owner_record.status = "INSTALLED"
        owner_record.delivered_at = now
        owner_record.last_event = event
        owner_record.save()

        state.current_organization = customer_org
        state.status = "INSTALLED"
        state.last_event = event
        state.last_event_time = now
        state.save()

        stored_data = {
            "customer_type": customer_data["customer_type"],
            "customer_name": customer_org.name,
            "contact_name": customer_data["contact_name"],
            "phone": customer_data["phone"],
            "region": customer_data["region"],
            "address": customer_data.get("address", ""),
            "vehicle_no": customer_data["vehicle_no"],
        }
        invitation.invitee = user
        invitation.target_organization = customer_org
        invitation.customer_data = stored_data
        invitation.status = InstallationInvitation.Status.ACCEPTED
        invitation.accepted_at = now
        invitation.save()

        if user.phone != customer_data["phone"]:
            user.phone = customer_data["phone"]
            user.save(update_fields=("phone",))

        return invitation, identity

    @staticmethod
    def _customer_identity(user):
        identity = get_active_identity(user)
        if not identity:
            raise CustomerFlowPermissionError("当前账号还不是用户负责人")
        if not (
            identity.organization.organization_type.code == "CUSTOMER"
            and identity.role.code == "customer_owner"
        ):
            raise CustomerFlowPermissionError("当前岗位不能访问用户设备页面")
        return identity

    @staticmethod
    def _accepted_invitation(*, device, user, identity):
        return (
            InstallationInvitation.objects.filter(
                device=device,
                invitee=user,
                target_organization=identity.organization,
                status=InstallationInvitation.Status.ACCEPTED,
            )
            .order_by("-accepted_at", "-id")
            .first()
        )

    @classmethod
    def _owned_device_context(cls, *, device_id, user):
        identity = cls._customer_identity(user)
        device = (
            Device.objects.select_related(
                "product_model",
                "product_model__category",
                "state",
                "batch",
                "batch__bom",
            )
            .filter(pk=device_id)
            .first()
        )
        if not device:
            raise CustomerFlowStateError("设备不存在")
        ownership = resolve_device_ownership(
            device=device,
            viewer_identity=identity,
        )
        if not ownership.is_direct_owner:
            raise CustomerFlowPermissionError("该设备不属于当前用户组织")
        invitation = cls._accepted_invitation(
            device=device,
            user=user,
            identity=identity,
        )
        return identity, device, invitation

    @staticmethod
    def _customer_type(invitation):
        return str((invitation.customer_data if invitation else {}).get("customer_type") or "").upper()

    @staticmethod
    def _ordered_documents(device):
        documents = {
            document.document_type: document
            for document in ProductDocument.objects.filter(
                product_model=device.product_model,
                is_active=True,
            )
        }
        return [
            documents[document_type]
            for document_type in (
                ProductDocument.DocumentType.USER_MANUAL,
                ProductDocument.DocumentType.DAILY_MAINTENANCE,
                ProductDocument.DocumentType.TROUBLESHOOTING_GUIDE,
            )
            if document_type in documents
        ]

    @classmethod
    def _document_gate(cls, *, device, user, invitation):
        documents = cls._ordered_documents(device)
        current_versions = {(document.id, document.version) for document in documents}
        read_versions = set(
            ProductDocumentRead.objects.filter(
                user=user,
                device=device,
                document_id__in=[document.id for document in documents],
            ).values_list("document_id", "document_version")
        )
        documents_complete = len(documents) == 3
        all_read = documents_complete and current_versions.issubset(read_versions)
        state = device.state
        customer_type = cls._customer_type(invitation)
        gate_required = (
            customer_type == "PERSONAL"
            and state.activation_status != "ACTIVATED"
        )
        activation_allowed = not gate_required or all_read
        if not documents_complete:
            message = "该型号的三篇必读资料尚未配置完整，请联系厂家"
        elif gate_required and not all_read:
            message = "个人用户激活前必须阅读全部三篇资料"
        else:
            message = "必读资料已准备完成"

        return {
            "customer_type": customer_type,
            "gate_required": gate_required,
            "documents_complete": documents_complete,
            "all_read": all_read,
            "activation_allowed": activation_allowed,
            "message": message,
            "documents": [
                {
                    "id": document.id,
                    "document_type": document.document_type,
                    "title": document.title,
                    "version": document.version,
                    "has_file": bool(document.document_file),
                    "original_filename": document.original_filename,
                    "file_type": document.file_type,
                    "file_size": document.file_size,
                    "is_read": (document.id, document.version) in read_versions,
                }
                for document in documents
            ],
        }

    @classmethod
    def document_list(cls, *, device_id, user):
        _, device, invitation = cls._owned_device_context(
            device_id=device_id,
            user=user,
        )
        return cls._document_gate(
            device=device,
            user=user,
            invitation=invitation,
        )

    @classmethod
    def document_detail(cls, *, device_id, document_id, user):
        _, device, invitation = cls._owned_device_context(
            device_id=device_id,
            user=user,
        )
        document = ProductDocument.objects.filter(
            id=document_id,
            product_model=device.product_model,
            is_active=True,
        ).first()
        if not document:
            raise CustomerFlowStateError("型号资料不存在")
        gate = cls._document_gate(device=device, user=user, invitation=invitation)
        item = next(
            (item for item in gate["documents"] if item["id"] == document.id),
            None,
        )
        return {
            **(item or {}),
            "content": document.content,
            "mime_type": document.mime_type,
            "file_endpoint": (
                f"/lifecycle/customer/devices/{device.id}/documents/"
                f"{document.id}/file/"
                if document.document_file
                else ""
            ),
            "product_model": {
                "id": device.product_model_id,
                "code": device.product_model.code,
                "name": device.product_model.name,
            },
        }

    @classmethod
    def document_file(cls, *, device_id, document_id, user):
        _, device, _ = cls._owned_device_context(
            device_id=device_id,
            user=user,
        )
        document = ProductDocument.objects.filter(
            id=document_id,
            product_model=device.product_model,
            is_active=True,
        ).first()
        if not document:
            raise CustomerFlowStateError("型号资料不存在")
        if not document.document_file:
            raise CustomerFlowStateError("该资料没有原始文件")
        return document

    @classmethod
    def mark_document_read(cls, *, device_id, document_id, user):
        _, device, invitation = cls._owned_device_context(
            device_id=device_id,
            user=user,
        )
        document = ProductDocument.objects.filter(
            id=document_id,
            product_model=device.product_model,
            is_active=True,
        ).first()
        if not document:
            raise CustomerFlowStateError("型号资料不存在")
        ProductDocumentRead.objects.get_or_create(
            user=user,
            device=device,
            document=document,
            document_version=document.version,
        )
        return cls._document_gate(device=device, user=user, invitation=invitation)

    @classmethod
    def telemetry_snapshot(cls, *, device_id, user):
        _, device, _ = cls._owned_device_context(device_id=device_id, user=user)
        return DeviceTelemetryService.snapshot(device=device, user=user)

    @classmethod
    def device_result(cls, *, device_id, user):
        identity, device, invitation = cls._owned_device_context(
            device_id=device_id,
            user=user,
        )
        profile = invitation.customer_data if invitation else {}
        is_activated = device.state.activation_status == "ACTIVATED"
        return {
            "scan_result": ScanService.resolve_device(device=device, user=user),
            "customer": {
                "organization_id": identity.organization_id,
                "organization_name": identity.organization.name,
                "region": identity.organization.region,
                "role": identity.role.code,
                "role_name": identity.role.name,
                "customer_type": cls._customer_type(invitation),
                "profile": profile,
            },
            "actions": {
                "can_activate": not is_activated,
                "can_apply_aftersale": is_activated,
            },
        }

    @classmethod
    @transaction.atomic
    def activate(cls, *, device_id, user, vehicle_no, location=""):
        identity = cls._customer_identity(user)
        device = Device.objects.select_for_update().filter(pk=device_id).first()
        if not device:
            raise CustomerFlowStateError("设备不存在")

        state = DeviceState.objects.select_for_update().get(device=device)
        owner_record = OrganizationDevice.objects.select_for_update().get(
            device=device
        )
        if owner_record.organization_id != identity.organization_id:
            raise CustomerFlowPermissionError("该设备不属于当前用户组织")
        if state.install_status != "INSTALLED":
            raise CustomerFlowStateError("设备尚未完成装车")
        if state.activation_status == "ACTIVATED":
            raise CustomerFlowStateError("设备已经激活")
        if ActivationRecord.objects.filter(device=device).exists():
            raise CustomerFlowStateError("设备已经存在激活记录")

        invitation = cls._accepted_invitation(
            device=device,
            user=user,
            identity=identity,
        )
        document_gate = cls._document_gate(
            device=device,
            user=user,
            invitation=invitation,
        )
        if document_gate["gate_required"] and not document_gate["activation_allowed"]:
            raise CustomerFlowStateError(document_gate["message"])

        now = timezone.now()
        event = LifecycleEvent.objects.create(
            device=device,
            event_type="ACTIVATE",
            operator=user,
            organization=identity.organization,
            from_organization=identity.organization,
            to_organization=identity.organization,
            data={"vehicle_no": vehicle_no, "location": location or ""},
        )
        ActivationRecord.objects.create(
            device=device,
            organization=identity.organization,
            operator=user,
            vehicle_no=vehicle_no,
            location=location or "",
            lifecycle_event=event,
        )

        state.status = "ACTIVATED"
        state.activation_status = "ACTIVATED"
        state.last_event = event
        state.last_event_time = now
        state.save()

        owner_record.status = "SOLD"
        owner_record.last_event = event
        owner_record.save()

        if invitation:
            data = dict(invitation.customer_data or {})
            data["vehicle_no"] = vehicle_no
            invitation.customer_data = data
            invitation.save(update_fields=("customer_data", "updated_at"))

        return cls.device_result(device_id=device.id, user=user)
