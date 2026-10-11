from collections import defaultdict

from django.db.models import Count, Sum

from account.models import OrganizationRelation, OrganizationRelationType
from lifecycle.models import ActivationRecord, CustomerDeviceBinding, Device, InstallationRecord, OrganizationDevice
from sales.models import OrderPlan, OrderPlanItem, OrderPlanShipment, OrderPlanShipmentDevice

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
    """One current market state per physical device actually received.

    V2 production uses subplan batch codes, and a partial receipt is already
    inventory even while the rest of the order remains in transit. Its exact
    shipment allocations are therefore the source of receipt and attribution.
    Only orders without shipment batches retain the legacy batch-code lookup.
    """
    items = {
        row["order_batch_code"]: row
        for row in OrderPlanItem.objects.filter(
            plan__factory_organization_id=factory_id,
            plan__received_at__isnull=False,
            plan__shipments__isnull=True,
        ).values(
            "order_batch_code", "plan_id", "plan__created_by_id",
            "plan__merchant_organization_id",
        )
    }
    received_devices = {
        device["id"]: {
            "plan_id": items[device["batch__batch_no"]]["plan_id"],
            "sales_user_id": items[device["batch__batch_no"]]["plan__created_by_id"],
            "direct_merchant_id": items[device["batch__batch_no"]]["plan__merchant_organization_id"],
        }
        for device in Device.objects.filter(
            batch__manufacturer_id=factory_id, batch__batch_no__in=items,
        ).values("id", "batch__batch_no")
    }
    allocations = OrderPlanShipmentDevice.objects.filter(
        order_plan__factory_organization_id=factory_id,
        shipment__status=OrderPlanShipment.Status.RECEIVED,
        shipment__received_at__isnull=False,
    ).order_by("shipment__received_at", "shipment_id", "id").values(
        "device_id", "order_plan_id", "order_plan__created_by_id",
        "order_plan__merchant_organization_id",
    )
    # Reissued devices keep their original production batch. The latest
    # received allocation replaces earlier responsibility, without counting
    # the same device in both the original and replacement order/merchant.
    for allocation in allocations:
        received_devices[allocation["device_id"]] = {
            "plan_id": allocation["order_plan_id"],
            "sales_user_id": allocation["order_plan__created_by_id"],
            "direct_merchant_id": allocation["order_plan__merchant_organization_id"],
        }
    if not received_devices:
        return []
    ids = list(received_devices)
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
    for device_id, plan in received_devices.items():
        owner = current.get(device_id, {})
        status = owner.get("status", "")
        if device_id in activated:
            market_status = "ACTIVATED"
            channel_org_id = sources.get(device_id, plan["direct_merchant_id"])
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
            "device_id": device_id, "plan_id": plan["plan_id"],
            "sales_user_id": plan["sales_user_id"],
            "direct_merchant_id": plan["direct_merchant_id"],
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
            "display_scope": "TOTAL",
            "show_organization_inventory": False,
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
            "display_scope": "TOTAL",
            "show_organization_inventory": True,
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
            "role_code": identity.role.code, "display_scope": "TOTAL",
            "show_organization_inventory": False, "own": _empty_counts(),
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
    """Resolve the assigned manufacturer; never choose an arbitrary parent."""
    explicit_root = organization.get_factory_root()
    if explicit_root:
        return explicit_root

    # Compatibility for legacy rows that have not yet been backfilled. The
    # inference is accepted only when the active distribution graph has one
    # possible OWNER ancestor.
    frontier = {organization.pk}
    visited = set()
    roots = {}
    while frontier:
        target_ids = frontier - visited
        if not target_ids:
            break
        visited.update(target_ids)
        relations = OrganizationRelation.objects.filter(
            target_organization_id__in=target_ids,
            relation_type=OrganizationRelationType.DISTRIBUTION,
            is_active=True,
            source_organization__is_active=True,
            source_organization__organization_type__is_active=True,
        ).select_related("source_organization__organization_type")
        next_frontier = set()
        for relation in relations:
            parent = relation.source_organization
            if parent.organization_type.code == "OWNER":
                roots[parent.pk] = parent
            else:
                next_frontier.add(parent.pk)
        frontier = next_frontier
    return next(iter(roots.values())) if len(roots) == 1 else None


