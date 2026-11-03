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
    # 历史接口保留该字段；新交互统一建立“用户负责人账号”。
    customer_type = serializers.ChoiceField(
        choices=("PERSONAL", "FLEET"), required=False, default="PERSONAL"
    )
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
        attrs["customer_type"] = "PERSONAL"
        return attrs


class ActivateCustomerDeviceSerializer(serializers.Serializer):
    activation_method = serializers.ChoiceField(
        choices=("VEHICLE_PLATE", "DEVICE_CODE"),
        required=False,
        default="VEHICLE_PLATE",
    )
    vehicle_no = serializers.CharField(
        required=False, allow_blank=True, max_length=64, trim_whitespace=True
    )
    physical_device_code = serializers.CharField(
        required=False, allow_blank=True, max_length=1024, trim_whitespace=True
    )
    location = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=128,
        trim_whitespace=True,
    )

    def validate(self, attrs):
        method = attrs.get("activation_method", "VEHICLE_PLATE")
        vehicle_no = (attrs.get("vehicle_no") or "").strip()
        device_code = (attrs.get("physical_device_code") or "").strip()
        if method == "VEHICLE_PLATE" and not vehicle_no:
            raise serializers.ValidationError({"vehicle_no": "绑定车牌号时必须填写车辆牌号"})
        if method == "DEVICE_CODE" and not device_code:
            raise serializers.ValidationError({"physical_device_code": "请先扫描设备编码二维码"})
        if method == "VEHICLE_PLATE" and device_code:
            raise serializers.ValidationError({"physical_device_code": "车牌号与设备编码只能选择一种激活方式"})
        if method == "DEVICE_CODE" and vehicle_no:
            raise serializers.ValidationError({"vehicle_no": "设备编码与车牌号只能选择一种激活方式"})
        return attrs


class VerifyActivationDeviceCodeSerializer(serializers.Serializer):
    device_code = serializers.CharField(max_length=1024, trim_whitespace=True)


class GeneratePhysicalDeviceCodeBatchSerializer(serializers.Serializer):
    quantity = serializers.IntegerField(min_value=1, max_value=1000)


class VerifyAfterSalesDeviceCodeSerializer(serializers.Serializer):
    device_code = serializers.CharField(max_length=1024, trim_whitespace=True)


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


class FactoryReturnMaterialReturnSerializer(serializers.Serializer):
    bom_item_id = serializers.IntegerField(min_value=1)
    note = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=500,
        trim_whitespace=True,
    )
    is_fault_material = serializers.BooleanField(default=False)


class FactoryReturnFaultMaterialSerializer(serializers.Serializer):
    """兼容已发布但尚未升级的小程序请求。

    ``is_warranty_candidate`` 不再代表人工质保判断；兼容请求只把该字段
    映射为“故障物料”，质保状态仍由服务端重新计算。
    """

    bom_item_id = serializers.IntegerField(min_value=1)
    note = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=500,
        trim_whitespace=True,
    )
    is_warranty_candidate = serializers.BooleanField(default=False)


class FactoryReturnRepairMaterialSerializer(serializers.Serializer):
    bom_item_id = serializers.IntegerField(min_value=1)
    note = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=500,
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
    fault_materials = FactoryReturnFaultMaterialSerializer(
        many=True,
        required=False,
        allow_empty=False,
    )
    returned_materials = FactoryReturnMaterialReturnSerializer(
        many=True,
        required=False,
        allow_empty=False,
    )
    repair_materials = FactoryReturnRepairMaterialSerializer(
        many=True,
        required=False,
        allow_empty=False,
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
            and not attrs.get("repair_materials")
        ):
            raise serializers.ValidationError(
                {"repair_materials": "需要换料时请至少勾选一项原BOM物料"}
            )
        if action == "DISASSEMBLY_COMPLETE":
            returned = attrs.get("returned_materials")
            legacy = attrs.get("fault_materials")
            if not returned and not legacy:
                raise serializers.ValidationError(
                    {"returned_materials": "请至少勾选一项实际拆下退回物料"}
                )
            if returned and not any(row.get("is_fault_material") for row in returned):
                raise serializers.ValidationError(
                    {"returned_materials": "请至少标记一项导致本次拆机的故障物料"}
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
