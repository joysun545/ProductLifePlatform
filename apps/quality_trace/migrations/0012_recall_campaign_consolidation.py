from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def backfill_primary_case_links(apps, schema_editor):
    Campaign = apps.get_model("quality_trace", "QualityRecallCampaign")
    CaseLink = apps.get_model("quality_trace", "QualityRecallCampaignCaseLink")
    CaseLink.objects.bulk_create([
        CaseLink(
            campaign_id=campaign.pk,
            quality_case_id=campaign.quality_case_id,
            link_type="PRIMARY",
            linked_by_id=campaign.created_by_id,
            reason="历史召回批次主案件关系补录",
        )
        for campaign in Campaign.objects.order_by("pk").iterator()
    ], ignore_conflicts=True)


def remove_backfilled_primary_case_links(apps, schema_editor):
    CaseLink = apps.get_model("quality_trace", "QualityRecallCampaignCaseLink")
    CaseLink.objects.filter(reason="历史召回批次主案件关系补录").delete()


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("quality_trace", "0011_defer_recall_disposition"),
    ]

    operations = [
        migrations.AlterField(
            model_name="qualityrecallcampaign",
            name="status",
            field=models.CharField(
                choices=[
                    ("DRAFT", "待发布"),
                    ("ACTIVE", "召回中"),
                    ("COMPLETED", "已完成"),
                    ("CANCELLED", "已取消"),
                    ("SUPERSEDED", "已被主批次接管"),
                ],
                db_index=True,
                default="DRAFT",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="qualityrecallcampaign",
            name="superseded_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="qualityrecallcampaign",
            name="supersession_reason",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="qualityrecallcampaign",
            name="superseded_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="superseded_campaigns",
                to="quality_trace.qualityrecallcampaign",
            ),
        ),
        migrations.AddField(
            model_name="qualityrecallcampaign",
            name="superseded_by_user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="superseded_quality_recalls",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.CreateModel(
            name="QualityRecallCampaignCaseLink",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "link_type",
                    models.CharField(
                        choices=[("PRIMARY", "主案件"), ("CONTRIBUTING", "关联案件")],
                        max_length=16,
                    ),
                ),
                ("reason", models.TextField(blank=True, default="")),
                ("linked_at", models.DateTimeField(auto_now_add=True)),
                (
                    "campaign",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="case_links",
                        to="quality_trace.qualityrecallcampaign",
                    ),
                ),
                (
                    "linked_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="linked_quality_recall_cases",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "quality_case",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="recall_campaign_links",
                        to="quality_trace.batchqualitycase",
                    ),
                ),
                (
                    "source_campaign",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="migrated_case_links",
                        to="quality_trace.qualityrecallcampaign",
                    ),
                ),
            ],
            options={"ordering": ("campaign_id", "link_type", "quality_case_id")},
        ),
        migrations.AddConstraint(
            model_name="qualityrecallcampaigncaselink",
            constraint=models.UniqueConstraint(
                fields=("campaign", "quality_case"),
                name="qtrace_unique_recall_campaign_case",
            ),
        ),
        migrations.AddIndex(
            model_name="qualityrecallcampaigncaselink",
            index=models.Index(
                fields=["quality_case", "link_type"],
                name="qtrace_recall_case_link_idx",
            ),
        ),
        migrations.RunPython(
            backfill_primary_case_links,
            remove_backfilled_primary_case_links,
        ),
    ]
