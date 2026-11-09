from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("lifecycle", "0032_replacement_service_recovery")]
    operations = [
        migrations.AddField(model_name="aftersalesmedia", name="evidence_scope_key", field=models.CharField(blank=True, max_length=160, null=True)),
        migrations.AddField(model_name="aftersalesmedia", name="scope_slot", field=models.PositiveSmallIntegerField(blank=True, null=True)),
        migrations.AddField(model_name="aftersalesmedia", name="evidence_kind", field=models.CharField(blank=True, default="", max_length=16, null=True)),
        migrations.AddField(model_name="aftersalesmedia", name="client_upload_id", field=models.UUIDField(blank=True, null=True)),
        migrations.AddField(model_name="aftersalesmedia", name="content_sha256", field=models.CharField(blank=True, default="", max_length=64, null=True)),
        migrations.AddIndex(model_name="aftersalesmedia", index=models.Index(fields=["application", "evidence_scope_key"], name="life_as_media_scope")),
        migrations.AddConstraint(model_name="aftersalesmedia", constraint=models.UniqueConstraint(fields=("application", "evidence_scope_key", "scope_slot"), name="life_as_scope_slot_unique")),
        migrations.AddConstraint(model_name="aftersalesmedia", constraint=models.UniqueConstraint(fields=("application", "uploaded_by", "client_upload_id"), name="life_as_upload_request_unique")),
        migrations.AddConstraint(model_name="aftersalesmedia", constraint=models.CheckConstraint(condition=(
            models.Q(evidence_scope_key__isnull=True, scope_slot__isnull=True)
            | (models.Q(evidence_scope_key__isnull=False, scope_slot__isnull=False, scope_slot__gte=1, scope_slot__lte=9)
               & ~models.Q(evidence_scope_key=""))
        ), name="life_as_scope_slot_range")),
    ]
