import base64
import hashlib
import json
import re
import secrets
from datetime import timedelta
from io import BytesIO
import qrcode
from django.contrib.auth import login, logout as auth_logout
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.middleware.csrf import rotate_token
from django.shortcuts import render, redirect
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt, csrf_protect, ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST
from account.services.auth_service import authenticate_request, ApiAuthenticationError, _wechat_token_binding
from .models import WebLoginChallenge
from .access import GRANT, MODULES, principal_for, browser_principal

CHALLENGE='pldp_web_challenge_v1'
LOGIN_SECONDS=1800
QR_SECONDS=300

def digest(v):return hashlib.sha256(str(v).encode()).hexdigest()
def response(data=None,msg='',code=0,http=200):return JsonResponse({'code':code,'msg':msg,'data':data or {}},status=http)
def error(msg,http=400):return response(msg=str(msg),code=http,http=http)
def body(request):
    try:data=json.loads(request.body or b'{}')
    except (ValueError,UnicodeDecodeError):raise ValueError('请求格式错误。')
    if not isinstance(data,dict):raise ValueError('请求格式错误。')
    return data

@never_cache
@ensure_csrf_cookie
@require_GET
def workbench(request):
    try:p=browser_principal(request)
    except PermissionDenied:return redirect('/?login=1')
    return render(request,'web_portal/workbench.html',{'principal':p,'entries':[{'title':MODULES[m][0],'url':MODULES[m][1]} for m in p['modules']]})

@never_cache
@csrf_protect
@require_POST
def start(request):
    if request.user.is_authenticated and request.session.get(GRANT):
        try:
            browser_principal(request)
            return response({'status':'ready','redirect':'/pldp/web/portal/'})
        except PermissionDenied:pass
    if not request.session.session_key:request.session.create()
    old=request.session.pop(CHALLENGE,None)
    if old:WebLoginChallenge.objects.filter(pk=old,consumed_at__isnull=True).delete()
    token=secrets.token_hex(32);now=timezone.now()
    challenge=WebLoginChallenge.objects.create(token_digest=digest(token),browser_digest=digest(request.session.session_key),expires_at=now+timedelta(seconds=QR_SECONDS))
    request.session[CHALLENGE]=challenge.pk
    qr=qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,box_size=8,border=4)
    qr.add_data('PLDP_WEB_LOGIN|'+token);qr.make(fit=True)
    stream=BytesIO();qr.make_image(fill_color='#102d27',back_color='white').save(stream,format='PNG')
    WebLoginChallenge.objects.filter(expires_at__lt=now-timedelta(days=1)).delete()
    return response({'qr_image':'data:image/png;base64,'+base64.b64encode(stream.getvalue()).decode(),'expires_in':QR_SECONDS,'status':'pending'})

@never_cache
@csrf_exempt
@require_POST
def approve(request):
    if not (request.headers.get('Authorization') or '').lower().startswith('bearer '):return error('请在小程序登录后扫码。',401)
    try:
        user=authenticate_request(request);p=principal_for(user);token=body(request).get('token')
        if not isinstance(token,str) or not re.fullmatch('[a-f0-9]{64}',token):return error('登录二维码无效。')
        now=timezone.now()
        changed=WebLoginChallenge.objects.filter(token_digest=digest(token),expires_at__gt=now,approved_at__isnull=True,consumed_at__isnull=True).update(user=user,principal=p,binding=_wechat_token_binding(user),approved_at=now)
        if changed!=1:return error('二维码已过期或已授权，请在电脑端刷新。',410)
        return response({'role_name':p['role_name'],'organization_name':p['organization_name']})
    except ApiAuthenticationError as exc:return error(exc,401)
    except PermissionDenied as exc:return error(exc,403)
    except ValueError as exc:return error(exc)

@never_cache
@csrf_protect
@require_POST
def status(request):
    try:
        browser_principal(request);return response({'status':'ready','redirect':'/pldp/web/portal/'})
    except PermissionDenied:pass
    now=timezone.now()
    challenge=WebLoginChallenge.objects.filter(pk=request.session.get(CHALLENGE),browser_digest=digest(request.session.session_key or ''),expires_at__gt=now,consumed_at__isnull=True).select_related('user').first()
    if not challenge:return error('二维码已过期，请刷新。',410)
    if not challenge.approved_at:return response({'status':'pending'})
    try:
        p=principal_for(challenge.user)
        if p!=challenge.principal or not constant_time_compare(challenge.binding,_wechat_token_binding(challenge.user)):
            return error('扫码后身份发生变化，请重新扫码。',403)
        changed=WebLoginChallenge.objects.filter(pk=challenge.pk,consumed_at__isnull=True,expires_at__gt=now).update(consumed_at=now)
        if changed!=1:return error('二维码已使用，请重新扫码。',410)
        login(request,challenge.user,backend='django.contrib.auth.backends.ModelBackend')
        rotate_token(request);request.session.pop(CHALLENGE,None)
        request.session[GRANT]={'principal':p,'binding':challenge.binding,'expires_at':(now+timedelta(seconds=LOGIN_SECONDS)).timestamp()}
        request.session.set_expiry(LOGIN_SECONDS)
        return response({'status':'ready','redirect':'/pldp/web/portal/'})
    except PermissionDenied as exc:return error(exc,403)

@never_cache
@ensure_csrf_cookie
@require_GET
def session_info(request):
    try:
        p=browser_principal(request);return response({'logged_in':True,'role_name':p['role_name'],'redirect':'/pldp/web/portal/'})
    except PermissionDenied:return response({'logged_in':False})

def clear_browser_session(request):
    """End the workbench session and discard its outstanding QR challenge."""
    challenge_id = request.session.get(CHALLENGE)
    if challenge_id:
        WebLoginChallenge.objects.filter(pk=challenge_id).delete()
    auth_logout(request)


@never_cache
@csrf_protect
@require_POST
def logout(request):
    clear_browser_session(request)
    return response({'redirect':'/'})

@never_cache
@csrf_protect
@require_POST
def cancel(request):
    pk=request.session.pop(CHALLENGE,None)
    if pk:WebLoginChallenge.objects.filter(pk=pk,browser_digest=digest(request.session.session_key or '')).delete()
    return response()
