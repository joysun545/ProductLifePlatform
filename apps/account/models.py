# /apps/account/models.py

import uuid

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import F, Q


class InvitationScope(models.TextChoices):
    """普通组织邀请的两种业务范围。"""

    SAME_ORGANIZATION = "same_organization", "加入同一组织"
    CROSS_ORGANIZATION = "cross_organization", "建立跨组织关系"


class OrganizationRelationType(models.TextChoices):
    """组织之间的业务关系类型。"""

    SUPPLY = "SUPPLY", "供应关系"
    DISTRIBUTION = "DISTRIBUTION", "经销关系"
    SERVICE = "SERVICE", "服务关系"
    OEM = "OEM", "OEM关系"


class UIColorScheme(models.TextChoices):
    """The three operator-selectable brand colour systems."""

    JADE = "jade", "翡翠绿"
    OCEAN = "ocean", "深海蓝"
    AMBER = "amber", "琥珀金"


class UIStyleScheme(models.TextChoices):
    """The three operator-selectable page/header design languages."""

    FLOW = "flow", "柔和流线"
    ORDER = "order", "专业秩序"
    LAYER = "layer", "层叠质感"


class User(AbstractUser):
    """
    微信业务账号，不代表需要实名认证的自然人。

    一个微信账号最多拥有一个 UserOrganization 业务身份；
    没有 UserOrganization 的账号按游客处理。
    """

    phone = models.CharField(
        max_length=32,
        null=True,
        blank=True,
        db_index=True,
        verbose_name="手机号",
    )
    wx_openid = models.CharField(
        max_length=128,
        null=True,
        blank=True,
        unique=True,
        verbose_name="微信OpenID",
    )
    wx_nickname = models.CharField(
        max_length=128,
        null=True,
        blank=True,
        verbose_name="微信昵称",
    )
    wx_avatar = models.CharField(
        max_length=512,
        null=True,
        blank=True,
        verbose_name="微信头像",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="创建时间",
    )

    class Meta:
        verbose_name = "微信业务账号"
        verbose_name_plural = "微信业务账号"

    def __str__(self):
        return self.wx_nickname or self.username


class OrganizationType(models.Model):
    """厂家、商家、供应商、服务商、用户等组织类型。"""

    code = models.CharField(
        max_length=32,
        unique=True,
        verbose_name="类型编码",
    )
    name = models.CharField(
        max_length=64,
        verbose_name="类型名称",
    )
    description = models.TextField(
        null=True,
        blank=True,
        verbose_name="说明",
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name="是否启用",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="创建时间",
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name="更新时间",
    )

    class Meta:
        ordering = ("code",)
        verbose_name = "组织类型"
        verbose_name_plural = "组织类型"

    def __str__(self):
        return self.name


