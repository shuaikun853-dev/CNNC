# 中核价格监测校验全量数据获取方法

> 本文档面向其他 AI agent，说明如何从中核系统获取价格监测校验所需的全部数据。

---

## 一、数据来源

全流程需要两类数据：

| 数据 | 来源 | 用途 |
|------|------|------|
| SaaS 待审批数据 | SaaS 同款审批页面接口 | 互联网标题、互联网链接、SaaS 价格、SKU 编号 |
| 中核待处理数据 | 中核商城商品管理接口 | 客户标题、documentId、fourCategoryId、参考价 referencePrice |

两类数据通过 `vCommodityId == documentId` 关联。

---

## 二、接口一：SaaS 待审批数据

### 接口地址

```text
POST http://saasportal.iio.com/improxy/saas/iio2/sameAudit/queryAll
```

### 请求体

```json
{
  "commodityId": "",
  "commodityName": "",
  "syncState": "1",
  "isReferUrl": "0",
  "page": 1,
  "limit": 100,
  "tenantId": "188605",
  "commodityPoolId": "222"
}
```

### 认证

请求头 Cookie：

```text
x-access-token=<JWT>
refreshToken=<JWT>
```

从浏览器 F12 → Network → 任一请求 → Cookie 复制。

### 必须的请求头

```text
Content-Type: application/json
X-Requested-With: XMLHttpRequest
Cookie: x-access-token=xxx; refreshToken=xxx
```

### 分页逻辑

```text
1. page=1, limit=100 请求第一页
2. 返回 JSON 里 count = 总条数
3. rows = 当前页数据
4. 如果 rows.length < 100 或 累计 >= count，停止
5. 否则 page++ 继续请求
```

### 返回字段（rows 里每条）

```text
vCommodityId       SaaS 商品 ID（主键，与中核 documentId 对应）
queryStr           客户标题（标准商品描述）
skuId              SKU 编号
skuName            互联网标题（候选商品标题）
modelAuditResult   SaaS 模型审核结果
isMatch            SaaS 是否匹配
vendorName         供应商
shopName           店铺名称
price              SaaS 价格
extLink            互联网链接（要爬取的页面 URL）
commentNum         评论数
createTime         创建时间
updateTime         更新时间
isOperate          是否可操作
```

### 保存为 JSON

```python
# 保存为 data/saas_json.json
{
  "rspCode": "0000",
  "count": 72,
  "rows": [...]
}
```

---

## 三、接口二：中核待处理数据

### 接口地址

```text
POST https://www.cnncmall.com/emp/onecode/improxy/iio2/quasiStandardCommodityDocumentModel/query?y1AmuISe=<URL参数>
```

### 认证

中核接口用 `y1AmuISe` 参数认证，有两个不同的值：

| 位置 | 变量名 | 说明 |
|------|--------|------|
| URL query 参数 | `y1AmuISe` | 放在 URL `?y1AmuISe=xxx` |
| 请求头 | `y1AmuISe` | 放在 HTTP header `y1AmuISe: xxx` |

**这两个值不一样！** 从浏览器 F12 → Network → 对应请求的 cURL 里分别复制。

可选 Cookie（中核商城登录态）：

```text
auth-token=<JWT>
access-token=AT-xxx-xxx
```

### 必须的请求头

```text
Content-Type: application/json
X-Requested-With: XMLHttpRequest
y1AmuISe: <header参数值>
Cookie: auth-token=xxx; access-token=xxx  （可选但建议带上）
```

### 请求体

```json
{
  "page": 1,
  "limit": 100,
  "pageType": "1",
  "itemName": "",
  "materialCode": "",
  "materialDesc": "",
  "taxCode": "",
  "commodityType": "",
  "commodityName": "",
  "inputOperators": [],
  "inputStartTime": "",
  "inputEndTime": "",
  "commitAuditStartTime": "",
  "commitAuditEndTime": "",
  "auditOperators": [],
  "auditStartTime": "",
  "auditEndTime": "",
  "sassAuditOperators": [],
  "sassAuditStartTime": "",
  "sassAuditEndTime": "",
  "reviewAuditOperators": [],
  "reviewAuditStartTime": "",
  "reviewAuditEndTime": "",
  "fourCategoryIds": [],
  "sassCheckStatuss": ["0"],
  "specs": [
    {"propName": "品牌", "isMust": "1", "words": [], "propValue": ""},
    {"propName": "名称", "isMust": "1", "words": [], "propValue": ""},
    {"propName": "型号", "isMust": "1", "words": [], "propValue": ""},
    {"propName": "包装规格", "isMust": "1", "words": [], "propValue": ""},
    {"propName": "销售单位", "isMust": "1", "words": [], "propValue": ""},
    {"propName": "单位换算系数", "isMust": "1", "words": [], "propValue": ""}
  ]
}
```

