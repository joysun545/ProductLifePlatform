from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from account.models import Organization, OrganizationType, UserOrganization


class Command(BaseCommand):
    help = (
        "核对厂家内部角色槽位的邀请来源；仅在同一厂家恰有一个有效负责人时，"
        "可将负责人补为全部有效内部岗位的邀请人。默认只检查。"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="应用明确无歧义的厂家负责人邀请关系修复。",
        )

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        owner_type = OrganizationType.objects.get(code="OWNER")
        changes = []
        skipped = []

        organizations = Organization.objects.filter(
            organization_type=owner_type,
            is_active=True,
        ).order_by("id")

        for organization in organizations:
            active_slots = UserOrganization.objects.filter(
                organization=organization,
                is_active=True,
                user__is_active=True,
                role__is_active=True,
            ).select_related("user", "role", "inviter")

            owners = [
                slot for slot in active_slots
                if slot.role.code == "factory_admin"
            ]
            staff_slots = [
                slot for slot in active_slots
                if slot.role.code != "factory_admin"
            ]
            if len(owners) != 1:
                if staff_slots:
                    skipped.append((organization, len(owners), len(staff_slots)))
                continue

            owner = owners[0]
            for slot in staff_slots:
                if slot.inviter_id != owner.user_id:
                    changes.append((slot, owner))

        for slot, owner in changes:
            self.stdout.write(
                f"{'修复' if apply_changes else '待修复'}："
                f"组织={slot.organization.name} 槽位={slot.pk} "
                f"岗位={slot.role.code} 用户ID={slot.user_id} "
                f"邀请人={slot.inviter_id or '未记录'} -> 厂家负责人ID={owner.user_id}"
            )

        for organization, owner_count, staff_count in skipped:
            self.stdout.write(
                self.style.WARNING(
                    f"跳过：组织={organization.name} 有效厂家负责人={owner_count}，"
                    f"有效员工槽位={staff_count}；无法无歧义确定邀请人。"
                )
            )

        if apply_changes and changes:
            with transaction.atomic():
                now = timezone.now()
                for slot, owner in changes:
                    slot.inviter = owner.user
                    slot.updated_at = now
                    slot.save(update_fields=("inviter", "updated_at"))

        state = "APPLIED" if apply_changes else "CHECK_ONLY"
        self.stdout.write(
            self.style.SUCCESS(
                f"{state}: 待修复/已修复 {len(changes)} 个槽位；"
                f"因负责人不唯一或缺失跳过 {len(skipped)} 个厂家组织。"
            )
        )
