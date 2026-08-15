# apps/lifecycle/constants/status.py

class DeviceStatus:
    CREATED = "manufactured"
    INSTOCK = "in_stock"
    SHIPPED = "shipped"
    INSTALLED = "installed"
    ACTIVATED = "activated"
    RUNNING = "running"
    FAULT = "fault"


class QRStatus:
    GENERATED = "generated"
    ASSIGNED = "assigned"
    PRINTED = "printed"
    INVALID = "invalid"
