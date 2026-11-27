"""Original-installer stock check and explicit nearest-station fallback preview."""
from django.core import signing
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import F
from account.services.auth_service import get_active_identity
from account.services.role_delegation_service import identity_has_role
from lifecycle.models import AfterSalesApplication
from supply_chain.models import OrganizationMaterialLot

SALT = 'lifecycle.installer-stock-fallback.v1'


def installer_row(service, application):
    try:
        installation = application.device.installation
    except ObjectDoesNotExist:
        return None, '该设备没有可追溯的装车记录'
    if installation.status != 'INSTALLED' or not installation.operator_id:
        return None, '原装车记录已取消或未记录操作账号'
    user = installation.operator
    identity = get_active_identity(user)
    root = application.factory_organization.get_factory_root()
    if not root or installation.organization.get_factory_root() != root:
        return None, '装车组织与本售后厂家链路不一致'
    if not identity or not user.is_active or not user.wx_openid:
        return None, '原装车操作账号或当前岗位无效'
    if identity.organization.organization_type.code == 'SERVICE':
        return None, '服务商已停止接收新的维修派工，请选择其他维修路径'
    if not identity.role.is_owner_role and not identity_has_role(identity, 'merchant_electrician'):
        return None, '原装车账号当前不再具备装车操作岗位或相应分管授权'
    if identity.organization_id != installation.organization_id:
        return None, '原装车操作账号当前组织与装车记录不一致，请另选维修路径'
    snapshot = service._contact_snapshot(user=user, organization=installation.organization,
        role_code='installation_operator', role_name='原装车操作人员', fallback_address=installation.location)
    return {'user':user, 'organization':installation.organization, 'route':'ORIGINAL_INSTALLER',
            'snapshot':snapshot, 'available':True, 'unavailable_reason':''}, ''


def stock_check(application, organization_id, materials):
    root = application.factory_organization.get_factory_root()
    def valid_spec(value):
        text = str(value or '')
        return int(text) if text.isascii() and text.isdigit() and int(text) > 0 else None
    spec_ids = {valid_spec(item.get('specification_id')) for item in materials}
    spec_ids.discard(None)
    available = set(OrganizationMaterialLot.objects.filter(
        stock__organization_id=organization_id, stock__source_factory_id=root.pk,
        stock__material_spec_id__in=spec_ids, on_hand_milli__gt=F('reserved_milli'),
    ).values_list('stock__material_spec_id', flat=True)) if root else set()
    checks = []
    for item in materials:
        spec = valid_spec(item.get('specification_id'))
        checks.append({'bom_item_id':item.get('bom_item_id'), 'material_id':item.get('material_id'),
            'material_name':item.get('material_name') or '',
            'specification_id':spec, 'specification_name':item.get('specification_name') or '',
            'available':spec is not None and spec in available,
            'reason':'' if spec is not None and spec in available else ('冻结BOM缺少物料规格，不能校验库存' if spec is None else '无未预占的可用库存')})
    return checks


def confirmation_data(application, user, fault, materials, station, original):
    return {'application_id':application.pk, 'reviewer_id':user.pk,
            'dispatch_version':application.dispatch_version,
            'original_user_id':original['user_id'], 'original_organization_id':original['organization_id'],
            'fault_type':fault,
            'material_keys':sorted(str(item.get('bom_item_id') or item.get('material_id')) for item in materials),
            'station_id':station['station_application'].pk,
            'station_user_id':station['user'].pk}


def inspect_installer(service, application, user, fault, materials):
    if fault not in dict(AfterSalesApplication.FaultType.choices):
        return {'status':'NEEDS_SELECTION', 'message':'请先选择初步故障认定。'}, None
    if fault == 'COMPONENT_SUSPECTED' and not materials:
        return {'status':'NEEDS_SELECTION', 'message':'请先勾选可能需要更换的BOM物料，再校验原装车组织库存。'}, None
    row, error = installer_row(service, application)
    if row is None:
        return {'status':'INSTALLER_UNAVAILABLE', 'message':error, 'nearest_station':None}, None
    checks = stock_check(application, row['organization'].pk, materials)
    missing = [item for item in checks if not item['available']]
    result = {'status':'READY', 'message':'原装车操作身份有效，所选物料均有可用库存，可确认派单。' if checks else '原装车操作身份有效，本次未选择换件物料，无需配件库存校验。',
        'original_installer':service._public_candidate(row), 'stock_checks':checks,
        'missing_materials':missing, 'nearest_station':None, 'confirmation_token':''}
    if not missing:
        return result, row
    result['status'] = 'MISSING_PARTS'
    names = '；'.join((item['material_name']+' '+item['specification_name']).strip() for item in missing)
    result['message'] = '原装车组织缺少可用配件：'+names+'。不能派给原装车操作人。'
    stations, station_message = service._service_station_recommendations(application, materials)
    if not stations:
        result['message'] += station_message+' 请保持待处理或调整维修方案。'
        return result, None
    nearest = stations[0]
    result['nearest_station'] = service._public_service_station(nearest)
    result['confirmation_token'] = signing.dumps(confirmation_data(application,user,fault,materials,nearest,result['original_installer']),salt=SALT,compress=True)
    result['message'] += ' 推荐改派最近的合格服务站，请厂家售后经理确认后再派单。'
    return result, nearest


def validate_fallback(service, application, user, fault, materials, data):
    preview, nearest = inspect_installer(service,application,user,fault,materials)
    if preview['status'] != 'MISSING_PARTS' or not nearest:
        raise ValueError('原装车库存或候选资格已变化，请重新查看校验提示后再确认。'+preview['message'])
    if data.get('original_installer_fallback_confirmed') is not True:
        raise ValueError('请先查看缺料提示，并明确确认改派最近服务站。')
    try:
        token_data = signing.loads(str(data.get('original_installer_fallback_token') or ''),salt=SALT,max_age=600)
    except signing.BadSignature:
        raise ValueError('缺料改派确认已失效，请重新校验后确认。')
    if token_data != confirmation_data(application,user,fault,materials,nearest,preview['original_installer']):
        raise ValueError('所需物料或最近服务站已变化，请重新查看提示并确认。')
    if str(data.get('service_station_application_id') or '') != str(nearest['station_application'].pk):
        raise ValueError('缺料改派只能确认当前最近的合格服务站。')
    return nearest, preview
