import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):

    dependencies = [
        ("account", "0010_after_sales_qualification"),
        ("lifecycle", "0022_goods_transfer_rolling_receivables"),
    ]

    operations = [
        migrations.CreateModel(
            name="AfterSalesServiceStationApplication",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING_REVIEW", "待审核"),
                            ("APPROVED", "已通过"),
                            ("REJECTED", "未通过"),
                        ],
                        db_index=True,
                        default="PENDING_REVIEW",
                        max_length=24,
                        verbose_name="申请状态",
                    ),
                ),
                (
                    "service_location_name",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=128,
                        verbose_name="维修服务点名称",
                    ),
                ),
                (
                    "service_location_address",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=255,
                        verbose_name="维修服务点地图地址",
                    ),
                ),
                (
                    "service_latitude",
                    models.DecimalField(
                        decimal_places=7,
                        max_digits=10,
                        verbose_name="维修服务点纬度",
                    ),
                ),
                (
                    "service_longitude",
                    models.DecimalField(
                        decimal_places=7,
                        max_digits=10,
                        verbose_name="维修服务点经度",
                    ),
                ),
                (
                    "location_captured_at",
                    models.DateTimeField(verbose_name="定位确认时间"),
                ),
                (
                    "receiving_address",
                    models.CharField(max_length=255, verbose_name="官方标准收件地址"),
                ),
                (
                    "receiving_contact_name",
                    models.CharField(max_length=64, verbose_name="收件联系人"),
                ),
                (
                    "receiving_contact_phone",
                    models.CharField(max_length=32, verbose_name="收件联系电话"),
                ),
                (
                    "notice_version",
                    models.CharField(
                        default="2026-09-29-v1",
                        max_length=32,
                        verbose_name="申请须知版本",
                    ),
                ),
                (
                    "notice_agreed_at",
                    models.DateTimeField(verbose_name="确认申请须知时间"),
                ),
                (
                    "submitted_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now,
                        verbose_name="首次提交时间",
                    ),
                ),
                (
                    "review_note",
                    models.TextField(blank=True, default="", verbose_name="审核说明"),
                ),
                (
                    "reviewed_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="审核时间"),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="创建时间"),
                ),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True, verbose_name="更新时间"),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="after_sales_station_applications",
                        to="account.organization",
                        verbose_name="申请商家组织",
                    ),
                ),
                (
                    "reviewed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="after_sales_station_applications_reviewed",
                        to="account.user",
                        verbose_name="审核账号",
                    ),
                ),
                (
                    "submitted_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="after_sales_station_applications_submitted",
                        to="account.user",
                        verbose_name="申请账号",
                    ),
                ),
            ],
            options={
                "verbose_name": "售后服务站申请",
                "verbose_name_plural": "售后服务站申请",
                "ordering": ("-submitted_at", "-id"),
                "indexes": [
                    models.Index(
                        fields=["organization", "status"],
                        name="life_station_org_status",
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        condition=Q(status="PENDING_REVIEW"),
                        fields=("organization",),
                        name="life_one_pending_station_app",
                    ),
                    models.CheckConstraint(
                        condition=Q(service_latitude__gte=-90)
                        & Q(service_latitude__lte=90),
                        name="life_station_latitude_range",
                    ),
                    models.CheckConstraint(
                        condition=Q(service_longitude__gte=-180)
                        & Q(service_longitude__lte=180),
                        name="life_station_longitude_range",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="AfterSalesServiceStationLiaison",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "member_name_snapshot",
                    models.CharField(max_length=128, verbose_name="成员名称快照"),
                ),
                (
                    "role_code_snapshot",
                    models.CharField(max_length=64, verbose_name="成员岗位代码快照"),
                ),
                (
                    "role_name_snapshot",
                    models.CharField(max_length=64, verbose_name="成员岗位名称快照"),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="创建时间"),
                ),
                (
                    "application",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="liaison_links",
                        to="lifecycle.aftersalesservicestationapplication",
                        verbose_name="服务站申请",
                    ),
                ),
                (
                    "identity",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="service_station_liaisons",
                        to="account.userorganization",
                        verbose_name="组织成员身份",
                    ),
                ),
            ],
            options={
                "verbose_name": "售后服务站对接成员",
                "verbose_name_plural": "售后服务站对接成员",
                "ordering": ("role_code_snapshot", "identity_id"),
                "constraints": [
                    models.UniqueConstraint(
                        fields=("application", "identity"),
                        name="life_station_liaison_unique",
                    ),
                ],
            },
        ),
        migrations.AddField(
            model_name="aftersalesservicestationapplication",
            name="liaison_members",
            field=models.ManyToManyField(
                blank=True,
                related_name="after_sales_station_applications",
                through="lifecycle.AfterSalesServiceStationLiaison",
                to="account.userorganization",
                verbose_name="售后服务对接成员",
            ),
        ),
    ]
