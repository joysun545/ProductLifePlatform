"""Platform-admin account maintenance; business roles remain invitation-based."""
from django import forms
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from account.models import User, OrganizationRole
from web_portal.access import require_web_identity

class UserProfileForm(forms.ModelForm):
    class Meta:
        model=User
        fields=('first_name','last_name','phone','is_active')
    def __init__(self,*args,actor=None,**kwargs):
        self.actor=actor
        super().__init__(*args,**kwargs)
    def clean_is_active(self):
        value=self.cleaned_data['is_active']
        if self.actor and self.actor.pk==self.instance.pk and not value:
            raise forms.ValidationError('不能在此停用自己的管理员账号。')
        return value

def user_manage(request):
    require_web_identity(request, "users")
    search=request.GET.get('search','').strip();role=request.GET.get('role','').strip()
    users=User.objects.select_related('organization_identity__organization','organization_identity__role').order_by('-date_joined')
    if search:users=users.filter(Q(username__icontains=search)|Q(phone__icontains=search)|Q(wx_nickname__icontains=search)|Q(first_name__icontains=search)|Q(last_name__icontains=search))
    if role:users=users.filter(organization_identity__is_active=True,organization_identity__role__code=role)
    return render(request,'account/user_manage.html',{'page_obj':Paginator(users,20).get_page(request.GET.get('page')),'search':search,'role':role,'roles':OrganizationRole.objects.filter(is_active=True).order_by('code')})

def user_edit(request,pk):
    require_web_identity(request, "users")
    target=get_object_or_404(User.objects.select_related('organization_identity__organization','organization_identity__role'),pk=pk)
    form=UserProfileForm(request.POST if request.method=='POST' else None,instance=target,actor=request.user)
    if request.method=='POST' and form.is_valid():
        form.save();messages.success(request,'账号资料已保存；组织岗位及微信绑定未改变。')
        return redirect('pldp:web:account:user_manage')
    return render(request,'account/user_edit.html',{'target':target,'form':form})
