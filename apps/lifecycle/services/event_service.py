from lifecycle.models.event import LifecycleEvent


class EventService:
    """生命周期事实的统一写入入口。"""

    @staticmethod
    def create_event(
        device,
        event_type,
        operator=None,
        organization=None,
        from_organization=None,
        to_organization=None,
        source="platform",
        remark=None,
        data=None,
    ):
        valid_types = {value for value, _label in LifecycleEvent.EVENT_TYPE_CHOICES}
        if event_type not in valid_types:
            raise ValueError(f"不支持的生命周期事件类型：{event_type}")

        event_data = dict(data or {})
        if source:
            event_data.setdefault("source", source)
        if remark:
            event_data.setdefault("remark", remark)

        return LifecycleEvent.objects.create(
            device=device,
            event_type=event_type,
            operator=operator,
            organization=organization,
            from_organization=from_organization,
            to_organization=to_organization,
            data=event_data,
        )
