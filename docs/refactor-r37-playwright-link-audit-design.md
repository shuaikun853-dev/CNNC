# R3.7：内部爬取结果接入与页面核验结果适配方案

## 1. 文档定位

R3.7 不再以 Agent 自己使用 Playwright 抓取页面为主线，而是设计“内部爬取结果接入 / 页面核验结果适配”能力。

当前业务不是只处理京东链接，SaaS 同款审批中会出现多种商城链接。公司内部已有爬取系统，大部分链接可以通过内部爬取系统获得页面标题、页面价格、上下架状态、截图路径、失败原因等信息。因此 R3.7 的主线应是：Agent 接收内部爬取系统结果，并把这些结果接入 `work_01 SaaS 同款审批 JSON 预审`。

Playwright 只作为后续 R3.8 或 R7+ 兜底方案预留，不进入 R3.7 实施范围。

## 2. R3.7 目标

R3.7 目标：接收公司内部爬取系统返回的商品页面核验结果，包括页面标题、页面价格、上下架状态、截图路径、失败原因等，将其接入 `work_01 SaaS 同款审批 JSON 预审` 流程，用于判断 SaaS 价格是否变动、链接是否下架、候选价格是否高于客户价格 30%，最终输出人工审批建议。

Agent 不负责爬取，不负责打开页面，不负责登录商城，不负责处理验证码。

## 3. 数据流

```text
SaaS 待审批 JSON
↓
公司内部爬取系统批量爬取 extLink
↓
获得内部爬取结果 JSON
↓
Agent 接收 SaaS JSON + 爬取结果
↓
按 vCommodityId / skuId / extLink 做关联
↓
综合标题模型判断、SKU/链接一致性、爬取价格、上下架状态、客户价格 30% 规则
↓
输出人工审批建议
```

R3.7 中，内部爬取系统是页面采集能力提供方；Agent 只是消费统一的爬取结果 JSON，并执行本地规则判断。

## 4. 禁止事项与安全边界

R3.7 必须明确：

1. R3.7 不实现爬虫。
2. R3.7 不接 Playwright 默认抓取。
3. R3.7 不接 OCR。
4. R3.7 不绕过验证码。
5. R3.7 不登录任何商城。
6. R3.7 不使用个人浏览器 Profile。
7. R3.7 不保存 Cookie/Token。
8. R3.7 不调用 SaaS 审批接口。
9. R3.7 不自动审批。
10. R3.7 不自动驳回。
11. R3.7 不写数据库。
12. R3.7 不执行 SQL。
13. R3.7 不接 Excel/CSV。
14. R3.7 不读取 Excel。
15. R3.7 不读取 CSV。
16. R3.7 不导出 Excel。
17. R3.7 不导出 CSV。
18. R3.7 不通过 Excel/CSV 触发任何导入、审批、驳回动作。
19. R3.7 不新增 action executor。
20. 所有输出仍然是 recommendation-only。

R3.7 只接收请求中显式传入的内部爬取结果，不主动调用内部爬取接口，也不主动访问外部商城页面。

## 5. 内部爬取结果 Schema 设计

建议新增统一输入结构：

```python
class InternalCrawlResult(StrictModel):
    extLink: str = ""
    skuId: str = ""
    vendorName: str = ""
    shopName: str = ""

    crawl_status: str = ""
    # SUCCESS / OFF_SHELF / SOLD_OUT / PRICE_MISSING / CAPTCHA / ACCESS_DENIED / TIMEOUT / FAILED / UNKNOWN

    page_title: str = ""
    page_price: Decimal | None = None
    page_status: str = ""
    # NORMAL / OFF_SHELF / SOLD_OUT / CAPTCHA / ACCESS_DENIED / TIMEOUT / UNKNOWN

    screenshot_path: str = ""
    crawl_time: str = ""
    error_code: str = ""
    error_message: str = ""
    raw_price_candidates: list[str] = Field(default_factory=list)
    price_source: str = ""
    # INTERNAL_CRAWLER / DOM / TEXT / OCR / UNKNOWN
```

