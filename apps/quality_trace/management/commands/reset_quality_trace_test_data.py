import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from quality_trace.models import (
    BatchQualityAffectedDevice,
    BatchQualityCase,
    QualityAlert,
    QualityAlertRule,
    QualityAssessment,
    QualityControlAction,
    QualityGateDecision,
    QualityMetricSnapshot,
    QualityMetricSnapshotDevice,
    QualityRecallCampaign,
    QualityRecallCampaignCaseLink,
    QualityRecallCloseout,
    QualityRecallCustomerHandover,
    QualityRecallDevice,
    QualityRecallDeviceEvent,
    QualityRecallExceptionDecision,
    QualityRecallHandoverEvidence,
    QualityRecallIsolationReceipt,
    QualityRecallProgressSnapshot,
    QualityRecallReturnLeg,
    QualityRecallTransportLeg,
    QualityTraceEvent,
)
from workflow.models import UserTask, WorkItem


CONFIRMATION = "RESET_QUALITY_TRACE"


RUNTIME_MODELS = (
    QualityAlert,
    QualityAssessment,
    BatchQualityCase,
    BatchQualityAffectedDevice,
    QualityControlAction,
    QualityTraceEvent,
    QualityMetricSnapshot,
    QualityMetricSnapshotDevice,
    QualityGateDecision,
    QualityRecallCampaign,
    QualityRecallCampaignCaseLink,
    QualityRecallDevice,
    QualityRecallDeviceEvent,
    QualityRecallReturnLeg,
    QualityRecallCustomerHandover,
    QualityRecallHandoverEvidence,
    QualityRecallTransportLeg,
    QualityRecallIsolationReceipt,
    QualityRecallProgressSnapshot,
    QualityRecallExceptionDecision,
    QualityRecallCloseout,
)


DELETE_ORDER = (
    QualityRecallCloseout,
    QualityRecallExceptionDecision,
    QualityRecallIsolationReceipt,
    QualityRecallHandoverEvidence,
    QualityRecallCustomerHandover,
    QualityRecallTransportLeg,
    QualityRecallReturnLeg,
    QualityRecallDeviceEvent,
    QualityRecallProgressSnapshot,
    QualityRecallCampaignCaseLink,
    QualityRecallDevice,
    QualityRecallCampaign,
    QualityGateDecision,
    QualityTraceEvent,
    QualityControlAction,
    BatchQualityAffectedDevice,
    BatchQualityCase,
    QualityAssessment,
    QualityMetricSnapshotDevice,
    QualityMetricSnapshot,
    QualityAlert,
)


def _quality_workitems():
    return WorkItem.objects.filter(
        Q(type__startswith="QUALITY_")
        | Q(uniq_key__startswith="quality-")
    )


def _runtime_counts():
    return {
        model.__name__: model.objects.count()
        for model in RUNTIME_MODELS
    }


def _preview():
    workitems = _quality_workitems()
    active_rules = QualityAlertRule.objects.filter(
        status=QualityAlertRule.Status.ACTIVE,
    )
    return {
        "runtime_model_counts": _runtime_counts(),
        "quality_workitem_count": workitems.count(),
        "quality_user_task_count": UserTask.objects.filter(
            workitem__in=workitems,
        ).count(),
        "rules_preserved_count": QualityAlertRule.objects.count(),
        "active_rule_ids_to_suspend": list(
            active_rules.order_by("id").values_list("id", flat=True)
        ),
    }


class Command(BaseCommand):
    help = (
        "预演或重置质量追溯测试运行数据；保留规则版本及所有业务主链数据，"
        "提交时暂停当前启用规则"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--commit",
            action="store_true",
            help="实际提交；未提供时仅输出预演结果",
        )
        parser.add_argument(
            "--confirm",
            default="",
            help=f"提交时必须明确提供：{CONFIRMATION}",
        )

    def handle(self, *args, **options):
        before = _preview()
        if not options["commit"]:
            self.stdout.write(json.dumps({
                "mode": "DRY_RUN",
                "confirmation_required": CONFIRMATION,
                "before": before,
                "preserved": {
                    "quality_rules_and_versions": True,
                    "accounts_organizations_roles": True,
                    "devices_lifecycle_inventory_orders_bom_aftersales": True,
                    "non_quality_workitems": True,
                },
            }, ensure_ascii=False))
            return

        if options.get("confirm") != CONFIRMATION:
            raise CommandError(
                f"拒绝执行：请同时提供 --confirm {CONFIRMATION}。"
            )

        try:
            with transaction.atomic():
                suspended_rule_count = QualityAlertRule.objects.filter(
                    status=QualityAlertRule.Status.ACTIVE,
                ).update(status=QualityAlertRule.Status.SUSPENDED)

                workitems = _quality_workitems()
                deleted_quality_user_task_count = UserTask.objects.filter(
                    workitem__in=workitems,
                ).count()
                deleted_quality_workitem_count = workitems.count()
                workitems.delete()

                # SUPERSEDED campaigns point to their canonical campaign with
                # PROTECT.  All campaigns are intentionally removed in this
                # test reset, so clear the self-reference inside the same
                # transaction before deleting the campaign graph.
                QualityRecallCampaign.objects.filter(
                    superseded_by__isnull=False,
                ).update(
                    superseded_by=None,
                    superseded_at=None,
                    superseded_by_user=None,
                    supersession_reason="",
                )

                deleted_runtime_counts = {}
                for model in DELETE_ORDER:
                    count = model.objects.count()
                    if count:
                        model.objects.all().delete()
                    deleted_runtime_counts[model.__name__] = count

                remaining = _runtime_counts()
                remaining_workitems = _quality_workitems().count()
                if any(remaining.values()) or remaining_workitems:
                    raise CommandError(
                        "重置后仍存在质量追溯运行记录，事务已回滚。"
                    )

                result = {
                    "mode": "COMMIT",
                    "status": "COMPLETED",
                    "before": before,
                    "suspended_rule_count": suspended_rule_count,
                    "deleted_quality_workitem_count": deleted_quality_workitem_count,
                    "deleted_quality_user_task_count": deleted_quality_user_task_count,
                    "deleted_runtime_counts": deleted_runtime_counts,
                    "after": {
                        "runtime_model_counts": remaining,
                        "quality_workitem_count": remaining_workitems,
                        "rules_preserved_count": QualityAlertRule.objects.count(),
                        "active_rule_count": QualityAlertRule.objects.filter(
                            status=QualityAlertRule.Status.ACTIVE,
                        ).count(),
                        "suspended_rule_count": QualityAlertRule.objects.filter(
                            status=QualityAlertRule.Status.SUSPENDED,
                        ).count(),
                    },
                }
        except CommandError:
            raise
        except Exception as exc:
            raise CommandError(
                f"质量追溯测试数据重置失败，事务已回滚：{exc}"
            ) from exc

        self.stdout.write(json.dumps(result, ensure_ascii=False))
