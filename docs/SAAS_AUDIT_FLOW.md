# SaaS 审计 + 多商城爬虫 + 招商局自动建任务 完整流程

> 本文档面向另一个 AI agent，说明如何使用这套自动化流程。
> 项目根目录：`E:\.A中核运维\cnnc-ops-agent`
> 爬虫项目根目录：`E:\.A2026\多商城爬取\multi_mall_crawler`
> 招商局任务工具：`E:\.A2026\招商局自动建任务`

---

## 一、整体流程（一句话版）

```
读 SaaS JSON + 客户 JSON → 生成京东/震坤行爬取模板 → 调中核 detail 接口拿客户最高价 → 调模型校验标题 → multi_mall_crawler 爬非京东/非震坤行链接 → 招商局建任务爬京东+震坤行 → 回填所有价格 → 计算 30% 高价/价格变动/最终决策 → 输出中文表头 Excel
```

---

## 二、输入文件（必须提前放好）

| 文件 | 路径 | 说明 |
|------|------|------|
| SaaS 审批 JSON | `data/saas_json.json` | SaaS 同款审批页面导出，含 `vCommodityId`、`queryStr`（客户标题）、`skuName`（互联网标题）、`extLink`（互联网链接）、`price`（SaaS 价格）、`skuId` |
| 客户导出 JSON | `data/customer_json.json` | 中核客户后台导出，含 `documentId`、`fourCategoryId`、`referencePrice`、`referenceInfos` |
| detail cURL | `data/detail_request.curl` | 中核 detail 接口的完整 cURL（含登录态 auth-token/access-token/y1AmuISe/Cookie） |
| 模型配置 | `data/model.env` | `TITLE_MATCH_MODEL_API_KEY`、`TITLE_MATCH_MODEL_BASE_URL`、`TITLE_MATCH_MODEL_NAME` |

### JSON 格式容错

两个 JSON 文件支持多段拼接（分页导出首尾相连）。脚本会自动补全缺失的右花括号、跳过段间冗余字符、合并所有段的 rows。

### 主键对应关系

```
saas_json.vCommodityId == customer_json.documentId
```

SaaS 端没有 documentId 字段，也没有 fourCategoryId。用 vCommodityId 去 customer_json 里匹配 documentId，拿到 fourCategoryId 后才能调 detail 接口。

---

## 三、运行命令

### 完整流程（含模型 + 爬虫 + 招商局）

```powershell
python scripts/audit_saas_excel_file.py ^
  --input-saas-json      ./data/saas_json.json ^
  --input-customer-json  ./data/customer_json.json ^
  --curl-file            ./data/detail_request.curl ^
  --output-xlsx          ./runtime/saas_full_audit_new.xlsx ^
  --use-model ^
  --request-interval     0.5 ^
  --enable-crawler ^
  --crawler-headless ^
  --crawler-concurrency  3 ^
  --crawler-per-domain-delay 2 ^
  --zhaoshang-task ^
  --zhaoshang-wait-timeout 600 ^
  --zhaoshang-progress-interval 30
```

### 不含招商局（只跑 multi_mall_crawler）

去掉 `--zhaoshang-task`。

### 不含爬虫（只跑 detail + 模型）

去掉 `--enable-crawler` 和 `--zhaoshang-task`。

### 不含模型

去掉 `--use-model`。

---

## 四、流程步骤详解

### 步骤 1：加载模型环境变量

```
读取 data/model.env 注入 os.environ
  TITLE_MATCH_MODEL_API_KEY
  TITLE_MATCH_MODEL_BASE_URL
  TITLE_MATCH_MODEL_NAME
不打印 KEY 值（只显示 SET / NOT_SET）
```

### 步骤 2：解析输入 JSON

```
saas_json 多段解析合并 rows，每行提取：
  vCommodityId（主键）/ queryStr（客户标题）/ skuName（互联网标题）
  extLink（互联网链接）/ price（SaaS 价格）/ skuId

customer_json 多段解析合并 rows，建 documentId 索引：
  documentId -> {fourCategoryId, fourCategoryName, list_reference_price}
```

### 步骤 3：生成京东/震坤行爬取模板

```
扫描 saas_json 所有行：
  京东链接（item.jd.com）-> 爬取模板/<日期>/京东.xlsx
  震坤行链接（zkh.com）-> 爬取模板/<日期>/震坤行.xlsx

每个 xlsx 表头：商品编码 | url
商品编码 = saas.skuId，url = saas.extLink，按 skuId+url 去重
这两个文件不参与 multi_mall_crawler，交给招商局任务系统处理
```

### 步骤 4：调中核 detail 接口拿客户最高价

```
对每个 saas 行：
  用 vCommodityId 在 customer_index 里找 fourCategoryId
  找不到 -> customerPriceStatus = LOOKUP_FAILED（跳过 detail）
  找到 -> POST detail 接口（documentId + fourCategoryId）

detail 返回提取规则（四级优先级）：
  1. data.referenceInfos[].referencePrice 取 max
  2. data.referencePrice
  3. rows[].referenceInfos[].referencePrice 取 max
  4. rows[].referencePrice

多价（>=2）-> FOUND
单价 -> FOUND_SINGLE_REFERENCE
请求失败 -> DETAIL_REQUEST_FAILED

按 (documentId, fourCategoryId) 缓存，同一 documentId 只请求一次
每条间隔 0.5 秒
```

### 步骤 5：调模型校验标题

```
对每个 saas 行调 audit_saas_approval_records(use_model=True)：
  queryStr（客户标题）vs skuName（互联网标题）
  模型返回 SAME / NOT_SAME / MANUAL_REVIEW / MODEL_UNAVAILABLE + confidence

注意：不管模型结果是什么，所有链接都要爬取价格
模型 NOT_SAME 不再跳过爬虫
```

### 步骤 6：multi_mall_crawler 爬取非京东/非震坤行链接

```
收集所有 PENDING_CRAWL 的 URL（去重）：
  京东链接 -> SKIPPED_JD（不爬，交给招商局）
  震坤行链接 -> SKIPPED_JD（不爬，交给招商局）
  无链接 -> SKIPPED_NO_LINK
  其他 -> PENDING_CRAWL

子进程调用 multi_mall_crawler/main.py：
  python main.py --input <url列表.xlsx> --output <结果.xlsx> --headless --concurrency 3

先 dry-run 验证链接识别，再正式爬取
fail-tolerant：单条失败标 FAILED，继续
风控停止：某域名连续 2 次风控 -> 该域名剩余全部标 FAILED

支持 28 个商城（含 tima365）
需 cookies 的商城：zkh / stbchina / 66123123 / tima365 / comix
cookies 文件放在 multi_mall_crawler/cookies/<域名>.json
```

### 步骤 7：招商局自动建任务爬京东+震坤行

```
仅当 --zhaoshang-task 启用时执行

对京东.xlsx 和 震坤行.xlsx 各调一次：
  run_crawl_task(
    task_name="中核价格更新-YYYYMMDD-京东",
    mall_key="jd",
    excel_path="爬取模板/<日期>/京东.xlsx",
    skip_if_exists=True,
  )

任务名格式：中核价格更新-YYYYMMDD-京东 / 中核价格更新-YYYYMMDD-震坤行

招商局工具内部流程：
  读取Excel -> 校验链接域名 -> 新建任务 -> 查taskId -> 启用任务
  -> 轮询进度 -> 导出结果xlsx -> 返回文件路径

token 来源：auto_crawl_task.py 内置 _DEFAULT_BLADE_AUTH / _DEFAULT_COOKIE
也可用环境变量 BLADE_AUTH / COOKIE 覆盖

结果 xlsx 关键列：
  商品价格（第 6 列）
  商品详情地址（第 13 列，即 URL 列）

价格=0 也是有效数据，标 page_price_status="价格为0"
```

### 步骤 8：回填所有价格 + 重新计算

```
multi_mall_crawler 结果 -> 按 URL 匹配回填 page_price_value
招商局结果 -> 按 URL 匹配回填 page_price_value（覆盖京东/震坤行的 SKIPPED_JD）

对每行计算：
  1. saas_price_vs_customer（爬虫前预检）
     SaaS 自身价 > 客户价 x 1.3 -> SAAS_HIGHER_THAN_CUSTOMER_30

  2. page_vs_customer_check（爬虫后检查）
     爬取价 > 客户价 x 1.3 -> HIGHER_THAN_CUSTOMER_PRICE_30
     over_30pct = TRUE

  3. price_changed（价格变动检查）
     |爬取价 - SaaS价| / SaaS价 > 1% -> CHANGED
     SaaS 链接价格与页面实际价格不一致 -> 要驳回

  4. final_decision（综合决策）
     PASS                    全部通过
     REJECT_SAAS_HIGH        SaaS 价或爬取价超客户价 30%
     REJECT_PRICE_CHANGED    价格变动
     REJECT_BOTH             两种异常同时存在
```

