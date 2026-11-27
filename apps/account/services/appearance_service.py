from account.models import (
    OrganizationRelation,
    UIColorScheme,
    UIStyleScheme,
)


DEFAULT_COLOR_SCHEME = UIColorScheme.ASH
DEFAULT_STYLE_SCHEME = UIStyleScheme.FLOW
MAX_UPSTREAM_DEPTH = 32
COLOR_DESCRIPTIONS = {
    UIColorScheme.AMETHYST: "紫色强调与暖米白底",
    UIColorScheme.MARS: "暖橙强调与暖米白底",
    UIColorScheme.ASH: "深绿强调与暖米白底",
    UIColorScheme.SPLASHED: "洒蓝强调与暖米白底",
    UIColorScheme.NAVY_ORANGE: "深蓝前景与亮橙底",
    UIColorScheme.TEAL_ROSE: "深青绿前景与玫瑰粉底",
    UIColorScheme.PRUSSIAN_MIST: "普鲁士蓝前景与雾灰底",
    UIColorScheme.KLEIN_YELLOW: "克莱因蓝前景与松花黄底",
}
STYLE_DESCRIPTIONS = {
    UIStyleScheme.FLOW: "大圆角、柔和渐变和轻盈卡片",
    UIStyleScheme.ORDER: "规整边界、低阴影和信息密度优先",
    UIStyleScheme.LAYER: "悬浮层次、胶囊控件和品牌展示感",
}


class AppearanceError(Exception):
    """The operator appearance cannot be read or changed."""


def _appearance_owner(identity):
    """Use the explicit factory root; only infer legacy chains when unambiguous."""

    if not identity:
        return None

    organization = identity.organization
    if organization.organization_type.code == "OWNER":
        return organization
    explicit_root = organization.get_factory_root()
    if explicit_root:
        return explicit_root

    visited = {organization.pk}
    frontier = {organization.pk}
    owners_by_id = {}

    for _depth in range(MAX_UPSTREAM_DEPTH):
        relations = list(
            OrganizationRelation.objects.select_related(
                "source_organization__organization_type"
            )
            .filter(target_organization_id__in=frontier, is_active=True)
            .order_by("source_organization_id", "id")
        )
        if not relations:
            break

        owners = [
            relation.source_organization
            for relation in relations
            if relation.source_organization.is_active
            and relation.source_organization.organization_type.is_active
            and relation.source_organization.organization_type.code == "OWNER"
        ]
        for owner in owners:
            owners_by_id[owner.pk] = owner

        next_frontier = {
            relation.source_organization_id
            for relation in relations
            if relation.source_organization_id not in visited
            and relation.source_organization.is_active
            and relation.source_organization.organization_type.is_active
        }
        if not next_frontier:
            break
        visited.update(next_frontier)
        frontier = next_frontier

    return next(iter(owners_by_id.values())) if len(owners_by_id) == 1 else None


def serialize_appearance(identity) -> dict:
    owner = _appearance_owner(identity)
    color_scheme = (
        owner.ui_color_scheme if owner and owner.ui_color_scheme else DEFAULT_COLOR_SCHEME
    )
    style_scheme = (
        owner.ui_style_scheme if owner and owner.ui_style_scheme else DEFAULT_STYLE_SCHEME
    )
    return {
        "color_scheme": str(color_scheme),
        "style_scheme": str(style_scheme),
        "option_code": f"{color_scheme}-{style_scheme}",
        "owner_organization_id": owner.pk if owner else None,
        "owner_organization_name": owner.name if owner else "",
    }


def appearance_options() -> dict:
    return {
        "colors": [
            {
                "code": code,
                "name": name,
                "description": COLOR_DESCRIPTIONS[code],
            }
            for code, name in UIColorScheme.choices
        ],
        "styles": [
            {
                "code": code,
                "name": name,
                "description": STYLE_DESCRIPTIONS[code],
            }
            for code, name in UIStyleScheme.choices
        ],
    }


def update_operator_appearance(*, identity, color_scheme, style_scheme) -> dict:
    if (
        not identity
        or identity.organization.organization_type.code != "OWNER"
        or not identity.role.is_owner_role
    ):
        raise AppearanceError("仅系统运营业主负责人可以修改全局界面主题")

    color_scheme = str(color_scheme or "").strip()
    style_scheme = str(style_scheme or "").strip()
    valid_colors = {choice[0] for choice in UIColorScheme.choices}
    valid_styles = {choice[0] for choice in UIStyleScheme.choices}
    if color_scheme not in valid_colors:
        raise AppearanceError("请选择有效的全局色系")
    if style_scheme not in valid_styles:
        raise AppearanceError("请选择有效的页面风格")

    organization = identity.organization
    organization.ui_color_scheme = color_scheme
    organization.ui_style_scheme = style_scheme
    organization.full_clean()
    organization.save(
        update_fields=("ui_color_scheme", "ui_style_scheme", "updated_at")
    )
    return serialize_appearance(identity)
