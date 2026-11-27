import uuid
from datetime import timedelta

from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError, OperationalError, transaction
from django.utils import timezone

from account.models import (
    Organization,
    OrganizationRelation,
    OrganizationRelationType,
    OrganizationType,
    OrganizationRole,
    User,
    UserOrganization,
)
from account.services.auth_service import get_active_identity
from common.domain.device_ownership import resolve_device_ownership
from lifecycle.models import (
    ActivationRecord,
    CustomerDeviceBinding,
    CustomerVehicle,
    DriverVehicleBinding,
    DriverVehicleInvitation,
    Device,
    DeviceState,
    InstallationInvitation,
    LifecycleEvent,
    OrganizationDevice,
    ProductDocumentRead,
    QRCode,
    VehicleDeviceAssignment,
)
from lifecycle.services.scan_service import ScanService
from lifecycle.services.physical_device_code_service import (
    PhysicalDeviceCodeError,
    bind_device_code,
    normalize_device_code,
    verify_device_code_for_activation,
)
from integration.services.device_telemetry import DeviceTelemetryService
from product.models import ProductDocument
from quality_trace.services import enforce_device_operation, quality_gate_atomic


class CustomerFlowError(Exception):
    """用户负责人接受邀请或激活链路不能继续。"""


class CustomerFlowPermissionError(CustomerFlowError):
    """当前账号无权执行该用户操作。"""


class CustomerFlowStateError(CustomerFlowError):
    """邀请或设备当前状态不允许继续。"""


