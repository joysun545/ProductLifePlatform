"""
=========================================================
PLDP - Integration Module

职责：
外部系统集成（控制器 / BMS / GPS / ERP）

说明：
只负责连接，不负责业务逻辑
=========================================================
"""

from django.db import models


class ExternalSystem(models.Model):
    """
    外部系统（供应商云平台 / IoT平台等）
    """

    name = models.CharField(max_length=128)

    system_type = models.CharField(max_length=64)
    # controller / gps / bms / erp / mes

    api_base = models.CharField(max_length=256, null=True, blank=True)


class Connector(models.Model):
    """
    数据连接器（适配层）
    """

    system = models.ForeignKey(
        ExternalSystem,
        on_delete=models.CASCADE,
        related_name='connectors'
    )

    connector_type = models.CharField(max_length=32)
    # api / webhook / mqtt / sdk / ble

    config = models.JSONField(default=dict)


class ExternalDeviceData(models.Model):
    """
    外部设备数据（运行数据缓存）
    """

    lifecycle_id = models.CharField(max_length=64)

    system = models.ForeignKey(
        ExternalSystem,
        on_delete=models.CASCADE
    )

    data_type = models.CharField(max_length=64)

    payload = models.JSONField()

    created_at = models.DateTimeField(auto_now_add=True)
