from dataclasses import dataclass

from account.models import UserOrganization

from account.services.auth_service import get_active_identity


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
    identity = get_active_identity(user)
    if not identity:
        raise StatisticsAccessError(
            "当前账号没有归属明确厂家链路的有效组织岗位，无法查看数据统计。"
        )
    return StatisticsIdentity(user, identity, identity.organization, identity.role)