class CustomerDeviceService:
    @staticmethod
    def _vehicle(*, organization, vehicle_no):
        display = (vehicle_no or "").strip() or "未登记车辆"
        normalized = "".join(display.upper().split())
        vehicle, _ = CustomerVehicle.objects.get_or_create(
            customer_organization=organization,
            normalized_vehicle_no=normalized,
            defaults={"vehicle_no": display},
        )
        return vehicle

    @staticmethod
    def _assign_device_vehicle(*, binding, vehicle, user, reason, at=None):
        """切换当前车辆并保留上一段归属历史；重复调用保持幂等。"""
        at = at or timezone.now()
        current = VehicleDeviceAssignment.objects.select_for_update().filter(
            device=binding.device, is_current=True,
        ).first()
        if current and current.vehicle_id == vehicle.id:
            return current
        if current:
            current.is_current = False
            current.ended_at = at
            current.save(update_fields=("is_current", "ended_at", "updated_at"))
        return VehicleDeviceAssignment.objects.create(
            device=binding.device,
            customer_organization=binding.customer_organization,
            vehicle=vehicle,
            source_binding=binding,
            assigned_by=user,
            reason=reason,
            started_at=at,
        )

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
                "device__qrcode",
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
        customer_profile = customer_data
        if identity and identity.role.code == "customer_owner":
            customer_profile = cls._customer_profile(identity=identity, user=user)
        if accepted_by_me:
            acceptance_mode = "ALREADY_BOUND"
        elif blocked_reason:
            acceptance_mode = "BLOCKED"
        elif identity:
            acceptance_mode = "ADD_DEVICE"
        else:
            acceptance_mode = "NEW_CUSTOMER"
        viewer = {
            "has_identity": bool(identity),
            "role": identity.role.code if identity else "tourist",
            "organization_id": identity.organization_id if identity else None,
            "organization_name": identity.organization.name if identity else "",
            "phone": user.phone or "",
            "nickname": user.wx_nickname or user.username,
            "region": identity.organization.region if identity else "",
        }
        try:
            invitation_lifecycle_code = invitation.device.qrcode.code
        except ObjectDoesNotExist:
            invitation_lifecycle_code = ""

        root = invitation.device.batch.manufacturer.get_factory_root()
        return {
            "token": str(invitation.token),
            "root_organization": {"id": root.pk, "name": root.name} if root else None,
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
                "lifecycle_code": invitation_lifecycle_code,
                "physical_device_code": invitation.device.physical_device_code or "",
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
            "customer_profile": customer_profile,
            "acceptance_mode": acceptance_mode,
        }

    @staticmethod
    def _customer_profile(*, identity, user):
        binding = (
            CustomerDeviceBinding.objects.filter(
                customer_organization=identity.organization,
                status=CustomerDeviceBinding.Status.ACTIVE,
            )
            .select_related("invitation")
            .order_by("-accepted_at", "-id")
            .first()
        )
        invitation_data = (
            dict(binding.invitation.customer_data or {})
            if binding and binding.invitation
            else {}
        )
        return {
            "customer_type": (
                binding.customer_type
                if binding
                else str(invitation_data.get("customer_type") or "PERSONAL").upper()
            ),
            "customer_name": identity.organization.name,
            "contact_name": invitation_data.get("contact_name")
            or user.wx_nickname
            or user.username,
            "phone": user.phone or invitation_data.get("phone") or "",
            "region": identity.organization.region,
            "address": identity.organization.address,
            "vehicle_no": (
                binding.vehicle_no
                if binding
                else invitation_data.get("vehicle_no") or ""
            ),
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

        base_name = data["contact_name"].strip() or (user.wx_nickname or "用户")
        organization_name = f"{base_name}的用户档案"

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

    @classmethod
    def _restore_inactive_customer_identity(cls, *, user, invitation):
        """Reuse a compatible inactive customer slot instead of inserting a duplicate.

        UserOrganization is one-to-one with User, including inactive historical rows.
        A guest lookup can therefore return no active identity even though the account
        still has a dormant customer-owner slot. Only restore that exact role when its
        organization is active and belongs to the invitation's factory tenant.
        """
        identity = (
            UserOrganization.objects.select_for_update()
            .select_related(
                "organization__organization_type",
                "organization__factory_root",
                "role__organization_type",
            )
            .filter(user_id=user.pk)
            .first()
        )
        if identity is None:
            return None

        if identity.is_active:
            raise CustomerFlowPermissionError(
                "当前微信已有未能识别的组织身份，请联系管理员核实后再接受邀请"
            )

        organization = identity.organization
        role = identity.role
        if not (
            invitation.target_role.code == "customer_owner"
            and organization.organization_type.code == "CUSTOMER"
            and organization.is_active
            and organization.organization_type.is_active
            and role.code == "customer_owner"
            and role.is_active
        ):
            raise CustomerFlowPermissionError(
                "当前微信保留有其他或已停用的历史身份，不能直接接受设备邀请；请联系管理员核实"
            )

        source_root = invitation.source_organization.get_factory_root()
        customer_root = organization.get_factory_root()
        if not source_root or not customer_root or source_root.pk != customer_root.pk:
            raise CustomerFlowPermissionError(
                "当前用户档案不属于该设备的厂家链路，不能接受此邀请"
            )

        identity.is_active = True
        identity.full_clean()
        identity.save(update_fields=("is_active", "updated_at"))
        return identity

    @staticmethod
    def _ensure_sales_relation(*, source_organization, customer_organization):
        """记录每台设备真实来源；终端用户可从多个商家购买设备。"""
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
        existing_customer = bool(identity)
        if identity is None:
            identity = cls._restore_inactive_customer_identity(
                user=user,
                invitation=invitation,
            )
            existing_customer = bool(identity)
        profile_data = dict(customer_data)
        if identity:
            if not (
                identity.organization.organization_type.code == "CUSTOMER"
                and identity.role.code == "customer_owner"
            ):
                raise CustomerFlowPermissionError(
                    "当前微信已有其他业务岗位，请使用独立微信账号接受"
                )
            existing_profile = cls._customer_profile(identity=identity, user=user)
            # 已有用户再次扫码只是在同一用户档案下添加设备，不能借此改变
            # 个人/车队类型或另建用户组织。车辆编号仍按设备单独保存。
            profile_data["customer_type"] = existing_profile["customer_type"]
            profile_data["customer_name"] = identity.organization.name
        profile_data["target_role"] = invitation.target_role
        profile_data["inviter"] = invitation.inviter
        customer_org, identity = cls._customer_organization(
            user=user,
            identity=identity,
            data=profile_data,
        )
        cls._ensure_sales_relation(
            source_organization=invitation.source_organization,
            customer_organization=customer_org,
        )
        vehicle = cls._vehicle(
            organization=customer_org,
            vehicle_no=profile_data["vehicle_no"],
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
                "vehicle_no": profile_data["vehicle_no"],
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
            "customer_type": profile_data["customer_type"],
            "customer_name": customer_org.name,
            "contact_name": profile_data["contact_name"],
            "phone": profile_data["phone"],
            "region": profile_data["region"],
            "address": profile_data.get("address", ""),
            "vehicle_no": profile_data["vehicle_no"],
        }
        invitation.invitee = user
        invitation.target_organization = customer_org
        invitation.customer_data = stored_data
        invitation.status = InstallationInvitation.Status.ACCEPTED
        invitation.accepted_at = now
        invitation.save()

        customer_binding, _ = CustomerDeviceBinding.objects.update_or_create(
            device=device,
            defaults={
                "customer_organization": customer_org,
                "source_organization": invitation.source_organization,
                "invitation": invitation,
                "accepted_by": user,
                "acceptance_mode": (
                    CustomerDeviceBinding.AcceptanceMode.EXISTING_CUSTOMER
                    if existing_customer
                    else CustomerDeviceBinding.AcceptanceMode.NEW_CUSTOMER
                ),
                "customer_type": stored_data["customer_type"],
                "vehicle_no": stored_data["vehicle_no"],
                "vehicle": vehicle,
                "accepted_at": now,
                "status": CustomerDeviceBinding.Status.ACTIVE,
            },
        )
        cls._assign_device_vehicle(
            binding=customer_binding,
            vehicle=vehicle,
            user=user,
            reason=VehicleDeviceAssignment.Reason.INITIAL_INSTALL,
            at=now,
        )

        if user.phone != profile_data["phone"]:
            user.phone = profile_data["phone"]
            user.save(update_fields=("phone",))

        return invitation, identity

    @staticmethod
    def _customer_identity(user):
        identity = get_active_identity(user)
        if not identity:
            raise CustomerFlowPermissionError("当前账号还不是用户负责人")
        if not (
            identity.organization.organization_type.code == "CUSTOMER"
            and identity.role.code in {"customer_owner", "driver"}
        ):
            raise CustomerFlowPermissionError("当前岗位不能访问用户设备页面")
        return identity

    @staticmethod
    def _accepted_invitation(*, device, user, identity):
        binding = CustomerDeviceBinding.objects.filter(
            device=device, customer_organization=identity.organization,
            status=CustomerDeviceBinding.Status.ACTIVE,
        ).select_related("invitation").first()
        return binding.invitation if binding else None

    @classmethod
    def _owned_device_context(cls, *, device_id, user):
        identity = cls._customer_identity(user)
        device = (
            Device.objects.select_related(
                "product_model",
                "product_model__category",
                "state",
                "batch",
                "batch__manufacturer",
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
        if identity.role.code == "driver" and not DriverVehicleBinding.objects.filter(
            driver=user, is_active=True,
            vehicle__device_bindings__device=device,
            vehicle__device_bindings__status=CustomerDeviceBinding.Status.ACTIVE,
        ).exists():
            raise CustomerFlowPermissionError("该设备不属于当前司机获授权车辆")
        invitation = cls._accepted_invitation(
            device=device,
            user=user,
            identity=identity,
        )
        return identity, device, invitation

    @staticmethod
    def _customer_type(invitation, binding=None):
        if binding:
            return binding.customer_type
        return str(
            (invitation.customer_data if invitation else {}).get("customer_type")
            or "PERSONAL"
        ).upper()

    @staticmethod
    def _event_payload(event):
        return {
            "id": event.id,
            "event_type": event.event_type,
            "event_type_name": dict(LifecycleEvent.EVENT_TYPE_CHOICES).get(
                event.event_type, event.event_type
            ),
            "created_at": event.created_at.isoformat(),
            "operator": (
                event.operator.wx_nickname or event.operator.username
                if event.operator
                else "系统"
            ),
            "organization": event.organization.name if event.organization else "",
            "from_organization": (
                event.from_organization.name if event.from_organization else ""
            ),
            "to_organization": (
                event.to_organization.name if event.to_organization else ""
            ),
            "data": event.data or {},
        }

    @classmethod
    def device_list(cls, *, user):
        identity = cls._customer_identity(user)
        records = list(
            OrganizationDevice.objects.filter(
                organization=identity.organization,
            )
            .select_related(
                "device__product_model__category",
                "device__batch__manufacturer",
                "device__batch__bom",
                "device__state",
                "device__qrcode",
            )
            .order_by("-updated_at", "-id")
        )
        bindings = {
            binding.device_id: binding
            for binding in CustomerDeviceBinding.objects.filter(
                customer_organization=identity.organization,
                device_id__in=[record.device_id for record in records],
                status=CustomerDeviceBinding.Status.ACTIVE,
            ).select_related("source_organization", "vehicle")
        }
        allowed_vehicle_ids = None
        if identity.role.code == "driver":
            allowed_vehicle_ids = set(DriverVehicleBinding.objects.filter(
                driver=user, is_active=True,
            ).values_list("vehicle_id", flat=True))
        devices = []
        for record in records:
            device = record.device
            binding = bindings.get(device.id)
            try:
                lifecycle_code = device.qrcode.code
            except ObjectDoesNotExist:
                lifecycle_code = ""
            if allowed_vehicle_ids is not None and (
                not binding or binding.vehicle_id not in allowed_vehicle_ids
            ):
                continue
            devices.append(
                {
                    "id": device.id,
                    "sn": device.sn,
                    "lifecycle_code": lifecycle_code,
                    "physical_device_code": device.physical_device_code or "",
                    "product_model": device.product_model.name,
                    "product_model_code": device.product_model.code,
                    "category": device.product_model.category.name,
                    "manufacturer": device.batch.manufacturer.name,
                    "production_batch": device.batch.batch_no,
                    "bom_version": device.batch.bom.version,
                    "organization_status": record.status,
                    "install_status": device.state.install_status,
                    "activation_status": device.state.activation_status,
                    "vehicle_no": binding.vehicle_no if binding else "",
                    "vehicle_id": binding.vehicle_id if binding else None,
                    "source_organization": (
                        binding.source_organization.name if binding else ""
                    ),
                    "accepted_at": (
                        binding.accepted_at.isoformat() if binding else ""
                    ),
                }
            )
        vehicle_groups = []
        by_vehicle = {}
        for item in devices:
            key = item["vehicle_id"] or f"legacy-{item['vehicle_no']}"
            group = by_vehicle.setdefault(key, {
                "id": item["vehicle_id"],
                "vehicle_no": item["vehicle_no"] or "未登记车辆",
                "devices": [],
            })
            group["devices"].append(item)
        for group in by_vehicle.values():
            group["device_count"] = len(group["devices"])
            group["can_share_driver"] = bool(
                identity.role.code == "customer_owner" and group["id"]
            )
            vehicle_groups.append(group)
        active_driver_bindings = DriverVehicleBinding.objects.filter(
            vehicle_id__in=[group["id"] for group in vehicle_groups if group["id"]],
            is_active=True,
        ).select_related("driver").order_by("created_at", "id")
        drivers_by_vehicle = {}
        for driver_binding in active_driver_bindings:
            drivers_by_vehicle.setdefault(driver_binding.vehicle_id, []).append({
                "user_id": driver_binding.driver_id,
                "nickname": (
                    driver_binding.driver.get_full_name()
                    or driver_binding.driver.wx_nickname
                    or "司机"
                ),
                "shared_at": driver_binding.created_at.isoformat(),
            })
        for group in vehicle_groups:
            shared_drivers = drivers_by_vehicle.get(group["id"], [])
            names = [item["nickname"] for item in shared_drivers]
            group["shared_drivers"] = shared_drivers
            group["shared_driver_count"] = len(shared_drivers)
            group["shared_driver_names"] = names
            group["sharing_text"] = (
                f"该车辆绑定设备已与{'、'.join(names)}共享" if names else ""
            )
        return {
            "customer": {
                "organization_id": identity.organization_id,
                "organization_name": identity.organization.name,
                "region": identity.organization.region,
                "role": identity.role.code,
                "role_name": identity.role.name,
            },
            "total": len(devices),
            "activated_count": sum(
                item["activation_status"] == "ACTIVATED" for item in devices
            ),
            "pending_activation_count": sum(
                item["activation_status"] != "ACTIVATED" for item in devices
            ),
            "devices": devices,
            "vehicles": vehicle_groups,
        }

    @classmethod
    @transaction.atomic
    def revoke_driver_share(cls, *, vehicle_id, driver_id, user):
        identity = cls._customer_identity(user)
        if identity.role.code != "customer_owner":
            raise CustomerFlowPermissionError("只有用户负责人可以取消车辆分享")
        bindings = DriverVehicleBinding.objects.filter(
            vehicle_id=vehicle_id, vehicle__customer_organization_id=identity.organization_id,
            driver_id=driver_id,
        )
        if not bindings.exists():
            raise CustomerFlowStateError("未找到本组织该车辆的司机分享")
        # Match acceptance's lock order for this driver. Old unclaimed links
        # are general vehicle invitations and could otherwise restore access.
        User.objects.select_for_update().get(pk=driver_id)
        if not bindings.filter(is_active=True).exists():
            return {"vehicle_id": vehicle_id, "driver_id": driver_id, "changed": False}
        now = timezone.now()
        DriverVehicleInvitation.objects.filter(
            vehicle_id=vehicle_id, status=DriverVehicleInvitation.Status.PENDING,
        ).update(status=DriverVehicleInvitation.Status.CANCELLED, updated_at=now)
        changed = bindings.filter(is_active=True).update(is_active=False, updated_at=now)
        return {"vehicle_id": vehicle_id, "driver_id": driver_id, "changed": bool(changed)}

    @classmethod
    def create_driver_invitation(cls, *, vehicle_id, user):
        identity = cls._customer_identity(user)
        if identity.role.code != "customer_owner":
            raise CustomerFlowPermissionError("只有用户负责人可以分享车辆")
        vehicle = CustomerVehicle.objects.filter(
            id=vehicle_id, customer_organization=identity.organization,
            status=CustomerVehicle.Status.ACTIVE,
        ).first()
        if not vehicle:
            raise CustomerFlowStateError("车辆不存在")
        now = timezone.now()
        invitation = DriverVehicleInvitation.objects.filter(
            vehicle=vehicle, inviter=user,
            status=DriverVehicleInvitation.Status.PENDING,
            expires_at__gt=now,
        ).order_by("-id").first()
        if not invitation:
            invitation = DriverVehicleInvitation.objects.create(
                vehicle=vehicle, inviter=user, expires_at=now + timedelta(days=7)
            )
        return cls._driver_invitation_payload(invitation, user=user)

    @classmethod
    def _driver_invitation_eligibility(cls, invitation, *, user, lock_identity=False):
        """Read the historical slot separately from its current permission.

        An inactive slot still owns the account's one-to-one identity. Driver
        sharing never restores or moves that slot. Preview and POST deliberately
        use this same decision, including accepted-invitation replays.
        """
        identities = UserOrganization.objects.select_related(
            "user", "organization__organization_type",
            "organization__factory_root", "role",
        )
        if lock_identity:
            identities = identities.select_for_update(of=("self",))
        identity = identities.filter(user_id=user.pk).first()
        accepted_by_me = bool(
            invitation.status == DriverVehicleInvitation.Status.ACCEPTED
            and invitation.invitee_id == user.pk
        )
        decision = {
            "identity": identity, "driver_role": None,
            "accepted_by_me": accepted_by_me,
            "acceptance_allowed": False, "current_access_allowed": False,
            "blocked_reason": "", "error_class": CustomerFlowStateError,
        }

        def blocked(reason, error_class=CustomerFlowPermissionError):
            decision.update(blocked_reason=reason, error_class=error_class)
            return decision

        if invitation.inviter_id == user.pk:
            return blocked("分享人不能接受自己的司机邀请")
        if invitation.status == DriverVehicleInvitation.Status.ACCEPTED:
            if not accepted_by_me:
                return blocked("该邀请已由其他账号接受", CustomerFlowStateError)
        elif invitation.status != DriverVehicleInvitation.Status.PENDING:
            return blocked("司机邀请已失效", CustomerFlowStateError)
        elif invitation.expires_at <= timezone.now():
            return blocked("司机邀请已过期", CustomerFlowStateError)

        organization = invitation.vehicle.customer_organization
        root = organization.get_factory_root()
        if not (organization.is_active and organization.organization_type.is_active
                and root and root.is_active):
            return blocked("分享车辆所属组织当前不可用，请联系用户负责人")
        if invitation.vehicle.status != CustomerVehicle.Status.ACTIVE:
            return blocked("分享车辆已失效，请联系用户负责人")
        if not user.is_active or (identity and not identity.user.is_active):
            return blocked("当前账号已停用，请联系管理员")

        if identity:
            # None from the effective-identity getter means unusable, never that
            # this account has no historical slot and may create another one.
            active_identity = get_active_identity(user)
            if not active_identity or active_identity.pk != identity.pk:
                return blocked("当前账号的组织身份已停用或不可用，请联系管理员")
            if not (identity.organization_id == organization.pk
                    and identity.role.code == "driver"
                    and identity.role.organization_type_id == organization.organization_type_id):
                return blocked("当前微信已有其他业务岗位，请使用独立微信账号接受")
        elif accepted_by_me:
            return blocked("当前司机身份不可用，请联系管理员")
        else:
            decision["driver_role"] = OrganizationRole.objects.filter(
                organization_type_id=organization.organization_type_id,
                code="driver", is_active=True,
            ).first()
            if not decision["driver_role"]:
                return blocked("司机岗位当前不可用，请联系管理员")

        if accepted_by_me:
            if not DriverVehicleBinding.objects.filter(
                vehicle_id=invitation.vehicle_id, driver_id=user.pk,
                is_active=True,
            ).exists():
                return blocked("当前车辆共享授权已停用，请联系用户负责人重新分享")
            decision["current_access_allowed"] = True
        else:
            decision["acceptance_allowed"] = True
        return decision

    @classmethod
    def _driver_invitation_payload(cls, invitation, *, user, eligibility=None):
        devices = list(CustomerDeviceBinding.objects.filter(
            vehicle=invitation.vehicle,
            status=CustomerDeviceBinding.Status.ACTIVE,
        ).select_related("device__product_model", "device__state"))
        eligibility = eligibility or cls._driver_invitation_eligibility(invitation, user=user)
        root = invitation.vehicle.customer_organization.get_factory_root()
        return {
            "token": str(invitation.token), "status": invitation.status,
            "root_organization": {"id": root.pk, "name": root.name} if root else None,
            "expires_at": invitation.expires_at.isoformat(),
            "vehicle": {"id": invitation.vehicle_id,
                        "vehicle_no": invitation.vehicle.vehicle_no,
                        "device_count": len(devices),
                        "devices": [{"id": x.device_id, "sn": x.device.sn,
                                     "product_model": x.device.product_model.name,
                                     "activation_status": x.device.state.activation_status}
                                    for x in devices]},
            "owner": {
                "organization_name": invitation.vehicle.customer_organization.name,
                "nickname": invitation.inviter.wx_nickname or invitation.inviter.username,
            },
            "accepted_by_me": eligibility["accepted_by_me"],
            "acceptance_allowed": eligibility["acceptance_allowed"],
            "current_access_allowed": eligibility["current_access_allowed"],
            "blocked_reason": eligibility["blocked_reason"],
        }

    @classmethod
    def driver_invitation_detail(cls, *, token, user):
        invitation = DriverVehicleInvitation.objects.select_related(
            "vehicle__customer_organization__organization_type",
            "vehicle__customer_organization__factory_root", "inviter", "invitee"
        ).filter(token=token).first()
        if not invitation:
            raise CustomerFlowStateError("司机邀请不存在")
        if invitation.status == DriverVehicleInvitation.Status.PENDING and invitation.expires_at <= timezone.now():
            # A preview must not overwrite a concurrently accepted invitation.
            DriverVehicleInvitation.objects.filter(
                pk=invitation.pk, status=DriverVehicleInvitation.Status.PENDING,
                expires_at__lte=timezone.now(),
            ).update(status=DriverVehicleInvitation.Status.EXPIRED, updated_at=timezone.now())
            invitation.refresh_from_db()
        if invitation.status == DriverVehicleInvitation.Status.ACCEPTED and invitation.invitee_id != user.id:
            raise CustomerFlowStateError("该邀请已由其他账号接受")
        if invitation.status not in {DriverVehicleInvitation.Status.PENDING,
                                     DriverVehicleInvitation.Status.ACCEPTED}:
            raise CustomerFlowStateError("司机邀请已失效")
        return cls._driver_invitation_payload(invitation, user=user)

    @classmethod
    def accept_driver_invitation(cls, *, token, user):
        try:
            with transaction.atomic():
                return cls._accept_driver_invitation_locked(token=token, user=user)
        except IntegrityError as exc:
            # The one-to-one identity and unique vehicle/driver pair are the
            # final concurrency fence. Querying in the failed atomic block
            # would itself fail; the complete claim has already rolled back.
            raise CustomerFlowStateError("账号身份或车辆授权已变化，请刷新后重试") from exc
        except OperationalError as exc:
            # SQLite has no row locks: concurrent writers may instead receive
            # a database/table lock error. Return a retryable conflict only for
            # that condition, after the transaction has rolled back.
            if "locked" not in str(exc).lower():
                raise
            raise CustomerFlowStateError("车辆分享正在被处理，请刷新后重试") from exc

    @classmethod
    def _accept_driver_invitation_locked(cls, *, token, user):
        # Serialize one account accepting different organizations' invitations
        # on databases with row locks, even when no identity row exists yet.
        user = User.objects.select_for_update().get(pk=user.pk)
        invitation = DriverVehicleInvitation.objects.select_for_update(of=("self",)).select_related(
            "vehicle__customer_organization__organization_type",
            "vehicle__customer_organization__factory_root", "inviter"
        ).filter(token=token).first()
        if not invitation:
            raise CustomerFlowStateError("司机邀请不存在")
        eligibility = cls._driver_invitation_eligibility(
            invitation, user=user, lock_identity=True,
        )
        if eligibility["blocked_reason"]:
            raise eligibility["error_class"](eligibility["blocked_reason"])
        if eligibility["accepted_by_me"]:
            # This is a read-only replay. Never re-enable a vehicle, identity or
            # revoked binding merely because this user accepted in the past.
            return cls._driver_invitation_payload(invitation, user=user, eligibility=eligibility)

        now = timezone.now()
        claimed = DriverVehicleInvitation.objects.filter(
            pk=invitation.pk, status=DriverVehicleInvitation.Status.PENDING,
            invitee__isnull=True, expires_at__gt=now,
            vehicle__status=CustomerVehicle.Status.ACTIVE,
        ).update(invitee_id=user.pk, status=DriverVehicleInvitation.Status.ACCEPTED,
                 accepted_at=now, updated_at=now)
        if claimed != 1:
            raise CustomerFlowStateError("司机邀请状态已变化，请刷新后重试")
        identity = eligibility["identity"]
        if not identity:
            identity = UserOrganization.objects.create(
                user=user, organization=invitation.vehicle.customer_organization,
                role=eligibility["driver_role"], inviter=invitation.inviter,
            )
        DriverVehicleBinding.objects.update_or_create(
            vehicle=invitation.vehicle, driver=user,
            defaults={"invitation": invitation, "is_active": True},
        )
        invitation.invitee = user
        invitation.status = DriverVehicleInvitation.Status.ACCEPTED
        invitation.accepted_at = now
        return cls._driver_invitation_payload(invitation, user=user)

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
        # Same-user, same-version reading facts remain valid across replacement;
        # do not fabricate new reads or bypass changed document versions.
        from lifecycle.models import ReplacementFlow
        reading_devices=[device.pk]
        cursor=device.pk
        for _ in range(50):
            predecessor=ReplacementFlow.objects.filter(application__replacement_device_id=cursor,swapped_at__isnull=False).select_related('application__device').first()
            if not predecessor or predecessor.application.device.product_model_id!=device.product_model_id:
                break
            cursor=predecessor.application.device_id
            if cursor in reading_devices:break
            reading_devices.append(cursor)
        read_versions = set(
            ProductDocumentRead.objects.filter(
                user=user,
                device_id__in=reading_devices,
                document_id__in=[document.id for document in documents],
            ).values_list("document_id", "document_version")
        )
        documents_complete = len(documents) == 3
        all_read = documents_complete and current_versions.issubset(read_versions)
        state = device.state
        binding = (
            CustomerDeviceBinding.objects.filter(
                device=device,
                customer_organization=device.state.current_organization,
                status=CustomerDeviceBinding.Status.ACTIVE,
            )
            .order_by("-accepted_at", "-id")
            .first()
        )
        customer_type = cls._customer_type(invitation, binding)
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
            "root_organization": {"id": device.batch.manufacturer_id, "name": device.batch.manufacturer.name},
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

    @staticmethod
    def is_activated(device):
        state = getattr(device, "state", None)
        return state is not None and state.activation_status == "ACTIVATED"

    @classmethod
    def device_result(cls, *, device_id, user):
        identity, device, invitation = cls._owned_device_context(
            device_id=device_id,
            user=user,
        )
        binding = CustomerDeviceBinding.objects.filter(
            device=device,
            customer_organization=identity.organization,
            status=CustomerDeviceBinding.Status.ACTIVE,
        ).select_related("source_organization").first()
        profile = cls._customer_profile(identity=identity, user=user)
        if binding:
            profile["vehicle_no"] = binding.vehicle_no
        is_activated = cls.is_activated(device)
        activation_record = getattr(device, "activation", None)
        from lifecycle.models import ReplacementFlow
        replacement = ReplacementFlow.objects.filter(application__replacement_device=device,swapped_at__isnull=False).first()
        product_model = device.product_model
        batch = device.batch
        events = list(
            LifecycleEvent.objects.filter(device=device)
            .select_related(
                "operator",
                "organization",
                "from_organization",
                "to_organization",
            )
            .order_by("-created_at", "-id")
        )
        scan_result = ScanService.resolve_device(device=device, user=user)
        capabilities = list(scan_result.get("capabilities") or [])
        # 司机对已分享车辆中的设备与用户负责人具有相同的设备操作权限。
        # ScanService 的通用角色能力不包含司机激活，因此在已经通过
        # _owned_device_context 严格验证车辆分享关系后补充该能力。
        if identity.role.code == "driver" and "activate_device" not in capabilities:
            capabilities.append("activate_device")
            scan_result["capabilities"] = capabilities
        return {
            "scan_result": scan_result,
            "asset": {
                "device_id": device.id,
                "sn": device.sn,
                "lifecycle_code": scan_result.get("qr_code", ""),
                "physical_device_code": device.physical_device_code or "",
                "product_model_id": product_model.id,
                "product_model": product_model.name,
                "product_model_code": product_model.code,
                "category": product_model.category.name,
                "description": product_model.description,
                "source_organization": (
                    binding.source_organization.name if binding else ""
                ),
            },
            "production": {
                "batch_no": batch.batch_no,
                "production_date": batch.production_date.isoformat(),
                "manufacturer": batch.manufacturer.name,
                "bom_id": batch.bom_id,
                "bom_version": batch.bom.version,
                "bom_remark": batch.bom.remark,
            },
            "parameters": [
                {"label": "型号编码", "value": product_model.code},
                {"label": "产品品类", "value": product_model.category.name},
                {"label": "生产批次", "value": batch.batch_no},
                {"label": "BOM版本", "value": batch.bom.version},
            ],
            "lifecycle": [cls._event_payload(event) for event in events],
            "activation": {
                "required_activation_method": 'DEVICE_CODE' if replacement and not activation_record else '',
                "inherited_first_activation_at": replacement.first_activation_at.isoformat() if replacement else '',
                "activation_method": (
                    activation_record.activation_method if activation_record else (replacement.activation_method if replacement else "")
                ),
                "activated_at": (
                    activation_record.activation_time.isoformat()
                    if activation_record else ""
                ),
                "vehicle_no": activation_record.vehicle_no if activation_record else "",
                "physical_device_code": (
                    activation_record.physical_device_code_snapshot
                    if activation_record else ""
                ),
            },
            "customer": {
                "organization_id": identity.organization_id,
                "organization_name": identity.organization.name,
                "region": identity.organization.region,
                "role": identity.role.code,
                "role_name": identity.role.name,
                "customer_type": cls._customer_type(invitation, binding),
                "profile": profile,
            },
            "actions": {
                "can_activate": not is_activated,
                "can_apply_aftersale": is_activated,
            },
        }

    @staticmethod
    def normalize_physical_device_code(value):
        try:
            return normalize_device_code(value)
        except PhysicalDeviceCodeError as exc:
            raise CustomerFlowStateError(str(exc)) from exc

    @classmethod
    def verify_activation_device_code(cls, *, device_id, user, device_code):
        identity, device, _invitation = cls._owned_device_context(
            device_id=device_id,
            user=user,
        )
        device = Device.objects.select_for_update().select_related(
            "batch__manufacturer"
        ).get(pk=device.pk)
        state = DeviceState.objects.select_for_update().get(device=device)
        owner_record = OrganizationDevice.objects.select_for_update().get(
            device=device
        )
        if owner_record.organization_id != identity.organization_id:
            raise CustomerFlowPermissionError("该设备不属于当前用户组织")
        if state.install_status != "INSTALLED":
            raise CustomerFlowStateError("设备尚未完成装车")
        if state.activation_status == "ACTIVATED" or ActivationRecord.objects.filter(
            device=device
        ).exists():
            raise CustomerFlowStateError("设备已经激活，不能再绑定设备编码")
        try:
            _entry, normalized_code = verify_device_code_for_activation(
                device=device,
                raw_code=device_code,
            )
        except PhysicalDeviceCodeError as exc:
            raise CustomerFlowStateError(str(exc)) from exc
        return {
            "verified": True,
            "device_code": normalized_code,
            "status": "AVAILABLE",
            "message": "设备编码已登记且可用于当前设备激活",
        }

    @classmethod
    @quality_gate_atomic(CustomerFlowStateError)
    def activate(
        cls,
        *,
        device_id,
        user,
        vehicle_no="",
        activation_method="VEHICLE_PLATE",
        physical_device_code="",
        location="",
    ):
        # 这里必须复用详情页的属主和司机车辆授权校验，避免同一用户组织中
        # 未获得车辆分享的司机绕过页面直接调用激活接口。
        identity, _, invitation = cls._owned_device_context(
            device_id=device_id,
            user=user,
        )
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

        from lifecycle.models import ReplacementFlow
        inherited_flow = ReplacementFlow.objects.filter(application__replacement_device=device).first()
        if inherited_flow and (not inherited_flow.swapped_at or inherited_flow.activation_method != 'DEVICE_CODE'
                or str(activation_method).upper() != 'DEVICE_CODE'):
            raise CustomerFlowStateError('换新设备必须先签收，并按原设备编码方式重新扫码激活售后')
        activation_method = str(activation_method or "VEHICLE_PLATE").strip().upper()
        vehicle_no = str(vehicle_no or "").strip()
        normalized_device_code = ""
        device_code_entry = None
        if activation_method == ActivationRecord.ActivationMethod.VEHICLE_PLATE:
            if not vehicle_no:
                raise CustomerFlowStateError("请填写车辆牌号")
            if physical_device_code:
                raise CustomerFlowStateError("车牌号与设备编码只能选择一种激活方式")
        elif activation_method == ActivationRecord.ActivationMethod.DEVICE_CODE:
            if vehicle_no:
                raise CustomerFlowStateError("设备编码与车牌号只能选择一种激活方式")
            try:
                device_code_entry, normalized_device_code = (
                    verify_device_code_for_activation(
                        device=device,
                        raw_code=physical_device_code,
                    )
                )
            except PhysicalDeviceCodeError as exc:
                raise CustomerFlowStateError(str(exc)) from exc
        else:
            raise CustomerFlowStateError("激活方式无效")

        document_gate = cls._document_gate(
            device=device,
            user=user,
            invitation=invitation,
        )
        if document_gate["gate_required"] and not document_gate["activation_allowed"]:
            raise CustomerFlowStateError(document_gate["message"])

        enforce_device_operation(
            device_id=device.pk,
            operation="ACTIVATION",
            user=user,
            context={
                "source": "CUSTOMER_DEVICE_ACTIVATION",
                "customer_organization_id": identity.organization_id,
                "activation_method": activation_method,
                "vehicle_no": vehicle_no,
                "physical_device_code": normalized_device_code,
            },
        )

        now = timezone.now()
        if device_code_entry:
            try:
                bind_device_code(
                    entry=device_code_entry,
                    device=device,
                    user=user,
                    code=normalized_device_code,
                    bound_at=now,
                )
            except PhysicalDeviceCodeError as exc:
                raise CustomerFlowStateError(str(exc)) from exc
        event = LifecycleEvent.objects.create(
            device=device,
            event_type="ACTIVATE",
            operator=user,
            organization=identity.organization,
            from_organization=identity.organization,
            to_organization=identity.organization,
            data={
                "activation_method": activation_method,
                "vehicle_no": vehicle_no,
                "physical_device_code": normalized_device_code,
                "location": location or "",
            },
        )
        new_activation = ActivationRecord.objects.create(
            device=device,
            organization=identity.organization,
            operator=user,
            activation_method=activation_method,
            physical_device_code_snapshot=normalized_device_code,
            vehicle_no=vehicle_no,
            location=location or "",
            lifecycle_event=event,
        )

        if inherited_flow:
            ActivationRecord.objects.filter(pk=new_activation.pk).update(activation_time=inherited_flow.first_activation_at)
            event.data = {**event.data, 'first_activation_at':inherited_flow.first_activation_at.isoformat(),
                          'replacement_application_id':inherited_flow.application_id, 'actual_reactivation_at':now.isoformat()}
            event.save(update_fields=['data'])
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
            data["activation_method"] = activation_method
            if activation_method == ActivationRecord.ActivationMethod.VEHICLE_PLATE:
                data["vehicle_no"] = vehicle_no
            invitation.customer_data = data
            invitation.save(update_fields=("customer_data", "updated_at"))

        binding = CustomerDeviceBinding.objects.select_for_update().filter(
            device=device,
            customer_organization=identity.organization,
            status=CustomerDeviceBinding.Status.ACTIVE,
        ).first()
        if binding and activation_method == ActivationRecord.ActivationMethod.VEHICLE_PLATE:
            vehicle = cls._vehicle(
                organization=identity.organization, vehicle_no=vehicle_no,
            )
            binding.vehicle_no = vehicle_no
            binding.vehicle = vehicle
            binding.save(update_fields=("vehicle_no", "vehicle", "updated_at"))
            cls._assign_device_vehicle(
                binding=binding,
                vehicle=vehicle,
                user=user,
                reason=VehicleDeviceAssignment.Reason.ACTIVATION_UPDATE,
                at=now,
            )

        return cls.device_result(device_id=device.id, user=user)
