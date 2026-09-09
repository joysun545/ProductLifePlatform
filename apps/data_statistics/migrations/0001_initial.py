from django.db import migrations, models
import django.db.models.deletion


DEFINITIONS = [
    ("material_batch_count", "物料批次数", "MATERIAL", "批", "已登记的供应商物料批次"),
    ("material_covered_device_count", "物料配套设备数", "MATERIAL", "台", "已确认物料批次的订单对应设备"),
    ("material_fault_device_count", "物料故障关联设备数", "MATERIAL", "台", "存在物料故障事实的去重设备"),
    ("produced_device_count", "生产设备数", "FLOW", "台", "已经创建唯一设备身份的设备"),
    ("inventory_device_count", "当前库存设备数", "FLOW", "台", "当前组织归属状态为库存的设备"),
    ("installed_device_count", "已安装设备数", "FLOW", "台", "有效安装记录对应的去重设备"),
    ("activated_device_count", "已激活设备数", "FLOW", "台", "有效激活记录对应的去重设备"),
    ("aftersales_case_count", "售后工单数", "AFTERSALES", "单", "有效售后主单数量"),
    ("aftersales_fault_device_count", "售后故障设备数", "AFTERSALES", "台", "发生有效售后的去重设备"),
    ("aftersales_open_count", "未结案售后数", "AFTERSALES", "单", "尚未关闭或取消的售后主单"),
    ("aftersales_fault_rate", "已激活售后故障率", "AFTERSALES", "%", "故障设备数除以同范围已激活设备数"),
    ("active_recall_device_count", "召回中设备数", "QUALITY", "台", "有效召回批次内未完成处置的设备"),
]


def seed_definitions(apps, schema_editor):
    Definition = apps.get_model("data_statistics", "MetricDefinition")
    source_map = {
        "MATERIAL": ["supply_chain.SupplierMaterialBatch", "sales.OrderPlanItemMaterialBatch", "lifecycle.DeviceMaterialFaultRecord"],
        "FLOW": ["lifecycle.Device", "lifecycle.OrganizationDevice", "lifecycle.InstallationRecord", "lifecycle.ActivationRecord"],
        "AFTERSALES": ["lifecycle.AfterSalesApplication"],
        "QUALITY": ["quality_trace.QualityRecallCampaign", "quality_trace.QualityRecallDevice"],
    }
    for code, name, domain, unit, description in DEFINITIONS:
        Definition.objects.update_or_create(
            code=code,
            defaults={"name": name, "domain": domain, "unit": unit, "description": description, "source_models": source_map[domain]},
        )


class Migration(migrations.Migration):
    initial = True
    dependencies = [("account", "0005_add_factory_chief_engineer_role")]
    operations = [
        migrations.CreateModel(
            name="MetricDefinition",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(max_length=64, unique=True)),
                ("name", models.CharField(max_length=128)),
                ("domain", models.CharField(choices=[("MATERIAL", "物料配套"), ("FLOW", "产品流转"), ("AFTERSALES", "售后服务"), ("QUALITY", "质量追溯")], db_index=True, max_length=16)),
                ("unit", models.CharField(blank=True, default="", max_length=16)),
                ("description", models.TextField(blank=True, default="")),
                ("numerator_definition", models.TextField(blank=True, default="")),
                ("denominator_definition", models.TextField(blank=True, default="")),
                ("source_models", models.JSONField(blank=True, default=list)),
                ("enabled", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ("domain", "code")},
        ),
        migrations.CreateModel(
            name="DailyMetricSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("snapshot_date", models.DateField(db_index=True)),
                ("value", models.DecimalField(decimal_places=6, max_digits=20)),
                ("dimensions", models.JSONField(blank=True, default=dict)),
                ("dimension_key", models.CharField(blank=True, default="", max_length=255)),
                ("generated_at", models.DateTimeField(auto_now=True)),
                ("metric", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="daily_snapshots", to="data_statistics.metricdefinition")),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="data_statistic_snapshots", to="account.organization")),
            ],
            options={"ordering": ("-snapshot_date", "metric__code", "dimension_key")},
        ),
        migrations.AddConstraint(
            model_name="dailymetricsnapshot",
            constraint=models.UniqueConstraint(fields=("organization", "snapshot_date", "metric", "dimension_key"), name="data_stats_unique_daily_metric_dimension"),
        ),
        migrations.AddIndex(
            model_name="dailymetricsnapshot",
            index=models.Index(fields=["organization", "metric", "snapshot_date"], name="data_stats_org_metric_date_idx"),
        ),
        migrations.RunPython(seed_definitions, migrations.RunPython.noop),
    ]
