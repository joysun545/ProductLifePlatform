# pldp/api/views.py
from django.http import JsonResponse
from rest_framework.decorators import api_view
from rest_framework.response import Response

# ====================== 示例接口 ======================

@api_view(['GET'])
def health_check(request):
    """健康检查接口"""
    return Response({
        "code": 0,
        "msg": "PLDP API is running",
        "data": {}
    })


# ====================== 生命周期事件 ======================

@api_view(['POST'])
def create_life_event(request):
    """
    创建生命周期事件（交接、运输、安装、维修等）
    """
    # TODO: 实现事件记录逻辑
    return Response({
        "code": 0,
        "msg": "事件创建成功",
        "data": {"event_id": 123}
    })
