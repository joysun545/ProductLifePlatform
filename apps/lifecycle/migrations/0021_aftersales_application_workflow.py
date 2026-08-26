from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    """Extend the already-applied canonical after-sales application.

    This migration deliberately does not touch the abandoned AfterSalesCase
    branch.  Production has only ever applied 0020_aftersales_application and
    already stores real application, media and event rows in that schema.
    """

    dependencies = [
        ("account", "0004_add_factory_aftersales_role"),
        ("lifecycle", "0020_aftersales_application"),
    ]

    operations = [
        migrations.AddField(
            model_name="aftersalesapplication",
            name="assigned_organization",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="assigned_after_sales_applications", to="account.organization"),
        ),
        migrations.AddField(
            model_name="aftersalesapplication",
            name="assigned_user",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="assigned_after_sales_applications", to="account.user"),
        ),
        migrations.AddField(
            model_name="aftersalesapplication",
            name="applicant_confirm_due_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="aftersalesapplication",
            name="applicant_confirm_mode",
            field=models.CharField(blank=True, choices=[("MANUAL", "发起人确认"), ("TIMEOUT", "七天超时默认确认")], default="", max_length=16),
        ),
        migrations.AddField(
            model_name="aftersalesapplication",
            name="applicant_confirmed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="aftersalesapplication",
            name="assignee_snapshot",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="aftersalesapplication",
            name="closed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="aftersalesapplication",
            name="closed_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="closed_after_sales_applications", to="account.user"),
        ),
        migrations.AddField(
            model_name="aftersalesapplication",
            name="diagnosis_note",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="aftersalesapplication",
            name="dispatch_route",
            field=models.CharField(blank=True, choices=[("ORIGINAL_INSTALLER", "原装车操作人"), ("MERCHANT_ELECTRICIAN", "一级商家电工"), ("FACTORY_AFTERSALES", "厂家售后经理"), ("REMOTE", "厂家销售经理远程排障"), ("THIRD_PARTY", "第三方紧急维修")], default="", max_length=32),
        ),
        migrations.AddField(
            model_name="aftersalesapplication",
            name="dispatch_version",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="aftersalesapplication",
            name="execution_stage",
            field=models.CharField(choices=[("NONE", "待厂家判断"), ("CONTACT_INSTALLER", "联系原装车操作人"), ("AWAITING_APPLICANT_SHIPMENT", "待发起人寄送设备"), ("AWAITING_REPAIR_RECEIPT", "待维修人确认收件"), ("REPAIRING", "维修处理中"), ("AWAITING_RETURN_SHIPMENT", "待维修人寄回设备"), ("THIRD_PARTY_REPAIR", "第三方紧急维修处理中"), ("AWAITING_APPLICANT_CONFIRM", "待发起人确认结果"), ("FINAL_DIAGNOSIS", "待厂家最终归因"), ("CLOSED", "已结案")], default="NONE", max_length=40),
        ),
        migrations.AddField(
            model_name="aftersalesapplication",
            name="final_fault_materials",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="aftersalesapplication",
            name="final_fault_type",
            field=models.CharField(blank=True, choices=[("INSTALLATION_OR_WIRING", "安装或线路问题"), ("COMPONENT_SUSPECTED", "疑似关键配套物料故障"), ("OPERATION_OR_EXTERNAL_CONDITION", "使用或外部环境因素"), ("NO_FAULT_REPRODUCED", "未复现故障"), ("NEEDS_PHYSICAL_INSPECTION", "需实物检修确认"), ("OTHER", "其他")], default="", max_length=48),
        ),
        migrations.AddField(
            model_name="aftersalesapplication",
            name="final_solution_note",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="aftersalesapplication",
            name="final_solution_type",
            field=models.CharField(blank=True, choices=[("ON_SITE_ADJUSTMENT", "现场调整"), ("MAIL_IN_REPAIR", "寄修处理"), ("REMOTE_GUIDANCE", "远程指导"), ("COMPONENT_REPLACEMENT", "更换配套物料"), ("THIRD_PARTY_REPAIR", "第三方维修"), ("NO_REPAIR_REQUIRED", "无需维修"), ("OTHER", "其他")], default="", max_length=48),
        ),
        migrations.AddField(
            model_name="aftersalesapplication",
            name="preliminary_fault_type",
            field=models.CharField(blank=True, choices=[("INSTALLATION_OR_WIRING", "安装或线路问题"), ("COMPONENT_SUSPECTED", "疑似关键配套物料故障"), ("OPERATION_OR_EXTERNAL_CONDITION", "使用或外部环境因素"), ("NO_FAULT_REPRODUCED", "未复现故障"), ("NEEDS_PHYSICAL_INSPECTION", "需实物检修确认"), ("OTHER", "其他")], default="", max_length=48),
        ),
        migrations.AddField(
            model_name="aftersalesapplication",
            name="repair_completed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="aftersalesapplication",
            name="repair_completed_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="completed_after_sales_repairs", to="account.user"),
        ),
        migrations.AddField(
            model_name="aftersalesapplication",
            name="repair_material_replaced",
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="aftersalesapplication",
            name="repair_materials",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="aftersalesapplication",
            name="repair_plan",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="aftersalesapplication",
            name="repair_summary",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="aftersalesapplication",
            name="third_party_cost_amount",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name="aftersalesapplication",
            name="third_party_snapshot",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="aftersalesapplication",
            name="triaged_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="aftersalesapplication",
            name="triaged_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="triaged_after_sales_applications", to="account.user"),
        ),
        migrations.AddField(
            model_name="aftersalesmedia",
            name="caption",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="aftersalesmedia",
            name="purpose",
            field=models.CharField(choices=[("APPLICATION_EVIDENCE", "申请故障证据"), ("APPLICANT_SHIPMENT_DOCUMENT", "发起人寄送物流单据"), ("REPAIR_EVIDENCE", "维修过程或测试证据"), ("REPAIR_RETURN_SHIPMENT_DOCUMENT", "维修返件物流单据"), ("THIRD_PARTY_MATERIAL_SHIPMENT_DOCUMENT", "第三方备件寄送单据"), ("THIRD_PARTY_FEE_VOUCHER", "第三方维修费用凭证"), ("THIRD_PARTY_FAULT_MATERIAL_RETURN_DOCUMENT", "故障物料返还单据")], default="APPLICATION_EVIDENCE", max_length=64),
        ),
        migrations.AlterField(
            model_name="aftersalesmedia",
            name="media_type",
            field=models.CharField(choices=[("IMAGE", "图片"), ("VIDEO", "视频"), ("DOCUMENT", "单据")], max_length=16),
        ),
        migrations.AddIndex(
            model_name="aftersalesmedia",
            index=models.Index(fields=["application", "purpose"], name="life_as_media_purpose"),
        ),
    ]