### 步骤 9：输出 Excel

```
runtime/saas_full_audit_new.xlsx
  Sheet 1: summary
  Sheet 2: record_results（每行一条，中文表头，44+ 列）

关键列：
  客户标题 | 互联网标题 | 互联网链接 | SaaS价格 | 客户价 | 客户价状态
  标题匹配结果 | 标题匹配置信度
  爬取页面价格 | 页面价格状态 | 页面价格来源
  SaaS价对比客户价 | 价格对比客户价 | 超过客户价30% | 价格变动 | 最终决策
  建议操作 | 原因 | 证据

高亮：over_30pct=是 整行淡红 FFC7CE
冻结首行 + 自动筛选 + 适当列宽
```

---

## 四、流程步骤详解

### 步骤 1：加载模型环境变量

读取 `data/model.env`，注入 `os.environ`。不打印 KEY 值（只显示 SET / NOT_SET）。

### 步骤 2：解析输入 JSON

- `saas_json` 多段解析 → 合并 rows → 每行提取 vCommodityId / queryStr / skuName / extLink / price / skuId
- `customer_json` 多段解析 → 合并 rows → 建 documentId 索引

### 步骤 3：生成京东/震坤行爬取模板

扫描 saas_json 所有行：
- 京东链接（item.jd.com）→ `爬取模板/<日期>/京东.xlsx`
- 震坤行链接（zkh.com）→ `爬取模板/<日期>/震坤行.xlsx`

每个 xlsx 表头：`商品编码 | url`，按 skuId+url 去重。这两个文件交给招商局任务系统处理，不参与 multi_mall_crawler。

### 步骤 4：调中核 detail 接口拿客户最高价

用 vCommodityId 在 customer_index 里找 fourCategoryId → POST detail 接口。

提取规则（四级优先级）：
1. `data.referenceInfos[].referencePrice` → 取 max
2. `data.referencePrice`
3. `rows[].referenceInfos[].referencePrice` → 取 max
4. `rows[].referencePrice`

多价(>=2) → FOUND；单价 → FOUND_SINGLE_REFERENCE；失败 → DETAIL_REQUEST_FAILED。

按 (documentId, fourCategoryId) 缓存，同一 documentId 只请求一次。每条间隔 0.5 秒。

### 步骤 5：调模型校验标题

`queryStr`（客户标题） vs `skuName`（互联网标题），模型返回 SAME / NOT_SAME / MANUAL_REVIEW / MODEL_UNAVAILABLE。

**不管模型结果是什么，所有链接都要爬取价格。** 模型 NOT_SAME 不再跳过爬虫。

### 步骤 6：multi_mall_crawler 爬取非京东/非震坤行链接

收集所有 PENDING_CRAWL 的 URL（去重）：
- 京东链接 → SKIPPED_JD（不爬，交给招商局）
- 震坤行链接 → SKIPPED_JD（不爬，交给招商局）
- 无链接 → SKIPPED_NO_LINK
- 其他 → PENDING_CRAWL

子进程调用 `multi_mall_crawler/main.py`：先 dry-run 验证链接识别，再正式爬取。

fail-tolerant：单条失败标 FAILED，继续。风控停止：某域名连续 2 次风控 → 该域名剩余全部标 FAILED。

支持 28 个商城。需 cookies 的商城：zkh / stbchina / 66123123 / tima365 / comix。cookies 文件放在 `multi_mall_crawler/cookies/<域名>.json`。

### 步骤 7：招商局自动建任务爬京东+震坤行

仅当 `--zhaoshang-task` 启用时执行。

对京东.xlsx 和震坤行.xlsx 各调一次 `run_crawl_task`：
- 任务名格式：`中核价格更新-YYYYMMDD-京东` / `中核价格更新-YYYYMMDD-震坤行`
- `skip_if_exists=True`：同名任务已存在则跳过创建
- token 来源：`auto_crawl_task.py` 内置，也可用环境变量 `BLADE_AUTH` / `COOKIE` 覆盖

招商局工具内部流程：读取 Excel → 校验链接域名 → 新建任务 → 查 taskId → 启用任务 → 轮询进度 → 导出结果 xlsx

结果 xlsx 关键列：
- URL 列 = "商品详情地址"（第 13 列）
- 价格列 = "商品价格"（第 6 列）
- 价格=0 也算有效数据，标 page_price_status="价格为0"

### 步骤 8：回填所有价格 + 重新计算

- multi_mall_crawler 结果 → 按 URL 匹配回填 page_price_value
- 招商局结果 → 按 URL 匹配回填 page_price_value（覆盖京东/震坤行的 SKIPPED_JD）

对每行计算：
1. **saas_price_vs_customer**（爬虫前预检）：SaaS 自身价 > 客户价 x 1.3 → SAAS_HIGHER_THAN_CUSTOMER_30
2. **page_vs_customer_check**（爬虫后检查）：爬取价 > 客户价 x 1.3 → HIGHER_THAN_CUSTOMER_PRICE_30，over_30pct=TRUE
3. **price_changed**（价格变动）：|爬取价 - SaaS价| / SaaS价 > 1% → CHANGED（要驳回）
4. **final_decision**（综合决策）：PASS / REJECT_SAAS_HIGH / REJECT_PRICE_CHANGED / REJECT_BOTH

### 步骤 9：输出 Excel

`runtime/saas_full_audit_new.xlsx`
- Sheet 1: summary（汇总）
- Sheet 2: record_results（每行一条，中文表头，44+ 列）
- over_30pct=是 整行高亮淡红 (FFC7CE)
- 冻结首行 + 自动筛选 + 适当列宽

---

## 五、输出产物清单

| 文件 | 路径 | 说明 |
|------|------|------|
| 主审计 Excel | `runtime/saas_full_audit_new.xlsx` | 最终结果，中文表头 |
| 爬虫输入 | `runtime/_crawl_input.xlsx` | 喂给 multi_mall_crawler 的 URL 列表 |
| 爬虫输出 | `runtime/_crawl_output.xlsx` | multi_mall_crawler 原始返回 |
| 招商局京东结果 | `runtime/zhaoshang_jd_result.xlsx` | 招商局导出的京东价格 |
| 招商局震坤行结果 | `runtime/zhaoshang_zkh_result.xlsx` | 招商局导出的震坤行价格 |
| 京东爬取模板 | `爬取模板/<日期>/京东.xlsx` | 商品编码 + url |
| 震坤行爬取模板 | `爬取模板/<日期>/震坤行.xlsx` | 商品编码 + url |

---

## 六、安全约束（硬性红线）

```
不绕过验证码 / 不自动登录 / 不注入 stealth / 不轮换 UA / 不用代理池
不调用审批/驳回接口 / 不写数据库 / 不执行 SQL
不持久化 cookie/token/auth-token/access-token/y1AmuISe
不打印 cookie/token/auth-token/access-token/y1AmuISe
不把 detail_request.curl / model.env 提交到 git
```

---

## 七、常见问题

**detail 全部 DETAIL_REQUEST_FAILED** → cURL 登录态过期，重新抓 cURL

**模型全部 MODEL_UNAVAILABLE** → 检查 data/model.env 三个变量

**zkh 全部 FAILED** → 阿里云 WAF 识别 headless，等 30 分钟或人工解验证码

**JSON 解析失败** → 脚本自动修复缺右花括号，如仍失败看报错 char 位置手动检查

**招商局 result_path=None** → 用 tools/_export_zhaoshang.py 单独导出已存在的任务

**Excel PermissionError** → 关闭打开的 Excel 文件后重跑

**价格=0** → 有效数据，标"价格为0"，price_changed=CHANGED，final_decision=REJECT_PRICE_CHANGED

---

## 八、给另一个 AI 的操作指引

1. 确认 `data/saas_json.json` 和 `data/customer_json.json` 存在且非空
2. 确认 `data/detail_request.curl` 存在（检查登录态是否过期）
3. 确认 `data/model.env` 存在且 3 个变量都填了
4. 确认 `runtime/saas_full_audit_new.xlsx` 没有被打开
5. 执行第三节的完整命令
6. 跑完检查 `runtime/saas_full_audit_new.xlsx` 的 summary sheet
7. 如果招商局任务没自动跑成功，手动执行 `python tools/_export_zhaoshang.py`
---

