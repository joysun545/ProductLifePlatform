from decimal import Decimal

from account.models import Organization, UserOrganization

from data_statistics.models import DailyMetricSnapshot, MetricDefinition

from .dashboard_service import build_dashboard
from .identity_service import StatisticsIdentity


def generate_daily_snapshots(snapshot_date, organization_id=None):
    organizations = Organization.objects.filter(is_active=True)
    if organization_id:
        organizations = organizations.filter(pk=organization_id)
    results = []
    for organization in organizations.select_related("organization_type"):
        identity_row = (
            UserOrganization.objects.select_related("user", "role", "organization__organization_type")
            .filter(organization=organization, is_active=True, user__is_active=True).first()
        )
        if not identity_row or organization.organization_type.code == "CUSTOMER":
            continue
        identity = StatisticsIdentity(identity_row.user, identity_row, organization, identity_row.role)
        dashboard = build_dashboard(identity)
        written = 0
        for section in dashboard["sections"]:
            for code, raw_value in section["metrics"].items():
                definition = MetricDefinition.objects.filter(code=code, enabled=True).first()
                if not definition or not isinstance(raw_value, (int, float, Decimal)):
                    continue
                DailyMetricSnapshot.objects.update_or_create(
                    organization=organization,
                    snapshot_date=snapshot_date,
                    metric=definition,
                    dimension_key="",
                    defaults={"value": Decimal(str(raw_value)), "dimensions": {}},
                )
                written += 1
        results.append({"organization_id": organization.pk, "snapshot_count": written})
    return results
