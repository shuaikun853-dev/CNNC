# 中核价格监测校验全量数据获取指南

> 本文档面向其他 AI agent，说明如何自动获取中核 SaaS 待审批数据 + 中核客户待处理数据，并跑完整价格校验流程。

---

## 一、整体流程

```
自动拉取脚本 → SaaS 接口 + 中核接口 → 生成 JSON + Excel → 全流程审计脚本 → 最终 Excel
```

涉及 3 个项目目录：

| 目录 | 作用 |
|------|------|
| `E:\.A中核运维\自动拉取\` | 自动拉取 SaaS + 中核接口数据 |
| `E:\.A中核运维\cnnc-ops-agent\` | 全流程审计（模型 + 爬虫 + 招商局 + 价格校验） |
| `E:\.A2026\多商城爬取\multi_mall_crawler\` | 非招商局商城的页面价格爬取 |

---

## 二、认证信息

### 需要的认证值（4 个）

| 配置项 | 说明 | 来源 |
|--------|------|------|
| `SAAS_COOKIE` | SaaS 接口 Cookie | 浏览器 F12 → saasportal.iio.com → Cookie |
| `CNNC_QUERY_Y1AMUISE` | 中核 URL 里的 y1AmuISe 参数 | 浏览器 F12 → cnncmall.com → 请求 URL |
| `CNNC_HEADER_Y1AMUISE` | 中核请求头里的 y1AmuISe | 同上但取 -H 里的值（与 URL 里的不同！） |
| `CNNC_COOKIE` | 中核接口 Cookie（auth-token + access-token） | 浏览器 F12 → cnncmall.com → Cookie |

### 存放位置

优先级：`.env` 文件 > 系统环境变量 > `config.yaml`

```
E:\.A中核运维\自动拉取\.env
```

格式：

```text
SAAS_COOKIE=x-access-token=xxx; refreshToken=xxx
CNNC_COOKIE=auth-token=xxx; access-token=xxx
CNNC_QUERY_Y1AMUISE=0L335ValqWcSxA...
CNNC_HEADER_Y1AMUISE=0IuRqSGlqWcSxA...
```

### 招商局 token

招商局 token 存在：

```
E:\.A2026\招商局自动建任务\external_tools\crawl_task_tool\auto_crawl_task.py
```

里面的 `_DEFAULT_BLADE_AUTH` 和 `_DEFAULT_COOKIE` 变量。过期后从招商局后台抓 curl 更新。

### 模型 API 配置

```
E:\.A中核运维\cnnc-ops-agent\data\model.env
```

格式：

```text
TITLE_MATCH_MODEL_API_KEY=<key>
TITLE_MATCH_MODEL_BASE_URL=https://ark.cn-beijing.volces.com/api/v3/chat/completions
TITLE_MATCH_MODEL_NAME=doubao-seed-2-0-mini-260215
```

### 中核 detail 接口 cURL

```
E:\.A中核运维\cnnc-ops-agent\data\detail_request.curl
```

从中核商品详情页 F12 抓 detail 接口的 cURL，保存为文本文件。包含 auth-token/access-token/y1AmuISe/Cookie。

---

## 三、接口说明

### 接口一：SaaS 待审批数据

```
POST http://saasportal.iio.com/improxy/saas/iio2/sameAudit/queryAll
```

请求体：

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

- `syncState=1`：待审批状态
- 自动分页直到取完 `count` 条
- 返回 `rows` 是数据明细
- 每行字段：`vCommodityId`、`queryStr`（客户标题）、`skuName`（互联网标题）、`extLink`（互联网链接）、`price`（SaaS 价格）、`skuId`

### 接口二：中核待处理数据

```
POST https://www.cnncmall.com/emp/onecode/improxy/iio2/quasiStandardCommodityDocumentModel/query?y1AmuISe=<URL参数>
```

请求头：

```
y1AmuISe: <header参数（与URL里的不同）>
Cookie: auth-token=xxx; access-token=xxx
```

请求体：

```json
{
  "page": 1,
  "limit": 100,
  "pageType": "1",
  "sassCheckStatuss": ["0"],
  "specs": [...]
}
```

- `sassCheckStatuss=["0"]`：待 SaaS 处理状态
- 自动分页直到取完 `count` 条
- 每行字段：`documentId`、`fourCategoryId`、`fourCategoryName`、`referencePrice`、`referenceInfos`、`commodityName`、`referenceLink`

### 接口三：中核商品详情（获取客户最高价）

```
POST https://www.cnncmall.com/emp/onecode/improxy/iio2/quasiStandardCommodityDocumentModel/detail?y1AmuISe=<URL参数>
```

请求体：

```json
{
  "documentId": "681905981645938688",
  "fourCategoryId": "18099901"
}
```

返回 `data.referenceInfos[].referencePrice`，取最大值作为客户最高价。

---

## 四、主键对应关系

```
saas_json.vCommodityId == customer_json.documentId
```

SaaS 端没有 `documentId` 和 `fourCategoryId`，需要用 `vCommodityId` 去客户 JSON 里匹配 `documentId`，拿到 `fourCategoryId` 后才能调 detail 接口。

---

## 五、全流程步骤

### 步骤 1：自动拉取数据

```powershell
cd E:\.A中核运维\自动拉取
python auto_pull_and_run.py --once
```

输出目录：`E:\.A中核运维\自动拉取\<时间戳>\`

| 文件 | 说明 |
|------|------|
| `raw_saas.json` | SaaS 原始 JSON |
| `raw_cnnc.json` | 中核原始 JSON |
| `01_SaaS待审批.xlsx` | SaaS Excel |
| `02_中核待处理.xlsx` | 中核 Excel |
| `03_本轮合并数据.xlsx` | 合并 Excel |
| `logs/run.log` | 运行日志 |

### 步骤 2：复制 JSON 到审计项目

```powershell
Copy-Item "E:\.A中核运维\自动拉取\<时间戳>\raw_saas.json" "E:\.A中核运维\cnnc-ops-agent\data\saas_json.json" -Force
Copy-Item "E:\.A中核运维\自动拉取\<时间戳>\raw_cnnc.json" "E:\.A中核运维\cnnc-ops-agent\data\customer_json.json" -Force
```

### 步骤 3：跑全流程审计

```powershell
cd E:\.A中核运维\cnnc-ops-agent