字段说明：

| 字段 | 含义 |
| --- | --- |
| `extLink` | 被爬取的候选商品链接 |
| `skuId` | 内部爬取系统识别或透传的 SKU |
| `vendorName` | 商城/供应商名称 |
| `shopName` | 店铺或商城名称 |
| `crawl_status` | 内部爬取任务状态 |
| `page_title` | 页面标题或商品标题 |
| `page_price` | 内部爬取系统识别出的页面价格 |
| `page_status` | 页面业务状态 |
| `screenshot_path` | 内部爬取系统留存的截图路径 |
| `crawl_time` | 爬取时间，用于多结果去重取最新 |
| `error_code` | 爬取失败或异常状态码 |
| `error_message` | 爬取失败说明 |
| `raw_price_candidates` | 内部爬取系统返回的原始价格候选 |
| `price_source` | 价格来源，R3.7 只消费，不自行识别 |

`price_source=OCR` 可以被接收为证据，但不能直接作为 `APPROVE` 依据；若价格只来自 OCR，应进入 `MANUAL_CHECK`。

## 6. SaaS JSON 请求扩展

现有请求：

```python
class SaasSameProductApprovalAuditRequest(StrictModel):
    scope: WorkflowScope
    records: list[SaasSameProductApprovalItem]
    use_model: bool = True
    use_link_audit: bool = True
```

建议扩展为：

```python
class SaasSameProductApprovalAuditRequest(StrictModel):
    scope: WorkflowScope
    records: list[SaasSameProductApprovalItem] = Field(..., min_length=1)
    crawl_results: list[InternalCrawlResult] = Field(default_factory=list)
    use_model: bool = True
    use_crawl_result: bool = True
```

说明：

- `crawl_results` 是公司内部爬取系统的结果，不是 Agent 自己爬取。
- `use_crawl_result=false` 时，保持 R3.6 已有逻辑，不消费爬取结果。
- `crawl_results` 为空或无法关联时，不抛错，单条记录降级人工核验。

## 7. 爬取结果关联规则

关联优先级：

1. `extLink` 完全一致。
2. `skuId` 完全一致。
3. `vendorName + skuId` 一致。
4. 无法关联则视为缺少爬取结果。

如果同一个 `extLink` 有多个爬取结果，优先使用 `crawl_time` 最新的一条。

如果没有爬取结果：

```text
price_check = WAIT_PAGE_PRICE_CHECK
primary_action = MANUAL_CHECK
evidence = 缺少内部爬取结果，需人工核验页面价格
```

缺少爬取结果不能导致 `APPROVE`，也不能导致服务崩溃。

## 8. 页面状态处理规则

根据内部爬取结果中的 `crawl_status` / `page_status` 判断：

| 状态 | 处理建议 |
| --- | --- |
| `SUCCESS + page_status=NORMAL` | 正常参与价格比较 |
| `OFF_SHELF` | `REJECT` 建议 |
| `SOLD_OUT` | `MANUAL_CHECK`，后续可配置为 `REJECT` |
| `PRICE_MISSING` | `MANUAL_CHECK` |
| `CAPTCHA` | `MANUAL_CHECK` |
| `ACCESS_DENIED` | `MANUAL_CHECK` |
| `TIMEOUT` | `MANUAL_CHECK` |
| `FAILED` | `MANUAL_CHECK` |
| `UNKNOWN` | `MANUAL_CHECK` |

以上均只是人工建议，不自动驳回，不自动审批。

遇到 `CAPTCHA`、`ACCESS_DENIED`、`TIMEOUT`、`FAILED` 时，Agent 只记录证据和失败原因，不尝试重试爬取、不绕过验证码、不访问页面。

## 9. 价格比较规则

使用内部爬取结果中的 `page_price` 作为页面实际价格。

候选页面价格优先级：

1. `InternalCrawlResult.page_price`
2. `SaasSameProductApprovalItem.pageActualPrice` / `page_actual_price`
3. `SaasSameProductApprovalItem.price`