class Organization(models.Model):
    """
    企业或业务主体。

    上下游关系不在本表保存，统一由 OrganizationRelation 表达，
    避免 Organization.parent 与真实业务链路形成两套来源。
    """

    name = models.CharField(
        max_length=128,
        verbose_name="组织名称",
    )
    code = models.CharField(
        max_length=64,
        unique=True,
        verbose_name="组织编码",
    )
    organization_type = models.ForeignKey(
        OrganizationType,
        on_delete=models.PROTECT,
        related_name="organizations",
        verbose_name="组织类型",
    )
    factory_root = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="tenant_organizations",
        verbose_name="所属厂家根组织",
        help_text="厂家负责人组织以自身为根；其他组织沿邀请链路归属唯一厂家。",
    )
    region = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="所在或负责区域",
    )
    address = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="详细地址",
    )
    provides_after_sales = models.BooleanField(
        default=False,
        verbose_name="是否提供售后服务",
        help_text="由有权管理该商家组织的上级在管理下级用户时设置。",
    )
    ui_color_scheme = models.CharField(
        max_length=16,
        choices=UIColorScheme.choices,
        blank=True,
        default="",
        verbose_name="小程序全局色系",
        help_text="仅系统运营业主设置；留空时使用翡翠绿。",
    )
    ui_style_scheme = models.CharField(
        max_length=16,
        choices=UIStyleScheme.choices,
        blank=True,
        default="",
        verbose_name="小程序全局页面风格",
        help_text="仅系统运营业主设置；留空时使用柔和流线。",
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name="是否启用",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="创建时间",
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name="更新时间",
    )

    class Meta:
        ordering = ("name", "id")
        verbose_name = "组织"
        verbose_name_plural = "组织"

        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(ui_color_scheme="")
                    | Q(ui_color_scheme__in=[choice[0] for choice in UIColorScheme.choices])
                ),
                name="account_organization_valid_ui_color",
            ),
            models.CheckConstraint(
                condition=(
                    Q(ui_style_scheme="")
                    | Q(ui_style_scheme__in=[choice[0] for choice in UIStyleScheme.choices])
                ),
                name="account_organization_valid_ui_style",
            ),
        ]

    def clean(self):
        super().clean()
        if self.organization_type_id and self.organization_type.code == "OWNER":
            if self.factory_root_id and self.pk and self.factory_root_id != self.pk:
                raise ValidationError({"factory_root": "厂家负责人组织必须以自身为根。"})
        elif self.factory_root_id:
            root = self.factory_root
            if (
                root.organization_type.code != "OWNER"
                or root.factory_root_id != root.pk
                or (self.is_active and not root.is_active)
            ):
                raise ValidationError({"factory_root": "所属厂家根必须是厂家负责人组织。"})
        elif self.organization_type_id and self.is_active:
            raise ValidationError({"factory_root": "有效业务组织必须明确所属厂家根组织。"})
        if (
            self.provides_after_sales
            and self.organization_type_id
            and self.organization_type.code != "MERCHANT"
        ):
            raise ValidationError("只有商家组织可以设置为提供售后服务。")
        if (
            (self.ui_color_scheme or self.ui_style_scheme)
            and self.organization_type_id
            and self.organization_type.code != "OWNER"
        ):
            raise ValidationError(
                "小程序全局主题只能配置在系统运营业主（厂家）组织上。"
            )

    @transaction.atomic
    def save(self, *args, **kwargs):
        is_factory_root = bool(
            self.organization_type_id
            and self.organization_type.code == "OWNER"
        )
        if self.pk:
            persisted_root_id = (
                type(self).objects.filter(pk=self.pk)
                .values_list("factory_root_id", flat=True)
                .first()
            )
            if persisted_root_id and persisted_root_id != self.factory_root_id:
                raise ValidationError(
                    "组织一旦归属厂家根组织，就不能迁移到另一条厂家链路。"
                )
        if is_factory_root and self.pk:
            if self.factory_root_id not in (None, self.pk):
                raise ValidationError("厂家负责人组织必须以自身为根。")
            root_was_unset = self.factory_root_id is None
            self.factory_root_id = self.pk
            if root_was_unset and kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = tuple(
                    set(kwargs["update_fields"]) | {"factory_root"}
                )
        super().save(*args, **kwargs)
        # The primary key is allocated by the first insert, then the root FK
        # points back to this same row. Keep this invariant on admin edits too.
        if is_factory_root and self.factory_root_id != self.pk:
            type(self).objects.filter(pk=self.pk).update(factory_root_id=self.pk)
            self.factory_root_id = self.pk

    def get_factory_root(self):
        if self.organization_type_id and self.organization_type.code == "OWNER":
            return self
        return self.factory_root

    def __str__(self):
        return self.name


