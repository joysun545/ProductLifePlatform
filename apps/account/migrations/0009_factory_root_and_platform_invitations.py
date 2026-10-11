from collections import defaultdict, deque

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.db.models.expressions
import uuid


def backfill_factory_roots(apps, schema_editor):
    alias = schema_editor.connection.alias
    Organization = apps.get_model("account", "Organization")
    OrganizationRelation = apps.get_model("account", "OrganizationRelation")
    UserOrganization = apps.get_model("account", "UserOrganization")

    organizations = list(
        Organization.objects.using(alias).select_related("organization_type")
    )
    by_id = {org.pk: org for org in organizations}
    parents = defaultdict(list)
    relations = OrganizationRelation.objects.using(alias).filter(is_active=True)
    for source_id, target_id in relations.values_list(
        "source_organization_id", "target_organization_id"
    ):
        parents[target_id].append(source_id)

    updates = []
    for organization in organizations:
        if organization.organization_type.code == "OWNER":
            organization.factory_root_id = organization.pk
            updates.append(organization)
            continue

        roots = set()
        visited = set()
        queue = deque([organization.pk])
        while queue:
            target_id = queue.popleft()
            if target_id in visited:
                continue
            visited.add(target_id)
            for source_id in parents.get(target_id, ()):
                source = by_id.get(source_id)
                if not source:
                    continue
                if source.organization_type.code == "OWNER":
                    roots.add(source.pk)
                else:
                    queue.append(source.pk)

        # Do not guess when old data has zero or multiple possible roots.
        if len(roots) == 1:
            organization.factory_root_id = next(iter(roots))
            updates.append(organization)

    Organization.objects.using(alias).bulk_update(updates, ["factory_root"])

    # Keep legacy links only when both ends now have the same unambiguous
    # tenant root. Ambiguous or cross-root links remain as history but are
    # made inactive so they cannot join manufacturer data scopes.
    roots_by_id = {
        organization.pk: organization.factory_root_id
        for organization in organizations
    }
    unsafe_relation_ids = []
    for relation_id, source_id, target_id in relations.values_list(
        "id", "source_organization_id", "target_organization_id"
    ):
        source_root = roots_by_id.get(source_id)
        target_root = roots_by_id.get(target_id)
        if not source_root or not target_root or source_root != target_root:
            unsafe_relation_ids.append(relation_id)
    if unsafe_relation_ids:
        OrganizationRelation.objects.using(alias).filter(
            pk__in=unsafe_relation_ids
        ).update(is_active=False)

    # An unresolved organization has no safe tenant boundary. Preserve its role
    # slots but disable them until an administrator assigns a reviewed root.
    unresolved_organization_ids = [
        organization.pk
        for organization in organizations
        if organization.is_active
        and organization.organization_type.code != "OWNER"
        and not organization.factory_root_id
    ]
    if unresolved_organization_ids:
        UserOrganization.objects.using(alias).filter(
            organization_id__in=unresolved_organization_ids,
            is_active=True,
        ).update(is_active=False)


class Migration(migrations.Migration):

    dependencies = [
        ("account", "0008_merge_20260925_1313"),
    ]

    operations = [
        migrations.AddField(
            model_name="organization",
            name="factory_root",
            field=models.ForeignKey(
                blank=True,
                help_text="厂家负责人组织以自身为根；其他组织沿邀请链路归属唯一厂家。",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="tenant_organizations",
                to="account.organization",
                verbose_name="所属厂家根组织",
            ),
        ),
        migrations.RunPython(backfill_factory_roots),
        migrations.CreateModel(
            name="PlatformFactoryInvitation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("token", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("organization_name", models.CharField(max_length=128, verbose_name="厂家组织名称")),
                ("region", models.CharField(blank=True, default="", max_length=255)),
                ("address", models.CharField(blank=True, default="", max_length=255)),
                ("status", models.CharField(choices=[("pending", "待接受"), ("accepted", "已接受"), ("cancelled", "已取消"), ("expired", "已过期")], db_index=True, default="pending", max_length=16)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("accepted_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("invitee", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="received_platform_factory_invitations", to=settings.AUTH_USER_MODEL)),
                ("inviter", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="platform_factory_invitations_sent", to=settings.AUTH_USER_MODEL)),
                ("target_role", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="platform_factory_invitations", to="account.organizationrole")),
            ],
            options={
                "verbose_name": "平台厂家负责人邀请",
                "verbose_name_plural": "平台厂家负责人邀请",
                "ordering": ("-created_at", "-id"),
            },
        ),
        migrations.AddConstraint(
            model_name="platformfactoryinvitation",
            constraint=models.CheckConstraint(
                condition=(~models.Q(("status", "accepted")) | (models.Q(("invitee__isnull", False)) & models.Q(("accepted_at__isnull", False)))),
                name="account_platform_invite_accepted_fields",
            ),
        ),
        migrations.AddConstraint(
            model_name="platformfactoryinvitation",
            constraint=models.CheckConstraint(
                condition=models.Q(("invitee__isnull", True)) | ~models.Q(("invitee", django.db.models.expressions.F("inviter"))),
                name="account_platform_invite_not_self",
            ),
        ),
    ]
