from collections import defaultdict, deque

from django.core.management.base import BaseCommand

from account.models import Organization, OrganizationRelation, UserOrganization


class Command(BaseCommand):
    help = "Read-only report of active organizations without one explicit factory root."

    def handle(self, *args, **options):
        organizations = list(
            Organization.objects.select_related("organization_type", "factory_root")
            .order_by("id")
        )
        by_id = {organization.pk: organization for organization in organizations}
        parents = defaultdict(list)
        for source_id, target_id in OrganizationRelation.objects.filter(
            is_active=True
        ).values_list("source_organization_id", "target_organization_id"):
            parents[target_id].append(source_id)

        unresolved = []
        for organization in organizations:
            if not organization.is_active:
                continue
            if organization.organization_type.code == "OWNER":
                if organization.factory_root_id == organization.pk:
                    continue
                unresolved.append((organization, [], "厂家负责人根标记异常"))
                continue
            if organization.factory_root_id:
                root = organization.factory_root
                if (
                    root
                    and root.is_active
                    and root.organization_type.code == "OWNER"
                    and root.factory_root_id == root.pk
                ):
                    continue
                unresolved.append((organization, [], "已配置的厂家根无效"))
                continue
            visited = set()
            roots = set()
            queue = deque([organization.pk])
            while queue:
                target_id = queue.popleft()
                if target_id in visited:
                    continue
                visited.add(target_id)
                for source_id in parents.get(target_id, ()):
                    parent = by_id.get(source_id)
                    if not parent or not parent.is_active:
                        continue
                    if parent.organization_type.code == "OWNER":
                        roots.add(parent.pk)
                    else:
                        queue.append(parent.pk)
            candidates = [
                f"{root_id}:{by_id[root_id].name}"
                for root_id in sorted(roots)
                if root_id in by_id
            ]
            unresolved.append((organization, candidates, ""))

        self.stdout.write(f"未明确厂家根的有效组织：{len(unresolved)}")
        for organization, candidates, configured_status in unresolved:
            status = configured_status or (
                "多个候选根" if len(candidates) > 1 else "无唯一有效根链路"
            )
            roots = ", ".join(candidates) if candidates else "无"
            self.stdout.write(
                f"组织ID={organization.pk} 类型={organization.organization_type.code} "
                f"名称={organization.name} 状态={status} 候选厂家={roots}"
            )
        unresolved_ids = {organization.pk for organization, _, _ in unresolved}
        unresolved_memberships = list(
            UserOrganization.objects.filter(
                organization_id__in=unresolved_ids,
            ).select_related("user", "organization").order_by("organization_id", "id")
        )
        active_unresolved_count = sum(
            1 for membership in unresolved_memberships if membership.is_active
        )
        self.stdout.write(
            f"厂家根未明确的角色槽位：{len(unresolved_memberships)} "
            f"（其中仍有效 {active_unresolved_count}）"
        )
        for membership in unresolved_memberships:
            self.stdout.write(
                f"槽位ID={membership.pk} 用户ID={membership.user_id} "
                f"组织ID={membership.organization_id}:{membership.organization.name} "
                f"槽位状态={'有效' if membership.is_active else '已停用'}"
            )
        unsafe_relations = list(
            OrganizationRelation.objects.select_related(
                "source_organization__factory_root",
                "target_organization__factory_root",
            )
            .order_by("id")
        )
        unsafe_relations = [
            relation
            for relation in unsafe_relations
            if (
                not relation.source_organization.get_factory_root()
                or not relation.target_organization.get_factory_root()
                or relation.source_organization.get_factory_root().pk
                != relation.target_organization.get_factory_root().pk
            )
        ]
        self.stdout.write(f"跨厂家或根归属不明的组织关系（含停用历史）：{len(unsafe_relations)}")
        for relation in unsafe_relations:
            self.stdout.write(
                f"关系ID={relation.pk} 状态={'有效' if relation.is_active else '历史/已停用'} "
                f"类型={relation.relation_type} "
                f"来源={relation.source_organization_id}:{relation.source_organization.name} "
                f"目标={relation.target_organization_id}:{relation.target_organization.name}"
            )
        self.stdout.write("FACTORY_ROOT_AUDIT_READ_ONLY_DONE")