class OrganizationRole(models.Model):
    """某种组织类型下可使用的岗位。"""

    organization_type = models.ForeignKey(
        OrganizationType,
        on_delete=models.PROTECT,
        related_name="roles",
        verbose_name="所属组织类型",
    )
    code = models.CharField(
        max_length=64,
        db_index=True,
        verbose_name="角色编码",
    )
    name = models.CharField(
        max_length=64,
        verbose_name="角色名称",
    )
    is_owner_role = models.BooleanField(
        default=False,
        verbose_name="是否负责人角色",
    )
    description = models.TextField(
        null=True,
        blank=True,
        verbose_name="角色说明",
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name="是否启用",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="创建时间",
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name="更新时间",
    )

    class Meta:
        ordering = ("organization_type__code", "code")
        constraints = [
            models.UniqueConstraint(
                fields=("organization_type", "code"),
                name="account_unique_org_type_role_code",
            ),
        ]
        verbose_name = "组织岗位"
        verbose_name_plural = "组织岗位"

    def __str__(self):
        return f"{self.organization_type.name}-{self.name}"


class RoleInvitationRule(models.Model):
    """
    角色邀请规则。

    例如：
    factory_matching -> supplier_owner / 跨组织 / SUPPLY
    factory_sales -> merchant_owner / 跨组织 / DISTRIBUTION
    merchant_senior_manager -> merchant_owner / 跨组织 / DISTRIBUTION

    同组织邀请规则由负责人或已获授权的高级经理岗位作为邀请方。
    """

    inviter_role = models.ForeignKey(
        OrganizationRole,
        on_delete=models.PROTECT,
        related_name="outgoing_invitation_rules",
        verbose_name="邀请方角色",
    )
    target_role = models.ForeignKey(
        OrganizationRole,
        on_delete=models.PROTECT,
        related_name="incoming_invitation_rules",
        verbose_name="目标角色",
    )
    scope = models.CharField(
        max_length=32,
        choices=InvitationScope.choices,
        verbose_name="邀请范围",
    )
    relation_type = models.CharField(
        max_length=32,
        choices=OrganizationRelationType.choices,
        blank=True,
        default="",
        verbose_name="建立的组织关系",
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name="是否启用",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="创建时间",
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name="更新时间",
    )

    class Meta:
        ordering = ("inviter_role__code", "target_role__code")
        constraints = [
            models.UniqueConstraint(
                fields=(
                    "inviter_role",
                    "target_role",
                    "scope",
                    "relation_type",
                ),
                name="account_unique_role_invitation_rule",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        scope=InvitationScope.SAME_ORGANIZATION,
                        relation_type="",
                    )
                    | (
                        Q(scope=InvitationScope.CROSS_ORGANIZATION)
                        & ~Q(relation_type="")
                    )
                ),
                name="account_invite_scope_relation_consistent",
            ),
        ]
        verbose_name = "角色邀请规则"
        verbose_name_plural = "角色邀请规则"

    def clean(self):
        super().clean()

        if not self.inviter_role_id or not self.target_role_id:
            return

        if self.scope == InvitationScope.SAME_ORGANIZATION:
            if not self.inviter_role.is_owner_role and self.inviter_role.code not in {
                "factory_senior_manager",
                "merchant_senior_manager",
            }:
                raise ValidationError(
                    {"inviter_role": "同组织邀请只能由负责人或高级经理角色发起。"}
                )

            if (
                self.inviter_role.organization_type_id
                != self.target_role.organization_type_id
            ):
                raise ValidationError(
                    {"target_role": "同组织邀请的双方角色必须属于同一组织类型。"}
                )

            if self.relation_type:
                raise ValidationError(
                    {"relation_type": "同组织邀请不能建立新的组织关系。"}
                )

        elif not self.relation_type:
            raise ValidationError(
                {"relation_type": "跨组织邀请必须指定建立的组织关系。"}
            )

    def __str__(self):
        return f"{self.inviter_role.code} -> {self.target_role.code}"