$env:PYTHONIOENCODING='utf-8'
$env:TITLE_MATCH_MODEL_BATCH_SIZE='30'
$env:TITLE_MATCH_MODEL_BATCH_CONCURRENCY='12'
$env:TITLE_MATCH_MODEL_TIMEOUT='180'

E:\Python314\python.exe scripts\audit_saas_excel_file.py `
  --input-saas-json .\data\saas_json.json `
  --input-customer-json .\data\customer_json.json `
  --curl-file .\data\detail_request.curl `
  --output-xlsx .\runtime\saas_audit_final.xlsx `
  --use-model `
  --request-interval 0.5 `
  --enable-crawler `
  --skip-dry-run `
  --crawler-headless `
  --crawler-concurrency 3 `
  --crawler-per-domain-delay 2 `
  --crawler-timeout-ms 18000 `
  --excluded-output-dir .\runtime\full_current_optimized\zhaoshang_templates `
  --zhaoshang-task `
  --zhaoshang-concurrency 6 `
  --zhaoshang-task-prefix 'cnnc_current_opt' `
  --zhaoshang-wait-timeout 600 `
  --zhaoshang-progress-interval 30
```

### 步骤 4：查看结果

输出文件：`runtime/saas_audit_final.xlsx`

- Sheet 1 `summary`：总记录数 / 建议通过 / 人工核查 / 建议驳回 / 客户价FOUND / 超过30%
- Sheet 2 `record_results`：每行一条，中文表头
  - 客户标题 / 互联网标题 / 互联网链接 / SaaS价格 / 客户价
  - 标题匹配结果 / 爬取页面价格 / 页面价格状态
  - SaaS价对比客户价 / 超过客户价30% / 价格变动 / 最终决策
  - 异常行整行高亮淡红

---

## 六、全流程内部逻辑

```
1. 解析 saas_json + customer_json（多段 JSON 容错）
2. 生成 11 个招商局商城爬取模板
3. 异步启动招商局 11 个商城任务（并发 6）
4. 主线程：
   a. detail 接口拿客户最高价（缓存，同 documentId 只调一次）
   b. 批量模型校验（去重 → 分批 30 条 → 并发 12 批）
   c. multi_mall_crawler 爬非招商局商城（comix/ehsy/ofs/xhgj 等）
