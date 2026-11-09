from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import redirect
from django.middleware.csrf import CsrfViewMiddleware
from account.services.auth_service import authenticate_request, ApiAuthenticationError
from .access import browser_principal, principal_for

SAFE={'GET','HEAD','OPTIONS','TRACE'}

def modules_for_path(path):
    if path.startswith('/pldp/admin/'):return {'admin'}
    if path.startswith('/pldp/web/account/'):return {'users'}
    if path.startswith('/pldp/web/product/material-fault-causes/'):return {'causes'}
    if path.startswith('/pldp/web/product/'):return {'product'}
    if path.startswith('/pldp/web/supply-chain/'):
        return {'materials'}
    if path.startswith('/pldp/web/device-codes/'):return {'device_codes'}
    if path.startswith('/pldp/web/print/'):return {'print'}
    if path.startswith('/pldp/web/quality-trace/'):return {'quality'}
    return set()

class PortalAccessMiddleware:
    def __init__(self,get_response):self.get_response=get_response
    def __call__(self,request):return self.get_response(request)
    def process_view(self,request,view_func,view_args,view_kwargs):
        path=request.path
        # Ending only this browser session must remain possible after its
        # business grant expires or is revoked. The view enforces POST/CSRF.
        if path == '/pldp/web/print/api/logout/':return None
        if path.startswith('/pldp/web/portal/'):return None
        product_api=path.startswith('/pldp/api/product/')
        legacy=('/account/','/product/','/print/','/supply-chain/','/api/product/','/api/supply-chain/')
        # Older URLs on this domain must not provide an alternate unguarded entry.
        if path.startswith(legacy):return JsonResponse({'code':404,'msg':'请从首页用户登录进入工作台。'},status=404)
        required=modules_for_path(path)
        if product_api:required={'product'}
        if not required:return None
        bearer=(request.headers.get('Authorization') or '').lower().startswith('bearer ')
        is_api=product_api or '/api/' in path or path.endswith(('rule-simulation/','rule-publish/','rules/','root-causes/'))
        try:
            if bearer:
                user=authenticate_request(request)
                # Existing small-program product queries are retained; browser writes
                # and web management paths still require the explicit role matrix.
                if product_api and request.method in SAFE:return None
                p=principal_for(user)
            else:p=browser_principal(request)
            if path.startswith('/pldp/web/supply-chain/api/') and request.method in SAFE:
                required={'materials','product','causes','quality'}
            if not required.intersection(p['modules']):raise PermissionDenied('当前角色无权执行此操作。')
            request.web_portal_principal=p
        except (PermissionDenied,ApiAuthenticationError) as exc:
            if is_api:return JsonResponse({'code':403,'msg':str(exc),'data':{}},status=403)
            if request.user.is_authenticated:return JsonResponse({'code':403,'msg':str(exc)},status=403)
            return redirect('/?login=1')
        if not bearer and request.method not in SAFE:
            # DRF views may be csrf_exempt; cookie-based web writes must still
            # pass CSRF validation rather than inheriting that exemption.
            def cookie_view(request):pass
            denied=CsrfViewMiddleware(lambda r:None).process_view(request,cookie_view,(),{})
            if denied:return denied
        return None