class UserOrganization(models.Model):
    """
    微信账号唯一的组织岗位身份。

    user 使用 OneToOneField，数据库层面保证一个微信账号不能同时属于
    多个组织或担任多个岗位。身兼多职时应使用多个微信账号。
    """

    user = models.OneToOneField(
        User,
        on_delete=models.PROTECT,
        related_name="organization_identity",
        verbose_name="微信业务账号",
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="members",
        verbose_name="所属组织",
    )
    role = models.ForeignKey(
        OrganizationRole,
        on_delete=models.PROTECT,
        related_name="members",
        verbose_name="组织岗位",
    )
    inviter = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="invited_organization_identities",
        verbose_name="邀请来源账号",
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name="是否有效",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="加入时间",
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name="更新时间",
    )

    class Meta:
        ordering = ("organization__name", "role__code", "id")
        constraints = [
            models.CheckConstraint(
                condition=(Q(inviter__isnull=True) | ~Q(user=F("inviter"))),
                name="account_membership_inviter_not_self",
            ),
        ]
        verbose_name = "账号组织身份"
        verbose_name_plural = "账号组织身份"

    def clean(self):
        super().clean()

        if self.is_active and self.organization_id:
            factory_root = self.organization.get_factory_root()
            if not factory_root:
                raise ValidationError(
                    {"organization": "有效角色槽位必须归属一个明确的厂家根组织。"}
                )

        if (
            self.organization_id
            and self.role_id
            and self.organization.organization_type_id
            != self.role.organization_type_id
        ):
            raise ValidationError(
                {"role": "岗位类型必须与所属组织类型一致。"}
            )

        if self.user_id and self.inviter_id and self.user_id == self.inviter_id:
            raise ValidationError(
                {"inviter": "账号不能邀请自己建立组织身份。"}
            )

    def __str__(self):
        return f"{self.user}-{self.organization}-{self.role.name}"


