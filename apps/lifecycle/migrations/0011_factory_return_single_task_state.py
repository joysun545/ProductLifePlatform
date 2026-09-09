from django.db import migrations
from django.utils import timezone


def reconcile_factory_return_tasks(apps, schema_editor):
    AccountCase = apps.get_model("lifecycle", "GoodsTransferAccountCase")
    FactoryReturnProcess = apps.get_model("lifecycle", "FactoryReturnProcess")
    FactoryReturnDocument = apps.get_model("lifecycle", "FactoryReturnDocument")
    UserOrganization = apps.get_model("account", "UserOrganization")
    UserTask = apps.get_model("workflow", "UserTask")
    WorkItem = apps.get_model("workflow", "WorkItem")
    now = timezone.now()

    obsolete_types = (
        "FACTORY_RETURN_LOGISTICS_DOCUMENT_NOTICE",
        "FACTORY_RETURN_GOODS_LIST_NOTICE",
        "FACTORY_RETURN_SALES_NOTICE_PICKED_UP",
    )
    obsolete = WorkItem.objects.filter(type__in=obsolete_types)
    obsolete.update(status="CANCELLED", last_activity_at=now, updated_at=now)
    UserTask.objects.filter(workitem__in=obsolete).exclude(state="DONE").update(
        state="DONE",
        need_action=False,
        done_at=now,
        done_reason="OTHER_ACTION",
        cursor_time=now,
        updated_at=now,
    )

    processes = FactoryReturnProcess.objects.select_related(
        "transfer__initiated_by",
        "transfer__from_organization",
        "transfer__to_organization",
        "account_case",
    )
    for process in processes.iterator():
        transfer = process.transfer
        summary = (
            f"{transfer.from_organization.name}退回｜"
            f"共{transfer.items.count()}台"
        )

        account_case, _ = AccountCase.objects.get_or_create(
            transfer_id=transfer.pk,
            case_type="RETURN_CREDIT",
            defaults={
                "upper_organization_id": transfer.to_organization_id,
                "lower_organization_id": transfer.from_organization_id,
                "creditor_organization_id": transfer.from_organization_id,
                "debtor_organization_id": transfer.to_organization_id,
            },
        )
        if process.account_case_id != account_case.pk:
            process.account_case_id = account_case.pk
            process.save(update_fields=("account_case", "updated_at"))

        logistics_uploaded = FactoryReturnDocument.objects.filter(
            process_id=process.pk,
            document_kind="LOGISTICS",
        ).exists()
        document_task = WorkItem.objects.filter(
            uniq_key=f"lifecycle:factory-return:{transfer.pk}:documents"
        ).first()
        if document_task:
            document_task.title = (
                "退厂退货物流单据已上传"
                if logistics_uploaded
                else "退厂退货待补传物流单据"
            )
            document_task.summary = (
                f"退至{transfer.to_organization.name}｜"
                f"共{transfer.items.count()}台"
            )
            document_task.payload = {
                **(document_task.payload or {}),
                "stage": "return-logistics-document",
                "document_operation_owner_user_id": transfer.initiated_by_id,
                "logistics_document_blocks_pickup": False,
            }
            document_task.status = "DONE" if logistics_uploaded else "OPEN"
            if not logistics_uploaded:
                document_task.done_by_openid = ""
                document_task.done_at = None
            document_task.last_activity_at = now
            document_task.save()
            document_task.user_tasks.update(
                state="DONE" if logistics_uploaded else "NEW",
                need_action=not logistics_uploaded,
                read_at=None if not logistics_uploaded else now,
                done_at=now if logistics_uploaded else None,
                done_reason="SELF_ACTION" if logistics_uploaded else "",
                display_title_snapshot=document_task.title,
                display_subtitle_snapshot=document_task.summary,
                cursor_time=now,
                updated_at=now,
            )

        amount_done = bool(
            account_case.status == "CONFIRMED"
            and account_case.confirmed_amount is not None
        )
        posting_done = process.phase == "POSTED"
        picked = bool(process.picked_at)
        if not picked:
            title = "退货待物流取回，随后下账处理"
        elif not amount_done and not posting_done:
            title = "退货待填写冲抵金额并分类入库"
        elif amount_done and not posting_done:
            title = "退货待分类入库"
        elif posting_done and not amount_done:
            title = "退货待填写冲抵金额"
        else:
            title = "退货下账与分类入库已完成"

        assistant_openids = list(
            UserOrganization.objects.filter(
                organization_id=transfer.to_organization_id,
                role__code="factory_sales_assistant",
                is_active=True,
                role__is_active=True,
                user__is_active=True,
            )
            .exclude(user__wx_openid__isnull=True)
            .exclude(user__wx_openid="")
            .values_list("user__wx_openid", flat=True)
            .distinct()
        )
        assistant_key = f"lifecycle:factory-return:{transfer.pk}:posting"
        existing_assistant_task = WorkItem.objects.filter(
            uniq_key=assistant_key
        ).first()
        assistant_task, _ = WorkItem.objects.update_or_create(
            uniq_key=assistant_key,
            defaults={
                "domain": "LOGISTICS",
                "type": "FACTORY_RETURN_POSTING",
                "biz_id": transfer.flow_no,
                "biz_ref": f"lifecycle.GoodsTransfer:{transfer.pk}",
                "title": title,
                "summary": summary,
                "link": (
                    "/pages/stock/factory_return_process/factory_return_process"
                    f"?transfer_id={transfer.pk}"
                ),
                "payload": {
                    "goods_transfer_id": transfer.pk,
                    "flow_no": transfer.flow_no,
                    "from_organization_id": transfer.from_organization_id,
                    "from_organization_name": transfer.from_organization.name,
                    "to_organization_id": transfer.to_organization_id,
                    "to_organization_name": transfer.to_organization.name,
                    "device_count": transfer.items.count(),
                    "blocked_by_pickup": not picked,
                    "credit_amount_recorded": amount_done,
                    "inventory_posted": posting_done,
                    "required_substeps": ["CREDIT_AMOUNT", "INVENTORY_POSTING"],
                },
                "action_mode": "ANYONE_DO",
                "status": "DONE" if amount_done and posting_done else "OPEN",
                "actor_openid": transfer.initiated_by.wx_openid or "",
                "done_by_openid": (
                    existing_assistant_task.done_by_openid
                    if existing_assistant_task and amount_done and posting_done
                    else ""
                ),
                "done_at": (
                    existing_assistant_task.done_at
                    if existing_assistant_task
                    and existing_assistant_task.done_at
                    and amount_done
                    and posting_done
                    else (now if amount_done and posting_done else None)
                ),
                "last_activity_at": now,
            },
        )
        assistant_task.user_tasks.exclude(to_openid__in=assistant_openids).delete()
        for openid in assistant_openids:
            UserTask.objects.update_or_create(
                workitem=assistant_task,
                to_openid=openid,
                defaults={
                    "to_role_snapshot": "factory_sales_assistant",
                    "need_action": not (amount_done and posting_done),
                    "state": "DONE" if amount_done and posting_done else "NEW",
                    "read_at": None,
                    "done_at": now if amount_done and posting_done else None,
                    "done_reason": "OTHER_ACTION" if amount_done and posting_done else "",
                    "cursor_time": now,
                    "badge_group": "MY",
                    "display_title_snapshot": title,
                    "display_subtitle_snapshot": summary,
                },
            )

        sales_task = WorkItem.objects.filter(
            uniq_key=f"lifecycle:factory-return:{transfer.pk}:sales:submitted"
        ).first()
        if sales_task and picked:
            sales_task.title = "厂家物流已取回商家退货"
            sales_task.payload = {
                **(sales_task.payload or {}),
                "stage": "picked-up",
                "pickup_confirmed": True,
            }
            sales_task.last_activity_at = now
            sales_task.save(
                update_fields=(
                    "title",
                    "payload",
                    "last_activity_at",
                    "updated_at",
                )
            )
            sales_task.user_tasks.update(
                display_title_snapshot=sales_task.title,
                cursor_time=now,
                updated_at=now,
            )


class Migration(migrations.Migration):

    dependencies = [
        ("lifecycle", "0010_factory_return_documents"),
        ("workflow", "0003_alter_workitem_action_mode"),
    ]

    operations = [
        migrations.RunPython(
            reconcile_factory_return_tasks,
            migrations.RunPython.noop,
        ),
    ]
