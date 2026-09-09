from dataclasses import dataclass
from typing import Optional, Tuple


class ViewerRelation:
    """当前账号相对于某一台设备的主要属主关系。"""

    PUBLIC = "public"
    UNRELATED = "unrelated"
    DIRECT_OWNER = "direct_owner"
    UPSTREAM_ACCOUNT_OWNER = "upstream_account_owner"
    UPSTREAM_ORGANIZATION_OWNER = "upstream_organization_owner"
    REGIONAL_OWNER = "regional_owner"
    UPSTREAM_ORGANIZATION_MEMBER = "upstream_organization_member"

    LABELS = {
        PUBLIC: "游客",
        UNRELATED: "系统用户",
        DIRECT_OWNER: "本组织库存设备",
        UPSTREAM_ACCOUNT_OWNER: "我的下级链路设备",
        UPSTREAM_ORGANIZATION_OWNER: "本组织下级链路设备",
        REGIONAL_OWNER: "本区域设备",
        UPSTREAM_ORGANIZATION_MEMBER: "上级组织链路设备",
    }


def organization_payload(organization):
    if not organization:
        return None
    organization_type = getattr(organization, "organization_type", None)
    return {
        "id": organization.pk,
        "code": organization.code,
        "name": organization.name,
        "type": organization_type.code if organization_type else "",
        "type_name": organization_type.name if organization_type else "",
        "region": organization.region or "",
    }


def identity_payload(identity):
    if not identity:
        return None
    return {
        "identity_id": identity.pk,
        "user_id": identity.user_id,
        "nickname": identity.user.wx_nickname or identity.user.username,
        "role": identity.role.code,
        "role_name": identity.role.name,
        "organization": organization_payload(identity.organization),
    }


@dataclass(frozen=True)
class OwnershipLink:
    """一段上级组织 -> 下级组织关系，按距直接属主由近到远保存。"""

    relation: object
    upstream_account_identity: Optional[object] = None

    def as_dict(self):
        return {
            "relation_id": self.relation.pk,
            "relation_type": self.relation.relation_type,
            "source_organization": organization_payload(
                self.relation.source_organization
            ),
            "target_organization": organization_payload(
                self.relation.target_organization
            ),
            "upstream_account_owner": identity_payload(
                self.upstream_account_identity
            ),
        }


@dataclass(frozen=True)
class DeviceOwnershipContext:
    viewer_identity: Optional[object]
    direct_owner: Optional[object]
    links: Tuple[OwnershipLink, ...]
    regional_owner: Optional[object]
    viewer_relation: str
    is_direct_owner: bool = False
    is_upstream_account_owner: bool = False
    is_upstream_organization: bool = False
    is_upstream_organization_owner: bool = False
    is_regional_owner: bool = False

    @property
    def upstream_organizations(self):
        return tuple(link.relation.source_organization for link in self.links)

    @property
    def organization_path(self):
        """厂家/最高上级到当前直接属主的有序组织链路。"""

        ancestors = list(reversed(self.upstream_organizations))
        if self.direct_owner:
            ancestors.append(self.direct_owner)

        result = []
        seen = set()
        for organization in ancestors:
            if organization.pk in seen:
                continue
            seen.add(organization.pk)
            result.append(organization)
        return tuple(result)

    @property
    def is_chain_related(self):
        return any(
            (
                self.is_direct_owner,
                self.is_upstream_account_owner,
                self.is_upstream_organization,
                self.is_upstream_organization_owner,
                self.is_regional_owner,
            )
        )

    @property
    def viewer_relations(self):
        """返回全部同时成立的关系；同一账号可能兼具多种上级关系。"""

        if not self.viewer_identity:
            return (ViewerRelation.PUBLIC,)

        relations = []
        if self.is_direct_owner:
            relations.append(ViewerRelation.DIRECT_OWNER)
        if self.is_upstream_account_owner:
            relations.append(ViewerRelation.UPSTREAM_ACCOUNT_OWNER)
        if self.is_upstream_organization_owner:
            relations.append(ViewerRelation.UPSTREAM_ORGANIZATION_OWNER)
        if self.is_regional_owner:
            relations.append(ViewerRelation.REGIONAL_OWNER)
        if (
            self.is_upstream_organization
            and not self.is_upstream_organization_owner
        ):
            relations.append(ViewerRelation.UPSTREAM_ORGANIZATION_MEMBER)
        if not relations:
            relations.append(ViewerRelation.UNRELATED)
        return tuple(relations)

    def as_dict(self):
        return {
            "viewer_relation": self.viewer_relation,
            "viewer_relation_name": ViewerRelation.LABELS.get(
                self.viewer_relation,
                "设备关系",
            ),
            "viewer_relations": list(self.viewer_relations),
            "viewer_relation_names": [
                ViewerRelation.LABELS[relation]
                for relation in self.viewer_relations
            ],
            "direct_owner": organization_payload(self.direct_owner),
            "regional_owner": organization_payload(self.regional_owner),
            "upstream_account_owners": [
                identity_payload(link.upstream_account_identity)
                for link in self.links
                if link.upstream_account_identity
            ],
            "upstream_organization_owners": [
                organization_payload(organization)
                for organization in self.upstream_organizations
            ],
            "organization_path": [
                organization_payload(organization)
                for organization in self.organization_path
            ],
            "flags": {
                "is_direct_owner": self.is_direct_owner,
                "is_upstream_account_owner": self.is_upstream_account_owner,
                "is_upstream_organization": self.is_upstream_organization,
                "is_upstream_organization_owner": (
                    self.is_upstream_organization_owner
                ),
                "is_regional_owner": self.is_regional_owner,
            },
        }


@dataclass(frozen=True)
class DeviceAccessDecision:
    view_mode: str
    capabilities: Tuple[str, ...]
    ownership: DeviceOwnershipContext

    def allows(self, capability):
        return capability in self.capabilities
