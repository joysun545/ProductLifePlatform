from decimal import Decimal

from rest_framework import serializers


class CreateLifecycleSerializer(serializers.Serializer):
    """
    创建生命周期请求
    """

    product_model_id = serializers.IntegerField()
    batch_id = serializers.IntegerField()
    sn = serializers.CharField(required=False, allow_blank=False, max_length=64)


class ResolveQRCodeSerializer(serializers.Serializer):
    qr_text = serializers.CharField(max_length=1024, trim_whitespace=True)


class InstallDeviceSerializer(serializers.Serializer):
    device_id = serializers.IntegerField(min_value=1)
    location = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=128,
        trim_whitespace=True,
    )


class RefreshInstallationInvitationSerializer(serializers.Serializer):
    device_id = serializers.IntegerField(min_value=1)


class AcceptInstallationInvitationSerializer(serializers.Serializer):
    customer_type = serializers.ChoiceField(choices=("PERSONAL", "FLEET"))
    customer_name = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=128,
        trim_whitespace=True,
    )
    contact_name = serializers.CharField(max_length=64, trim_whitespace=True)
    phone = serializers.CharField(max_length=32, trim_whitespace=True)
    region = serializers.CharField(max_length=255, trim_whitespace=True)
    address = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=255,
        trim_whitespace=True,
    )
    vehicle_no = serializers.CharField(max_length=64, trim_whitespace=True)

    def validate(self, attrs):
        if attrs["customer_type"] == "FLEET" and not attrs.get(
            "customer_name", ""
        ):
            raise serializers.ValidationError(
                {"customer_name": "企业或车队用户必须填写名称"}
            )
        return attrs


class ActivateCustomerDeviceSerializer(serializers.Serializer):
    vehicle_no = serializers.CharField(max_length=64, trim_whitespace=True)
    location = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=128,
        trim_whitespace=True,
    )


class CreateGoodsTransferSerializer(serializers.Serializer):
    flow_type = serializers.ChoiceField(
        choices=("SHIPMENT", "RETURN", "TRANSFER")
    )
    client_request_id = serializers.CharField(max_length=64)
    source_account_id = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=1,
    )
    target_account_id = serializers.IntegerField(min_value=1)
    remark = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=255,
        trim_whitespace=True,
    )


class ScanGoodsTransferDeviceSerializer(serializers.Serializer):
    qr_text = serializers.CharField(max_length=1024, trim_whitespace=True)


class FactoryReturnCreditAmountSerializer(serializers.Serializer):
    amount = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        min_value=Decimal("0.01"),
    )


class FactoryReturnPostingItemSerializer(serializers.Serializer):
    item_id = serializers.IntegerField(min_value=1)
    stock_status = serializers.ChoiceField(
        choices=(
            "PENDING_DISASSEMBLY",
            "PENDING_REISSUE",
            "PENDING_REPAIR",
        )
    )
    remark = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=255,
        trim_whitespace=True,
    )


class FactoryReturnPostingSerializer(serializers.Serializer):
    items = FactoryReturnPostingItemSerializer(many=True, allow_empty=False)
    posting_remark = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=255,
        trim_whitespace=True,
    )


class FactoryReturnProductionActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(
        choices=(
            "REPAIR_DIAGNOSIS",
            "REPAIR_COMPLETE",
            "DISASSEMBLY_COMPLETE",
        )
    )
    needs_replacement_material = serializers.BooleanField(
        required=False,
        allow_null=True,
    )
    production_remark = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=1000,
        trim_whitespace=True,
    )
    material_requirement = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=1000,
        trim_whitespace=True,
    )
    disassembly_result = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=1000,
        trim_whitespace=True,
    )
    has_warranty_material = serializers.BooleanField(
        required=False,
        allow_null=True,
    )

    def validate(self, attrs):
        action = attrs["action"]
        if (
            action == "REPAIR_DIAGNOSIS"
            and "needs_replacement_material" not in attrs
        ):
            raise serializers.ValidationError(
                {"needs_replacement_material": "请选择是否需要更换物料"}
            )
        if (
            action == "REPAIR_DIAGNOSIS"
            and attrs.get("needs_replacement_material")
            and not attrs.get("material_requirement", "")
        ):
            raise serializers.ValidationError(
                {"material_requirement": "需要换料时必须填写物料需求"}
            )
        if action == "DISASSEMBLY_COMPLETE":
            if not attrs.get("disassembly_result", ""):
                raise serializers.ValidationError(
                    {"disassembly_result": "请填写拆机结果和拆出物料清单"}
                )
            if "has_warranty_material" not in attrs:
                raise serializers.ValidationError(
                    {"has_warranty_material": "请选择是否包含三包物料"}
                )
        return attrs


class FactoryReturnMatchingActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(
        choices=(
            "REPAIR_MATERIAL_PLAN",
            "DISASSEMBLY_MATERIAL_DISPOSITION",
        )
    )
    material_plan = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=1000,
        trim_whitespace=True,
    )
    warranty_disposition = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=1000,
        trim_whitespace=True,
    )
    matching_remark = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=1000,
        trim_whitespace=True,
    )

    def validate(self, attrs):
        if (
            attrs["action"] == "REPAIR_MATERIAL_PLAN"
            and not attrs.get("material_plan", "")
        ):
            raise serializers.ValidationError(
                {"material_plan": "请填写修复物料方案"}
            )
        if (
            attrs["action"] == "DISASSEMBLY_MATERIAL_DISPOSITION"
            and not attrs.get("warranty_disposition", "")
        ):
            raise serializers.ValidationError(
                {"warranty_disposition": "请填写三包物料处置意见"}
            )
        return attrs