class SeniorManagerProfile(models.Model):
    """Informational title and the explicit system roles delegated to a manager."""

    identity = models.OneToOneField(
        UserOrganization,
        on_delete=models.CASCADE,
        related_name="senior_manager_profile",
        verbose_name="高级经理组织身份",
    )
    job_title = models.CharField(
        max_length=64,
        blank=True,
        default="",
        verbose_name="分管职位名称",
        help_text="仅供链路信息展示，不作为额外权限来源。",
    )
    managed_roles = models.ManyToManyField(
        OrganizationRole,
        related_name="senior_manager_profiles",
        blank=True,
        verbose_name="分管系统岗位",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "高级经理分管岗位"
        verbose_name_plural = "高级经理分管岗位"

    def clean(self):
        super().clean()
        if not self.identity_id:
            return
        identity = self.identity
        if identity.role.code not in {
            "factory_senior_manager",
            "merchant_senior_manager",
        }:
            raise ValidationError({"identity": "只有高级经理身份可以设置分管岗位。"})

    def __str__(self):
        return f"{self.identity.user}-{self.job_title or self.identity.role.name}"


class InvitationRecord(models.Model):
    """
    普通组织邀请的完整事件记录。

    记录在分享链接生成时创建，而不是等接受后才创建。目标角色由邀请方
    根据有效的 RoleInvitationRule 指定，受邀者只能确认或拒绝，不能修改。
    终端用户通过安装临时二维码建立关系，不使用本模型的普通邀请流程。
    """

    class Status(models.TextChoices):
        PENDING = "pending", "待接受"
        ACCEPTED = "accepted", "已接受"
        CANCELLED = "cancelled", "已取消"
        EXPIRED = "expired", "已过期"

    token = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
        verbose_name="邀请令牌",
    )
    inviter = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name="sent_invitations",
        verbose_name="邀请账号",
    )
    source_organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="outgoing_invitations",
        verbose_name="邀请方组织",
    )
    inviter_role = models.ForeignKey(
        OrganizationRole,
        on_delete=models.PROTECT,
        related_name="invitations_sent_as_role",
        verbose_name="邀请时的岗位",
    )
    rule = models.ForeignKey(
        RoleInvitationRule,
        on_delete=models.PROTECT,
        related_name="invitations",
        verbose_name="采用的邀请规则",
    )
    target_role = models.ForeignKey(
        OrganizationRole,
        on_delete=models.PROTECT,
        related_name="invitations_targeting_role",
        verbose_name="指定的目标岗位",
    )
    senior_manager_job_title = models.CharField(
        max_length=64,
        blank=True,
        default="",
        verbose_name="高级经理分管职位名称",
    )
    senior_manager_managed_role_codes = models.JSONField(
        default=list,
        blank=True,
        verbose_name="高级经理分管岗位编码快照",
        help_text="邀请创建时的授权快照；接受后写入高级经理分管岗位。",
    )
    target_organization = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="incoming_invitations",
        verbose_name="目标组织",
    )
    target_organization_provides_after_sales = models.BooleanField(
        default=False,
        editable=False,
        verbose_name="旧版邀请售后资格标记",
        help_text="仅为兼容历史邀请保留；新邀请流程不再使用此字段。",
    )
    invitee = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="received_invitations",
        verbose_name="接受邀请的账号",
    )
    scope = models.CharField(
        max_length=32,
        choices=InvitationScope.choices,
        verbose_name="邀请范围",
    )
    relation_type = models.CharField(
        max_length=32,
        choices=OrganizationRelationType.choices,
        blank=True,
        default="",
        verbose_name="建立的组织关系",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
        verbose_name="邀请状态",
    )
    expires_at = models.DateTimeField(
        verbose_name="失效时间",
    )
    accepted_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="接受时间",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="创建时间",
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name="更新时间",
    )

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.CheckConstraint(
                condition=(
                    ~Q(status="accepted")
                    | (
                        Q(invitee__isnull=False)
                        & Q(accepted_at__isnull=False)
                    )
                ),
                name="account_accepted_invite_has_user_time",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        scope=InvitationScope.SAME_ORGANIZATION,
                        relation_type="",
                    )
                    | (
                        Q(scope=InvitationScope.CROSS_ORGANIZATION)
                        & ~Q(relation_type="")
                    )
                ),
                name="account_record_scope_relation_consistent",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(scope=InvitationScope.SAME_ORGANIZATION)
                    | Q(target_organization=F("source_organization"))
                ),
                name="account_same_org_invite_target_matches",
            ),
            models.CheckConstraint(
                condition=(
                    Q(invitee__isnull=True)
                    | ~Q(invitee=F("inviter"))
                ),
                name="account_invitation_not_self",
            ),
        ]
        verbose_name = "邀请记录"
        verbose_name_plural = "邀请记录"

    def clean(self):
        super().clean()

        is_senior_manager_invitation = bool(
            self.target_role_id
            and self.target_role.code in {
                "factory_senior_manager",
                "merchant_senior_manager",
            }
        )
        selected_managed_role_codes = self.senior_manager_managed_role_codes
        if not isinstance(selected_managed_role_codes, list) or any(
            not isinstance(code, str) or not code.strip()
            for code in selected_managed_role_codes
        ):
            raise ValidationError(
                {"senior_manager_managed_role_codes": "高级经理分管岗位数据无效。"}
            )
        if len(set(selected_managed_role_codes)) != len(selected_managed_role_codes):
            raise ValidationError(
                {"senior_manager_managed_role_codes": "分管岗位不能重复。"}
            )
        if is_senior_manager_invitation:
            if self.scope != InvitationScope.SAME_ORGANIZATION:
                raise ValidationError({"scope": "高级经理必须加入邀请方组织。"})
            if not selected_managed_role_codes:
                raise ValidationError(
                    {"senior_manager_managed_role_codes": "请至少选择一个分管系统岗位。"}
                )
            roles = OrganizationRole.objects.filter(
                code__in=selected_managed_role_codes,
                organization_type_id=self.target_role.organization_type_id,
                is_active=True,
                is_owner_role=False,
            )
            valid_codes = set(roles.values_list("code", flat=True))
            if valid_codes != set(selected_managed_role_codes) or valid_codes.intersection(
                {"factory_senior_manager", "merchant_senior_manager"}
            ):
                raise ValidationError(
                    {"senior_manager_managed_role_codes": "只能选择本组织类型下有效的普通系统岗位。"}
                )
        elif selected_managed_role_codes or self.senior_manager_job_title:
            raise ValidationError(
                {"senior_manager_managed_role_codes": "只有邀请高级经理时才能填写分管信息。"}
            )

        if self.target_organization_provides_after_sales:
            if (
                self.scope != InvitationScope.CROSS_ORGANIZATION
                or not self.target_role_id
                or self.target_role.code != "merchant_owner"
                or self.target_role.organization_type.code != "MERCHANT"
            ):
                raise ValidationError(
                    {
                        "target_organization_provides_after_sales":
                            "该选项仅适用于邀请建立新的商家组织。"
                    }
                )

            if self.status == self.Status.ACCEPTED:
                if not self.target_organization_id:
                    raise ValidationError(
                        {
                            "target_organization_provides_after_sales":
                                "已接受的售后服务资格邀请必须关联新建的商家组织。"
                        }
                    )

                source_root = (
                    self.source_organization.get_factory_root()
                    if self.source_organization_id
                    else None
                )
                target_root = self.target_organization.get_factory_root()
                if (
                    not source_root
                    or not target_root
                    or source_root.pk != target_root.pk
                ):
                    raise ValidationError(
                        {
                            "target_organization":
                                "目标商家必须属于邀请方的厂家根链路。"
                        }
                    )
            elif self.target_organization_id is not None:
                raise ValidationError(
                    {
                        "target_organization_provides_after_sales":
                            "待接受的售后服务资格邀请不能预先关联目标组织。"
                    }
                )

        if (
            self.source_organization_id
            and self.inviter_role_id
            and self.source_organization.organization_type_id
            != self.inviter_role.organization_type_id
        ):
            raise ValidationError(
                {"inviter_role": "邀请岗位必须属于邀请方组织类型。"}
            )

        if self.rule_id:
            errors = {}
            if self.inviter_role_id != self.rule.inviter_role_id:
                errors["inviter_role"] = "邀请岗位与所选邀请规则不一致。"
            if self.target_role_id != self.rule.target_role_id:
                errors["target_role"] = "目标岗位与所选邀请规则不一致。"
            if self.scope != self.rule.scope:
                errors["scope"] = "邀请范围与所选邀请规则不一致。"
            if self.relation_type != self.rule.relation_type:
                errors["relation_type"] = "组织关系与所选邀请规则不一致。"
            if errors:
                raise ValidationError(errors)

        if self.scope == InvitationScope.SAME_ORGANIZATION:
            if (
                self.source_organization_id
                and self.target_organization_id
                != self.source_organization_id
            ):
                raise ValidationError(
                    {"target_organization": "同组织邀请必须加入邀请方组织。"}
                )

        elif (
            self.target_organization_id
            and self.target_role_id
            and self.target_organization.organization_type_id
            != self.target_role.organization_type_id
        ):
            raise ValidationError(
                {"target_organization": "目标组织类型与目标岗位类型不一致。"}
            )

        if (
            self.status == self.Status.ACCEPTED
            and (not self.invitee_id or not self.accepted_at)
        ):
            raise ValidationError("已接受的邀请必须记录接受账号和接受时间。")

        if self.inviter_id and self.invitee_id and self.inviter_id == self.invitee_id:
            raise ValidationError(
                {"invitee": "邀请账号不能接受自己发出的邀请。"}
            )

    def __str__(self):
        return f"{self.inviter} -> {self.target_role.name} ({self.status})"


