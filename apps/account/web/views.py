# apps/account/web/views.py

from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q

from account.models import User, Organization


def user_manage(request):
    """
    系统管理员 - 用户管理
    """

    # ------------------------
    # 查询条件
    # ------------------------
    search = request.GET.get("search", "").strip()
    role = request.GET.get("role", "")

    users = User.objects.select_related(
        "organization",
        "parent_user"
    ).order_by("-date_joined")

    if search:
        users = users.filter(
            Q(name__icontains=search) |
            Q(phone__icontains=search) |
            Q(wx_nickname__icontains=search) |
            Q(wx_openid__icontains=search)
        )

    if role:
        users = users.filter(role=role)

    # ------------------------
    # 分页
    # ------------------------
    paginator = Paginator(users, 20)

    page_number = request.GET.get("page")

    page_obj = paginator.get_page(page_number)

    context = {
        "page_obj": page_obj,
        "search": search,
        "role": role,
        "role_choices": User._meta.get_field("role").choices,
    }

    return render(
        request,
        "account/user_manage.html",
        context
    )


def user_edit(request, pk):
    """
    系统管理员 - 编辑用户
    """

    user = get_object_or_404(User, pk=pk)

    if request.method == "POST":

        # ------------------------
        # 基本资料
        # ------------------------
        user.name = request.POST.get("name", "").strip()
        user.phone = request.POST.get("phone", "").strip()
        user.region = request.POST.get("region", "").strip()

        # ------------------------
        # 角色
        # ------------------------
        user.role = request.POST.get("role", user.role)

        # ------------------------
        # 所属组织
        # ------------------------
        organization_id = request.POST.get("organization")

        if organization_id:
            user.organization_id = organization_id
        else:
            user.organization = None

        # ------------------------
        # 上级用户
        # ------------------------
        parent_user_id = request.POST.get("parent_user")

        if parent_user_id:
            user.parent_user_id = parent_user_id
        else:
            user.parent_user = None

        # ------------------------
        # 属主用户
        # ------------------------
        owner_user_id = request.POST.get("owner_user")

        if owner_user_id:
            user.owner_user_id = owner_user_id
        else:
            user.owner_user = None

        # ------------------------
        # 属主类型
        # ------------------------
        owner_type = request.POST.get("owner_type")

        if owner_type:
            user.owner_type = owner_type
        else:
            user.owner_type = None

        user.save()

        messages.success(request, "用户信息保存成功。")

        return redirect("pldp:web:account:user_manage")

    # ==============================
    # 页面数据
    # ==============================

    organizations = Organization.objects.order_by("name")

    users = User.objects.exclude(pk=user.pk).order_by(
        "name",
        "wx_nickname"
    )

    context = {
        "user": user,

        # 角色
        "role_choices": User._meta.get_field("role").choices,

        # 属主类型
        "owner_type_choices":
            User._meta.get_field("owner_type").choices,

        # 所属组织
        "organizations": organizations,

        # 上级/属主用户
        "users": users,
    }

    return render(
        request,
        "account/user_edit.html",
        context
    )
