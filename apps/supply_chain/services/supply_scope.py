from account.models import OrganizationRelation, OrganizationRelationType


def active_supplier_ids(factory_id):
    """Supplier organizations authorized to supply this factory root."""
    if not factory_id:
        return OrganizationRelation.objects.none().values_list("target_organization_id", flat=True)
    return OrganizationRelation.objects.filter(
        source_organization_id=factory_id,
        source_organization__factory_root_id=factory_id,
        source_organization__is_active=True,
        source_organization__organization_type__code="OWNER",
        source_organization__organization_type__is_active=True,
        relation_type=OrganizationRelationType.SUPPLY,
        is_active=True,
        target_organization__factory_root_id=factory_id,
        target_organization__is_active=True,
        target_organization__organization_type__code="SUPPLIER",
        target_organization__organization_type__is_active=True,
    ).order_by().values_list("target_organization_id", flat=True)


def supplier_can_supply(factory_id, supplier_id):
    return active_supplier_ids(factory_id).filter(target_organization_id=supplier_id).exists()
