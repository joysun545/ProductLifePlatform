from django.core.management.base import BaseCommand

from lifecycle.services.aftersales_service import AfterSalesService


class Command(BaseCommand):
    help = "Default-confirm after-sales results that have waited seven days for the applicant."

    def handle(self, *args, **options):
        application_ids = AfterSalesService.process_confirmation_timeouts()
        self.stdout.write(
            self.style.SUCCESS(
                f"processed {len(application_ids)} after-sales confirmation timeout(s): "
                f"{', '.join(str(item) for item in application_ids) or '-'}"
            )
        )
