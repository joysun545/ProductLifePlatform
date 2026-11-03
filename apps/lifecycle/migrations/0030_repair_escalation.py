import django.db.models.deletion
from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [('lifecycle', '0029_repair_settlement_basis')]
    operations = [
        migrations.AddField(
                    model_name='aftersalesapplication',
                    name='escalation',
                    field=models.JSONField(blank=True, default=dict),
                ),
        migrations.AddField(
                    model_name='aftersalesapplication',
                    name='replacement_device',
                    field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='replacement_after_sales', to='lifecycle.device'),
                ),
        migrations.AlterField(
                    model_name='aftersalesapplication',
                    name='execution_stage',
                    field=models.CharField(choices=[('PENDING_ESCALATION', '无法维修待厂家确认'), ('AWAITING_FACTORY_HANDOVER', '待交接设备至厂家'), ('AWAITING_FACTORY_RECEIPT', '待厂家确认收件'), ('AWAITING_REPLACEMENT', '待厂家交付换新设备'), ('AWAITING_REPLACEMENT_CONFIRM', '待用户确认收到换新设备'), ('NONE', '待厂家判断'), ('CONTACT_INSTALLER', '联系原装车操作人'), ('AWAITING_APPLICANT_SHIPMENT', '待发起人寄送设备'), ('AWAITING_REPAIR_RECEIPT', '待维修人确认收件'), ('REPAIRING', '维修处理中'), ('AWAITING_RETURN_SHIPMENT', '待维修人寄回设备'), ('THIRD_PARTY_REPAIR', '第三方紧急维修处理中'), ('AWAITING_APPLICANT_CONFIRM', '待发起人确认结果'), ('FINAL_DIAGNOSIS', '待厂家最终归因'), ('CLOSED', '已结案')], default='NONE', max_length=40),
                ),
        migrations.AlterField(
                    model_name='aftersalesapplication',
                    name='final_solution_type',
                    field=models.CharField(blank=True, choices=[('ON_SITE_ADJUSTMENT', '现场调整'), ('MAIL_IN_REPAIR', '寄修处理'), ('REMOTE_GUIDANCE', '远程指导'), ('COMPONENT_REPLACEMENT', '更换配套物料'), ('THIRD_PARTY_REPAIR', '第三方维修'), ('NO_REPAIR_REQUIRED', '无需维修'), ('DEVICE_REPLACEMENT', '售后整机换新'), ('OTHER', '其他')], default='', max_length=48),
                ),
    ]
