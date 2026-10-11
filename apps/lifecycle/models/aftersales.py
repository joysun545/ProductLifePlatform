import uuid

from django.db import models
from django.db.models import Q
from django.utils import timezone

from lifecycle.private_storage import after_sales_private_storage


def aftersales_media_upload_to(instance, filename):
    suffix = (filename.rsplit(".", 1)[-1] if "." in filename else "bin").lower()
    return f"{instance.application.case_no}/{uuid.uuid4().hex}.{suffix}"


class AfterSalesApplication(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "草稿"
        SUBMITTED = "SUBMITTED", "已提交"
        ROUTING = "ROUTING", "待分派"
        PROCESSING = "PROCESSING", "处理中"
        WAITING_CUSTOMER_CONFIRM = "WAITING_CUSTOMER_CONFIRM", "待用户确认"
        PENDING_FACTORY_CLOSE = "PENDING_FACTORY_CLOSE", "待厂家结案"
        CLOSED = "CLOSED", "已完成"
        CANCELLED = "CANCELLED", "已取消"

    class DispatchRoute(models.TextChoices):
        ORIGINAL_INSTALLER = "ORIGINAL_INSTALLER", "原装车操作人"
        MERCHANT_ELECTRICIAN = "MERCHANT_ELECTRICIAN", "一级商家电工"
        FACTORY_AFTERSALES = "FACTORY_AFTERSALES", "厂家售后经理"
        REMOTE = "REMOTE", "厂家销售经理远程排障"
        THIRD_PARTY = "THIRD_PARTY", "第三方紧急维修"

    class ExecutionStage(models.TextChoices):
        NONE = "NONE", "待厂家判断"
        CONTACT_INSTALLER = "CONTACT_INSTALLER", "联系原装车操作人"
        AWAITING_APPLICANT_SHIPMENT = "AWAITING_APPLICANT_SHIPMENT", "待发起人寄送设备"
        AWAITING_REPAIR_RECEIPT = "AWAITING_REPAIR_RECEIPT", "待维修人确认收件"
        REPAIRING = "REPAIRING", "维修处理中"
        AWAITING_RETURN_SHIPMENT = "AWAITING_RETURN_SHIPMENT", "待维修人寄回设备"
        THIRD_PARTY_REPAIR = "THIRD_PARTY_REPAIR", "第三方紧急维修处理中"
        AWAITING_APPLICANT_CONFIRM = "AWAITING_APPLICANT_CONFIRM", "待发起人确认结果"
        FINAL_DIAGNOSIS = "FINAL_DIAGNOSIS", "待厂家最终归因"
        CLOSED = "CLOSED", "已结案"

    class FaultType(models.TextChoices):
        INSTALLATION_OR_WIRING = "INSTALLATION_OR_WIRING", "安装或线路问题"
        COMPONENT_SUSPECTED = "COMPONENT_SUSPECTED", "疑似关键配套物料故障"
        OPERATION_OR_EXTERNAL_CONDITION = "OPERATION_OR_EXTERNAL_CONDITION", "使用或外部环境因素"
        NO_FAULT_REPRODUCED = "NO_FAULT_REPRODUCED", "未复现故障"
        NEEDS_PHYSICAL_INSPECTION = "NEEDS_PHYSICAL_INSPECTION", "需实物检修确认"
        OTHER = "OTHER", "其他"

    class SolutionType(models.TextChoices):
        ON_SITE_ADJUSTMENT = "ON_SITE_ADJUSTMENT", "现场调整"
        MAIL_IN_REPAIR = "MAIL_IN_REPAIR", "寄修处理"
        REMOTE_GUIDANCE = "REMOTE_GUIDANCE", "远程指导"
        COMPONENT_REPLACEMENT = "COMPONENT_REPLACEMENT", "更换配套物料"
        THIRD_PARTY_REPAIR = "THIRD_PARTY_REPAIR", "第三方维修"
        NO_REPAIR_REQUIRED = "NO_REPAIR_REQUIRED", "无需维修"
        OTHER = "OTHER", "其他"

    class ApplicantConfirmMode(models.TextChoices):
        MANUAL = "MANUAL", "发起人确认"
        TIMEOUT = "TIMEOUT", "七天超时默认确认"

    case_no = models.CharField(max_length=48, unique=True, verbose_name="售后单号")
    device = models.ForeignKey("lifecycle.Device", on_delete=models.PROTECT, related_name="after_sales_applications")
    customer_organization = models.ForeignKey("account.Organization", on_delete=models.PROTECT, related_name="customer_after_sales")
    factory_organization = models.ForeignKey("account.Organization", on_delete=models.PROTECT, related_name="factory_after_sales")
    applicant = models.ForeignKey("account.User", on_delete=models.PROTECT, related_name="after_sales_applications")
    applicant_role_code = models.CharField(max_length=64, blank=True, default="")
    applicant_role_name = models.CharField(max_length=64, blank=True, default="")
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.DRAFT)
    fault_description = models.TextField(blank=True, default="")
    location_latitude = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    location_longitude = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    location_name = models.CharField(max_length=128, blank=True, default="")
    location_address = models.CharField(max_length=255, blank=True, default="")
    location_confirmed = models.BooleanField(default=False)
    location_confirmed_at = models.DateTimeField(null=True, blank=True)
    activation_snapshot = models.JSONField(default=dict, blank=True)
    device_snapshot = models.JSONField(default=dict, blank=True)
    bom_snapshot = models.JSONField(default=dict, blank=True)
    warranty_snapshot = models.JSONField(default=dict, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)

    # 售后处理全过程仍归属于同一张申请主单；这些字段只保存当前结论和
    # 冻结快照，逐步事实另由 AfterSalesEvent 追加记录。
    dispatch_route = models.CharField(max_length=32, choices=DispatchRoute.choices, blank=True, default="")
    execution_stage = models.CharField(max_length=40, choices=ExecutionStage.choices, default=ExecutionStage.NONE)
    preliminary_fault_type = models.CharField(max_length=48, choices=FaultType.choices, blank=True, default="")
    diagnosis_note = models.TextField(blank=True, default="")
    repair_plan = models.TextField(blank=True, default="")
    assigned_user = models.ForeignKey(
        "account.User", null=True, blank=True, on_delete=models.PROTECT,
        related_name="assigned_after_sales_applications",
    )
    assigned_organization = models.ForeignKey(
        "account.Organization", null=True, blank=True, on_delete=models.PROTECT,
        related_name="assigned_after_sales_applications",
    )
    assignee_snapshot = models.JSONField(default=dict, blank=True)
    triaged_by = models.ForeignKey(
        "account.User", null=True, blank=True, on_delete=models.PROTECT,
        related_name="triaged_after_sales_applications",
    )
    triaged_at = models.DateTimeField(null=True, blank=True)
    dispatch_version = models.PositiveIntegerField(default=0)
    repair_summary = models.TextField(blank=True, default="")
    repair_material_replaced = models.BooleanField(null=True, blank=True)
    repair_materials = models.JSONField(default=list, blank=True)
    repair_completed_by = models.ForeignKey(
        "account.User", null=True, blank=True, on_delete=models.PROTECT,
        related_name="completed_after_sales_repairs",
    )
    repair_completed_at = models.DateTimeField(null=True, blank=True)
    applicant_confirm_due_at = models.DateTimeField(null=True, blank=True)
    applicant_confirmed_at = models.DateTimeField(null=True, blank=True)
    applicant_confirm_mode = models.CharField(
        max_length=16, choices=ApplicantConfirmMode.choices, blank=True, default=""
    )
    third_party_snapshot = models.JSONField(default=dict, blank=True)
    third_party_cost_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    final_fault_type = models.CharField(max_length=48, choices=FaultType.choices, blank=True, default="")
    final_fault_materials = models.JSONField(default=list, blank=True)
    final_solution_type = models.CharField(max_length=48, choices=SolutionType.choices, blank=True, default="")
    final_solution_note = models.TextField(blank=True, default="")
    closed_by = models.ForeignKey(
        "account.User", null=True, blank=True, on_delete=models.PROTECT,
        related_name="closed_after_sales_applications",
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at", "-id")
        constraints = [
            models.UniqueConstraint(
                fields=("device",),
                condition=Q(status__in=("DRAFT", "SUBMITTED", "ROUTING", "PROCESSING", "WAITING_CUSTOMER_CONFIRM", "PENDING_FACTORY_CLOSE")),
                name="life_one_active_aftersale",
            )
        ]
        indexes = [models.Index(fields=("factory_organization", "status"), name="life_as_factory_status")]


class AfterSalesMedia(models.Model):
    class MediaType(models.TextChoices):
        IMAGE = "IMAGE", "图片"
        VIDEO = "VIDEO", "视频"
        DOCUMENT = "DOCUMENT", "单据"

    class Purpose(models.TextChoices):
        APPLICATION_EVIDENCE = "APPLICATION_EVIDENCE", "申请故障证据"
        APPLICANT_SHIPMENT_DOCUMENT = "APPLICANT_SHIPMENT_DOCUMENT", "发起人寄送物流单据"
        REPAIR_EVIDENCE = "REPAIR_EVIDENCE", "维修过程或测试证据"
        REPAIR_RETURN_SHIPMENT_DOCUMENT = "REPAIR_RETURN_SHIPMENT_DOCUMENT", "维修返件物流单据"
        THIRD_PARTY_MATERIAL_SHIPMENT_DOCUMENT = "THIRD_PARTY_MATERIAL_SHIPMENT_DOCUMENT", "第三方备件寄送单据"
        THIRD_PARTY_FEE_VOUCHER = "THIRD_PARTY_FEE_VOUCHER", "第三方维修费用凭证"
        THIRD_PARTY_FAULT_MATERIAL_RETURN_DOCUMENT = "THIRD_PARTY_FAULT_MATERIAL_RETURN_DOCUMENT", "故障物料返还单据"

    application = models.ForeignKey(AfterSalesApplication, on_delete=models.CASCADE, related_name="media")
    media_type = models.CharField(max_length=16, choices=MediaType.choices)
    purpose = models.CharField(
        max_length=64, choices=Purpose.choices, default=Purpose.APPLICATION_EVIDENCE,
    )
    file = models.FileField(storage=after_sales_private_storage, upload_to=aftersales_media_upload_to)
    original_name = models.CharField(max_length=255, blank=True, default="")
    caption = models.CharField(max_length=255, blank=True, default="")
    content_type = models.CharField(max_length=128, blank=True, default="")
    file_size = models.PositiveBigIntegerField(default=0)
    duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    sequence = models.PositiveIntegerField(default=0)
    uploaded_by = models.ForeignKey("account.User", on_delete=models.PROTECT, related_name="after_sales_media")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("sequence", "id")
        indexes = [models.Index(fields=("application", "purpose"), name="life_as_media_purpose")]


class AfterSalesEvent(models.Model):
    application = models.ForeignKey(AfterSalesApplication, on_delete=models.CASCADE, related_name="events")
    event_type = models.CharField(max_length=64)
    actor = models.ForeignKey("account.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="after_sales_events")
    actor_openid = models.CharField(max_length=128, blank=True, default="")
    actor_role_code = models.CharField(max_length=64, blank=True, default="")
    actor_role_name = models.CharField(max_length=64, blank=True, default="")
    organization_id_snapshot = models.PositiveBigIntegerField(null=True, blank=True)
    organization_name_snapshot = models.CharField(max_length=128, blank=True, default="")
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at", "id")


class AfterSalesServiceStationApplication(models.Model):
    """Merchant application to register a repair and receiving location.

    This application is intentionally separate from device-level
    AfterSalesApplication records. It stores the merchant organization's
    qualification details and does not change the existing case workflow.
    """

    class Status(models.TextChoices):
        PENDING_REVIEW = "PENDING_REVIEW", "待审核"
        APPROVED = "APPROVED", "已通过"
        REJECTED = "REJECTED", "未通过"

    organization = models.ForeignKey(
        "account.Organization",
        on_delete=models.PROTECT,
        related_name="after_sales_station_applications",
        verbose_name="申请商家组织",
    )
    submitted_by = models.ForeignKey(
        "account.User",
        on_delete=models.PROTECT,
        related_name="after_sales_station_applications_submitted",
        verbose_name="申请账号",
    )
    status = models.CharField(
        max_length=24,
        choices=Status.choices,
        default=Status.PENDING_REVIEW,
        db_index=True,
        verbose_name="申请状态",
    )
    service_location_name = models.CharField(
        max_length=128,
        blank=True,
        default="",
        verbose_name="维修服务点名称",
    )
    service_location_address = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="维修服务点地图地址",
    )
    service_latitude = models.DecimalField(
        max_digits=10,
        decimal_places=7,
        verbose_name="维修服务点纬度",
    )
    service_longitude = models.DecimalField(
        max_digits=10,
        decimal_places=7,
        verbose_name="维修服务点经度",
    )
    location_captured_at = models.DateTimeField(
        verbose_name="定位确认时间",
    )
    receiving_address = models.CharField(
        max_length=255,
        verbose_name="官方标准收件地址",
    )
    receiving_contact_name = models.CharField(
        max_length=64,
        verbose_name="收件联系人",
    )
    receiving_contact_phone = models.CharField(
        max_length=32,
        verbose_name="收件联系电话",
    )
    notice_version = models.CharField(
        max_length=32,
        default="2026-09-29-v1",
        verbose_name="申请须知版本",
    )
    notice_agreed_at = models.DateTimeField(
        verbose_name="确认申请须知时间",
    )
    submitted_at = models.DateTimeField(
        default=timezone.now,
        verbose_name="首次提交时间",
    )
    reviewed_by = models.ForeignKey(
        "account.User",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="after_sales_station_applications_reviewed",
        verbose_name="审核账号",
    )
    reviewed_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="审核时间",
    )
    review_note = models.TextField(
        blank=True,
        default="",
        verbose_name="审核说明",
    )
    liaison_members = models.ManyToManyField(
        "account.UserOrganization",
        through="lifecycle.AfterSalesServiceStationLiaison",
        related_name="after_sales_station_applications",
        blank=True,
        verbose_name="售后服务对接成员",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        ordering = ("-submitted_at", "-id")
        constraints = [
            models.UniqueConstraint(
                fields=("organization",),
                condition=Q(status="PENDING_REVIEW"),
                name="life_one_pending_station_app",
            ),
            models.CheckConstraint(
                condition=(
                    Q(service_latitude__gte=-90)
                    & Q(service_latitude__lte=90)
                ),
                name="life_station_latitude_range",
            ),
            models.CheckConstraint(
                condition=(
                    Q(service_longitude__gte=-180)
                    & Q(service_longitude__lte=180)
                ),
                name="life_station_longitude_range",
            ),
        ]
        indexes = [
            models.Index(
                fields=("organization", "status"),
                name="life_station_org_status",
            ),
        ]
        verbose_name = "售后服务站申请"
        verbose_name_plural = "售后服务站申请"


class AfterSalesServiceStationLiaison(models.Model):
    """The same-organization member roles selected for station coordination."""

    application = models.ForeignKey(
        AfterSalesServiceStationApplication,
        on_delete=models.CASCADE,
        related_name="liaison_links",
        verbose_name="服务站申请",
    )
    identity = models.ForeignKey(
        "account.UserOrganization",
        on_delete=models.PROTECT,
        related_name="service_station_liaisons",
        verbose_name="组织成员身份",
    )
    member_name_snapshot = models.CharField(
        max_length=128,
        verbose_name="成员名称快照",
    )
    role_code_snapshot = models.CharField(
        max_length=64,
        verbose_name="成员岗位代码快照",
    )
    role_name_snapshot = models.CharField(
        max_length=64,
        verbose_name="成员岗位名称快照",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        ordering = ("role_code_snapshot", "identity_id")
        constraints = [
            models.UniqueConstraint(
                fields=("application", "identity"),
                name="life_station_liaison_unique",
            ),
        ]
        verbose_name = "售后服务站对接成员"
        verbose_name_plural = "售后服务站对接成员"
