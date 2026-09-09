import django.db.models.deletion
import lifecycle.private_storage
from django.conf import settings
from django.db import migrations, models
from django.utils import timezone


OLD_WORK_TYPE = "FACTORY_RETURN_MATERIAL_DOCUMENT_UPLOAD"
NEW_WORK_TYPE = "FACTORY_RETURN_DOCUMENT_UPLOAD"


def reconcile_document_tasks(apps, schema_editor):
    FactoryReturnDocument = apps.get_model(
        "lifecycle",
        "FactoryReturnDocument",
    )
    GoodsTransfer = apps.get_model("lifecycle", "GoodsTransfer")
    UserOrganization = apps.get_model("account", "UserOrganization")
    UserTask = apps.get_model("workflow", "UserTask")
    WorkItem = apps.get_model("workflow", "WorkItem")
    now = timezone.now()

    transfers = (
        GoodsTransfer.objects.select_related(
            "initiated_by",
            "from_organization",
            "to_organization",
        )
        .filter(factory_return_process__isnull=False)
        .distinct()
    )
    for transfer in transfers.iterator():
        old_key = f"lifecycle:factory-return:{transfer.pk}:material-document"
        new_key = f"lifecycle:factory-return:{transfer.pk}:documents"
        payload = {
            "goods_transfer_id": transfer.pk,
            "flow_no": transfer.flow_no,
            "from_organization_id": transfer.from_organization_id,
            "from_organization_name": transfer.from_organization.name,
            "to_organization_id": transfer.to_organization_id,
            "to_organization_name": transfer.to_organization.name,
            "device_count": transfer.items.count(),
            "stage": "return-documents",
            "document_operation_owner_user_id": transfer.initiated_by_id,
            "documents_block_pickup": False,
        }
        defaults = {
            "domain": "LOGISTICS",
            "type": NEW_WORK_TYPE,
            "biz_id": transfer.flow_no,
            "biz_ref": f"lifecycle.GoodsTransfer:{transfer.pk}",
            "title": "退厂退货待上传物流单据和货品清单",
            "summary": (
                f"退至{transfer.to_organization.name}｜"
                f"共{transfer.items.count()}台"
            ),
            "link": (
                "/pages/stock/goods_transfer_detail/goods_transfer_detail"
                f"?transfer_id={transfer.pk}"
            ),
            "payload": payload,
            "action_mode": "ANYONE_DO",
            "actor_openid": transfer.initiated_by.wx_openid or "",
            "last_activity_at": now,
        }
        workitem = WorkItem.objects.filter(uniq_key=old_key).first()
        if not workitem:
            workitem = WorkItem.objects.filter(uniq_key=new_key).first()
        if not workitem:
            workitem = WorkItem.objects.create(
                uniq_key=new_key,
                status="OPEN",
                **defaults,
            )
        else:
            for field, value in defaults.items():
                setattr(workitem, field, value)
            workitem.uniq_key = new_key
            workitem.status = "OPEN"
            workitem.done_by_openid = ""
            workitem.done_at = None
            workitem.save()

        initiator_openid = (transfer.initiated_by.wx_openid or "").strip()
        workitem.user_tasks.exclude(to_openid=initiator_openid).delete()
        if not initiator_openid:
            continue
        role_snapshot = (
            UserOrganization.objects.filter(
                user_id=transfer.initiated_by_id,
                is_active=True,
            )
            .values_list("role__code", flat=True)
            .first()
            or ""
        )
        UserTask.objects.update_or_create(
            workitem=workitem,
            to_openid=initiator_openid,
            defaults={
                "to_role_snapshot": role_snapshot,
                "need_action": True,
                "state": "NEW",
                "read_at": None,
                "done_at": None,
                "done_reason": "",
                "cursor_time": now,
                "badge_group": "MY",
                "display_title_snapshot": defaults["title"],
                "display_subtitle_snapshot": defaults["summary"],
            },
        )

        # 0009中的既有记录全部来自误写的“物料单据”入口，按物流单据保留。
        FactoryReturnDocument.objects.filter(
            process__transfer_id=transfer.pk,
        ).update(document_kind="LOGISTICS")


def reverse_document_tasks(apps, schema_editor):
    FactoryReturnDocument = apps.get_model(
        "lifecycle",
        "FactoryReturnDocument",
    )
    WorkItem = apps.get_model("workflow", "WorkItem")
    FactoryReturnDocument.objects.filter(document_kind="GOODS_LIST").delete()
    for workitem in WorkItem.objects.filter(type=NEW_WORK_TYPE).iterator():
        transfer_id = str(workitem.payload.get("goods_transfer_id") or "")
        workitem.type = OLD_WORK_TYPE
        workitem.title = "退厂退货待上传物料单据"
        if transfer_id:
            workitem.uniq_key = (
                f"lifecycle:factory-return:{transfer_id}:material-document"
            )
        workitem.save()


class Migration(migrations.Migration):

    dependencies = [
        ("lifecycle", "0009_factory_return_material_document"),
        ("workflow", "0003_alter_workitem_action_mode"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RenameModel(
            old_name="FactoryReturnMaterialDocument",
            new_name="FactoryReturnDocument",
        ),
        migrations.RemoveConstraint(
            model_name="factoryreturndocument",
            name="lifecycle_fr_material_sequence",
        ),
        migrations.RemoveConstraint(
            model_name="factoryreturndocument",
            name="lifecycle_fr_material_request",
        ),
        migrations.AddField(
            model_name="factoryreturndocument",
            name="document_kind",
            field=models.CharField(
                choices=[("LOGISTICS", "物流单据"), ("GOODS_LIST", "货品清单")],
                db_index=True,
                default="LOGISTICS",
                max_length=16,
                verbose_name="单据类型",
            ),
        ),
        migrations.AlterField(
            model_name="factoryreturndocument",
            name="process",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="documents",
                to="lifecycle.factoryreturnprocess",
                verbose_name="厂家退货处理流程",
            ),
        ),
        migrations.AlterField(
            model_name="factoryreturndocument",
            name="uploaded_by",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="uploaded_factory_return_documents",
                to=settings.AUTH_USER_MODEL,
                verbose_name="上传人",
            ),
        ),
        migrations.AlterField(
            model_name="factoryreturndocument",
            name="sequence_no",
            field=models.PositiveIntegerField(verbose_name="同类型单据序号"),
        ),
        migrations.AlterField(
            model_name="factoryreturndocument",
            name="document_file",
            field=models.FileField(
                storage=lifecycle.private_storage.GoodsTransferPrivateStorage(),
                upload_to="factory_return_documents/%Y/%m/",
                verbose_name="退厂退货单据",
            ),
        ),
        migrations.AlterModelOptions(
            name="factoryreturndocument",
            options={
                "ordering": ("document_kind", "sequence_no", "id"),
                "verbose_name": "厂家退货单据",
                "verbose_name_plural": "厂家退货单据",
            },
        ),
        migrations.AddConstraint(
            model_name="factoryreturndocument",
            constraint=models.UniqueConstraint(
                fields=("process", "document_kind", "sequence_no"),
                name="lifecycle_fr_document_sequence",
            ),
        ),
        migrations.AddConstraint(
            model_name="factoryreturndocument",
            constraint=models.UniqueConstraint(
                fields=("process", "document_kind", "client_request_id"),
                name="lifecycle_fr_document_request",
            ),
        ),
        migrations.RunPython(
            reconcile_document_tasks,
            reverse_document_tasks,
        ),
    ]
