# apps/lifecycle/models/organization_device.py


from django.core.exceptions import ValidationError
from django.db import models



class OrganizationDeviceQuerySet(models.QuerySet):
    def update(self, **kwargs):
        target_org = kwargs.get("organization")
        target_org_id = kwargs.get("organization_id")
        if target_org is not None:
            target_org_id = getattr(target_org, "pk", target_org)
        if target_org_id is not None:
            organization_model = self.model._meta.get_field("organization").remote_field.model
            organization = (
                target_org
                if hasattr(target_org, "get_factory_root")
                else organization_model.objects.get(pk=target_org_id)
            )
            organization_root = organization.get_factory_root()
            if not organization_root:
                raise ValidationError("库存组织尚未明确所属厂家根组织。")
            for record in self.select_related("device__batch__manufacturer"):
                device_root = record.device.batch.manufacturer.get_factory_root()
                if not device_root or device_root.pk != organization_root.pk:
                    raise ValidationError("设备库存不能登记到其他厂家根组织。")
        return super().update(**kwargs)


class OrganizationDeviceManager(models.Manager.from_queryset(OrganizationDeviceQuerySet)):
    pass


class OrganizationDevice(models.Model):
    """
    设备组织归属记录。


    用于记录设备当前属于哪个组织。


    作用：

    1. 查询组织当前拥有多少设备。

    2. 查询厂家链路下设备分布。

    3. 支撑库存统计。

    4. 支撑激活率统计。


    注意：

    该模型记录当前关系。

    历史变化由：

    LifecycleEvent

    保存。


    示例：


    Device:

    JL5000-000001


    当前：

    重庆XX市代


    OrganizationDevice:

    device:
    JL5000-000001


    organization:
    重庆XX市代
    """



    STATUS_CHOICES = [

        (
            "IN_STOCK",
            "库存"
        ),

        (
            "INSTALLED",
            "已安装"
        ),

        (
            "SOLD",
            "已销售"
        ),

        (
            "RETURNED",
            "退货中"
        ),

        (
            "TRANSFERRED",
            "流转中"
        ),

        (
            "RETURNED_TO_FACTORY",
            "已退回厂家"
        ),

    ]



    objects = OrganizationDeviceManager()


    device = models.OneToOneField(
        "lifecycle.Device",
        on_delete=models.CASCADE,
        related_name="organization_record",
        verbose_name="设备"
    )



    organization = models.ForeignKey(
        "account.Organization",
        on_delete=models.PROTECT,
        related_name="owned_devices",
        verbose_name="所属组织"
    )



    status = models.CharField(
        max_length=32,
        choices=STATUS_CHOICES,
        default="IN_STOCK",
        db_index=True,
        verbose_name="设备状态"
    )



    received_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="接收时间"
    )



    delivered_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="交付时间"
    )



    last_event = models.ForeignKey(
        "lifecycle.LifecycleEvent",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="organization_device_records",
        verbose_name="最后流转事件"
    )



    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name="更新时间"
    )



    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="创建时间"
    )



    class Meta:

        verbose_name = "设备组织归属"

        verbose_name_plural = "设备组织归属"


        indexes = [

            models.Index(
                fields=[
                    "organization",
                    "status"
                ]
            ),

            models.Index(
                fields=[
                    "device"
                ]
            ),

        ]



    def _validate_factory_boundary(self):
        if not self.device_id or not self.organization_id:
            return
        manufacturer = self.device.batch.manufacturer
        factory_root = manufacturer.get_factory_root()
        organization_root = self.organization.get_factory_root()
        if not factory_root or not organization_root:
            raise ValidationError("设备和库存组织都必须明确所属厂家根组织。")
        if factory_root.pk != organization_root.pk:
            raise ValidationError("设备库存不能登记到其他厂家根组织。")

    def clean(self):
        super().clean()
        self._validate_factory_boundary()

    def save(self, *args, **kwargs):
        self._validate_factory_boundary()
        super().save(*args, **kwargs)

    def __str__(self):

        return (
            f"{self.device.sn}"
            f"-"
            f"{self.organization.name}"
        )