### 关键参数

```text
sassCheckStatuss: ["0"]  → 待 SaaS 处理 / 待校验数据
pageType: "1"            → 当前列表页类型
specs                     → 默认必填属性筛选，属性值为空表示不过滤
```

### 分页逻辑

与 SaaS 接口相同：page=1 开始，limit=100，直到取完 count 条。

### 返回字段（rows 里每条）

```text
id                        记录 ID
documentId                文档 ID（与 SaaS vCommodityId 对应）
referenceId               参考 ID
commodityName             商品名称
referenceLink             参考链接
referencePrice            参考价格（列表层单价）
categoryId                类目 ID
categoryName              类目名称
itemName                  物料名称
materialCode              物料编码
materialDesc              物料描述
materialType              物料类型
sassCheckStatus           SaaS 校验状态值
sassCheckStatusNum        SaaS 校验状态数值
sassVCommodityName        SaaS 商品名称
fourCategoryId            四级类目 ID（调 detail 接口必需）
fourCategoryName          四级类目名称
quasiStandardCommodityCode  准标准商品编码
brandId                   品牌 ID
brandName                 品牌名称
commodityType             商品类型
auditStatus               审核状态
rejectNum                 驳回次数
commitAuditOperator       提交审核人
commitAuditTime           提交审核时间
auditOperator             审核人
auditTime                 审核时间
inputOperator             录入人
inputTime                 录入时间
createTime                创建时间
updateTime                更新时间
```

### 保存为 JSON

```python
# 保存为 data/customer_json.json
{
  "rspCode": "0000",
  "count": 159,
  "rows": [...]
}
```

---

## 四、接口三：中核详情接口（获取客户最高价）

### 接口地址

```text
POST https://www.cnncmall.com/emp/onecode/improxy/iio2/quasiStandardCommodityDocumentModel/detail?y1AmuISe=<URL参数>
```

### 认证

与接口二相同的 y1AmuISe（URL + header）+ Cookie。

### 请求体

```json
{
  "documentId": "681905981645938688",
  "fourCategoryId": "18099901"
}
```

`documentId` 和 `fourCategoryId` 从接口二的返回数据里获取。

### 返回结构

```json
{
  "rspCode": "0000",
  "data": {
    "documentId": "681905981645938688",
    "referencePrice": "169",
    "referenceInfos": [
      {"referencePrice": "157.34", "order": 1},
      {"referencePrice": "169", "order": 2}
    ],
    "categoryId": "180999",
    "categoryName": "其他切削工具"
  }
}
```

### 客户最高价提取规则（四级优先级）

```text
1. data.referenceInfos[].referencePrice → 取 max（多个参考价取最大值）
2. data.referencePrice                  → 单价
3. rows[].referenceInfos[].referencePrice → 取 max
4. rows[].referencePrice                → 单价
```

```text
多个有效参考价（≥2）→ FOUND（取 max）
单个有效参考价      → FOUND_SINGLE_REFERENCE
请求失败            → DETAIL_REQUEST_FAILED
```

---

## 五、认证信息管理

### 不要写死在代码里

支持三种方式（优先级从高到低）：

```text
1. 系统环境变量
2. .env 文件
3. config.yaml 配置文件
```

### 需要的配置项

| 配置项 | 说明 |
|--------|------|
| `SAAS_COOKIE` | SaaS 接口 Cookie |
| `CNNC_COOKIE` | 中核接口 Cookie（auth-token + access-token） |
| `CNNC_QUERY_Y1AMUISE` | 中核 URL 里的 y1AmuISe 参数 |
| `CNNC_HEADER_Y1AMUISE` | 中核请求头里的 y1AmuISe（与 URL 里的不同） |

### .env 文件示例

```text
SAAS_COOKIE=x-access-token=xxx; refreshToken=xxx
CNNC_COOKIE=auth-token=xxx; access-token=AT-xxx-xxx
CNNC_QUERY_Y1AMUISE=0L335ValqWcSxA...
CNNC_HEADER_Y1AMUISE=0IuRqSGlqWcSxA...
```

### 认证过期判断

```text
HTTP 401/403 → 疑似登录态过期
返回非 JSON  → 接口异常
返回 count=null → y1AmuISe 可能过期
```

过期后需要重新从浏览器抓取 Cookie / y1AmuISe。

---

## 六、JSON 格式容错

中核/SaaS 导出的 JSON 可能是多段拼接（分页导出首尾相连），常见问题：

```text
问题1：两段 JSON 之间缺右花括号 }
  "success": true
  {                  ← 缺 }

问题2：末尾多余的 } 或 ,
  "success": true
  }}                 ← 多余

问题3：BOM 头
  \ufeff{"rspCode"...}
```

