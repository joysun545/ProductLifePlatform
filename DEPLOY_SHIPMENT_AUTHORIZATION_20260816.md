# 订单实物流转：销售经理发货授权部署说明

## 业务链路

本次把订单实物流转调整为：

`生产经理确认生产完成 → 创建订单的销售经理下达发货指令 → 库管打印二维码 → 库管一键出库 → 商家入库 → 销售助理确认已入账`

发货授权与订单清单、付款凭证、累计收款、应收余额完全独立；不会读取或修改资金主账。

## 后端门禁

- 生产完成后创建 `ORDER_PLAN_SHIPMENT_AUTHORIZATION` 操作任务。
- 仅创建该订单的厂家销售经理可以确认。
- 销售经理确认后记录 `shipment_authorized_at` 和 `shipment_authorized_by`。
- 未授权订单不会出现在网页二维码打印中心。
- 生成二维码、确认打印、查看库管出库详情和确认一键出库均有后端授权校验。
- 全部二维码打印后才创建 `ORDER_PLAN_STOCK_OUTBOUND` 操作任务。
- 财务主账、商家多人入库和“确认已入账”代码未修改。

## 历史订单兼容

迁移 `sales.0008_order_plan_shipment_authorization` 会：

- 将已经发货或商家已入库的订单回填为已授权，不改变既有业务事实。
- 为仍处于“生产完成”的订单补发销售经理授权任务。
- 暂时关闭旧逻辑提前生成的未授权库管出库任务。
- 销售经理授权后，如果二维码此前已全部打印，原库管任务会自动重新打开。

## 服务器部署

先在 Windows PowerShell 中从下载目录上传后端补丁：

```powershell
$backendPatch = "$env:USERPROFILE\Downloads\PLDPlatform_shipment_authorization_backend_patch_20260816.tar.gz"
scp $backendPatch root@8.137.204.209:/root/
```

然后在服务器中执行：

```bash
cd /root/ProductLifePlatform

DEPLOY_STAMP=$(date +%Y%m%d_%H%M%S)
tar \
  --exclude='./venv' \
  --exclude='./.git' \
  --exclude='./db.sqlite3*' \
  --exclude='./private_order_finance' \
  --exclude='./media' \
  --exclude='./staticfiles' \
  -czf "/root/ProductLifePlatform_before_shipment_authorization_${DEPLOY_STAMP}.tar.gz" \
  .
cp -a db.sqlite3 "/root/db.sqlite3.before_shipment_authorization_${DEPLOY_STAMP}"

tar -xzf /root/PLDPlatform_shipment_authorization_backend_patch_20260816.tar.gz \
  -C /root/ProductLifePlatform

venv/bin/python manage.py check
venv/bin/python manage.py makemigrations --check
venv/bin/python manage.py test sales workflow -v 2
venv/bin/python manage.py migrate

systemctl restart productlifeplatform
systemctl is-active productlifeplatform
curl -I https://joytest.site/pldp/
```

只有 `check`、`makemigrations --check`、测试和 `migrate` 全部通过后才重启服务。任一命令失败都应停止，不要重启。

## 小程序部署

在 Windows PowerShell 执行：

```powershell
$frontendRoot = "D:\Users\joysu\WeChatProjects\PLDPlatform"
$patch = "$env:USERPROFILE\Downloads\PLDPlatform_shipment_authorization_frontend_patch_20260816.tar.gz"
$backup = "$env:USERPROFILE\Downloads\order_plan_list_before_shipment_authorization_20260816"

Copy-Item "$frontendRoot\pages\sales\order_plan_list" $backup -Recurse
tar -xzf $patch -C $frontendRoot
```

然后在微信开发者工具中执行“编译”，确认订单计划列表无 WXML/JavaScript 报错，再上传体验版。

## 真机验收

1. 生产经理确认生产完成。
2. 厂家库管此时不能打印二维码，也不能出库。
3. 创建订单的厂家销售经理收到“生产完成，待下达发货指令”操作任务。
4. 其他销售经理调用接口应被拒绝。
5. 创建订单的销售经理点击“确认通知库房发货”。
6. 厂家库管收到知晓任务，网页打印中心出现该订单。
7. 全部型号二维码打印完成后，厂家库管收到“一键出库”操作任务。
8. 库管确认出库后，原商家多人入库和“确认已入账”链路继续执行。
9. 整个过程不要求订单清单、付款凭证或应收余额达到任何状态。
