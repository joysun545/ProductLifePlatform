"""Current recall access is based on business identities, never frozen OpenIDs.

The launch snapshot remains an audit record. A new occupant of a role inherits
its organization's scope, while moving a WeChat account cannot move that scope.
"""
from functools import cached_property

from django.db.models import Q
from account.models import OrganizationRelation
from account.services.auth_service import get_active_identity
from account.services.role_delegation_service import (
    SENIOR_MANAGER_ROLE_CODES,
    effective_role_codes,
)
from lifecycle.models import CustomerDeviceBinding, DriverVehicleBinding
from quality_trace.models import QualityRecallCampaign
from workflow.models import UserTask

FACTORY_FULL_RECALL_ROLES = {
    "factory_chief_engineer", "factory_sales_assistant", "factory_logistics",
}
FACTORY_RECALL_OBSERVER_ROLES = {"factory_admin", "factory_manager"}
FACTORY_SCOPED_RECALL_ROLES = {"factory_sales", "factory_production", "factory_matching"}
FACTORY_RECALL_VIEW_ROLES = FACTORY_FULL_RECALL_ROLES | FACTORY_SCOPED_RECALL_ROLES | FACTORY_RECALL_OBSERVER_ROLES
MERCHANT_RECALL_ROLES = {
    "merchant_owner", "merchant_manager", "merchant_senior_manager",
    "merchant_sales", "merchant_stock",
}
CUSTOMER_RECALL_ROLES = {"customer_owner", "driver"}
SUPPLIER_RECALL_ROLES = {"supplier_owner", "supplier_sales"}


