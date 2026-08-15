from django.core.exceptions import ValidationError
from django.db import models


class MerchantChannelAssignment(models.Model):
    """厂家与直接商家的区域、品牌经营授权。"""

    relation = models.OneToOneField(
        "account.OrganizationRelation",
        on_delete=models.PROTECT,
        related_name="merchant_channel_assignment",
        verbose_name="经销组织关系",
    )
    brand = models.ForeignKey(
        "product.Brand",
        on_delete=models.PROTECT,
        related_name="merchant_channel_assignments",
        verbose_name="授权品牌",
    )
    region = models.CharField(
        max_length=255,
        verbose_name="负责区域",
    )
    assigned_by = models.ForeignKey(
        "account.User",
        on_delete=models.PROTECT,
        related_name="assigned_merchant_channels",
        verbose_name="指定销售经理",
    )
    is_active = models.BooleanField(default=True, verbose_name="是否有效")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        ordering = ("relation__target_organization__name", "id")
        verbose_name = "商家渠道授权"
        verbose_name_plural = "商家渠道授权"

    def clean(self):
        super().clean()
        if self.relation_id and self.relation.relation_type != "DISTRIBUTION":
            raise ValidationError({"relation": "只有经销关系可以配置商家渠道授权。"})
        if self.relation_id and self.assigned_by_id:
            invitation = self.relation.created_by_invitation
            if not invitation or invitation.inviter_id != self.assigned_by_id:
                raise ValidationError(
                    {"assigned_by": "渠道授权人必须是建立该直接商家关系的邀请账号。"}
                )
        if not (self.region or "").strip():
            raise ValidationError({"region": "负责区域不能为空。"})

    def __str__(self):
        return (
            f"{self.relation.target_organization.name}-"
            f"{self.brand.name}-{self.region}"
        )