## 四、流程步骤详解

### 步骤 1：加载模型环境变量

```
读取 data/model.env → 注入 os.environ
  TITLE_MATCH_MODEL_API_KEY
  TITLE_MATCH_MODEL_BASE_URL
  TITLE_MATCH_MODEL_NAME
不打印 KEY 值（只显示 SET / NOT_SET）
```

### 步骤 2：解析输入 JSON

```
saas_json → 多段 JSON 解析 → 合并所有 rows → 每行提取：
  vCommodityId（主键）
  queryStr（客户标题）
  skuName（互联网标题）
  extLink（互联网链接）
  price（SaaS 价格）
  skuId

customer_json → 多段 JSON 解析 → 合并所有 rows → 建 documentId 索引：
  documentId → {fourCategoryId, fourCategoryName, list_reference_price}
```

### 步骤 3：生成京东/震坤行爬取模板

```
扫描 saas_json 所有行：
  京东链接（item.jd.com）→ 爬取模板/<日期>/京东.xlsx
  震坤行链接（zkh.com）→ 爬取模板/<日期>/震坤行.xlsx

每个 xlsx 表头：商品编码 | url
商品编码 = saas.skuId
url = saas.extLink
按 skuId+url 去重

这两个文件不参与 multi_mall_crawler，交给招商局任务系统处理。
```

### 步骤 4：调中核 detail 接口拿客户最高价

```
对每个 saas 行：
  用 vCommodityId 在 customer_index 里找 fourCategoryId
  找不到 → customerPriceStatus = LOOKUP_FAILED（跳过 detail）
  找到 → POST detail 接口（documentId + fourCategoryId）

detail 返回提取规则（四级优先级）：
  1. data.referenceInfos[].referencePrice → 取 max
  2. data.referencePrice
  3. rows[].referenceInfos[].referencePrice → 取 max
  4. rows[].referencePrice

多价（>=2）→ FOUND
单价 → FOUND_SINGLE_REFERENCE
请求失败 → DETAIL_REQUEST_FAILED

按 (documentId, fourCategoryId) 缓存，同一 documentId 只请求一次。
每条间隔 0.5 秒（--request-interval）。
```

### 步骤 5：调模型校验标题

```
对每个 saas 行调 audit_saas_approval_records(use_model=True)：
  queryStr（客户标题） vs skuName（互联网标题）
  模型返回：SAME / NOT_SAME / MANUAL_REVIEW / MODEL_UNAVAILABLE
  confidence: 0-1

注意：不管模型结果是什么，所有链接都要爬取价格。
模型 NOT_SAME 不再跳过爬虫。
```

### 步骤 6：multi_mall_crawler 爬取非京东/非震坤行链接

```
收集所有 PENDING_CRAWL 的 URL（去重）：
  京东链接 → SKIPPED_JD（不爬，交给招商局）
  震坤行链接 → SKIPPED_JD（不爬，交给招商局）
  无链接 → SKIPPED_NO_LINK
  其他 → PENDING_CRAWL

子进程调用 multi_mall_crawler/main.py：
  python main.py --input <url列表.xlsx> --output <结果.xlsx> --headless --concurrency 3

先 dry-run 验证链接识别，再正式爬取。
fail-tolerant：单条失败标 FAILED，继续下一条。
风控停止：某域名连续 2 次风控 → 该域名剩余链接全部标 FAILED。

支持 28 个商城（含 tima365）。
需 cookies 的商城：zkh / stbchina / 66123123 / tima365 / comix。
cookies 文件放在 multi_mall_crawler/cookies/<域名>.json。
```

### 步骤 7：招商局自动建任务爬京东+震坤行

```
仅当 --zhaoshang-task 启用时执行。

对京东.xlsx 和 震坤行.xlsx 各调一次：
  from agent.tools.crawl_task_tool import run_crawl_task
  run_crawl_task(
    task_name="中核价格更新-YYYYMMDD-京东",
    mall_key="jd",  # 或 "zkh"
    excel_path="爬取模板/<日期>/京东.xlsx",
    skip_if_exists=True,
  )

任务名格式：中核价格更新-YYYYMMDD-京东 / 中核价格更新-YYYYMMDD-震坤行

招商局工具内部流程：
  读取 Excel → 校验链接域名 → 新建任务 → 查 taskId → 启用任务
  → 轮询进度 → 导出结果 xlsx → 返回文件路径

token 来源：auto_crawl_task.py 内置 _DEFAULT_BLADE_AUTH / _DEFAULT_COOKIE
也可用环境变量 BLADE_AUTH / COOKIE 覆盖。

结果 xlsx 列名：
  商品编码 | 商品采集id | ... | 商品价格 | ... | 商品详情地址 | ...
  URL 列 = "商品详情地址"（第 13 列）
  价格列 = "商品价格"（第 6 列）

价格=0 也算有效数据，标 page_price_status="价格为0"。
```

### 步骤 8：回填所有价格 + 重新计算

```
multi_mall_crawler 结果 → 按 URL 匹配回填 page_price_value
招商局结果 → 按 URL 匹配回填 page_price_value（覆盖京东/震坤行的 SKIPPED_JD）

对每行计算：
  1. saas_price_vs_customer（爬虫前预检）
     SaaS 自身价 > 客户价 x 1.3 → SAAS_HIGHER_THAN_CUSTOMER_30

  2. page_vs_customer_check（爬虫后检查）
     爬取价 > 客户价 x 1.3 → HIGHER_THAN_CUSTOMER_PRICE_30
     over_30pct = TRUE

  3. price_changed（价格变动检查）
     |爬取价 - SaaS价| / SaaS价 > 1% → CHANGED
     SaaS 链接价格与页面实际价格不一致 → 要驳回

  4. final_decision（综合决策）
     PASS                    全部通过
     REJECT_SAAS_HIGH        SaaS 价或爬取价超客户价 30%
     REJECT_PRICE_CHANGED    价格变动
     REJECT_BOTH             两种异常同时存在
```

### 步骤 9：输出 Excel

```
runtime/saas_full_audit_new.xlsx
  Sheet 1: summary
  Sheet 2: record_results（每行一条，中文表头，47 列）

关键列：
  客户标题 | 互联网标题 | 互联网链接 | SaaS价格 | 客户价 | 客户价状态
  标题匹配结果 | 标题匹配置信度
  爬取页面价格 | 页面价格状态 | 页面价格来源
  SaaS价对比客户价 | 价格对比客户价 | 超过客户价30% | 价格变动 | 最终决策
  建议操作 | 原因 | 证据

高亮规则：
  over_30pct=是 → 整行淡红 (FFC7CE)
  关键列 → 淡黄 (FFF2CC)

冻结首行 + 自动筛选 + 适当列宽。
```


---

## 四、流程步骤详解

### 步骤 1：加载模型环境变量

```
读取 data/model.env → 注入 os.environ
  TITLE_MATCH_MODEL_API_KEY
  TITLE_MATCH_MODEL_BASE_URL
  TITLE_MATCH_MODEL_NAME
不打印 KEY 值（只显示 SET / NOT_SET）
```

### 步骤 2：解析输入 JSON

```
saas_json → 多段 JSON 解析 → 合并所有 rows → 每行提取：
  vCommodityId（主键）
  queryStr（客户标题）
  skuName（互联网标题）
  extLink（互联网链接）
  price（SaaS 价格）
  skuId

customer_json → 多段 JSON 解析 → 合并所有 rows → 建 documentId 索引：
  documentId → {fourCategoryId, fourCategoryName, list_reference_price}
```

### 步骤 3：生成京东/震坤行爬取模板

```
扫描 saas_json 所有行：
  京东链接（item.jd.com）→ 爬取模板/<日期>/京东.xlsx
  震坤行链接（zkh.com）→ 爬取模板/<日期>/震坤行.xlsx

每个 xlsx 表头：商品编码 | url
商品编码 = saas.skuId
url = saas.extLink
按 skuId+url 去重

这两个文件不参与 multi_mall_crawler，交给招商局任务系统处理。
```

### 步骤 4：调中核 detail 接口拿客户最高价

```
对每个 saas 行：
  用 vCommodityId 在 customer_index 里找 fourCategoryId
  找不到 → customerPriceStatus = LOOKUP_FAILED（跳过 detail）
  找到 → POST detail 接口（documentId + fourCategoryId）

detail 返回提取规则（四级优先级）：
  1. data.referenceInfos[].referencePrice → 取 max
  2. data.referencePrice
  3. rows[].referenceInfos[].referencePrice → 取 max
  4. rows[].referencePrice

多价（>=2）→ FOUND
单价 → FOUND_SINGLE_REFERENCE
请求失败 → DETAIL_REQUEST_FAILED

按 (documentId, fourCategoryId) 缓存，同一 documentId 只请求一次。
每条间隔 0.5 秒（--request-interval）。
```

