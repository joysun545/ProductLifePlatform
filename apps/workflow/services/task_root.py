"""Resolve the display root from a task's own business context."""
from collections import defaultdict

from account.models import Organization


def _positive_id(value):
    if isinstance(value, bool):
        return None
    text = str(value or "")
    if not text.isascii() or not text.isdecimal() or len(text) > 19:
        return None
    number = int(text)
    return number if 0 < number < 2**63 else None


def _source(workitem):
    if workitem.type.startswith("QUALITY_RECALL_"):
        # Every current recall publisher uses the campaign's PK as biz_id,
        # even for a task about one transport leg or disposition.
        return "QualityRecallCampaign", _positive_id(workitem.biz_id)
    prefix, _, value = (workitem.biz_ref or "").partition(":")
    return prefix, _positive_id(value)


def _business_roots(source_ids):
    from lifecycle.models import AfterSalesApplication, GoodsTransfer, ReplacementFlow
    from quality_trace.models import QualityRecallCampaign
    from sales.models import OrderPlan, ReplacementSupplyPlan

    sources = {
        "sales.OrderPlan": (OrderPlan, "factory_organization_id"),
        "AfterSalesApplication": (AfterSalesApplication, "factory_organization_id"),
        "QualityRecallCampaign": (QualityRecallCampaign, "factory_organization_id"),
        "sales.ReplacementSupplyPlan": (ReplacementSupplyPlan, "factory_id"),
        "lifecycle.ReplacementFlow": (ReplacementFlow, "application__factory_organization_id"),
    }
    roots = {}
    for source, (model, field) in sources.items():
        ids = source_ids[source]
        if ids:
            rows = model.objects.filter(pk__in=ids).order_by().values_list("pk", field)
            roots.update({(source, pk): root_id for pk, root_id in rows})

    transfers = source_ids["lifecycle.GoodsTransfer"]
    if transfers:
        rows = GoodsTransfer.objects.filter(pk__in=transfers).order_by().values_list(
            "pk",
            "instruction_organization__factory_root_id",
            "from_organization__factory_root_id",
            "to_organization__factory_root_id",
        )
        for pk, instruction_root, from_root, to_root in rows:
            # Current transfer rows lack a root snapshot. Display a root only
            # when all organizations on that transfer agree.
            roots[("lifecycle.GoodsTransfer", pk)] = (
                instruction_root if instruction_root == from_root == to_root else None
            )
    return roots


def task_root_organizations(workitems):
    """Return confirmed root labels by WorkItem ID without using the viewer."""
    workitems = {item.pk: item for item in workitems}
    sources = {pk: _source(item) for pk, item in workitems.items()}
    source_ids = defaultdict(set)
    for source, record_id in sources.values():
        if record_id:
            source_ids[source].add(record_id)
    business_roots = _business_roots(source_ids)

    root_ids = {}
    for pk, item in workitems.items():
        payload = item.payload if isinstance(item.payload, dict) else {}
        candidates = [
            payload.get("root_organization_id"),
            payload.get("factory_root_id"),
        ]
        source = sources[pk]
        if source in business_roots:
            business_root = business_roots[source]
            if not business_root:
                continue
            candidates.append(business_root)
        if source[0] == "sales.OrderPlan":
            candidates.append(payload.get("factory_id"))
        if source[0] in {
            "lifecycle.FactoryReturnWarrantyDispatch",
            "lifecycle.FactoryReturnRepairMaterialRequirement",
        }:
            candidates.append(payload.get("factory_organization_id"))
        candidates = {_positive_id(candidate) for candidate in candidates} - {None}
        if len(candidates) == 1:
            root_ids[pk] = candidates.pop()

    names = dict(Organization.objects.filter(pk__in=set(root_ids.values())).values_list("pk", "name"))
    return {
        pk: {"id": root_id, "name": names[root_id]}
        for pk, root_id in root_ids.items()
        if names.get(root_id)
    }
