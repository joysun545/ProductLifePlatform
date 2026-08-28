from datetime import timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from account.models import (
    Organization,
    OrganizationRole,
    OrganizationType,
    User,
    UserOrganization,
)
from quality_trace.models import QualityAlertRule, QualityAlertRuleVersion
from quality_trace.services import QualityTraceAccessError, get_quality_trace_identity


class QualityTraceFoundationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.factory_type = OrganizationType.objects.get(code="OWNER")
        cls.merchant_type = OrganizationType.objects.get(code="MERCHANT")
        cls.factory = Organization.objects.create(
            name="质量测试厂家", code="QUALITY-FACTORY", organization_type=cls.factory_type,
        )
        cls.merchant = Organization.objects.create(
            name="质量测试商家", code="QUALITY-MERCHANT", organization_type=cls.merchant_type,
        )
        cls.factory_role = OrganizationRole.objects.get(
            organization_type=cls.factory_type, code="factory_manager",
        )
        cls.merchant_role = OrganizationRole.objects.get(
            organization_type=cls.merchant_type, code="merchant_owner",
        )
        cls.factory_user = User.objects.create_user(
            username="quality-factory-user", wx_openid="quality-factory-openid",
        )
        cls.merchant_user = User.objects.create_user(
            username="quality-merchant-user", wx_openid="quality-merchant-openid",
        )
        UserOrganization.objects.create(
            user=cls.factory_user, organization=cls.factory, role=cls.factory_role,
        )
        UserOrganization.objects.create(
            user=cls.merchant_user, organization=cls.merchant, role=cls.merchant_role,
        )

    def test_factory_identity_can_access_foundation(self):
        identity = get_quality_trace_identity(self.factory_user)
        self.assertEqual(identity.organization, self.factory)

    def test_merchant_identity_is_rejected(self):
        with self.assertRaisesMessage(QualityTraceAccessError, "无权访问"):
            get_quality_trace_identity(self.merchant_user)

    def test_rule_rejects_non_factory_owner(self):
        rule = QualityAlertRule(
            factory_organization=self.merchant,
            code="INVALID",
            name="错误组织规则",
            created_by=self.factory_user,
        )
        with self.assertRaises(ValidationError):
            rule.full_clean()

    def test_automatic_hold_requires_expiry(self):
        rule = QualityAlertRule.objects.create(
            factory_organization=self.factory,
            code="EARLY-FAULT",
            name="早期故障预警",
            created_by=self.factory_user,
        )
        version = QualityAlertRuleVersion(
            rule=rule,
            version=1,
            metric_type=QualityAlertRuleVersion.MetricType.EARLY_FAULT_RATE,
            scope_snapshot={"product_model_ids": [1]},
            denominator_policy_snapshot={"basis": "ACTIVATED_UNIQUE_DEVICE"},
            fault_policy_snapshot={"deduplicate_by": "device_id"},
            threshold_snapshot={"warning": "0.02"},
            effective_from=timezone.now(),
            effective_to=timezone.now() + timedelta(days=30),
            allow_automatic_temporary_hold=True,
            automatic_hold_hours=0,
            published_by=self.factory_user,
        )
        with self.assertRaises(ValidationError):
            version.full_clean()