5. 等招商局任务完成 → 回填价格
6. 回填爬虫价格
7. 计算：
   - saas_price_vs_customer（SaaS 自身价是否超客户价 30%）
   - price_changed（SaaS 价 vs 爬取价是否变动）
   - over_30pct（爬取价是否超客户价 30%）
   - final_decision（PASS / REJECT_SAAS_HIGH / REJECT_PRICE_CHANGED / REJECT_BOTH）
8. 写最终 Excel（中文表头 + 高亮 + 冻结首行 + 自动筛选）
```

---

## 七、11 个招商局商城

| 商城中文名 | mall_key | sourceId | 域名 |
|-----------|----------|----------|------|
| 京东 | jd | 3 | jd.com |
| 震坤行 | zkh | 18 | zkh.com |
| 浙江宏伟 | tima | 39 | tima365.com |
| 浙江物产 | zjmi | 40 | zjmi-mall.com |
| 鑫方盛商城 | xfs | 20 | xfs.com |
| 阳采集团 | ycw | 82 | ycw.cn |
| 得力商城 | nbdeli | 19 | nbdeli.com |
| 固安捷商城 | mymro | 9 | mymro.cn |
| 科力普商城 | colipu | 6 | colipu.com |
| 易派客 | epec | 80 | epec.com |
| 领先未来 | lxwl | 34 | 66123123.com |

剩余商城（comix/ehsy/ofs/xhgj 等）走 multi_mall_crawler。

---

## 八、认证过期判断

| 症状 | 原因 | 解决 |
|------|------|------|
| SaaS 接口返回 401/403 | SAAS_COOKIE 过期 | 重新从 saasportal.iio.com 抓 Cookie |
| 中核接口返回 count=None rows=0 | y1AmuISe 过期 | 重新从 cnncmall.com 抓 y1AmuISe |
| detail 接口 DETAIL_REQUEST_FAILED | detail_request.curl 过期 | 重新从中核商品详情页抓 cURL |
| 招商局建任务失败 | Blade-Auth token 过期 | 重新从招商局后台抓 cURL，更新 auto_crawl_task.py |
| 模型全部 MODEL_UNAVAILABLE | model.env 未配置或 key 过期 | 更新 model.env |

---

## 九、安全约束

```
不自动提交审批
不调用通过/驳回/保存/提交类接口
只允许查询接口
所有 token/cookie 只从配置读取
不打印 cookie/token 到日志
不把 .env / .curl / model.env 提交到 git
```

---

## 十、关键文件位置

```
自动拉取/
  auto_pull_and_run.py          # 自动拉取入口
  .env                          # 认证配置
  src/
    config.py                   # 配置加载
    clients.py                  # HTTP 客户端
    exporter.py                 # Excel 导出
    logger.py                   # 日志

cnnc-ops-agent/
  scripts/
    audit_saas_excel_file.py    # 全流程审计主脚本
    export_excluded_malls.py    # 招商局模板导出
  data/
    saas_json.json              # SaaS 数据（输入）
    customer_json.json          # 中核数据（输入）
    detail_request.curl         # 中核 detail cURL
    model.env                   # 模型 API 配置
  runtime/
    saas_audit_final.xlsx       # 最终输出 Excel
  app/services/
    title_match_model_adapter.py # 模型调用（批量 + 单条）

multi_mall_crawler/
  main.py                       # 爬虫入口
  crawlers/                     # 各商城爬虫
  cookies/                      # cookies 文件
  config/mall_domains.yaml      # 商城配置

招商局自动建任务/
  external_tools/crawl_task_tool/
    auto_crawl_task.py          # 招商局核心脚本（含 token）
  agent/tools/
    crawl_task_tool.py          # 招商局 wrapper
```
