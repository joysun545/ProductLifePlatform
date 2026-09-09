# 商品型号、v1 BOM 与物料编码功能部署说明

## 本次修改

- 商品型号创建页面不再要求选择品牌，`ProductModel.brand` 改为可空。
- 新增 `BOMItem.material_spec`，形成“物料 → 规格 → 供应商”完整链路。
- 创建商品型号与 v1 BOM 使用同一数据库事务。
- 新增按产品品类读取物料、规格和供应商的接口。
- 创建成功后自动跳转到“型号列表”，新型号显示在列表最上方。
- 点击型号行进入独立的 v1 BOM 查看页；创建页不再承担后续编辑或删除操作。
- 查看页下方的“编辑”按钮会显示当前品类的全部物料：原清单物料默认保留，新出现物料默认勾选删除。
- 编辑页提供“保存”和“删除v1版BOM清单”，删除只影响 BOM，商品型号始终保留。
- 后端会校验物料所属品类、规格所属物料及供应商供货关系。
- 新建物料时自动保存为“品类编码-品类内物料编码”，例如 `GEN` + `CTRL` 保存为 `GEN-CTRL`。
- 页面实时显示物料最终编码；服务端会防止重复添加相同品类前缀。

## 一、上传补丁包（Windows PowerShell）

把下载后的压缩包上传到服务器：

```powershell
scp "C:\Users\joysu\Downloads\product_bom_material_code_ui_flow_patch_20260807_v3.tar.gz" root@8.137.204.209:/root/
```

## 二、服务器备份

登录服务器后执行：

```bash
cd /root/ProductLifePlatform
BACKUP_DIR="/root/ProductLifePlatform_backup_product_bom_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"
cp --parents \
  apps/product/models/product.py \
  apps/product/models/bom.py \
  apps/product/api/views.py \
  apps/product/api/urls.py \
  apps/product/templates/product/product_manage.html \
  apps/product/tests.py \
  apps/supply_chain/web/views.py \
  apps/supply_chain/templates/supply_chain/material_config.html \
  apps/supply_chain/tests.py \
  "$BACKUP_DIR"
cp db.sqlite3 "$BACKUP_DIR/db.sqlite3"
echo "$BACKUP_DIR"
```

## 三、覆盖代码

```bash
tar -xzf /root/product_bom_material_code_ui_flow_patch_20260807_v3.tar.gz -C /root/ProductLifePlatform
```

## 四、迁移前检查

先确认旧数据中没有“同一 BOM 重复使用同一种物料”的记录：

```bash
source venv/bin/activate
python manage.py shell -c "from django.db.models import Count; from product.models import BOMItem; print(list(BOMItem.objects.values('bom_id','material_id').annotate(n=Count('id')).filter(n__gt=1)))"
```

正常结果应为：

```text
[]
```

如果不是空列表，先停止迁移并处理重复数据。

## 五、检查并执行迁移

```bash
python manage.py check
python manage.py makemigrations product --check --dry-run
python manage.py showmigrations product
python manage.py migrate
python manage.py showmigrations product
```

预期 `makemigrations --check --dry-run` 显示 `No changes detected`，迁移后 `product.0004_product_v1_bom` 显示 `[X]`。

## 六、运行测试并重启服务

```bash
python manage.py test product supply_chain
systemctl restart productlifeplatform
systemctl status productlifeplatform --no-pager
```

## 七、页面验证

打开：

```text
https://joytest.site/pldp/web/product/
```

按以下顺序验证：

1. 选择所属品类并填写型号编码、型号名称和规格描述。
2. 点击 `+`，确认自动出现该品类的全部物料，且按物料编码排序。
3. 为保留的物料选择规格和供应商；不需要的物料勾选“是否删除”。
4. 点击“确认添加v1版BOM清单”。
5. 确认页面自动切换到“型号列表”，且刚创建的型号位于第一行。
6. 点击该型号行，确认进入只读的 v1 BOM 表格，表格下方只显示“编辑”按钮。
7. 点击“编辑”，确认原清单物料的“是否删除”不勾选，新增加到该品类但不属于原清单的物料默认勾选。
8. 不做修改直接保存，确认 v1 BOM 内容不变。
9. 再次进入编辑状态并删除整个 v1 BOM，确认返回列表后商品型号仍存在，状态变为“未创建”。

然后打开：

```text
https://joytest.site/pldp/web/supply-chain/material-config/
```

验证物料编码规则：

1. 选择产品品类后，物料编码输入框左侧应显示该品类编码前缀。
2. 输入 `CTRL`，页面应显示类似“最终保存编码：GEN-CTRL”。
3. 保存后在物料列表中确认数据库编码为 `GEN-CTRL`。
4. 如果误输入完整的 `GEN-CTRL`，系统仍只保存一个前缀，不会成为 `GEN-GEN-CTRL`。
5. 本次规则只影响新建物料，不自动修改已有物料编码及其关联数据。

## 数据准备要求

如果某一物料没有规格，或者某个规格没有供应商，页面会显示但不能保存该物料。需要先在供应链资料中完成：

1. 物料关联产品品类。
2. 物料下创建物料规格。
3. 物料规格关联供应商组织。
