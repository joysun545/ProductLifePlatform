from collections import defaultdict

from django.db.models import Count

from account.models import OrganizationRelation, OrganizationRelationType
from lifecycle.models import ActivationRecord, CustomerDeviceBinding, Device, InstallationRecord, OrganizationDevice
from sales.models import OrderPlan, OrderPlanItem

from .query_scope import device_scope


def _empty_counts():
    return {key: 0 for key in (
        "market_inventory_device_count", "activated_device_count",
        "in_transit_device_count", "return_or_recall_device_count",
        "other_device_count", "market_device_count",
    )}


def _counts(rows):
    result = _empty_counts()
    for row in rows:
        result[f"{row['market_status'].lower()}_device_count"] += 1
        result["market_device_count"] += 1
    return result


def _market_rows(factory_id):
    """One mutually-exclusive market state for every received-plan device."""
    items = {
        row["order_batch_code"]: row
        for row in OrderPlanItem.objects.filter(
            plan__factory_organization_id=factory_id,
            plan__received_at__isnull=False,
        ).values(
            "order_batch_code", "plan_id", "plan__created_by_id",
            "plan__merchant_organization_id",
        )
    }
    if not items:
        return []
    devices = list(Device.objects.filter(
        batch__manufacturer_id=factory_id, batch__batch_no__in=items,
    ).values("id", "batch__batch_no"))
    ids = [row["id"] for row in devices]
    current = {row["device_id"]: row for row in OrganizationDevice.objects.filter(
        device_id__in=ids,
    ).values("device_id", "organization_id", "organization__organization_type__code", "status")}
    activated = set(ActivationRecord.objects.filter(
        device_id__in=ids, status="ACTIVATED",
    ).values_list("device_id", flat=True))
    sources = dict(CustomerDeviceBinding.objects.filter(
        device_id__in=activated, status=CustomerDeviceBinding.Status.ACTIVE,
    ).values_list("device_id", "source_organization_id"))
    result = []
    for device in devices:
        plan = items[device["batch__batch_no"]]
        owner = current.get(device["id"], {})
        status = owner.get("status", "")
        if device["id"] in activated:
            market_status = "ACTIVATED"
            channel_org_id = sources.get(device["id"], plan["plan__merchant_organization_id"])
        elif (
            status == "IN_STOCK"
            and owner.get("organization__organization_type__code") == "MERCHANT"
        ):
            market_status = "MARKET_INVENTORY"
            channel_org_id = owner.get("organization_id")
        elif status == "TRANSFERRED":
            market_status = "IN_TRANSIT"
            channel_org_id = owner.get("organization_id")
        elif status in {"RETURNED", "RETURNED_TO_FACTORY"}:
            market_status = "RETURN_OR_RECALL"
            channel_org_id = owner.get("organization_id")
        else:
            market_status = "OTHER"
            channel_org_id = owner.get("organization_id")
        result.append({
            "device_id": device["id"], "plan_id": plan["plan_id"],
            "sales_user_id": plan["plan__created_by_id"],
            "direct_merchant_id": plan["plan__merchant_organization_id"],
            "channel_org_id": channel_org_id, "market_status": market_status,
        })
    return result


class ResponsibilityTreeBuilder:
    def __init__(self, factory, rows):
        self.factory, self.rows = factory, rows
        self.outgoing = defaultdict(list)
        relations = OrganizationRelation.objects.filter(
            relation_type=OrganizationRelationType.DISTRIBUTION, is_active=True,
        ).select_related(
            "source_organization", "target_organization",
            "created_by_invitation__inviter__organization_identity__role",
        ).order_by("id")
        for relation in relations:
            self.outgoing[relation.source_organization_id].append(relation)

    def descendant_org_ids(self, org_id, path=None):
        path = set(path or ())
        if org_id in path:
            return {org_id}
        path.add(org_id)
        result = {org_id}
        for relation in self.outgoing.get(org_id, []):
            result |= self.descendant_org_ids(relation.target_organization_id, path)
        return result

    def rows_for_org(self, rows, org_id):
        ids = self.descendant_org_ids(org_id)
        return [row for row in rows if (
            row["channel_org_id"] in ids or row["direct_merchant_id"] == org_id
        )]

    @staticmethod
    def actor(relation):
        invitation = relation.created_by_invitation
        return invitation.inviter if invitation else None

    @staticmethod
    def actor_node(actor, rows, children):
        identity = getattr(actor, "organization_identity", None) if actor else None
        return {
            "node_type": "RESPONSIBILITY_ACCOUNT",
            "node_id": f"user:{actor.id if actor else 0}",
            "user_id": actor.id if actor else None,
            "name": (actor.wx_nickname or actor.username) if actor else "未指定责任人",
            "role_code": identity.role.code if identity else "",
            "own": _empty_counts(), "total": _counts(rows), "children": children,
        }

    def org_node(self, organization, rows, path=None):
        path = set(path or ())
        if organization.id in path:
            return None
        path.add(organization.id)
        scoped = self.rows_for_org(rows, organization.id)
        own = [row for row in scoped if row["channel_org_id"] == organization.id]
        groups = defaultdict(list)
        for relation in self.outgoing.get(organization.id, []):
            actor = self.actor(relation)
            groups[actor.id if actor else 0].append((actor, relation))
        children = []
        for relations in groups.values():
            actor, relation_rows, target_nodes = relations[0][0], [], []
            for _, relation in relations:
                target_rows = self.rows_for_org(scoped, relation.target_organization_id)
                if not target_rows:
                    continue
                relation_rows.extend(target_rows)
                child = self.org_node(relation.target_organization, target_rows, path)
                if child:
                    target_nodes.append(child)
            if target_nodes:
                children.append(self.actor_node(actor, relation_rows, target_nodes))
        return {
            "node_type": "ORGANIZATION", "node_id": f"organization:{organization.id}",
            "organization_id": organization.id, "name": organization.name, "role_code": "",
            "own": _counts(own), "total": _counts(scoped), "children": children,
        }

    def factory_root(self, identity):
        groups = defaultdict(list)
        for row in self.rows:
            groups[row["sales_user_id"]].append(row)
        from account.models import User
        users = User.objects.in_bulk(groups)
        children = []
        relation_targets = {
            relation.target_organization_id: relation.target_organization
            for relation in self.outgoing.get(self.factory.id, [])
        }
        for user_id, rows in groups.items():
            if identity.role.code == "factory_sales" and user_id != identity.user.id:
                continue
            merchants = []
            for merchant_id in sorted({row["direct_merchant_id"] for row in rows}):
                org = relation_targets.get(merchant_id)
                node = self.org_node(org, rows, {self.factory.id}) if org else None
                if node:
                    merchants.append(node)
            if merchants:
                children.append(self.actor_node(users.get(user_id), rows, merchants))
        if identity.role.code == "factory_sales":
            return children[0] if children else self.actor_node(identity.user, [], [])
        return {
            "node_type": "ORGANIZATION", "node_id": f"organization:{self.factory.id}",
            "organization_id": self.factory.id, "name": self.factory.name,
            "role_code": identity.role.code, "own": _empty_counts(),
            "total": _counts(self.rows), "children": children,
        }