价格一致性规则：

```text
abs(page_price - saas_json_price) <= 0.01 -> MATCH
abs(page_price - saas_json_price) > 0.01 -> MISMATCH
page_price 缺失 -> WAIT_PAGE_PRICE_CHECK
saas_json_price 缺失 -> JSON_PRICE_MISSING
```

客户价 30% 高价过滤继续使用已有规则：

```text
candidate_price > customer_price * 1.30 -> REJECT 建议
customer_price missing -> CUSTOMER_PRICE_MISSING，不触发高价过滤
customer_price <= 0 -> CUSTOMER_PRICE_INVALID，不触发高价过滤
candidate_price missing -> CANDIDATE_PRICE_MISSING，不触发高价过滤
candidate_price <= 0 -> CANDIDATE_PRICE_INVALID，不触发高价过滤
```

只有 `HIGHER_THAN_CUSTOMER_PRICE_30` 才能触发客户价 30% 高价 `REJECT` 建议。`CUSTOMER_PRICE_MISSING` / `CUSTOMER_PRICE_INVALID` / `CANDIDATE_PRICE_MISSING` / `CANDIDATE_PRICE_INVALID` 都不得触发高价 `REJECT`。

## 10. 商城范围

R3.7 支持多商城，但商城解析能力由公司内部爬取系统承担。

Agent 不直接适配每个商城页面结构，只消费统一的 `InternalCrawlResult`。这意味着京东、天猫、淘宝、苏宁等商城只要内部爬取系统能输出统一结构，Agent 均可消费其结果。

保留现有 SKU/链接一致性检查策略：第一版仍可优先支持京东 URL 解析；其他商城返回 `UNKNOWN`，由爬取结果和人工核验补充。

## 11. 截图路径策略

截图来源改为内部爬取系统。Agent 只接收：

```text
screenshot_path
```

安全要求：

1. `screenshot_path` 来源于内部爬取系统。
2. Agent 只把 `screenshot_path` 作为证据字段。
3. 不上传截图到外部服务。
4. 不执行 `screenshot_path`。
5. 不读取任意路径。
6. 不信任路径穿越字符串。
7. 后续若支持打开截图，必须限制目录。
8. 不保存 Cookie。
9. 不保存 HAR。
10. 不保存敏感请求头。
11. 包含 `..`、绝对路径、盘符、URL、控制字符的路径应拒绝或忽略。
12. evidence 中可记录“截图路径已忽略/待人工确认”，但不得执行路径。

## 12. Playwright 兜底方案：后续阶段预留

Playwright 仅作为后续 R3.8 或 R7+ 兜底方案预留，不属于 R3.7 实施范围。

R3.7 明确：

1. R3.7 不实施 Playwright。
2. R3.7 不新增浏览器依赖。
3. R3.7 不新增浏览器开关。
4. R3.7 不实现 OCR。
5. R3.7 不打开页面。
6. R3.7 不登录任何商城。
7. R3.7 不处理验证码。

如果未来阶段保留 `ENABLE_BROWSER_LINK_AUDIT`、`ENABLE_PRICE_OCR` 等环境变量，也只能作为后续预留，不属于 R3.7 实施范围。

## 13. 失败降级策略

任何爬取结果缺失、解析失败、状态异常都不能让服务崩溃。

- 单条失败返回 `MANUAL_CHECK`。
- 批量任务继续处理其他记录。
- evidence 中记录 `error_code`、`error_message` 或缺失原因。
- 不因为缺少爬取结果输出 `APPROVE`。
- 不因为爬取异常执行任何动作。
- 不主动调用内部爬取接口补抓。

建议 evidence 示例：

```text
缺少内部爬取结果，需人工核验页面价格
内部爬取状态=TIMEOUT，需人工核验页面价格
内部爬取状态=CAPTCHA，不尝试绕过验证码，需人工核验
内部爬取页面价格为空，需人工核验
```

## 14. 测试策略

R3.7 实施时测试应围绕“内部爬取结果适配”，至少覆盖：