### 容错处理

```python
# 1. 去 BOM
text = text.lstrip('\ufeff')

# 2. 补缺右花括号（"success": true 后紧跟 { 的情况）
text = re.sub(r'("success"\s*:\s*(?:true|false))(\s*\n\s*)(\{)', r'\1}\2\3', text)

# 3. 补缺 ]} （}\n{ 且下一行有 rspCode 的情况）
text = re.sub(r'(\})(\s*\n\s*)(\{\s*\n\s*"rspCode")', r'\1\n    ]\n}\2\3', text)

# 4. 逐段解析，只接受 { 或 [ 开头
#    其他字符（}、]、, 等）跳过
```

---

## 七、数据关联

```text
SaaS 数据                    中核数据
vCommodityId  ←──────────→  documentId

关联后从中核数据获取：
  fourCategoryId   → 调 detail 接口拿客户最高价
  fourCategoryName → 四级类目名称
  referencePrice   → 列表参考价（对比用）
```

匹配规则：
```text
saas.vCommodityId == customer.documentId → MATCHED
找不到                                  → NOT_FOUND / LOOKUP_FAILED
```

---

## 八、自动拉取脚本

已实现的自动拉取脚本：

```text
E:\.A中核运维\自动拉取\auto_pull_and_run.py
```

### 单次运行

```bash
cd E:\.A中核运维\自动拉取
python auto_pull_and_run.py --once
```

### 每小时循环

```bash
python auto_pull_and_run.py --loop
```

### 输出目录结构

```text
自动拉取/
  20260624_181215/           ← 每轮时间戳目录
    01_SaaS待审批.xlsx
    02_中核待处理.xlsx
    03_本轮合并数据.xlsx
    raw_saas.json            ← SaaS 原始 JSON
    raw_cnnc.json            ← 中核原始 JSON
    audit_result.xlsx        ← 下游审计脚本输出
    logs/run.log
  master.log                 ← 总日志
  .env                       ← 认证配置
  config.yaml                ← 配置文件
```

---

## 九、安全约束

```text
1. 不自动提交审批
2. 不调用通过/驳回/保存/提交类接口
3. 只允许查询接口（queryAll / query / detail）
4. 所有 token/cookie 只从配置读取，不写死在代码里
5. 日志里不打印 cookie/token/y1AmuISe
6. 不把 .env / config.yaml / *.curl 提交到 git
```

---

## 十、常见问题

### Q: 中核 query 接口返回 count=null, rows=[]

```text
y1AmuISe 过期了。重新从中核页面 F12 抓取 y1AmuISe（URL 和 header 两个值都要更新）。
```

### Q: SaaS 接口超时

```text
1. 检查网络是否能访问 saasportal.iio.com
2. Cookie 过期 → 401/403
3. 接口本身慢 → 加大 timeout 到 120 秒
4. curl 里有 --insecure → Python 需要 ssl.CERT_NONE
```

### Q: detail 接口返回 DETAIL_REQUEST_FAILED

```text
1. 检查 detail_request.curl 里的登录态是否过期
2. auth-token / access-token / y1AmuISe / Cookie 都需要更新
3. 重新从中核商品详情页 F12 抓取 detail 接口的 cURL
```

### Q: 两个 y1AmuISe 值一样吗

```text
不一样。
URL 里的 y1AmuISe 和 header 里的 y1AmuISe 是两个不同的值。
从 cURL 里分别复制：
  - URL: ?y1AmuISe=xxx → CNNC_QUERY_Y1AMUISE
  - Header: y1AmuISe: xxx → CNNC_HEADER_Y1AMUISE
```

### Q: JSON 解析失败

```text
多段 JSON 拼接缺右花括号。用 _heal_missing_closing_brace() 自动修复。
如果还有问题，看报错的 char 位置手动检查。
```

---

## 十一、关键文件位置

```text
自动拉取脚本：
  E:\.A中核运维\自动拉取\auto_pull_and_run.py
  E:\.A中核运维\自动拉取\.env
  E:\.A中核运维\自动拉取\config.yaml

审计主脚本：
  E:\.A中核运维\cnnc-ops-agent\scripts\audit_saas_excel_file.py

detail cURL：
  E:\.A中核运维\cnnc-ops-agent\data\detail_request.curl

模型配置：
  E:\.A中核运维\cnnc-ops-agent\data\model.env

输出 Excel：
  E:\.A中核运维\cnnc-ops-agent\runtime\saas_audit_final.xlsx

完整流程文档：
  E:\.A中核运维\cnnc-ops-agent\docs\SAAS_AUDIT_FLOW.md
```