def build_flow_statistics(identity):
    organization_type = identity.organization_type
    role_code = identity.role.code
    if organization_type == "OWNER":
        factory_id = identity.organization.id
    else:
        factory = _upstream_factory(identity.organization)
        factory_id = factory.id if factory else None

    rows = _market_rows(factory_id) if factory_id else []
    if organization_type == "OWNER" and role_code == "factory_sales":
        visible_rows = [
            row for row in rows if row["sales_user_id"] == identity.user.id
        ]
    elif organization_type == "MERCHANT" and factory_id:
        visible_rows = ResponsibilityTreeBuilder(
            factory, rows
        ).rows_for_org(rows, identity.organization.id)
    else:
        visible_rows = rows

    if (
        organization_type == "MERCHANT"
        or (organization_type == "OWNER" and role_code == "factory_sales")
    ):
        devices = Device.objects.filter(
            id__in=[row["device_id"] for row in visible_rows]
        )
    else:
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
    tree = _tree(identity, rows)
    totals = tree["total"] if tree else _empty_counts()
    own = tree["own"] if tree else _empty_counts()

    if organization_type == "MERCHANT":
        metrics = {
            "received_device_count": totals["market_device_count"],
            "channel_inventory_device_count": totals["market_inventory_device_count"],
            "organization_inventory_device_count": own["market_inventory_device_count"],
        }
        rate_base = totals["market_device_count"]
        metric_note = "入库设备为进入当前商家链路的设备；链路库存含本组织及全部下级；组织库存仅统计本组织。"
    elif organization_type == "OWNER" and role_code == "factory_sales":
        # The card is labelled "订单计划", but its business unit is devices,
        # not documents. Keep the existing all-created-plan scope (including
        # cancelled historical plans) and sum each plan's declared quantity.
        order_plan_device_count = OrderPlan.objects.filter(
            factory_organization=identity.organization,
            created_by=identity.user,
        ).aggregate(total=Sum("total_quantity"))["total"] or 0
        metrics = {
            "order_plan_count": order_plan_device_count,
            "channel_inventory_device_count": totals["market_inventory_device_count"],
        }
        rate_base = totals["market_device_count"]
        metric_note = "订单计划统计当前销售经理创建的全部订单计划设备数量合计；其余产品流转指标仅统计该销售经理负责的商家链路。"
    else:
        metrics = {
            "produced_device_count": produced,
            "channel_inventory_device_count": totals["market_inventory_device_count"],
        }
        rate_base = produced
        metric_note = "厂家非销售岗位按厂家全域统计生产、市场链路库存、安装与激活数据。"

    metrics.update({
        "installed_device_count": installed,
        "activated_device_count": totals["activated_device_count"],
        "installation_rate": round(installed / rate_base * 100, 2) if rate_base else 0,
        "activation_rate": round(raw_activated / rate_base * 100, 2) if rate_base else 0,
        # Backward-compatible aliases for older clients and regression tests.
        # The current mini-program hides these when semantic fields are present.
        "inventory_device_count": totals["market_inventory_device_count"],
    })
    return {
        "domain": "FLOW",
        "metrics": metrics,
        "metric_note": metric_note,
        "market_status_totals": totals,
        "responsibility_tree": tree,
        "tree_semantics": {
            "inventory": "一级商家已入库且当前仍在商家渠道库存、未激活",
            "activation": "终端用户有效激活",
            "node_total": "本节点及全部下级节点", "node_own": "仅本节点，不含下级",
            "headline": "链路库存含本组织及全部下级；组织库存仅表示本组织",
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
