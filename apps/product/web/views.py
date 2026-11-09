# apps/product/web/views.py

from django.contrib import messages
from django.shortcuts import redirect, render

from supply_chain.models import Material, MaterialFaultCause



def product_manage(request):
    """
    产品基础资料管理中心

    Web页面渲染入口

    数据通过：
    apps.product.api
    提供接口
    """

    context = {

        "title":
            "产品基础资料管理中心",

    }


    return render(
        request,
        "product/product_manage.html",
        context
    )


def material_fault_causes(request):
    """Development-stage controlled dictionary for key-material root causes."""
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "create":
            try:
                cause = MaterialFaultCause(
                    material_id=int(request.POST.get("material_id") or 0),
                    code=str(request.POST.get("code") or "").strip().upper(),
                    name=str(request.POST.get("name") or "").strip(),
                    grade=str(request.POST.get("grade") or "").strip().upper(),
                    description=str(request.POST.get("description") or "").strip(),
                )
                cause.full_clean()
                cause.save()
                messages.success(request, "物料根因已添加。")
            except Exception as exc:
                messages.error(request, f"保存失败：{exc}")
        elif action == "toggle":
            cause = MaterialFaultCause.objects.filter(pk=request.POST.get("cause_id")).first()
            if cause:
                cause.is_active = not cause.is_active
                cause.save(update_fields=("is_active", "updated_at"))
        return redirect("pldp:web:product:material_fault_causes")
    return render(request, "product/material_fault_causes.html", {
        "materials": Material.objects.filter(is_key_part=True).order_by("code"),
        "causes": MaterialFaultCause.objects.select_related("material").order_by("material__code", "grade", "code"),
    })
