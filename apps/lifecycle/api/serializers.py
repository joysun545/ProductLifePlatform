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
