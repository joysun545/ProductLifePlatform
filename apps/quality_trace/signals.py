import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from lifecycle.models import AfterSalesApplication, DeviceMaterialFaultRecord

from .models import QualityMetricSnapshot
from .services.metric_engine import evaluate_active_rules


logger = logging.getLogger(__name__)


def _evaluate_after_commit(factory_id):
    try:
        evaluate_active_rules(
            source=QualityMetricSnapshot.Source.REALTIME,
            factory_id=factory_id,
        )
    except Exception:
        # Quality alert calculation must never roll back the completed source
        # business transaction. Scheduled evaluation will retry the same facts.
        logger.exception("Realtime quality-rule evaluation failed for factory %s", factory_id)


@receiver(post_save, sender=AfterSalesApplication, dispatch_uid="qtrace_aftersales_closed")
def aftersales_closed(sender, instance, **kwargs):
    update_fields = kwargs.get("update_fields")
    relevant_fields = {
        "status",
        "closed_at",
        "final_fault_type",
        "final_fault_materials",
        "repair_material_replaced",
        "repair_materials",
    }
    if update_fields and not relevant_fields.intersection(update_fields):
        return
    if (
        instance.status == AfterSalesApplication.Status.CLOSED
        and instance.factory_organization_id
    ):
        transaction.on_commit(
            lambda factory_id=instance.factory_organization_id: _evaluate_after_commit(factory_id)
        )


@receiver(post_save, sender=DeviceMaterialFaultRecord, dispatch_uid="qtrace_material_fault_created")
def material_fault_created(sender, instance, created, **kwargs):
    if created and instance.source_production_batch_id:
        factory_id = instance.source_production_batch.manufacturer_id
        transaction.on_commit(
            lambda factory_id=factory_id: _evaluate_after_commit(factory_id)
        )
