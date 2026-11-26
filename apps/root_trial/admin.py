from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404
from django.shortcuts import redirect, render
from django.urls import path, reverse
from django.utils.html import format_html

from .models import RootTrialApplication
from .services import (
    RootTrialError, get_application_for_user, get_license_for_user,
    open_license_file, require_superadmin, review_application,
)


class ReviewForm(forms.Form):
    decision = forms.ChoiceField(
        label="审核决定", choices=(("APPROVE", "通过并开通根组织"), ("REJECT", "未通过")),
        widget=forms.RadioSelect,
    )
    reason = forms.CharField(
        label="审核说明", required=False, max_length=1000,
        widget=forms.Textarea(attrs={"rows": 4, "cols": 70}),
        help_text="未通过时必须填写原因。",
    )


@admin.register(RootTrialApplication)
class RootTrialApplicationAdmin(admin.ModelAdmin):
    list_display = ("id", "organization_name", "applicant", "status", "created_at", "review_entry")
    list_filter = ("status",)
    search_fields = ("organization_name", "applicant__username", "applicant__wx_nickname")
    ordering = ("-created_at", "-pk")
    readonly_fields = (
        "applicant", "request_id", "organization_name", "license_entry", "status",
        "reviewed_by", "reviewed_at", "review_reason", "organization", "identity",
        "created_at", "updated_at", "review_entry",
    )
    fields = readonly_fields
    actions = None

    def has_module_permission(self, request):
        return bool(request.user.is_active and request.user.is_superuser)

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_urls(self):
        return [
            path(
                "<int:application_id>/review/", self.admin_site.admin_view(self.review_view),
                name="root_trial_roottrialapplication_review",
            ),
            path(
                "license/<uuid:upload_id>/", self.admin_site.admin_view(self.license_view),
                name="root_trial_roottrialapplication_license",
            ),
        ] + super().get_urls()

    @admin.display(description="审核")
    def review_entry(self, obj):
        return format_html(
            '<a href="{}">{}</a>',
            reverse("admin:root_trial_roottrialapplication_review", args=(obj.pk,)),
            "审核申请" if obj.status == "PENDING" else "查看审核结果",
        )

    @admin.display(description="企业营业执照")
    def license_entry(self, obj):
        return format_html(
            '<a href="{}" target="_blank" rel="noopener">查看营业执照图片</a>',
            reverse("admin:root_trial_roottrialapplication_license", args=(obj.license_upload_id,)),
        )

    def review_view(self, request, application_id):
        try:
            require_superadmin(request.user)
            application = get_application_for_user(request.user, application_id)
        except RootTrialError as exc:
            if exc.http_status == 404:
                raise Http404(str(exc)) from exc
            raise PermissionDenied(str(exc)) from exc
        form = ReviewForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            try:
                application, changed = review_application(
                    request.user, application_id, form.cleaned_data["decision"],
                    form.cleaned_data["reason"],
                )
                self.message_user(
                    request, "审核已完成。" if changed else "此申请已完成同一审核决定。", messages.SUCCESS,
                )
                return redirect("admin:root_trial_roottrialapplication_review", application_id)
            except RootTrialError as exc:
                form.add_error(None, str(exc))
                application.refresh_from_db()
        context = {
            **self.admin_site.each_context(request),
            "title": "根组织厂家负责人试用申请审核",
            "opts": self.model._meta, "application": application,
            "form": form, "license_url": reverse(
                "admin:root_trial_roottrialapplication_license", args=(application.license_upload_id,),
            ),
        }
        return render(request, "root_trial/admin_review.html", context)

    def license_view(self, request, upload_id):
        try:
            require_superadmin(request.user)
            media = get_license_for_user(request.user, upload_id)
            response = FileResponse(
                open_license_file(media), content_type=media.content_type,
                as_attachment=False, filename=media.original_name,
            )
            response["Cache-Control"] = "private, no-store"
            response["X-Content-Type-Options"] = "nosniff"
            return response
        except RootTrialError as exc:
            if exc.http_status in {404, 409}:
                raise Http404(str(exc)) from exc
            raise PermissionDenied(str(exc)) from exc