1. `extLink` 完全一致时正确关联 `crawl_result`。
2. `skuId` 一致时正确关联 `crawl_result`。
3. 多个 `crawl_result` 时选择 `crawl_time` 最新。
4. 无 `crawl_result` 时返回 `WAIT_PAGE_PRICE_CHECK` / `MANUAL_CHECK`。
5. `crawl_status=OFF_SHELF` -> `REJECT` 建议。
6. `crawl_status=SOLD_OUT` -> `MANUAL_CHECK`。
7. `crawl_status=PRICE_MISSING` -> `MANUAL_CHECK`。
8. `crawl_status=CAPTCHA` -> `MANUAL_CHECK`。
9. `crawl_status=ACCESS_DENIED` -> `MANUAL_CHECK`。
10. `crawl_status=TIMEOUT` -> `MANUAL_CHECK`。
11. `crawl_status=FAILED` -> `MANUAL_CHECK`。
12. `page_price` 与 SaaS JSON `price` 一致 -> `MATCH`。
13. `page_price` 与 SaaS JSON `price` 不一致 -> `MISMATCH`。
14. `page_price` 高于客户价 30% -> `REJECT` 建议。
15. `customer_price <= 0` 不触发高价过滤。
16. `customer_price missing` 不触发客户价 30% 高价过滤。
17. `candidate_price missing` 不触发客户价 30% 高价过滤。
18. `customer_price missing` / `candidate_price missing` 不得输出 `HIGHER_THAN_CUSTOMER_PRICE_30`。
19. 爬取结果缺 `page_price` 时不直接 `APPROVE`。
20. `screenshot_path` 路径穿越被拒绝或忽略。
21. 禁止 Excel/CSV 读写或路径能力。
22. 不保存 HAR。
23. 不保存敏感请求头。
24. 不调用 Playwright。
25. 不调用 OCR。
26. `/diagnose`、`/health` 不受影响。
27. `work_01` 原有规则不受影响。
28. `work_02` 框架不受影响。
29. `work_03` 占位不受影响。

## 15. Codex 审查清单

1. 是否没有实现爬虫。
2. 是否没有默认 Playwright。
3. 是否没有 OCR。
4. 是否只消费内部爬取结果。
5. 是否对 `crawl_result` 做安全解析。
6. 是否处理状态 `OFF_SHELF` / `SOLD_OUT` / `CAPTCHA` / `TIMEOUT` / `FAILED`。
7. 是否缺少 `crawl_result` 时降级 `MANUAL_CHECK`。
8. 是否 `page_price` 高于客户价 30% 触发 `REJECT` 建议。
9. 是否明确 `customer_price missing` 不触发高价过滤。
10. 是否明确 `candidate_price missing` 不触发高价过滤。
11. 是否截图路径安全。
12. 是否明确不接 Excel/CSV。
13. 是否明确不保存 HAR。
14. 是否明确不保存敏感请求头。
15. 是否没有自动审批/驳回。
16. 是否没有 SaaS 审批接口调用。
17. 是否所有输出 recommendation-only。

## 16. 最终结论

1. R3.7 是否实施 Playwright：不实施 Playwright。
2. R3.7 是否实施 OCR：不实施 OCR。
3. R3.7 是否自己爬页面：不自己爬页面。
4. R3.7 是否只消费内部爬取结果：是，只消费内部爬取系统结果。
5. R3.7 是否支持多商城：是，支持多商城结果接入。
6. 多商城能力由谁提供：多商城爬取能力由公司内部爬取系统提供。
7. Agent 是否自动审批/驳回：不自动审批/驳回，只输出人工建议。
8. 缺少爬取结果时怎么处理：降级为 `MANUAL_CHECK`，并记录缺少内部爬取结果的 evidence。
9. R3.7 是否接 Excel/CSV：不接 Excel/CSV，不读取、不导出，也不通过 Excel/CSV 触发导入、审批、驳回动作。
10. R3.7 是否保存 HAR 或敏感请求头：不保存 HAR，不保存敏感请求头。