### 步骤 5：调模型校验标题

```
对每个 saas 行调 audit_saas_approval_records(use_model=True)：
  queryStr（客户标题） vs skuName（互联网标题）
  模型返回：SAME / NOT_SAME / MANUAL_REVIEW / MODEL_UNAVAILABLE
  confidence: 0-1

注意：不管模型结果是什么，所有链接都要爬取价格。
模型 NOT_SAME 不再跳过爬虫。
```

### 步骤 6：multi_mall_crawler 爬取非京东/非震坤行链接

```
收集所有 PENDING_CRAWL 的 URL（去重）：
  京东链接 → SKIPPED_JD（不爬）
  震坤行链接 → SKIPPED_JD（不爬，交给招商局）
  无链接 → SKIPPED_NO_LINK
  其他 → PENDING_CRAWL

子进程调用 multi_mall_crawler/main.py：
  python main.py --input <url列表.xlsx> --output <结果.xlsx> --headless --concurrency 3

先 dry-run 验证链接识别，再正式爬取。
fail-tolerant：单条失败标 FAILED，继续下一条。
风控停止：某域名连续 2 次风控 → 该域名剩余链接全部标 FAILED。

支持 28 个商城（含 tima365）。
需 cookies 的商城：zkh / stbchina / 66123123 / tima365 / comix。
cookies 文件放在 multi_mall_crawler/cookies/<域名>.json。
```

### 步骤 7：招商局自动建任务爬京东+震坤行

```
仅当 --zhaoshang-task 启用时执行。

对京东.xlsx 和 震坤行.xlsx 各调一次：
  from agent.tools.crawl_task_tool import run_crawl_task
  run_crawl_task(
    task_name="中核价格更新-YYYYMMDD-京东",
    mall_key="jd",  # 或 "zkh"
    excel_path="爬取模板/<日期>/京东.xlsx",
    skip_if_exists=True,
  )

任务名格式：中核价格更新-YYYYMMDD-京东 / 中核价格更新-YYYYMMDD-震坤行

招商局工具内部流程：
  读取 Excel → 校验链接域名 → 新建任务 → 查 taskId → 启用任务
  → 轮询进度 → 导出结果 xlsx → 返回文件路径

token 来源：auto_crawl_task.py 内置 _DEFAULT_BLADE_AUTH / _DEFAULT_COOKIE
也可用环境变量 BLADE_AUTH / COOKIE 覆盖。

结果 xlsx 列名：
  商品编码 | 商品采集id | ... | 商品价格 | ... | 商品详情地址 | ...
  URL 列 = "商品详情地址"（第 13 列）
  价格列 = "商品价格"（第 6 列）

价格=0 也算有效数据，标 page_price_status="价格为0"。
```

### 步骤 8：回填所有价格 + 重新计算

```
multi_mall_crawler 结果 → 按 URL 匹配回填 page_price_value
招商局结果 → 按 URL 匹配回填 page_price_value（覆盖京东/震坤行的 SKIPPED_JD）

对每行计算：
  1. saas_price_vs_customer（爬虫前预检）
     SaaS 自身价 > 客户价 * 1.3 → SAAS_HIGHER_THAN_CUSTOMER_30

  2. page_vs_customer_check（爬虫后检查）
     爬取价 > 客户价 * 1.3 → HIGHER_THAN_CUSTOMER_PRICE_30
     over_30pct = TRUE

  3. price_changed（价格变动检查）
     |爬取价 - SaaS价| / SaaS价 > 1% → CHANGED
     SaaS 链接价格与页面实际价格不一致 → 要驳回

  4. final_decision（综合决策）
     PASS                    全部通过
     REJECT_SAAS_HIGH        SaaS 价或爬取价超客户价 30%
     REJECT_PRICE_CHANGED    价格变动
     REJECT_BOTH             两种异常同时存在
```

### 步骤 9：输出 Excel

```
runtime/saas_full_audit_new.xlsx
  Sheet 1: summary（总记录数/建议通过/人工核查/建议驳回/客户价FOUND/客户价缺失/超过30%数量/输出时间）
  Sheet 2: record_results（每行一条，中文表头，44+ 列）

关键列：
  客户标题 | 互联网标题 | 互联网链接 | SaaS价格 | 客户价 | 客户价状态
  标题匹配结果 | 标题匹配置信度
  爬取页面价格 | 页面价格状态 | 页面价格来源
  SaaS价对比客户价 | 价格对比客户价 | 超过客户价30% | 价格变动 | 最终决策
  建议操作 | 原因 | 证据

高亮规则：
  over_30pct=是 → 整行淡红 (FFC7CE)
  关键列 → 淡黄 (FFF2CC)

冻结首行 + 自动筛选 + 适当列宽。
```

---

## 四、流程步骤详解

### 步骤 1：加载模型环境变量

```
读取 data/model.env → 注入 os.environ
  TITLE_MATCH_MODEL_API_KEY
  TITLE_MATCH_MODEL_BASE_URL
  TITLE_MATCH_MODEL_NAME
不打印 KEY 值（只显示 SET / NOT_SET）
```

### 步骤 2：解析输入 JSON

```
saas_json → 多段 JSON 解析 → 合并所有 rows → 每行提取：
  vCommodityId（主键）
  queryStr（客户标题）
  skuName（互联网标题）
  extLink（互联网链接）
  price（SaaS 价格）
  skuId

customer_json → 多段 JSON 解析 → 合并所有 rows → 建 documentId 索引：
  documentId → {fourCategoryId, fourCategoryName, list_reference_price}
```

### 步骤 3：生成京东/震坤行爬取模板

```
扫描 saas_json 所有行：
  京东链接（item.jd.com）→ 爬取模板/<日期>/京东.xlsx
  震坤行链接（zkh.com）→ 爬取模板/<日期>/震坤行.xlsx

每个 xlsx 表头：商品编码 | url
商品编码 = saas.skuId
url = saas.extLink
按 skuId+url 去重

这两个文件不参与 multi_mall_crawler，交给招商局任务系统处理。
```

### 步骤 4：调中核 detail 接口拿客户最高价

```
对每个 saas 行：
  用 vCommodityId 在 customer_index 里找 fourCategoryId
  找不到 → customerPriceStatus = LOOKUP_FAILED（跳过 detail）
  找到 → POST detail 接口（documentId + fourCategoryId）

detail 返回提取规则（四级优先级）：
  1. data.referenceInfos[].referencePrice → 取 max
  2. data.referencePrice
  3. rows[].referenceInfos[].referencePrice → 取 max
  4. rows[].referencePrice

多价（≥2）→ FOUND
单价 → FOUND_SINGLE_REFERENCE
请求失败 → DETAIL_REQUEST_FAILED

按 (documentId, fourCategoryId) 缓存，同一 documentId 只请求一次。
每条间隔 0.5 秒（--request-interval）。
```

### 步骤 5：调模型校验标题

```
对每个 saas 行调 audit_saas_approval_records(use_model=True)：
  queryStr（客户标题） vs skuName（互联网标题）
  模型返回：SAME / NOT_SAME / MANUAL_REVIEW / MODEL_UNAVAILABLE
  confidence: 0-1

注意：不管模型结果是什么，所有链接都要爬取价格。
模型 NOT_SAME 不再跳过爬虫。
```

### 步骤 6：multi_mall_crawler 爬取非京东/非震坤行链接

```
收集所有 PENDING_CRAWL 的 URL（去重）：
  京东链接 → SKIPPED_JD（不爬）
  震坤行链接 → SKIPPED_JD（不爬，交给招商局）
  无链接 → SKIPPED_NO_LINK
  其他 → PENDING_CRAWL

子进程调用 multi_mall_crawler/main.py：
  python main.py --input <url列表.xlsx> --output <结果.xlsx> --headless --concurrency 3

先 dry-run 验证链接识别，再正式爬取。
fail-tolerant：单条失败标 FAILED，继续下一条。
风控停止：某域名连续 2 次风控 → 该域名剩余链接全部标 FAILED。

支持 28 个商城（含 tima365）。
需 cookies 的商城：zkh / stbchina / 66123123 / tima365 / comix。
cookies 文件放在 multi_mall_crawler/cookies/<域名>.json。
```