class PlatformFactoryInvitation(models.Model):
    """A project-admin invitation that creates one independent factory root."""

    class Status(models.TextChoices):
        PENDING = "pending", "待接受"
        ACCEPTED = "accepted", "已接受"
        CANCELLED = "cancelled", "已取消"
        EXPIRED = "expired", "已过期"

    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    inviter = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="platform_factory_invitations_sent"
    )
    organization_name = models.CharField(max_length=128, verbose_name="厂家组织名称")
    region = models.CharField(max_length=255, blank=True, default="")
    address = models.CharField(max_length=255, blank=True, default="")
    target_role = models.ForeignKey(
        OrganizationRole,
        on_delete=models.PROTECT,
        related_name="platform_factory_invitations",
    )
    invitee = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="received_platform_factory_invitations",
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True)
    expires_at = models.DateTimeField(db_index=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at", "-id")
        constraints = [
            models.CheckConstraint(
                condition=(
                    ~Q(status="accepted")
                    | (Q(invitee__isnull=False) & Q(accepted_at__isnull=False))
                ),
                name="account_platform_invite_accepted_fields",
            ),
            models.CheckConstraint(
                condition=Q(invitee__isnull=True) | ~Q(invitee=F("inviter")),
                name="account_platform_invite_not_self",
            ),
        ]
        verbose_name = "平台厂家负责人邀请"
        verbose_name_plural = "平台厂家负责人邀请"

    def clean(self):
        super().clean()
        if self.target_role_id and (
            self.target_role.code != "factory_admin"
            or self.target_role.organization_type.code != "OWNER"
            or not self.target_role.is_owner_role
        ):
            raise ValidationError({"target_role": "平台邀请只能指定厂家负责人岗位。"})
        self.organization_name = (self.organization_name or "").strip()
        if not self.organization_name:
            raise ValidationError({"organization_name": "请填写厂家组织名称。"})
        if self.inviter_id and self.invitee_id == self.inviter_id:
            raise ValidationError({"invitee": "邀请人不能接受自己发出的邀请。"})

    def __str__(self):
        return f"{self.organization_name} -> {self.target_role.name} ({self.status})"


class OrganizationRelation(models.Model):
    """
    邀请建立的组织业务链路。

    source_organization 固定表示邀请方所在组织，target_organization 固定表示
    被邀请建立或加入的组织。这个方向表示关系建立链路，不等同于实物流向；
    设备实际流转由 inventory/lifecycle 的业务记录表达。
    """

    source_organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="relations_out",
        verbose_name="关系发起组织",
    )
    target_organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="relations_in",
        verbose_name="关系目标组织",
    )
    relation_type = models.CharField(
        max_length=32,
        choices=OrganizationRelationType.choices,
        db_index=True,
        verbose_name="关系类型",
    )
    created_by_invitation = models.OneToOneField(
        InvitationRecord,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="created_organization_relation",
        verbose_name="建立关系的邀请记录",
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name="是否有效",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="创建时间",
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name="更新时间",
    )

    class Meta:
        ordering = ("relation_type", "source_organization_id", "id")
        constraints = [
            models.UniqueConstraint(
                fields=(
                    "source_organization",
                    "target_organization",
                    "relation_type",
                ),
                name="account_unique_organization_relation",
            ),
            models.CheckConstraint(
                condition=~Q(
                    source_organization=F("target_organization")
                ),
                name="account_organization_relation_not_self",
            ),
        ]
        verbose_name = "组织关系"
        verbose_name_plural = "组织关系"

    def __str__(self):
        return (
            f"{self.source_organization} -{self.relation_type}-> "
            f"{self.target_organization}"
        )

    def clean(self):
        super().clean()
        if not self.source_organization_id or not self.target_organization_id:
            return
        source_root = self.source_organization.get_factory_root()
        target_root = self.target_organization.get_factory_root()
        if not source_root or not target_root:
            raise ValidationError("组织关系双方都必须明确所属厂家根组织。")
        if source_root.pk != target_root.pk:
            raise ValidationError("组织关系不能跨越不同的厂家根组织。")

    @transaction.atomic
    def save(self, *args, **kwargs):
        if self.source_organization_id and self.target_organization_id:
            source = self.source_organization
            target = self.target_organization
            source_root = source.get_factory_root()
            target_root = target.get_factory_root()
            if not source_root:
                raise ValidationError("邀请方组织尚未明确所属厂家，不能建立新链路。")
            if target.organization_type.code == "OWNER":
                if target.pk != source_root.pk:
                    raise ValidationError("组织关系不能连接不同的厂家根组织。")
            elif target_root and target_root.pk != source_root.pk:
                raise ValidationError("组织关系不能跨越不同的厂家根组织。")
            elif not target_root:
                target.factory_root = source_root
                target.save(update_fields=("factory_root", "updated_at"))
        super().save(*args, **kwargs)
