# apps/lifecycle/models/qrcode.py


from django.db import models



class QRCode(models.Model):
    """
    产品二维码。


    二维码是设备进入PLDP系统的入口。


    示例：


    二维码：

    JL20260728000001


    绑定设备：

    SN:
    JL5000-000001



    扫码后：

    识别设备

    查询状态

    创建生命周期事件


    注意：

    二维码不是设备本身。

    二维码可以：

    - 生成
    - 绑定
    - 作废
    - 替换
    """



    STATUS_CHOICES = [

        (
            "CREATED",
            "已生成"
        ),

        (
            "ACTIVE",
            "使用中"
        ),

        (
            "REPLACED",
            "已替换"
        ),

        (
            "DISABLED",
            "已禁用"
        ),

    ]



    code = models.CharField(
        max_length=128,
        unique=True,
        db_index=True,
        verbose_name="二维码编号"
    )



    device = models.OneToOneField(
        "lifecycle.Device",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="qrcode",
        verbose_name="绑定设备"
    )



    qr_image = models.ImageField(
        upload_to="qrcode/",
        null=True,
        blank=True,
        verbose_name="二维码图片"
    )



    status = models.CharField(
        max_length=32,
        choices=STATUS_CHOICES,
        default="CREATED",
        db_index=True,
        verbose_name="二维码状态"
    )



    created_by = models.ForeignKey(
        "account.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_qrcodes",
        verbose_name="生成人员"
    )



    activated_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="启用时间"
    )



    replaced_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="替换时间"
    )



    disabled_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="禁用时间"
    )



    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="创建时间"
    )



    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name="更新时间"
    )



    class Meta:

        verbose_name = "设备二维码"

        verbose_name_plural = "设备二维码"


        indexes = [

            models.Index(
                fields=[
                    "code"
                ]
            ),

            models.Index(
                fields=[
                    "status"
                ]
            ),

        ]



    def __str__(self):

        return self.code
