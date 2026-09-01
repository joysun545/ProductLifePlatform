from dataclasses import dataclass

from account.models import UserOrganization


class StatisticsAccessError(Exception):
    pass


@dataclass(frozen=True)
class StatisticsIdentity:
    user: object
    identity: UserOrganization
    organization: object
    role: object

    @property
    def organization_type(self):
        return self.organization.organization_type.code


def get_statistics_identity(user):
    identity = (
        UserOrganization.objects
        .select_related("organization__organization_type", "role")
        .filter(user=user, user__is_active=True, is_active=True)
        .first()
    )
    if not identity:
        raise StatisticsAccessError("当前账号没有有效的组织岗位，无法查看数据统计。")
    if identity.organization.organization_type.code == "CUSTOMER":
        raise StatisticsAccessError("用户账号请从‘我的设备与服务’查看个人数据。")
    return StatisticsIdentity(user, identity, identity.organization, identity.role)
