from django.core.management.base import BaseCommand

from account.models import SeniorManagerProfile
from workflow.services.role_delivery import reconcile_organization_role_delivery


class Command(BaseCommand):
    help = "Reconcile open task recipients with current senior manager allocations."

    def handle(self, *args, **options):
        organization_ids = SeniorManagerProfile.objects.order_by(
            "identity__organization_id"
        ).values_list("identity__organization_id", flat=True).distinct()
        for organization_id in organization_ids:
            reconcile_organization_role_delivery(organization_id)
        self.stdout.write(self.style.SUCCESS("已重算可识别的未结分管任务"))
