# 中核价格监测校验全量数据获取方法

> 本文档面向其他 AI agent，说明如何自动获取中核 SaaS 待审批数据和中核客户待处理数据，并跑完整价格校验流程。

---

## 一、两个数据源

### 1. SaaS 待审批数据

- **接口**: `POST http://saasportal.iio.com/improxy/saas/iio2/sameAudit/queryAll`
- **认证**: Cookie（`x-access-token` + `refreshToken`）
- **请求体**:
  ```json
  {"commodityId":"","commodityName":"","syncState":"1","isReferUrl":"0","page":1,"limit":100,"tenantId":"188605","commodityPoolId":"222"}
  ```
- **分页**: 每页 100 条，`count` 字段是总数，自动翻页直到取完
- **返回**: `rows` 数组，每行含 `vCommodityId`、`queryStr`（客户标题）、`skuName`（互联网标题）、`extLink`（互联网链接）、`price`（SaaS 价格）、`skuId`

### 2. 中核客户待处理数据

- **接口**: `POST https://www.cnncmall.com/emp/onecode/improxy/iio2/quasiStandardCommodityDocumentModel/query?y1AmuISe=<URL参数>`
- **认证**: 两个不同的 `y1AmuISe` 值
  - URL query 参数: `CNNC_QUERY_Y1AMUISE`
  - 请求头: `y1AmuISe: <CNNC_HEADER_Y1AMUISE>`
  - Cookie: `auth-token` + `access-token`（中核商城登录态）
- **请求体**:
  ```json
  {"page":1,"limit":100,"pageType":"1","sassCheckStatuss":["0"],"specs":[...]}
  ```
- **分页**: 每页 100 条，自动翻页
- **返回**: `rows` 数组，每行含 `documentId`、`fourCategoryId`、`referencePrice`、`referenceInfos`、`commodityName`

### 3. 主键对应关系

```
saas_json.vCommodityId == customer_json.documentId
```

SaaS 数据没有 `documentId` 和 `fourCategoryId`，需要用 `vCommodityId` 去 customer 数据里匹配，拿到 `fourCategoryId` 后才能调 detail 接口。

---

## 二、认证信息

### 配置文件

路径: `E:\.A中核运维\自动拉取\.env`

```
SAAS_COOKIE=x-access-token=xxx; refreshToken=xxx
CNNC_COOKIE=auth-token=xxx; access-token=xxx
CNNC_QUERY_Y1AMUISE=<URL里的y1AmuISe>
CNNC_HEADER_Y1AMUISE=<请求头里的y1AmuISe>
```

### 过期判断

- SaaS 返回 401/403 → SaaS Cookie 过期
- 中核 query 返回 count=None/rows=0 且非正常空数据 → y1AmuISe 过期
- detail 接口返回非 200 → 中核登录态过期

### 更新方式

从浏览器 F12 → Network → 抓对应接口的 cURL，提取 Cookie 和 y1AmuISe 值。

---

## 三、detail 接口（取客户最高价）

- **接口**: `POST https://www.cnncmall.com/emp/onecode/improxy/iio2/quasiStandardCommodityDocumentModel/detail?y1AmuISe=<URL参数>`
- **请求体**: `{"documentId":"xxx","fourCategoryId":"xxx"}`
- **认证**: 同中核 query 接口（Cookie + y1AmuISe header）
- **返回**: `data.referenceInfos[].referencePrice`，取最大值作为客户最高价

### cURL 模板文件

路径: `E:\.A中核运维\cnnc-ops-agent\data\detail_request.curl`

保存完整 cURL 命令，脚本自动解析 URL/headers/cookie/data-raw。

---

## 四、自动拉取脚本

### 路径

```
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

### 输出

```
自动拉取/
  20260624_180000/           ← 时间戳目录
    01_SaaS待审批.xlsx
    02_中核待处理.xlsx
    03_本轮合并数据.xlsx
    raw_saas.json
    raw_cnnc.json
    logs/run.log
    audit_result.xlsx        ← 下游审计脚本输出
  master.log
  .env
  config.yaml
```

---

## 五、完整价格校验流程

### 脚本路径

```
E:\.A中核运维\cnnc-ops-agent\scripts\audit_saas_excel_file.py
```

### 运行命令

```powershell
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
  --excluded-output-dir .\runtime\zhaoshang_templates `
  --zhaoshang-task `
  --zhaoshang-concurrency 6 `
  --zhaoshang-task-prefix 'cnnc_current_opt' `
  --zhaoshang-wait-timeout 600 `
  --zhaoshang-progress-interval 30
```

### 流程步骤

```
1. 加载模型配置（data/model.env）
2. 解析 saas_json + customer_json（多段 JSON 容错）
3. 生成 11 个招商局商城爬取模板
4. 异步提交招商局任务（11 商城并发）
5. detail 接口取客户最高价（按 documentId 缓存）
6. 批量模型校验（去重→分批→并发）
7. multi_mall_crawler 爬非招商局链接
8. 等招商局任务完成 → 回填价格
9. 计算 SaaS价vs客户价 / 爬取价vs客户价 / 价格变动 / 最终决策
10. 输出 Excel（中文表头 + 高亮异常行）
```

### 三路并行

```
detail 完成后：
  ├─ 线程1：模型批量校验（几秒）
  ├─ 线程2：multi_mall_crawler 爬虫
  └─ 线程3：招商局建任务（模板导出后立即启动）
```

---

## 六、11 个招商局商城