def _tree(identity, rows):
    if identity.organization_type == "OWNER":
        return ResponsibilityTreeBuilder(identity.organization, rows).factory_root(identity)
    if identity.organization_type == "MERCHANT":
        factory = _upstream_factory(identity.organization)
        if not factory:
            return None
        builder = ResponsibilityTreeBuilder(factory, rows)
        return builder.org_node(
            identity.organization, builder.rows_for_org(rows, identity.organization.id),
        )
    return None


def _upstream_factory(organization):
    """Find the OWNER ancestor for direct and arbitrarily deep merchants."""
    current, visited = organization, set()
    while current and current.id not in visited:
        visited.add(current.id)
        relation = OrganizationRelation.objects.filter(
            target_organization=current,
            relation_type=OrganizationRelationType.DISTRIBUTION,
            is_active=True,
        ).select_related("source_organization__organization_type").order_by("id").first()
        if not relation:
            return None
        current = relation.source_organization
        if current.organization_type.code == "OWNER":
            return current
    return None


def build_flow_statistics(identity):
    devices = device_scope(identity)
    device_ids = devices.values("id")
    produced = devices.count()
    installed = InstallationRecord.objects.filter(
        device_id__in=device_ids, status=InstallationRecord.Status.INSTALLED,
    ).values("device_id").distinct().count()
    raw_activated = ActivationRecord.objects.filter(
        device_id__in=device_ids, status="ACTIVATED",
    ).values("device_id").distinct().count()
    status_rows = list(OrganizationDevice.objects.filter(device_id__in=device_ids)
        .values("status").annotate(value=Count("id")).order_by("status"))
    model_rows = list(devices.values(
        "product_model_id", "product_model__code", "product_model__name",
    ).annotate(value=Count("id", distinct=True)).order_by("-value", "product_model_id")[:10])
    if identity.organization_type == "OWNER":
        factory_id = identity.organization.id
    else:
        factory = _upstream_factory(identity.organization)
        factory_id = factory.id if factory else None
    rows = _market_rows(factory_id) if factory_id else []
    tree = _tree(identity, rows)
    totals = tree["total"] if tree else _empty_counts()
    return {
        "domain": "FLOW",
        "metrics": {
            "produced_device_count": produced,
            "inventory_device_count": totals["market_inventory_device_count"],
            "installed_device_count": installed,
            "activated_device_count": totals["activated_device_count"],
            "installation_rate": round(installed / produced * 100, 2) if produced else 0,
            "activation_rate": round(raw_activated / produced * 100, 2) if produced else 0,
        },
        "market_status_totals": totals,
        "responsibility_tree": tree,
        "tree_semantics": {
            "inventory": "一级商家已入库且当前仍在商家渠道库存、未激活",
            "activation": "终端用户有效激活",
            "node_total": "本节点及全部下级节点", "node_own": "仅本节点，不含下级",
        },
        "charts": {
            "current_status_distribution": status_rows,
            "product_model_distribution": model_rows,
            "conversion_funnel": [
                {"stage": "设备建档", "value": produced},
                {"stage": "已安装", "value": installed},
                {"stage": "已激活", "value": raw_activated},
            ],
        },
    }
