import time
from django.core.exceptions import PermissionDenied
from django.utils.crypto import constant_time_compare
from account.services.auth_service import get_active_identity, _wechat_token_binding
from account.services.role_delegation_service import identity_has_role

GRANT='pldp_web_portal_grant_v1'
MODULES={
 'admin':('Django 管理后台','/pldp/admin/'),
 'users':('用户管理','/pldp/web/account/users/'),
 'product':('产品基础资料管理','/pldp/web/product/'),
 'causes':('物料故障根因字典','/pldp/web/product/material-fault-causes/'),
 'materials':('物料配套配置','/pldp/web/supply-chain/material-config/'),
 'device_codes':('厂家设备编码发行','/pldp/web/device-codes/'),
 'print':('成品打印中心','/pldp/web/print/'),
 'quality':('质量追溯控制台','/pldp/web/quality-trace/'),
 'merchant_accounts':('直属商家往来账','/pldp/web/portal/merchant-accounts/'),
}
ROLE_MODULES={
 'factory_admin':('product','causes','materials','device_codes','print','quality','merchant_accounts'),
 'factory_production':('product','device_codes'),
 'factory_aftersales':('causes',),
 'factory_matching':('materials',),
 'factory_stock':('print',),
 'factory_sales_assistant':('merchant_accounts',),
 'factory_chief_engineer':('quality',),
}

def identity_can_access_module(identity, module):
    if not identity:return False
    org,role=identity.organization,identity.role
    return bool(identity.is_active and identity.user.is_active and org.is_active
        and org.organization_type.is_active and role.is_active
        and org.organization_type.code=='OWNER' and role.organization_type_id==org.organization_type_id
        and org.get_factory_root() and org.get_factory_root().pk==org.pk
        and (module in ROLE_MODULES.get(role.code,())
            or (module=='print' and role.code=='factory_senior_manager'
                and identity_has_role(identity,'factory_stock'))
            or (module=='merchant_accounts' and role.code=='factory_senior_manager'
                and identity_has_role(identity,'factory_sales_assistant'))))

def principal_for(user):
    if not user or not user.is_active or not user.wx_openid:
        raise PermissionDenied('请使用已绑定微信的有效账号扫码。')
    if user.is_superuser:
        if not user.is_staff:raise PermissionDenied('超级管理员账号尚未启用后台工作人员权限。')
        return {'kind':'superuser','user_id':user.pk,'role':'superuser','role_name':'超级管理员','organization_name':'平台管理','modules':['admin','users']}
    identity=get_active_identity(user)
    modules=[m for m in MODULES if identity_can_access_module(identity,m)]
    if not modules:raise PermissionDenied('当前角色没有电脑端工作台权限，请使用指定厂家岗位账号。')
    return {'kind':'factory','user_id':user.pk,'identity_id':identity.pk,'organization_id':identity.organization_id,
        'role_id':identity.role_id,'role':identity.role.code,'role_name':identity.role.name,
        'organization_name':identity.organization.name,'modules':modules}

def browser_principal(request):
    grant=request.session.get(GRANT) or {}
    if not request.user.is_authenticated or not isinstance(grant.get('expires_at'),(int,float)) or grant['expires_at']<=time.time():
        raise PermissionDenied('登录已失效，请重新扫码。')
    principal=principal_for(request.user)
    if principal!=grant.get('principal') or not constant_time_compare(grant.get('binding',''),_wechat_token_binding(request.user)):
        raise PermissionDenied('微信绑定、岗位或组织权限已变更，请重新扫码。')
    return principal

def require_web_identity(request,module):
    principal=browser_principal(request)
    if module not in principal['modules']:raise PermissionDenied('当前角色无权访问该页面。')
    return get_active_identity(request.user)