| 商城中文名 | mall_key | 域名 | sourceId |
|---|---|---|---|
| 京东 | jd | jd.com | 3 |
| 震坤行 | zkh | zkh.com | 18 |
| 浙江宏伟 | tima | tima365.com | 39 |
| 浙江物产 | zjmi | zjmi-mall.com | 40 |
| 鑫方盛商城 | xfs | xfs.com | 20 |
| 阳采集团 | ycw | ycw.cn | 82 |
| 得力商城 | nbdeli | nbdeli.com | 19 |
| 固安捷商城 | mymro | mymro.cn | 9 |
| 科力普商城 | colipu | colipu.com | 6 |
| 易派客 | epec | epec.com | 80 |
| 领先未来 | lxwl | 66123123.com | 34 |

### 招商局任务命名

```
中核价格爬取+<商城中文名>+<时间戳HHMMSS>
例：中核价格爬取+京东+185428
```

### 招商局认证

路径: `E:\.A2026\招商局自动建任务\external_tools\crawl_task_tool\auto_crawl_task.py`

内置 `_DEFAULT_BLADE_AUTH` 和 `_DEFAULT_COOKIE`，也可用环境变量 `BLADE_AUTH` / `COOKIE` 覆盖。

### 招商局结果回填

招商局结果 xlsx 没有 URL 列，只有 `商品编码` 和 `商品价格`。回填逻辑：
1. 从爬取模板建立 `商品编码 → URL` 映射
2. 读招商局结果 `商品编码 → 商品价格`
3. 通过映射反查 URL → 回填到主审计表

---

## 七、非招商局商城（走 multi_mall_crawler）

| 商城 | 域名 | 爬虫文件 | 备注 |
|---|---|---|---|
| 齐心商城 | comix.com.cn | crawlers/comix.py | OCR 价格图 |
| 咸亨国际 | xhgjmall.com | crawlers/xhgj.py | |
| 西域商城 | ehsy.com | crawlers/ehsy.py | |
| 欧菲斯 | ofs.cn | crawlers/ofs.py | |
| 史泰博 | stbchina.cn | crawlers/stbchina.py | 需 cookies |
| 天马 | tima365.com | crawlers/tima.py | 三层采集 API→DOM→store |

爬虫项目路径: `E:\.A2026\多商城爬取\multi_mall_crawler`

---

## 八、模型配置

路径: `E:\.A中核运维\cnnc-ops-agent\data\model.env`

```
TITLE_MATCH_MODEL_API_KEY=<火山方舟 API Key>
TITLE_MATCH_MODEL_BASE_URL=https://ark.cn-beijing.volces.com/api/v3/chat/completions
TITLE_MATCH_MODEL_NAME=doubao-seed-2-0-mini-260215
```

批量调用参数（环境变量）：
```
TITLE_MATCH_MODEL_BATCH_SIZE=30
TITLE_MATCH_MODEL_BATCH_CONCURRENCY=12
TITLE_MATCH_MODEL_TIMEOUT=180
```

---

## 九、输出 Excel 列

| 列名 | 说明 |
|---|---|
| SaaS商品ID | vCommodityId |
| 客户标题 | queryStr |
| 互联网标题 | skuName |
| 互联网链接 | extLink |
| SaaS价格 | price |
| 客户价 | detail 接口取出的最高价 |
| 客户价状态 | FOUND / FOUND_SINGLE_REFERENCE / LOOKUP_FAILED |
| 标题匹配结果 | SAME / NOT_SAME / MANUAL_REVIEW |
| 爬取页面价格 | 招商局或爬虫返回的价格 |
| 页面价格状态 | 爬取成功 / 价格为0 / 招商局跳过 |
| SaaS价对比客户价 | SAAS_HIGHER_THAN_CUSTOMER_30 / SAAS_WITHIN_CUSTOMER_THRESHOLD |
| 价格对比客户价 | HIGHER_THAN_CUSTOMER_PRICE_30 / WITHIN_THRESHOLD |
| 超过客户价30% | 是 / 否 |
| 价格变动 | CHANGED / UNCHANGED |
| 最终决策 | PASS / REJECT_SAAS_HIGH / REJECT_PRICE_CHANGED / REJECT_BOTH |
| 建议操作 | 人工核查 / 建议驳回 |

### 高亮规则

- 超过客户价30% = 是 → 整行淡红
- 价格变动 = CHANGED → 整行淡红
- 价格为0 → 整行淡红

---

## 十、安全约束

```
不自动提交审批
不调用通过/驳回/保存/提交类接口
只允许查询接口
所有 token/cookie 只从配置读取
不打印 cookie/token/auth-token/access-token/y1AmuISe
不把 .env / detail_request.curl / model.env 提交到 git
```

---

## 十一、关键文件位置

```
E:\.A中核运维\
  自动拉取\
    auto_pull_and_run.py          自动拉取脚本
    .env                          认证配置
    config.yaml                   配置文件
    src\                          拉取脚本模块
  cnnc-ops-agent\
    scripts\audit_saas_excel_file.py    主审计脚本
    scripts\export_excluded_malls.py    招商局模板导出
    data\saas_json.json                 SaaS 数据（输入）
    data\customer_json.json             客户数据（输入）
    data\detail_request.curl            detail 接口 cURL
    data\model.env                      模型配置
    runtime\                            输出目录
    app\services\title_match_model_adapter.py   模型调用
    app\workflows\work_01_saas_approval_json_audit.py  审查规则
  .A2026\
    多商城爬取\multi_mall_crawler\       爬虫项目
    招商局自动建任务\                     招商局工具
```