class RecallViewer:
    def __init__(self, user):
        self.user = user
        self.identity = get_active_identity(user) if user and user.is_active else None
        self.organization_id = self.identity.organization_id if self.identity else None
        self.role = self.identity.role.code if self.identity else ""
        self.roles = effective_role_codes(self.identity)
        self.kind = self.identity.organization.organization_type.code if self.identity else ""

    def has_role(self, role_codes):
        if isinstance(role_codes, str):
            role_codes = {role_codes}
        return bool(self.roles & set(role_codes or ()))

    @cached_property
    def open_action_task_keys(self):
        if not self.identity or self.role not in SENIOR_MANAGER_ROLE_CODES:
            return set()
        openid = (self.user.wx_openid or "").strip()
        if not openid:
            return set()
        return set(UserTask.objects.filter(
            to_openid=openid,
            workitem__status="OPEN",
            need_action=True,
            state__in=("NEW", "READ"),
        ).values_list("workitem__uniq_key", flat=True))

    def has_action_task_copy(self, uniq_keys):
        if isinstance(uniq_keys, str):
            uniq_keys = {uniq_keys}
        keys = {str(value) for value in (uniq_keys or ())}
        if not self.identity or not keys:
            return False
        if self.role not in SENIOR_MANAGER_ROLE_CODES:
            return True
        return bool(self.open_action_task_keys & keys)

    @property
    def valid(self):
        return bool(self.identity and self.identity.is_active
                    and self.identity.role.is_active and self.identity.organization.is_active)

    def factory(self, campaign):
        return bool(self.valid and self.kind == "OWNER"
                    and self.organization_id == campaign.factory_organization_id
                    and bool(self.roles & (FACTORY_FULL_RECALL_ROLES | FACTORY_SCOPED_RECALL_ROLES)))

    def full_campaign(self, campaign):
        return self.factory(campaign) and bool(self.roles & FACTORY_FULL_RECALL_ROLES)

    def observer(self, campaign):
        return bool(self.valid and self.kind == "OWNER"
                    and self.organization_id == campaign.factory_organization_id
                    and bool(self.roles & FACTORY_RECALL_OBSERVER_ROLES))

    def metadata_access(self, campaign):
        return self.full_campaign(campaign) or self.observer(campaign)

    @cached_property
    def sales_merchant_ids(self):
        # Current channel ownership belongs to the stable account/role slot,
        # not a WeChat identifier or a campaign-wide notification recipient.
        from account.services.role_delegation_service import delegated_role_user_ids

        sales_user_ids = (
            delegated_role_user_ids(self.identity, "factory_sales")
            if self.role in {"factory_senior_manager", "merchant_senior_manager"}
            else {self.user.pk}
        )
        if not sales_user_ids:
            return set()
        relations = OrganizationRelation.objects.filter(
            is_active=True, relation_type="DISTRIBUTION",
            target_organization__organization_type__code="MERCHANT",
            target_organization__is_active=True,
        )
        direct = relations.filter(source_organization_id=self.organization_id).filter(
            Q(merchant_channel_assignment__assigned_by_id__in=sales_user_ids,
              merchant_channel_assignment__is_active=True)
            | Q(merchant_channel_assignment__isnull=True,
                created_by_invitation__inviter_id__in=sales_user_ids,
                created_by_invitation__inviter_role__code="factory_sales",
                created_by_invitation__status="accepted")
        )
        owned = set(direct.values_list("target_organization_id", flat=True))
        edges = list(relations.values_list("source_organization_id", "target_organization_id"))
        frontier = set(owned)
        while frontier:
            frontier = {child for parent, child in edges if parent in frontier} - owned
            owned.update(frontier)
        return owned

    def factory_row(self, row):
        if self.full_campaign(row.campaign):
            return True
        if not self.factory(row.campaign):
            return False
        if "factory_sales" in self.roles:
            groups = (row.responsibility_snapshot or {}).get("groups", [])
            merchant_ids = {str(value) for value in self.sales_merchant_ids}
            return any(group.get("organization_type") == "MERCHANT"
                       and str(group.get("organization_id")) in merchant_ids for group in groups)
        disposition = getattr(row, "post_disposition", None)
        if not disposition:
            return False
        if "factory_production" in self.roles:
            return True  # Every issued disposition has a production execution node.
        if "factory_matching" in self.roles:
            return (disposition.method == "REPAIR_WITH_PARTS"
                    or (disposition.method == "DISASSEMBLE" and disposition.status == "COMPLETED"))
        return False

    @cached_property
    def driver_device_ids(self):
        # Mutations use the same active vehicle/device association. Historical
        # notification recipients do not authorize an unrelated/new driver.
        vehicles = DriverVehicleBinding.objects.filter(
            driver=self.user, is_active=True,
        ).values_list("vehicle_id", flat=True)
        return set(CustomerDeviceBinding.objects.filter(
            customer_organization_id=self.organization_id, vehicle_id__in=vehicles,
            status=CustomerDeviceBinding.Status.ACTIVE,
        ).values_list("device_id", flat=True))

    def customer_actor(self, row):
        handover = getattr(row, "customer_handover", None)
        return bool(self.valid and self.kind == "CUSTOMER" and handover
                    and self.organization_id == handover.customer_organization_id
                    and self.role in CUSTOMER_RECALL_ROLES
                    and (self.role != "driver" or row.device_id in self.driver_device_ids))

    def allows(self, row):
        if not self.valid:
            return False
        if self.kind == "OWNER":
            return self.factory_row(row)
        if self.kind == "CUSTOMER":
            if self.customer_actor(row):
                return True
            # Closing a repaired/disassembled lifecycle inactivates its device
            # binding. Keep the original driver's history readable while that
            # driver still belongs to the original vehicle; never use OpenID.
            handover = getattr(row, "customer_handover", None)
            if (self.role != "driver" or not handover
                    or handover.customer_organization_id != self.organization_id
                    or row.status != "DISPOSED"):
                return False
            groups = (row.responsibility_snapshot or {}).get("groups", [])
            original_driver = any(
                str(group.get("organization_id")) == str(self.organization_id)
                and group.get("organization_type") == "CUSTOMER"
                and any(recipient.get("user_id") == self.user.pk and recipient.get("role") == "driver"
                        for recipient in group.get("recipients", [])) for group in groups)
            return bool(original_driver and CustomerDeviceBinding.objects.filter(
                device_id=row.device_id, customer_organization_id=self.organization_id,
                created_at__lte=row.included_at,
                vehicle_id__in=DriverVehicleBinding.objects.filter(
                    driver=self.user, is_active=True,
                ).values_list("vehicle_id", flat=True),
            ).exists())
        if self.kind == "MERCHANT" and bool(self.roles & MERCHANT_RECALL_ROLES):
            groups = (row.responsibility_snapshot or {}).get("groups", [])
            if any(str(group.get("organization_id")) == str(self.organization_id)
                   and group.get("organization_type") == "MERCHANT"
                   for group in groups):
                return True
            handover = getattr(row, "customer_handover", None)
            if handover and handover.merchant_organization_id == self.organization_id:
                return True
            return any(self.organization_id in (leg.from_organization_id, leg.to_organization_id)
                       for leg in row.transport_legs.all() if leg.status != "CANCELLED")
        if self.kind == "SUPPLIER" and self.role in SUPPLIER_RECALL_ROLES:
            disposition = getattr(row, "post_disposition", None)
            if not disposition:
                return False
            if any(item.supplier_id == self.organization_id
                   for item in disposition.supplier_confirmations.all()):
                return True
            # Disassembly sends a notice, without a supplier confirmation row.
            return bool(disposition.method == "DISASSEMBLE"
                        and disposition.status == "COMPLETED"
                        and row.device.batch.bom.items.filter(supplier_id=self.organization_id).exists())
        return False

    @property
    def payload(self):
        return {
            "organization_id": self.organization_id,
            "organization_name": self.identity.organization.name if self.identity else "",
            "organization_type": self.kind, "role": self.role,
            "role_name": self.identity.role.name if self.identity else "",
        }


def recall_campaign_queryset():
    return QualityRecallCampaign.objects.select_related(
        "factory_organization", "quality_case", "superseded_by",
    ).prefetch_related(
        "devices__customer_handover__merchant_organization",
        "devices__customer_handover__evidence",
        "devices__transport_legs__from_organization__organization_type",
        "devices__transport_legs__to_organization__organization_type",
        "devices__isolation_receipt", "devices__post_disposition__supplier_confirmations",
        "devices__device__batch__bom",
    )


def visible_rows(campaign, viewer):
    rows = list(campaign.devices.all())
    # Django's prefetched reverse manager already has the parent cached.
    return [row for row in rows if viewer.allows(row)]