### 步骤 7：招商局自动建任务爬京东+震坤行

```
仅当 --zhaoshang-task 启用时执行。

对京东.xlsx 和 震坤行.xlsx 各调一次：
  from agent.tools.crawl_task_tool import run_crawl_task
  run_crawl_task(
    task_name="中核价格更新-YYYYMMDD-京东",
    mall_key="jd",  # 或 "zkh"
    excel_path="爬取模板/<日期>/京东.xlsx",
    skip_if_exists=True,
  )

任务名格式：中核价格更新-YYYYMMDD-京东 / 中核价格更新-YYYYMMDD-震坤行

招商局工具内部流程：
  读取 Excel → 校验链接域名 → 新建任务 → 查 taskId → 启用任务
  → 轮询进度 → 导出结果 xlsx → 返回文件路径

token 来源：auto_crawl_task.py 内置 _DEFAULT_BLADE_AUTH / _DEFAULT_COOKIE
也可用环境变量 BLADE_AUTH / COOKIE 覆盖。

结果 xlsx 列名：
  商品编码 | 商品采集id | ... | 商品价格 | ... | 商品详情地址 | ...
  URL 列 = "商品详情地址"（第 13 列）
  价格列 = "商品价格"（第 6 列）

价格=0 也算有效数据，标 page_price_status="价格为0"。
```

### 步骤 8：回填所有价格 + 重新计算

```
multi_mall_crawler 结果 → 按 URL 匹配回填 page_price_value
招商局结果 → 按 URL 匹配回填 page_price_value（覆盖京东/震坤行的 SKIPPED_JD）

对每行计算：
  1. saas_price_vs_customer（爬虫前预检）
     SaaS 自身价 > 客户价 × 1.3 → SAAS_HIGHER_THAN_CUSTOMER_30

  2. page_vs_customer_check（爬虫后检查）
     爬取价 > 客户价 × 1.3 → HIGHER_THAN_CUSTOMER_PRICE_30
     over_30pct = TRUE

  3. price_changed（价格变动检查）
     |爬取价 - SaaS价| / SaaS价 > 1% → CHANGED
     SaaS 链接价格与页面实际价格不一致 → 要驳回

  4. final_decision（综合决策）
     PASS                    全部通过
     REJECT_SAAS_HIGH        SaaS 价或爬取价超客户价 30%
     REJECT_PRICE_CHANGED    价格变动
     REJECT_BOTH             两种异常同时存在
```

### 步骤 9：输出 Excel

```
runtime/saas_full_audit_new.xlsx
  Sheet 1: summary
  Sheet 2: record_results（每行一条，中文表头，44+ 列）

关键列：
  客户标题 | 互联网标题 | 互联网链接 | SaaS价格 | 客户价 | 客户价状态
  标题匹配结果 | 标题匹配置信度
  爬取页面价格 | 页面价格状态 | 页面价格来源
  SaaS价对比客户价 | 价格对比客户价 | 超过客户价30% | 价格变动 | 最终决策
  建议操作 | 原因 | 证据

高亮规则：
  over_30pct=是 → 整行淡红 (FFC7CE)
  关键列 → 淡黄 (FFF2CC)

冻结首行 + 自动筛选 + 适当列宽。
```
## 四、流程步骤详解

### 步骤 1：加载模型环境变量

读取 data/model.env 注入 os.environ（3 个变量），不打印 KEY 值。

### 步骤 2：解析输入 JSON

- saas_json 多段解析合并 rows，提取 vCommodityId/queryStr/skuName/extLink/price/skuId
- customer_json 多段解析合并 rows，建 documentId 索引

### 步骤 3：生成京东/震坤行爬取模板

扫描 saas_json 所有行，京东链接(item.jd.com)和震坤行链接(zkh.com)分别导出到：
  爬取模板/<日期>/京东.xlsx
  爬取模板/<日期>/震坤行.xlsx

表头：商品编码 | url，按 skuId+url 去重。这两个文件交给招商局任务系统处理。

### 步骤 4：调中核 detail 接口拿客户最高价

用 vCommodityId 匹配 customer.documentId 拿 fourCategoryId，POST detail 接口。
按 (documentId, fourCategoryId) 缓存，同一 documentId 只请求一次。
提取规则：data.referenceInfos[].referencePrice 取 max → FOUND；单价 → FOUND_SINGLE_REFERENCE。

### 步骤 5：调模型校验标题

queryStr（客户标题）vs skuName（互联网标题），模型返回 SAME/NOT_SAME/MANUAL_REVIEW。
注意：不管模型结果是什么，所有链接都要爬取价格。NOT_SAME 不再跳过爬虫。

### 步骤 6：multi_mall_crawler 爬取非京东/非震坤行链接

收集所有 PENDING_CRAWL 的 URL（去重），子进程调用 multi_mall_crawler/main.py。
先 dry-run 验证链接识别，再正式爬取。fail-tolerant，单条失败标 FAILED 继续。
支持 28 个商城，需 cookies 的商城文件放在 multi_mall_crawler/cookies/<域名>.json。

### 步骤 7：招商局自动建任务爬京东+震坤行

仅当 --zhaoshang-task 启用时执行。
对京东.xlsx 和震坤行.xlsx 各调一次 run_crawl_task：
  task_name = "中核价格更新-YYYYMMDD-京东" / "中核价格更新-YYYYMMDD-震坤行"
  skip_if_exists=True

招商局工具内部：读Excel→建任务→查taskId→启用→轮询进度→导出结果xlsx。
token 来源：auto_crawl_task.py 内置 _DEFAULT_BLADE_AUTH / _DEFAULT_COOKIE。

结果 xlsx 关键列：
  URL 列 = "商品详情地址"（第 13 列）
  价格列 = "商品价格"（第 6 列）
  价格=0 也算有效数据，标 page_price_status="价格为0"。

### 步骤 8：回填所有价格 + 重新计算

multi_mall_crawler 结果按 URL 匹配回填 page_price_value。
招商局结果按 URL 匹配回填 page_price_value（覆盖京东/震坤行的 SKIPPED_JD）。

对每行计算：
  1. saas_price_vs_customer（爬虫前预检）：SaaS 自身价 > 客户价*1.3 → SAAS_HIGHER_THAN_CUSTOMER_30
  2. page_vs_customer_check（爬虫后）：爬取价 > 客户价*1.3 → over_30pct=TRUE
  3. price_changed（价格变动）：|爬取价-SaaS价|/SaaS价 > 1% → CHANGED（要驳回）
  4. final_decision（综合决策）：
     PASS / REJECT_SAAS_HIGH / REJECT_PRICE_CHANGED / REJECT_BOTH

### 步骤 9：输出 Excel

runtime/saas_full_audit_new.xlsx
  Sheet 1: summary（总记录数/建议通过/人工核查/建议驳回/客户价FOUND/超过30%数量/输出时间）
  Sheet 2: record_results（每行一条，中文表头，44+ 列）

高亮：over_30pct=是 整行淡红 (FFC7CE)；关键列淡黄 (FFF2CC)。
冻结首行 + 自动筛选 + 适当列宽。

---

## 五、输出产物清单

| 文件 | 路径 | 说明 |
|------|------|------|
| 主审计 Excel | runtime/saas_full_audit_new.xlsx | 最终结果 |
| 爬虫输入 | runtime/_crawl_input.xlsx | URL 列表 |
| 爬虫输出 | runtime/_crawl_output.xlsx | 爬虫原始返回 |
| 招商局京东结果 | runtime/zhaoshang_jd_result.xlsx | 京东价格 |
| 招商局震坤行结果 | runtime/zhaoshang_zkh_result.xlsx | 震坤行价格 |
| 京东爬取模板 | 爬取模板/<日期>/京东.xlsx | 商品编码+url |
| 震坤行爬取模板 | 爬取模板/<日期>/震坤行.xlsx | 商品编码+url |

---

## 六、安全约束（硬性红线）

不绕过验证码、不自动登录、不注入 stealth、不轮换 UA、不用代理池。
不调用审批/驳回接口、不写数据库、不执行 SQL。
不持久化 cookie/token/auth-token/access-token/y1AmuIse。
不打印 cookie/token/auth-token/access-token/y1AmuIse。
不把 detail_request.curl / model.env 提交到 git。

.gitignore 已排除：data/*.curl、data/*token*、data/*.env、data/*.json、runtime/、*.xlsx。

---

## 七、常见问题

### detail 接口全部 DETAIL_REQUEST_FAILED
cURL 登录态过期，重新从中核页面抓 detail 接口 cURL 覆盖 data/detail_request.curl。

### 模型全部 MODEL_UNAVAILABLE
检查 data/model.env 是否存在且 3 个变量都填了。

### zkh 全部 FAILED（触发验证码）
阿里云 WAF 识别了 headless。等 30 分钟重试或人工解验证码。不要绕过。

### JSON 解析失败
脚本自动修复 "success": true\n{ 和 }\n{"rspCode" 两种缺右花括号模式。其他格式看报错 char 位置手动检查。

### 招商局任务返回 result_path=None
可能 skip_if_exists 跳过。用 tools/_export_zhaoshang.py 单独导出已存在任务。

### Excel 保存 PermissionError
文件被 Excel/WPS 打开了，关闭后重跑。

### 价格=0 怎么处理
价格=0 是有效数据。page_price_status="价格为0"，over_30pct=""（0不超30%），price_changed=CHANGED（0不等于SaaS价），final_decision=REJECT_PRICE_CHANGED。

---

## 八、关键文件位置

```
cnnc-ops-agent/
  scripts/audit_saas_excel_file.py    主脚本（全部逻辑）
  scripts/export_excluded_malls.py    京东/震坤行模板导出
  tests/conftest.py                   pytest 全局 fixture
  tests/test_work_01_saas_approval_json_audit.py  全部测试
  data/saas_json.json                 SaaS 审批导出（输入）
  data/customer_json.json             客户后台导出（输入）
  data/detail_request.curl            中核 detail cURL（输入）
  data/model.env                      模型 API 配置（输入）
  runtime/saas_full_audit_new.xlsx    最终输出 Excel
  爬取模板/<日期>/京东.xlsx
  爬取模板/<日期>/震坤行.xlsx
  app/services/title_match_model_adapter.py  模型调用
  app/workflows/work_01_saas_approval_json_audit.py  审查规则

multi_mall_crawler/
  main.py                             爬虫入口
  config/mall_domains.yaml            28 个商城配置
  crawlers/base.py                    BaseCrawler 基类
  crawlers/tima.py                    天马（三层采集 API/DOM/store）
  crawlers/comix.py                   齐心（OCR 价格图）
  cookies/<域名>.json                 各商城 cookies
  AGENT_GUIDE.md                      爬虫使用指南

招商局自动建任务/
  agent/tools/crawl_task_tool.py      wrapper
  external_tools/crawl_task_tool/auto_crawl_task.py  核心脚本
```

---

## 九、给另一个 AI 的操作指引

1. 确认输入文件就位（saas_json.json / customer_json.json / detail_request.curl / model.env）
2. 确认 runtime/saas_full_audit_new.xlsx 没被打开
3. 跑命令（见第三节）
4. 跑完后检查 Excel：
   - summary Sheet 看总记录数/客户价FOUND/超过30%数量
   - record_results Sheet 看 final_decision 列，REJECT_* 的行需要驳回
5. 如果招商局任务失败，用 tools/_export_zhaoshang.py 单独导出
6. 如果 Excel 保存失败，关闭文件后重跑


---

## 四、流程步骤详解

### 步骤 1：加载模型环境变量

```
读取 data/model.env → 注入 os.environ
  TITLE_MATCH_MODEL_API_KEY
  TITLE_MATCH_MODEL_BASE_URL
  TITLE_MATCH_MODEL_NAME
不打印 KEY 值（只显示 SET / NOT_SET）
```

### 步骤 2：解析输入 JSON

```
saas_json → 多段 JSON 解析 → 合并所有 rows → 每行提取：
  vCommodityId（主键）
  queryStr（客户标题）
  skuName（互联网标题）
  extLink（互联网链接）
  price（SaaS 价格）
  skuId

customer_json → 多段 JSON 解析 → 合并所有 rows → 建 documentId 索引：
  documentId → {fourCategoryId, fourCategoryName, list_reference_price}
```

### 步骤 3：生成京东/震坤行爬取模板

```
扫描 saas_json 所有行：
  京东链接（item.jd.com）→ 爬取模板/<日期>/京东.xlsx
  震坤行链接（zkh.com）→ 爬取模板/<日期>/震坤行.xlsx

每个 xlsx 表头：商品编码 | url
商品编码 = saas.skuId
url = saas.extLink
按 skuId+url 去重

这两个文件不参与 multi_mall_crawler，交给招商局任务系统处理。
```

### 步骤 4：调中核 detail 接口拿客户最高价

```
对每个 saas 行：
  用 vCommodityId 在 customer_index 里找 fourCategoryId
  找不到 → customerPriceStatus = LOOKUP_FAILED（跳过 detail）
  找到 → POST detail 接口（documentId + fourCategoryId）

detail 返回提取规则（四级优先级）：
  1. data.referenceInfos[].referencePrice → 取 max
  2. data.referencePrice
  3. rows[].referenceInfos[].referencePrice → 取 max
  4. rows[].referencePrice

多价（>=2）→ FOUND
单价 → FOUND_SINGLE_REFERENCE
请求失败 → DETAIL_REQUEST_FAILED

按 (documentId, fourCategoryId) 缓存，同一 documentId 只请求一次。
每条间隔 0.5 秒（--request-interval）。
```

### 步骤 5：调模型校验标题

```
对每个 saas 行调 audit_saas_approval_records(use_model=True)：
  queryStr（客户标题） vs skuName（互联网标题）
  模型返回：SAME / NOT_SAME / MANUAL_REVIEW / MODEL_UNAVAILABLE
  confidence: 0-1

注意：不管模型结果是什么，所有链接都要爬取价格。
模型 NOT_SAME 不再跳过爬虫。
```

### 步骤 6：multi_mall_crawler 爬取非京东/非震坤行链接

```
收集所有 PENDING_CRAWL 的 URL（去重）：
  京东链接 → SKIPPED_JD（不爬）
  震坤行链接 → SKIPPED_JD（不爬，交给招商局）
  无链接 → SKIPPED_NO_LINK
  其他 → PENDING_CRAWL

子进程调用 multi_mall_crawler/main.py：
  python main.py --input <url列表.xlsx> --output <结果.xlsx> --headless --concurrency 3

先 dry-run 验证链接识别，再正式爬取。
fail-tolerant：单条失败标 FAILED，继续下一条。
风控停止：某域名连续 2 次风控 → 该域名剩余链接全部标 FAILED。

支持 28 个商城（含 tima365）。
需 cookies 的商城：zkh / stbchina / 66123123 / tima365 / comix。
cookies 文件放在 multi_mall_crawler/cookies/<域名>.json。
```

### 步骤 7：招商局自动建任务爬京东+震坤行

```
仅当 --zhaoshang-task 启用时执行。

对京东.xlsx 和 震坤行.xlsx 各调一次：
  from agent.tools.crawl_task_tool import run_crawl_task
  run_crawl_task(
    task_name="中核价格更新-YYYYMMDD-京东",
    mall_key="jd",  # 或 "zkh"
    excel_path="爬取模板/<日期>/京东.xlsx",
    skip_if_exists=True,
  )

任务名格式：中核价格更新-YYYYMMDD-京东 / 中核价格更新-YYYYMMDD-震坤行

招商局工具内部流程：
  读取 Excel → 校验链接域名 → 新建任务 → 查 taskId → 启用任务
  → 轮询进度 → 导出结果 xlsx → 返回文件路径

token 来源：auto_crawl_task.py 内置 _DEFAULT_BLADE_AUTH / _DEFAULT_COOKIE
也可用环境变量 BLADE_AUTH / COOKIE 覆盖。

结果 xlsx 列名：
  商品编码 | 商品采集id | ... | 商品价格 | ... | 商品详情地址 | ...
  URL 列 = "商品详情地址"（第 13 列）
  价格列 = "商品价格"（第 6 列）

价格=0 也算有效数据，标 page_price_status="价格为0"。
```

### 步骤 8：回填所有价格 + 重新计算

```
multi_mall_crawler 结果 → 按 URL 匹配回填 page_price_value
招商局结果 → 按 URL 匹配回填 page_price_value（覆盖京东/震坤行的 SKIPPED_JD）

对每行计算：
  1. saas_price_vs_customer（爬虫前预检）
     SaaS 自身价 > 客户价 × 1.3 → SAAS_HIGHER_THAN_CUSTOMER_30

  2. page_vs_customer_check（爬虫后检查）
     爬取价 > 客户价 × 1.3 → HIGHER_THAN_CUSTOMER_PRICE_30
     over_30pct = TRUE

  3. price_changed（价格变动检查）
     |爬取价 - SaaS价| / SaaS价 > 1% → CHANGED
     SaaS 链接价格与页面实际价格不一致 → 要驳回

  4. final_decision（综合决策）
     PASS                    全部通过
     REJECT_SAAS_HIGH        SaaS 价或爬取价超客户价 30%
     REJECT_PRICE_CHANGED    价格变动
     REJECT_BOTH             两种异常同时存在
```

### 步骤 9：输出 Excel

```
runtime/saas_full_audit_new.xlsx
  Sheet 1: summary（总记录数/建议通过/人工核查/建议驳回/客户价FOUND/客户价缺失/超过30%数量/输出时间）
  Sheet 2: record_results（每行一条，中文表头，47 列）

关键列：
  客户标题 | 互联网标题 | 互联网链接 | SaaS价格 | 客户价 | 客户价状态
  标题匹配结果 | 标题匹配置信度
  爬取页面价格 | 页面价格状态 | 页面价格来源
  SaaS价对比客户价 | 价格对比客户价 | 超过客户价30% | 价格变动 | 最终决策
  建议操作 | 原因 | 证据

高亮规则：
  over_30pct=是 → 整行淡红 (FFC7CE)
  关键列 → 淡黄 (FFF2CC)

冻结首行 + 自动筛选 + 适当列宽。
```


## 四、流程步骤详解

### 步骤 1：加载模型环境变量

```
读取 data/model.env → 注入 os.environ
不打印 KEY 值（只显示 SET / NOT_SET）
```

### 步骤 2：解析输入 JSON

```
saas_json → 多段 JSON 解析 → 合并所有 rows → 每行提取：
  vCommodityId（主键）、queryStr（客户标题）、skuName（互联网标题）
  extLink（互联网链接）、price（SaaS 价格）、skuId

customer_json → 多段 JSON 解析 → 合并所有 rows → 建 documentId 索引：
  documentId → {fourCategoryId, fourCategoryName, list_reference_price}
```

### 步骤 3：生成京东/震坤行爬取模板

```
扫描 saas_json 所有行：
  京东链接（item.jd.com）→ 爬取模板/<日期>/京东.xlsx
  震坤行链接（zkh.com）→ 爬取模板/<日期>/震坤行.xlsx

每个 xlsx 表头：商品编码 | url
商品编码 = saas.skuId，url = saas.extLink
按 skuId+url 去重。

这两个文件不参与 multi_mall_crawler，交给招商局任务系统处理。
```

### 步骤 4：调中核 detail 接口拿客户最高价

```
对每个 saas 行：
  用 vCommodityId 在 customer_index 里找 fourCategoryId
  找不到 → customerPriceStatus = LOOKUP_FAILED（跳过 detail）
  找到 → POST detail 接口（documentId + fourCategoryId）

detail 返回提取规则（四级优先级）：
  1. data.referenceInfos[].referencePrice → 取 max
  2. data.referencePrice
  3. rows[].referenceInfos[].referencePrice → 取 max
  4. rows[].referencePrice

多价（≥2）→ FOUND
单价 → FOUND_SINGLE_REFERENCE
请求失败 → DETAIL_REQUEST_FAILED

按 (documentId, fourCategoryId) 缓存，同一 documentId 只请求一次。
每条间隔 0.5 秒。
```

### 步骤 5：调模型校验标题

```
对每个 saas 行调 audit_saas_approval_records(use_model=True)：
  queryStr（客户标题） vs skuName（互联网标题）
  模型返回：SAME / NOT_SAME / MANUAL_REVIEW / MODEL_UNAVAILABLE
  confidence: 0-1

注意：不管模型结果是什么，所有链接都要爬取价格。
模型 NOT_SAME 不再跳过爬虫。
```

### 步骤 6：multi_mall_crawler 爬取非京东/非震坤行链接

```
收集所有 PENDING_CRAWL 的 URL（去重）：
  京东链接 → SKIPPED_JD（不爬，交给招商局）
  震坤行链接 → SKIPPED_JD（不爬，交给招商局）
  无链接 → SKIPPED_NO_LINK
  其他 → PENDING_CRAWL

子进程调用 multi_mall_crawler/main.py：
  python main.py --input <url列表.xlsx> --output <结果.xlsx> --headless --concurrency 3

先 dry-run 验证链接识别，再正式爬取。
fail-tolerant：单条失败标 FAILED，继续。
风控停止：某域名连续 2 次风控 → 该域名剩余链接全部标 FAILED。

支持 28 个商城（含 tima365）。
需 cookies 的商城：zkh / stbchina / 66123123 / tima365 / comix。
cookies 文件放在 multi_mall_crawler/cookies/<域名>.json。
```

### 步骤 7：招商局自动建任务爬京东+震坤行

```
仅当 --zhaoshang-task 启用时执行。

对京东.xlsx 和 震坤行.xlsx 各调一次：
  from agent.tools.crawl_task_tool import run_crawl_task
  run_crawl_task(
    task_name="中核价格更新-YYYYMMDD-京东",
    mall_key="jd" 或 "zkh",
    excel_path="爬取模板/<日期>/京东.xlsx",
    skip_if_exists=True,
  )

招商局工具内部流程：
  读取 Excel → 校验链接域名 → 新建任务 → 查 taskId → 启用任务
  → 轮询进度 → 导出结果 xlsx → 返回文件路径

token 来源：auto_crawl_task.py 内置 _DEFAULT_BLADE_AUTH / _DEFAULT_COOKIE
也可用环境变量 BLADE_AUTH / COOKIE 覆盖。

结果 xlsx 列名：
  URL 列 = "商品详情地址"（第 13 列）
  价格列 = "商品价格"（第 6 列）

价格=0 也算有效数据，标 page_price_status="价格为0"。
```

### 步骤 8：回填所有价格 + 重新计算

```
multi_mall_crawler 结果 → 按 URL 匹配回填 page_price_value
招商局结果 → 按 URL 匹配回填 page_price_value（覆盖京东/震坤行的 SKIPPED_JD）

对每行计算：
  1. saas_price_vs_customer（爬虫前预检）
     SaaS 自身价 > 客户价 × 1.3 → SAAS_HIGHER_THAN_CUSTOMER_30

  2. page_vs_customer_check（爬虫后检查）
     爬取价 > 客户价 × 1.3 → HIGHER_THAN_CUSTOMER_PRICE_30
     over_30pct = TRUE

  3. price_changed（价格变动检查）
     |爬取价 - SaaS价| / SaaS价 > 1% → CHANGED
     SaaS 链接价格与页面实际价格不一致 → 要驳回

  4. final_decision（综合决策）
     PASS                    全部通过
     REJECT_SAAS_HIGH        SaaS 价或爬取价超客户价 30%
     REJECT_PRICE_CHANGED    价格变动
     REJECT_BOTH             两种异常同时存在
```

### 步骤 9：输出 Excel

```
runtime/saas_full_audit_new.xlsx
  Sheet 1: summary
  Sheet 2: record_results（每行一条，中文表头，44+ 列）

关键列：
  客户标题 | 互联网标题 | 互联网链接 | SaaS价格 | 客户价 | 客户价状态
  标题匹配结果 | 标题匹配置信度
  爬取页面价格 | 页面价格状态 | 页面价格来源
  SaaS价对比客户价 | 价格对比客户价 | 超过客户价30% | 价格变动 | 最终决策
  建议操作 | 原因 | 证据

高亮：over_30pct=是 → 整行淡红 (FFC7CE)
冻结首行 + 自动筛选 + 适当列宽。
```


## 五、输出产物清单

| 文件 | 路径 | 说明 |
|------|------|------|
| 主审计 Excel | `runtime/saas_full_audit_new.xlsx` | 最终结果，中文表头 |
| 爬虫输入 | `runtime/_crawl_input.xlsx` | 喂给 multi_mall_crawler 的 URL 列表 |
| 爬虫输出 | `runtime/_crawl_output.xlsx` | multi_mall_crawler 原始返回 |
| 招商局京东结果 | `runtime/zhaoshang_jd_result.xlsx` | 招商局导出的京东价格 |
| 招商局震坤行结果 | `runtime/zhaoshang_zkh_result.xlsx` | 招商局导出的震坤行价格 |
| 京东爬取模板 | `爬取模板/<日期>/京东.xlsx` | 商品编码 + url |
| 震坤行爬取模板 | `爬取模板/<日期>/震坤行.xlsx` | 商品编码 + url |

---

## 六、安全约束（硬性红线）

```
不绕过验证码、不自动登录、不注入 stealth、不轮换 UA、不用代理池
不调用审批/驳回接口、不写数据库、不执行 SQL
不持久化 cookie/token/auth-token/access-token/y1AmuISe
不打印 cookie/token/auth-token/access-token/y1AmuISe
不把 detail_request.curl / model.env 提交到 git
```

`.gitignore` 已排除：`data/*.curl`、`data/*token*`、`data/*.env`、`data/*.json`、`runtime/`、`*.xlsx`

---

## 七、常见问题

### detail 接口全部 DETAIL_REQUEST_FAILED
cURL 登录态过期。重新从中核页面抓 detail 接口的 cURL，覆盖 `data/detail_request.curl`。

### 模型全部 MODEL_UNAVAILABLE
检查 `data/model.env` 是否存在且 3 个变量都填了。

### zkh 全部 FAILED（触发验证码）
阿里云 WAF 识别了 headless 浏览器。等 30 分钟后重试。不要绕过验证码。

### JSON 解析失败
脚本会自动修复 `"success": true\n{` 和 `}\n{` 两种缺右花括号模式。如果还有问题，看报错的 char 位置手动检查。

### 招商局任务返回 result_path=None
可能 skip_if_exists 跳过了。用 `tools/_export_zhaoshang.py` 单独导出已存在的任务。

### Excel 保存 PermissionError
`runtime/saas_full_audit_new.xlsx` 被打开了。关闭文件后重跑。

### 价格=0 怎么处理
价格=0 是有效数据。page_price_status="价格为0"，price_changed=CHANGED，final_decision=REJECT_PRICE_CHANGED。

---

## 八、给另一个 AI 的操作指引

1. 确认输入文件就位（saas_json.json / customer_json.json / detail_request.curl / model.env）
2. 确认 `runtime/saas_full_audit_new.xlsx` 没被打开
3. 跑命令（见第三节）
4. 跑完后检查 `runtime/saas_full_audit_new.xlsx` 的 summary sheet
5. 如果有 DETAIL_REQUEST_FAILED → cURL 过期，让用户重新抓
6. 如果有 MODEL_UNAVAILABLE → model.env 没配好
7. 不要修改 `data/` 下的任何文件
8. 不要打印 cookie/token/key

---

## 五、输出产物清单

| 文件 | 路径 | 说明 |
|------|------|------|
| 主审计 Excel | `runtime/saas_full_audit_new.xlsx` | 最终结果，中文表头 |
| 爬虫输入 | `runtime/_crawl_input.xlsx` | 喂给 multi_mall_crawler 的 URL 列表 |
| 爬虫输出 | `runtime/_crawl_output.xlsx` | multi_mall_crawler 原始返回 |
| 招商局京东结果 | `runtime/zhaoshang_jd_result.xlsx` | 招商局导出的京东价格 |
| 招商局震坤行结果 | `runtime/zhaoshang_zkh_result.xlsx` | 招商局导出的震坤行价格 |
| 京东爬取模板 | `爬取模板/<日期>/京东.xlsx` | 商品编码 + url |
| 震坤行爬取模板 | `爬取模板/<日期>/震坤行.xlsx` | 商品编码 + url |
| 模板汇总 | `爬取模板/<日期>/summary.txt` | 数量统计 |

---

## 六、安全约束（硬性红线）

```
不绕过验证码
不自动登录
不注入 stealth
不轮换 UA
不用代理池
不调用审批/驳回接口
不写数据库
不执行 SQL
不持久化 cookie/token/auth-token/access-token/y1AmuISe
不打印 cookie/token/auth-token/access-token/y1AmuISe
不把 detail_request.curl / model.env 提交到 git
```

`.gitignore` 已排除：`data/*.curl`、`data/*token*`、`data/*.env`、`data/*.json`、`runtime/`、`*.xlsx`

---

## 七、常见问题

### detail 接口全部 DETAIL_REQUEST_FAILED

cURL 登录态过期。重新从中核页面抓 detail 接口的 cURL，覆盖 `data/detail_request.curl`。

### 模型全部 MODEL_UNAVAILABLE

检查 `data/model.env` 是否存在且 3 个变量都填了。

### zkh 全部 FAILED（触发验证码）

zkh 的阿里云 WAF 识别了 headless 浏览器。等 30 分钟后重试，或人工访问 zkh 解一次验证码。不要绕过验证码。

### JSON 解析失败

检查 JSON 文件是否多段拼接缺右花括号。脚本会自动修复 `}"success": true\n{` 和 `}\n{` 两种模式。

### 招商局任务返回 result_path=None

可能任务已存在被跳过。用 `tools/_export_zhaoshang.py` 单独导出已存在的任务。

### Excel 保存 PermissionError

`runtime/saas_full_audit_new.xlsx` 被打开了。关闭文件后重跑。

### 价格=0 怎么处理

价格=0 是有效数据（商品可能已下架）。page_price_status="价格为0"，price_changed=CHANGED（0 不等于 SaaS 价），final_decision=REJECT_PRICE_CHANGED。

---

## 八、关键文件位置

```
cnnc-ops-agent/
├── scripts/
│   ├── audit_saas_excel_file.py    # 主脚本（全部逻辑）
│   └── export_excluded_malls.py    # 京东/震坤行模板导出
├── tests/
│   ├── conftest.py                 # pytest 全局 fixture
│   └── test_work_01_saas_approval_json_audit.py  # 全部测试
├── data/
│   ├── saas_json.json              # SaaS 审批导出（输入）
│   ├── customer_json.json          # 客户后台导出（输入）
│   ├── detail_request.curl         # 中核 detail cURL（输入）
│   └── model.env                   # 模型 API 配置（输入）
├── runtime/
│   └── saas_full_audit_new.xlsx    # 最终输出 Excel
├── 爬取模板/
│   └── <日期>/
│       ├── 京东.xlsx
│       ├── 震坤行.xlsx
│       └── summary.txt
├── app/
│   ├── schemas/workflow.py
│   ├── services/
│   │   ├── title_match_model_adapter.py  # 模型调用
│   │   └── link_audit_worker.py
│   └── workflows/
│       └── work_01_saas_approval_json_audit.py  # 审查规则
└── requirements.txt

multi_mall_crawler/
├── main.py                         # 爬虫入口
├── config/mall_domains.yaml        # 28 个商城配置
├── crawlers/
│   ├── base.py                     # BaseCrawler 基类
│   ├── tima.py                     # 天马（三层采集：API→DOM→store）
│   ├── comix.py                    # 齐心（OCR 价格图）
│   ├── zkh.py                      # 震坤行（阿里云 WAF）
│   └── ...
├── cookies/                        # 各商城 cookies
└── AGENT_GUIDE.md                  # 爬虫使用指南

招商局自动建任务/
├── agent/tools/crawl_task_tool.py  # wrapper
└── external_tools/crawl_task_tool/
    ├── auto_crawl_task.py          # 核心脚本
    └── .env.example
```

---

## 九、给另一个 AI 的操作指引

如果你是另一个 AI，接到"跑 SaaS 审计"的任务，按以下步骤：

1. **确认输入文件就位**
   - `data/saas_json.json` 存在且非空
   - `data/customer_json.json` 存在且非空
   - `data/detail_request.curl` 存在（检查登录态是否过期）
   - `data/model.env` 存在且 3 个变量都填了

2. **确认 Excel 没被占用**
   - `runtime/saas_full_audit_new.xlsx` 没有被打开

3. **跑命令**（完整流程）

```powershell
python scripts/audit_saas_excel_file.py ^
  --input-saas-json      ./data/saas_json.json ^
  --input-customer-json  ./data/customer_json.json ^
  --curl-file            ./data/detail_request.curl ^
  --output-xlsx          ./runtime/saas_full_audit_new.xlsx ^
  --use-model ^
  --request-interval     0.5 ^
  --enable-crawler ^
  --crawler-headless ^
  --crawler-concurrency  3 ^
  --crawler-per-domain-delay 2 ^
  --zhaoshang-task ^
  --zhaoshang-wait-timeout 600 ^
  --zhaoshang-progress-interval 30
```

4. **跑完后检查**
   - `runtime/saas_full_audit_new.xlsx` 已生成
   - summary Sheet 里"客户价FOUND数量" > 0（如果为 0 说明 cURL 过期）
   - "超过客户价30%数量" > 0 说明有异常行
   - 打开 Excel 看红色高亮行

5. **如果招商局任务失败**
   - 单独跑 `python tools/_export_zhaoshang.py` 导出已存在的任务
   - 再手动回填到 Excel

6. **如果需要重新跑**
   - 先关闭 Excel
   - 直接重跑命令（会覆盖输出文件）
