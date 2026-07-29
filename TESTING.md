# TESTING.md — 如何验证项目

> 本文件说明如何启动、测试和人工验证本项目。

---

## 一、环境准备

### Python 版本
- 需要 Python 3.11+（当前生产环境用 Python 3.14）
- 确认命令：`python --version`

### 安装依赖

```powershell
cd E:\.A中核运维\cnnc-ops-agent
pip install -r requirements.txt
```

依赖清单：fastapi / uvicorn / pydantic / pyyaml / pytest / httpx / openpyxl

监控状态查询额外依赖（项目外脚本）：
```powershell
pip install requests pandas openpyxl
```

### 认证文件确认
- `data/model.env` — 模型 API 配置（3 个变量）
- `data/detail_request.curl` — 中核 detail/standard detail 接口 cURL，支持 `documentId+fourCategoryId` 或 `vCommodityId+fourCategoryId` 请求体
- `data/saas_json.json` — SaaS 审批导出
- `data/customer_json.json` — 客户后台导出
- `E:\.A中核运维\自动拉取\.env` — SaaS + 中核认证（监控查询用）
- 招商局任务认证优先从当前进程环境变量读取：`BLADE_AUTH` 和 `COOKIE`。不要把真实值写入仓库文档、脚本或 `.env.example`。
- SQL 执行边界：只允许招商局当前爬虫任务优先级调整 SQL；其他任何 SQL 查询、统计、诊断、修复或写库操作都禁止执行。
- 招商局优先级要求：每次建招商局任务后，必须自动把当前任务调到最高优先级；运行前需确认 `ZHAOSHANG_DB_HOST` / `ZHAOSHANG_DB_PORT` / `ZHAOSHANG_DB_USER` / `ZHAOSHANG_DB_PASSWORD` 已配置。缺失或执行失败时必须暂停汇报。
- 招商局任务名要求：当前统一使用 `cnnc_current_opt_<中文商城名>_<纯数字时间标识>`。外部传入 `CNNC_PRICE_*` 等旧任务名时，`auto_crawl_task.py` 会自动归一化，确保优先级 SQL 能匹配当前批次。
- 爬价边界：价格审计必须先走招商局。招商局未走通、未覆盖或未爬到价格时，必须暂停并让用户确认后，才允许运行本地脚本爬取兜底。

---

### cURL 与内网代理验证

### 招商局导出路径编码验证
- 编译外部包装器：`E:\Python314\python.exe -m py_compile "E:\.A2026\招商局自动建任务\agent\tools\crawl_task_tool.py"`。
- 运行一条授权的招商局任务后，日志应包含可访问的 `result_path`，且路径文件名为 ASCII；最终审计表对应链接应回填招商局价格。

- 项目内脚本必须通过 `scripts/curl_utils.py` 解析浏览器复制的 cURL，不要把 `--data-raw` JSON 拼进 PowerShell 命令执行；否则 JSON 引号可能被吃掉并返回 `JSON parse error`。
- SaaS / 中核 / 招商局接口必须绕过系统代理。Python 内部请求优先使用 `internal_direct_opener()`；外部招商局子进程使用 `internal_subprocess_env()` 清除 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` 并设置 `NO_PROXY`。
- 手工验证 SaaS 或招商局连通性时，优先使用 `curl.exe --noproxy '*'`，并把 JSON 请求体写入临时文件后用 `--data-binary "@body.json"`，避免 PowerShell 转义破坏 JSON。
- 判断 Cookie / Token 是否过期前，先排查两件事：请求是否绕过代理、JSON 请求体是否仍是合法 JSON。
- 判断招商局导出失败时，必须区分三种状态：任务未完成、任务完成但 `exportTask` 返回 `未检索到商品价格信息`、导出 Excel 已生成但本地解析不到价格。第二种说明招商局导出接口没有价格结果，不能当作本地回填脚本问题；第三种可用 `read_zhaoshang_result_xlsx()` 检查表头是否包含 `商品详情地址` / `商品价格` 等列。
- 招商局任务名仍使用 `cnnc_current_opt_<中文商城名>_<纯数字时间标识>` 以保证优先级 SQL 可匹配；但本地导出结果文件名必须是 ASCII 安全文件名。若日志中出现 `Invalid argument` 且路径包含 `????`，优先检查 `auto_crawl_task.py` 的 `safe_output_path()` 是否生效。

验证命令：

```powershell
$env:PYTHONIOENCODING='utf-8'
pytest tests/test_work_01_saas_approval_json_audit.py::TestCurlUtils -q
python -m py_compile scripts\curl_utils.py scripts\audit_saas_excel_file.py scripts\process_monitor_no_push_zhaoshang.py
python -m py_compile "E:\.A2026\招商局自动建任务\external_tools\crawl_task_tool\auto_crawl_task.py"
```

## 二、自动化测试

### 运行全部测试

```powershell
cd E:\.A中核运维\cnnc-ops-agent
$env:PYTHONIOENCODING='utf-8'
E:\Python314\python.exe -m pytest tests/ -v
```

当前共 **577 个测试用例**，覆盖：
- SQL 安全校验（白名单表、写库拦截、多语句、注释）
- 范围守卫（非中核租户拒绝）
- SQL 模板渲染（参数类型校验、值安全）
- 知识库脱敏（fenced block、HTML pre、缩进代码块）
- /health 健康检查
- /diagnose 集成测试
- workflow API（route / analyze / scope / knowledge）
- work_01 审计规则（标题匹配 / 价格检查 / 决策）
- work_02 价格反馈框架
- human_approval_guard（建议-only 模式）

### 只跑特定测试

```powershell
# 只跑安全守卫测试
E:\Python314\python.exe -m pytest tests/test_safety_guard.py -v

# 只跑审计规则测试
E:\Python314\python.exe -m pytest tests/test_work_01_saas_approval_json_audit.py -v

# 跑并显示 print
E:\Python314\python.exe -m pytest tests/ -v -s
```

### 查看测试收集（不执行）

```powershell
E:\Python314\python.exe -m pytest tests/ --co -q
```

### 文档同步类改动验证

只修改 `AGENTS.md` / `CURRENT_STATE.md` / `TESTING.md` / `README.md` / `.env.example` 等交接文档时，通常不需要跑全量业务测试。建议验证：

```powershell
cd E:\.A中核运维\cnnc-ops-agent
git diff -- AGENTS.md CURRENT_STATE.md TESTING.md README.md .env.example
Select-String -Path AGENTS.md,CURRENT_STATE.md,TESTING.md,README.md,.env.example -Pattern "sk-|x-access-token=eyJ|auth-token=|access-token=AT-|Bearer eyJ|password="
```

验证点：
1. 没有写入真实 API Key、账号、密码、Cookie、Token、生产连接串。
2. 没有修改业务代码、测试代码、数据文件或运行产物。
3. 文档中的命令、路径、职责边界与当前项目状态一致；无法确认的信息标注“待确认”。
4. 如文档改动涉及测试命令、依赖、启动方式或安全边界，应再执行 `E:\Python314\python.exe -m pytest tests/ --co -q`。
5. 如文档改动同步了业务逻辑变化，应按对应模块执行定向测试或完整 `pytest tests/ -v`。

---

## 三、本地启动服务

### 启动 FastAPI（旧诊断 API）

```powershell
cd E:\.A中核运维\cnnc-ops-agent
$env:PYTHONIOENCODING='utf-8'
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

启动后访问：
- API 文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

### 健康检查验证

```powershell
curl http://localhost:8000/health
```

期望返回 `status: ok`，`knowledge_status: ok`，`template_count: 5`。

### 诊断接口验证

```powershell
curl -X POST http://localhost:8000/diagnose `
  -H "Content-Type: application/json" `
  -d '{\"question\": \"中核本周巡检\"}'
```

期望返回 `scenario: weekly_check` + SQL 模板列表。

---

## 四、关键功能验证步骤

### 验证 1：价格审计全流程

**前置条件**：
- `data/saas_json.json` 和 `data/customer_json.json` 存在且非空
- `data/detail_request.curl` 登录态有效
- `data/model.env` 三个变量已配置
- `runtime/saas_audit_final.xlsx` 没被 Excel 打开

**运行命令**（完整流程，含模型+招商局；脚本爬取兜底需遵守下方确认规则）：

```powershell
cd E:\.A中核运维\cnnc-ops-agent
$env:PYTHONIOENCODING='utf-8'
$env:TITLE_MATCH_MODEL_BATCH_SIZE='30'
$env:TITLE_MATCH_MODEL_BATCH_CONCURRENCY='1'
$env:TITLE_MATCH_MODEL_TIMEOUT='180'
$env:TITLE_MATCH_MODEL_RETRY_SLEEP='5'
$env:TITLE_MATCH_MODEL_MAX_RETRIES=''

E:\Python314\python.exe scripts\audit_saas_excel_file.py `
  --input-saas-json .\data\saas_json.json `
  --input-customer-json .\data\customer_json.json `
  --curl-file .\data\detail_request.curl `
  --output-xlsx .\runtime\saas_audit_final.xlsx `
  --use-model `
  --request-interval 0.5 `
  --enable-crawler `
  --crawler-headless `
  --crawler-concurrency 3 `
  --crawler-per-domain-delay 2 `
  --zhaoshang-task `
  --zhaoshang-concurrency 6 `
  --zhaoshang-wait-timeout 7200 `
  --zhaoshang-progress-interval 30
```

**验证点**：
1. 控制台输出 `[model] SET` 表示模型配置加载成功；配置完整时模型 HTTP/超时/解析失败必须重试，不允许静默降级为人工复核
2. 控制台输出 detail 接口 FOUND / FOUND_SINGLE_REFERENCE 表示客户价获取成功
3. 控制台输出招商局任务创建、完成、backfill 信息；如果招商局失败或未爬到，必须暂停并汇报原因
4. 控制台必须能看到招商局优先级调整成功信息；如果缺少 `ZHAOSHANG_DB_*` 或优先级调整失败，必须暂停，不得按已提速流程继续
5. `runtime/saas_audit_final.xlsx` 生成，含 summary + record_results 两个 sheet
6. summary sheet 有总记录数 / 建议通过 / 建议驳回 / 超过30% 数量
7. record_results 有中文表头，over_30pct=是 的行整行淡红

**脚本爬取兜底规则**：不得在招商局失败、未覆盖或未爬到价格后自动运行 `multi_mall_crawler`。必须先向用户列出失败商城、链接数量、失败原因和拟使用的脚本命令；只有用户明确确认后，才允许运行脚本兜底。

**不含招商局（只跑 multi_mall_crawler）**：默认禁止。只有用户明确要求或确认脚本兜底时，才可去掉 `--zhaoshang-task` 或单独运行多商城脚本。
**不含爬虫（只跑 detail + 模型）**：去掉 `--enable-crawler` 和 `--zhaoshang-task`
**不含模型**：去掉 `--use-model`

### 验证 2：监控状态查询（中核→SaaS）

```powershell
$env:PYTHONIOENCODING='utf-8'
python scripts\export_current_monitor_status.py
```

**验证点**：
1. 控制台 JSON 中 `cnnc_total` 等于中核接口 `count`
2. `saas_matched + saas_missing = cnnc_total`
3. `pushable + needs_processing + saas_missing = cnnc_total`
4. `runtime/saas状态查询/<时间戳>/中核待处理_SaaS监控状态_<时间戳>.xlsx` 生成
5. Excel 含 5 个 sheet：全量监控结果 / 可推送数据 / 需处理数据 / SaaS监控缺失 / 状态统计
6. Excel 显式包含原始 `newPrice` 列
7. 可推送数据 = `isSync=1 AND isSyncStandard=1 AND syncPriceStatus!=5 AND newPrice 为可解析、有限且大于 0 的数值`
8. `syncPriceStatus=5 + newPrice>0` → 不能推送，分类为“价格检测校验失败”
9. `syncPriceStatus!=5 + newPrice` 为空、`null`、非数字、非有限数值、`0` 或负数 → 不能推送，分类为“推送价格为空或无效”
10. `syncPriceStatus!=5 + newPrice` 为有效正数 → 可推送

### 验证 3：监控状态查询（本地 JSON 文件）

```powershell
python "E:\.A中核运维\monitor_status_export.py" --input "某个监控结果.json" --output "out.xlsx"
```

**验证点**：
1. Excel 含 4 个 sheet：全量状态结果 / 可推送数据 / 问题数据 / 状态统计
2. 全量状态结果行数 = JSON 中 rows 数量
3. 可推送数据只含三状态全 1 或价格为空
4. 原始字段不丢失

### 验证 4：接口请求模式（直接查 SaaS）

```powershell
$env:X_ACCESS_TOKEN="你的token"
python "E:\.A中核运维\monitor_status_export.py" --tenant-id "188605" --commodity-id "某个commodityId"
```

**验证点**：
1. 控制台输出 `[请求] page=1`
2. 控制台输出 `[接口] count=N`
3. 不写死 token（从环境变量读取）

---

## 五、人工验证（无自动化测试的部分）

以下功能没有自动化测试，需人工验证：

| 功能 | 验证方式 |
|------|---------|
| 招商局建任务 | 跑完整流程后，去招商局后台看任务是否创建成功 |
| 爬虫价格回填 | 优先检查招商局 backfill；脚本爬取结果只能在用户确认脚本兜底后检查 |
| 价格=0 处理 | 确认 page_price_status="价格为0"，final_decision=REJECT_PRICE_CHANGED |
| 多段 JSON 容错 | 用分页导出首尾拼接的 JSON 文件测试，确认 rows 全部合并 |
| cURL 解析 | 用 Windows ^ 续行的 cURL 测试，确认 headers/cookie/data 正确提取 |
| 分页获取 | 用小 limit（如 15）测试，确认自动翻页到取完 |

### 验证 5：文档同步规则

当修改业务代码、流程、配置、模型调用、SQL、文件输入输出、安全边界或测试方式后，必须人工检查：

1. `AGENTS.md` 是否仍能指导后续 AI 接手；
2. `CURRENT_STATE.md` 是否记录了当前阶段、最近重要修改、已知问题和下一步；
3. `TESTING.md` 是否记录了新的测试或人工验证方式；
4. `README.md` 是否仍能说明项目用途、输入输出和快速开始；
5. `.env.example` 是否包含新增环境变量，且只有占位符，没有真实密钥、Cookie、Token、账号或密码。

如果本次只是单次运行、单次查询、单次生成文件或只读分析，且没有改变项目逻辑，可不更新文档，但最终回复必须说明未触发文档同步规则。

### 验证 6：招商局建任务 + 启用 + 优先级

用于单独验证招商局链路，不跑完整价格审计。该验证会真实创建一个小任务，并只允许执行当前任务范围的优先级 SQL。

```powershell
cd E:\.A中核运维\cnnc-ops-agent

# 从招商局后台最新 cURL 提取，不要写入仓库文件
$env:BLADE_AUTH='bearer <your_zhaoshang_blade_auth>'
$env:COOKIE='<your_zhaoshang_cookie>'

# 只用于招商局当前任务优先级调整；不要执行其他 SQL
$env:ZHAOSHANG_DB_HOST='<host>'
$env:ZHAOSHANG_DB_PORT='3306'
$env:ZHAOSHANG_DB_USER='<user>'
$env:ZHAOSHANG_DB_PASSWORD='<password>'

python "E:\.A2026\招商局自动建任务\external_tools\crawl_task_tool\auto_crawl_task.py" `
  --task-name "CNNC_PRICE_zjmi_source40_$(Get-Date -Format HHmmss)" `
  --excel "E:\.A中核运维\cnnc-ops-agent\爬取模板\2026-07-02\浙江物产商城.xlsx" `
  --mall zjmi `
  --output "E:\.A中核运维\cnnc-ops-agent\runtime\zhaoshang_fix_test.xlsx" `
  --wait-timeout 5 `
  --progress-interval 2
```

预期现象：

1. 控制台出现 `[task-name] normalized for priority: ... -> cnnc_current_opt_浙江物产商城_<数字>`。
2. `[新建任务] Response` 返回 `rspCode=0000`。
3. `[启用任务] Response` 返回 `rspCode=0000`。
4. `【优先级调整】updated rows=<正整数>`，例如 6；如果刚启用任务后子任务尚未落入 `s_task_info`，会看到 `SELECT 返回空, 重试 ...`，当前允许最多 60 次、每次 2 秒。每次重试必须刷新数据库事务快照，不能在一个 `REPEATABLE READ` 事务中重复读取第一次空结果。
5. 如果 `--wait-timeout 5`，最后可能出现“等待爬取完成超时”，这只表示未等待完整爬取完成，不代表建任务或优先级失败。

优先级轮询事务回归测试：

```powershell
python -m pytest tests\test_zhaoshang_priority_boost.py -q
```

标题模型直连验证：运行一条 `call_title_match_model(...)`，结果应为 `SAME`、`NOT_SAME` 或 `MANUAL_REVIEW`，且进程网络连接不得落到本机代理端口；模型失败时按现有重试规则继续，不降级为人工核查。

该测试模拟第一次 SELECT 为空、第二次 SELECT 前调度行才落表的情况；验收要求第二次查询能看到新行并执行 `weight_type=-1` UPDATE。

### 验证 7：监控表三文件产出

本节对应 `AGENTS.md` 的“第三步标记”范围。新建第三步专用会话时，先确认 Agent 能明确复述第三步的输入、固定入口、招商局优先顺序、用户确认节点、三文件路径和暂停条件；不得把第一步价格审计或第二步全量状态查询混入本次运行。

处理 `runtime/saas状态查询/<时间戳>/` 监控表时，验收固定为三个文件：

1. `导入zhimport模版_<时间戳>.xlsx`
2. `处理日志_<时间戳>.xlsx`
3. `直接推送失败的数据_<时间戳>.xlsx`

人工核查点：

1. 只处理监控表中 `是否可推送=否` 的行；
2. 已先调用中核 detail 获取参考链接；
3. 参考链接数大于等于 2 的数据进入 zhimport 模板；
4. 参考链接数为 0 或 1 的数据进入直接推送失败数据；
5. 爬价阶段必须先走招商局；
6. 招商局认证失败、建任务失败、任务未返回、未爬到价格时必须暂停并汇报，不得自动运行本地脚本兜底；
7. 本地脚本兜底只有在用户明确确认后才能执行。

`data\ZHAOSHANG_DB.env` 仅可通过 `--zhaoshang-db-env` 在当前进程加载，必须包含 `ZHAOSHANG_DB_HOST`、`ZHAOSHANG_DB_PORT`、`ZHAOSHANG_DB_USER`、`ZHAOSHANG_DB_PASSWORD`；不得打印、提交或将实际值写入文档。用户明确授权本批次史泰博兜底时，可追加 `--auto-stbchina-fallback`，仅自动处理招商局后仍缺价的 `stbchina.cn` URL；其他商城仍需再次确认。用户再明确要求用 detail 价格和商品标题补全时，可追加 `--detail-reference-fallback`，但只允许补价格和标题同时为空、且同一 `documentId + 完整 URL` 精确命中的行。

固定脚本命令：

```powershell
cd E:\.A中核运维\cnnc-ops-agent

python scripts\process_monitor_no_push_zhaoshang.py `
  --monitor-xlsx "E:\.A中核运维\cnnc-ops-agent\runtime\saas状态查询\<时间戳>\中核待处理_SaaS监控状态_<时间戳>.xlsx" `
  --auth-curl-file "<包含中核 query/detail 和招商局 exportTask curl 的文本文件>" `
  --zhaoshang-db-env "data\ZHAOSHANG_DB.env" `
  --timestamp "<时间戳>" `
  --zs-wait-timeout 600
```

以后处理新批次只改命令参数，不要新建临时执行脚本。

第三步会话文档验收：

```powershell
Select-String -Path AGENTS.md -Pattern "第三步标记：开始|第三步标记：结束|process_monitor_no_push_zhaoshang.py|待确认脚本兜底清单|weight_type=-1"
```

预期同时命中第三步开始/结束标记、固定入口、脚本确认节点和招商局最高优先级规则。若任一项缺失，不应让新会话直接运行第三步。

### 验证 8：SKUID 补全、招商局补跑与史泰博兜底

当 zhimport 模板存在缺价格数据时，人工核查顺序如下：

1. 检查 `SKUID` / 商品编码是否为空或明显提错；
2. 必要时从 URL 中提取 `itemId`、`skuId`、`id` 或详情路径中的商品编码；
3. 补全后另存新文件，不覆盖原始模板；
4. 只对“仍无价格且属于招商局支持商城”的行重新建招商局任务；
5. 招商局补跑后，生成仍无价格清单；
6. 对非招商局商城，必须等待用户确认后才可运行专用脚本兜底。

史泰博 `stbchina.cn` 链接的验证命令：

```powershell
cd "E:\.A2026\多商城爬取\multi_mall_crawler"

python scripts\stbchina_fast_crawler.py `
  --input "input\stbchina_<时间戳>_missing_urls.txt" `
  --output "output\stbchina_<时间戳>_missing_result.xlsx"
```

验收点：

1. 输入文件只包含 `stbchina.cn` 链接，每行一个 URL；
2. 控制台输出 `total=N success=N failed=0` 时表示全部成功；
3. 输出 Excel 第一张表包含商品标题、uid、商品链接、商品价格、评论数、采集状态、失败原因；
4. 回填模板时按 URL 精确匹配，回填价格、商品标题、uid/SKUID、评论数；
5. 回填后另存为新文件，例如 `导入zhimport模版_<时间戳>_招商局_STB补价后.xlsx`；
6. 再生成 `STB补价后仍无价格清单_<时间戳>.xlsx`，确认剩余无价格数量。
7. 无价格行不得自行填入商城侧商品标题；如果价格为空，`互联网标题` 等商城侧标题字段也应保持为空，只保留 URL、SKUID/商品编码等基础字段。

用户明确授权 detail 参考数据补全时，追加验证：

1. 原 `导入zhimport模版_<时间戳>_招商局_STB补价后.xlsx` 不得覆盖；
2. 新文件名为 `导入zhimport模版_<时间戳>_招商局_STB_DETAIL参考补全后.xlsx`；
3. 只补原来价格和互联网标题同时为空的行，已有任一字段的行均不修改；
4. 匹配键必须是同一 `documentId + 完整 URL`，价格来自匹配 `referenceInfos[]` 的 `referencePrice`，标题来自本次 detail 的 `commodityName`；
5. 阶段处理日志中回填行必须标记 `中核detail参考回填`，汇报时不得称为商城实采价；
6. 生成 `DETAIL参考补全后仍无价格标题清单_<时间戳>.xlsx` 并核对剩余数量。

已验证案例：批次 `20260626194714` 中，史泰博输入 20 条，成功 20 条，失败 0 条；回填后剩余无价格从 28 条降到 8 条。

---

## 六、常见报错和排查方向

### 1. `detail 全部 DETAIL_REQUEST_FAILED`
- **原因**：`data/detail_request.curl` 登录态过期，或 cURL 请求体字段与接口不匹配。
- **解决**：从中核商品详情页 F12 重新抓 detail 或 `standardCommodityModel/queryStandardDetail` 接口 cURL，确认请求体包含 `documentId+fourCategoryId` 或 `vCommodityId+fourCategoryId`，再覆盖 `data/detail_request.curl`。

### 2. `模型全部 MODEL_UNAVAILABLE`
- **原因**：`data/model.env` 缺失、`TITLE_MATCH_MODEL_API_KEY` / `TITLE_MATCH_MODEL_BASE_URL` / `TITLE_MATCH_MODEL_NAME` 任一变量缺失，或 KEY 无效导致模型服务不可用。
- **解决**：检查 `data/model.env` 是否存在且 3 个变量都填了；确认 KEY 有效。配置完整但模型接口超时、HTTP 失败、JSON 解析失败或批量响应缺行时，当前代码会按 `TITLE_MATCH_MODEL_RETRY_SLEEP` 持续重试；不要把这类失败自动写成人工复核。

### 3. `SaaS 接口返回 401/403`
- **原因**：SAAS_COOKIE 过期
- **解决**：从 saasportal.iio.com 浏览器 F12 重新抓 Cookie，更新 `自动拉取/.env` 的 SAAS_COOKIE

### 4. `中核接口返回 count=None rows=0`
- **原因**：y1AmuISe 过期
- **解决**：从 cnncmall.com 浏览器 F12 重新抓 y1AmuISe（URL 和 header 两个值都不同！），更新 `.env`

### 5. `招商局建任务失败`
- **常见原因 1**：Blade-Auth / Cookie 过期，接口返回 `rspCode=3333`、`rspDesc=请重新登录`
- **解决**：从招商局后台 F12 重新抓 cURL，在当前 PowerShell 进程设置 `BLADE_AUTH` 和 `COOKIE` 后重跑；不要把真实值写入仓库
- **常见原因 2**：外部招商局工具未配置对应 mall_key/sourceId
- **解决**：确认 `商城sourceId映射表/商城sourceId映射表.xlsx` 有对应 sourceId，并确认外部工具 `auto_crawl_task.py` 的 `MALL_CONFIG` 已注册该 mall_key
- **红线**：招商局失败后不能自动改用脚本爬取；必须先暂停并让用户确认是否脚本兜底

### 6. `招商局任务很慢或没有提速`
- **常见原因**：只建任务但没有执行当前任务优先级 SQL。当前建任务 payload 的 `taskWeight` 必须按浏览器当前成功请求传 `"0"`，`startEt` 使用当前时间；最高优先级仍依赖后续 SQL 把 `weight_type` 调为 `-1`。
- **常见原因 2**：优先级 SQL 条件未使用当前任务名的三片段。页面/API 的 `taskId` 不用于 `s_task_info` 的优先级 UPDATE；必须在 `enable_task` 成功后，按 `cnnc_current_opt`、商城优先级名和纯数字时间标识三项参数化 `batch_num LIKE` 条件先查范围、再更新当前任务。
- **复用任务验收**：对已存在且被本次流程复用的任务，`enable_task` 返回成功后也必须出现优先级范围查询和 UPDATE 日志；不得因“未新建任务”跳过提优。
- **落表等待验收**：启用后先检查 `queryTaskProgress`，再进行最多 60 次、每次 2 秒的 `task_status IN ('00','01')` 范围查询。连接为 `autocommit=False` 时，每次 SELECT 前必须结束上一轮只读事务以刷新快照；范围为空时刷新快照后记录同三片段的只读状态分布；命中后只允许把当前范围的 `weight_type` 更新为 `-1`。
- **失败保护验收**：正式入口在优先级任何步骤失败时必须停止后续等待和导出并清晰报错，但不得自动停用已经启用的任务。只有日志出现 `updated rows>0` 才能认定已提优。
- **常见原因 3**：优先级 SQL 在 `enable_task` 前执行，`s_task_info.task_status` 还未进入 `00/01`。
- **常见原因 4**：`enable_task` 已成功，但 `s_task_info` 子任务落表延迟超过短等待窗口；当前工具会最多轮询 60 次、每次间隔 2 秒，仍查不到时才按失败处理。
- **必需配置**：`ZHAOSHANG_DB_HOST` / `ZHAOSHANG_DB_PORT` / `ZHAOSHANG_DB_USER` / `ZHAOSHANG_DB_PASSWORD`。如果当前 API 新任务和平台自动任务的精确批次号在该连接中都查不到，判定为 API/DB 环境不一致，停止重复建任务并确认当前平台对应的数据库实例。
- **红线**：每次建招商局任务后必须在 `enable_task` 成功后自动调最高优先级；缺少 DB 环境变量、`affected_rows=0` 或 SQL 失败时必须暂停并汇报，不能继续当作已提速任务运行。除当前任务优先级调整外，不允许执行任何其他 SQL。
- **主任务字段实验验收**：如果用户明确要求测试 `zhaosj_data_crawl.dc_main_task_info.TASK_WEIGHT=-1`，只能对本次新建任务返回的精确 `TASK_NUMBER` 参数化执行并立即回查。`affected_rows>0`、回查值为 `-1` 仍不能单独判定提优成功；必须继续观察 `queryTaskProgress` 是否从“待执行”推进。若完整观察窗口仍停留在待执行，记录为“字段写入成功、调度提优未验证”，不得自动接入正式流程。

### 7. `招商局 add 返回 3333 请重新登录但 getAll 正常`
- **原因 1**：`getAll`、`queryListByPage` 和 `exportTask` 可在无认证时返回数据，它们成功不能证明 `/add` 的写认证有效。
- **原因 2**：人工拆分认证或 PowerShell 转义破坏请求内容，也可能让写接口返回认证类错误。
- **解决**：优先提供浏览器实际建任务请求并保存为 `data/latest_zhaoshang_add.curl`；主脚本会优先从该文件原样解析 `Blade-Auth` 和 `Cookie`。`taskWeight` 按当前浏览器请求保持 `"0"`，最高优先级仍由当前任务范围 SQL 设置为 `-1`。
- **验证**：可对 `/add` 发送空 JSON；有效写认证通常进入参数校验并返回非 `3333`，无效写认证会直接返回 `3333 / 请重新登录`。该探针不得包含有效任务 payload，避免误建任务。

### 7.1 `启用成功但没有落入 s_task_info`
- 先通过 `queryById/{taskId}` 检查 `taskWeight` 和 `startEt`。当前浏览器成功 add 请求使用 `taskWeight="0"`，`startEt` 使用当前时间；不要再把 `"0"` 强制改为空字符串，也不要把开始时间固定为当天零点。
- 如果 `queryTaskProgress` 的待执行数量大于总任务量，或执行完成数量为负数，说明重复启用已经造成调度队列不一致。此时必须停用任务，禁止继续调用 `updateState=1`，也不能声称优先级已调整。
- 只有当前任务已实际落入 `central_control_sub_zsz.s_task_info` 且 `task_status IN ('00','01')` 后，才允许执行该任务范围的 `weight_type=-1` 更新。条件固定为当前任务名的 `cnnc_current_opt`、商城名、纯数字时间标识三个 `batch_num LIKE` 条件；查不到目标行时不得扩大条件、不得更新其他任务。
- 页面 `taskId` 仅用于招商局 API，不用于优先级 SQL。优先级验证必须保存本次精确三条件、命中行数和 `affected_rows`，不得以页面 ID 或跨表映射替代当前任务名限定。

### 8. `zkh 全部 FAILED`
- **原因**：阿里云 WAF 识别 headless
- **解决**：等 30 分钟重试或人工解验证码。**不要绕过**

### 9. `JSON 解析失败`
- **原因**：文件格式异常
- **解决**：脚本自动修复缺右花括号；如仍失败看报错 char 位置手动检查

### 8. `Excel PermissionError`
- **原因**：输出文件被 Excel/WPS 打开
- **解决**：关闭文件后重跑，或换个输出文件名

### 9. `价格=0`
- **说明**：价格=0 是有效数据，标 page_price_status="价格为0"，price_changed=CHANGED，final_decision=REJECT_PRICE_CHANGED
- **处理**：按价格异常驳回

### 10. `pytest 收集时报 StarletteDeprecationWarning`
- **说明**：httpx 版本兼容警告，不影响测试
- **处理**：可忽略

### 11. `monitor_status_export.py 接口请求卡住`
- **原因**：未传 commodityId 导致全量查询 17 万条
- **解决**：加 `--max-rows 5000` 限制，或 Ctrl+C 中断后用 `--commodity-id` 指定

### 12. `cnnc_to_saas_monitor.py 报 SaaS监控缺失`
- **说明**：部分 documentId 在 SaaS 监控查不到
- **可能原因**：数据刚审完未进入监控 / documentId 未关联监控池 / 时序延迟
- **处理**：查看 Excel "SaaS监控缺失" sheet 确认具体商品，过一段时间重查
## 2026-07-08 招商局 exportTask cURL 认证验证

- 价格审计主脚本可只更新 `data/latest_zhaoshang_exportTask.curl`。运行 `scripts/audit_saas_excel_file.py --zhaoshang-task` 时，会自动从该文件解析 `Blade-Auth` / `Cookie` 到当前进程环境变量，再调用外部招商局建任务脚本。即使该 cURL 的 JWT 本地显示过期，也要执行 `/add` 实测；Cookie/刷新态是否有效由接口响应决定，不能因本地 JWT 预检直接跳过。
- 验证命令：

```powershell
pytest tests/test_work_01_saas_approval_json_audit.py::TestCurlUtils -q
```

- 手工排查时不要拆 token；先确认 `scripts.curl_utils.parse_curl_command()` 能从 exportTask cURL 取到 `Blade-Auth` 和 `Cookie`，再判断是否为招商局登录态失效。

### 招商局建任务失败诊断

先运行一条商城模板的最小任务。正式入口现在会打印完整、已脱敏的外部工具 stdout/stderr；检查顺序固定为：

1. `rspCode=3333` 或登录提示：认证问题；
2. 接口返回字段校验信息：payload 问题；
3. 请求尚未到接口、模板读取或链接校验异常：商城模板字段问题。

不得只根据短异常摘要判断 Token 失效，也不得在失败后自动运行本地爬虫兜底。

2026-07-14 最小真实模板验证中，模板读取和 payload 生成均成功，而 `/crawlTaskManage/add` 返回 `rspCode=3333`。该结果归类为写接口认证失败；排查时应更新可访问 `/add` 的浏览器登录态，不应先修改模板或 payload。

若 `/add`、任务查询、启用成功后在优先级步骤报 `KeyError: ZHAOSHANG_DB_HOST`，说明外部招商局子进程未接收到完整 `ZHAOSHANG_DB_*` 环境变量。此时不得继续等待导出或宣称已提速；先补齐当前进程到子进程的环境传递，再用一条新任务复测。

复测时若当前批次状态分布只包含 `task_status='02'`，说明数据库连接和三片段范围匹配已正常，且任务已执行成功。不得放宽 SQL、不得更新该行；跳过提优并直接使用 `exportTask` 导出结果。`task_status`：`00` 待执行、`01` 执行中、`02` 执行成功、`03` 执行失败；`queue_status`：`00` 等待入队、`01` 已入队、`02` 已出队。
### 监控表仅生成可处理文件

当用户要求先不跑招商局时，使用固定入口并传入 `--skip-zhaoshang`。该命令不得配置或执行数据库优先级更新：

```powershell
python scripts\process_monitor_no_push_zhaoshang.py `
  --monitor-xlsx "runtime\saas状态查询\<时间戳>\<监控表>.xlsx" `
  --cnnc-query-curl-file "data\latest_cnnc_query.curl" `
  --detail-curl-file "data\detail_request.curl" `
  --zhaoshang-export-curl-file "data\latest_zhaoshang_exportTask.curl" `
  --timestamp "<时间戳>" `
  --skip-zhaoshang
```

验收：生成 `导入zhimport模版`、`处理日志`、`直接推送失败的数据` 和待确认清单；不出现招商局建任务或优先级调整日志。
## 2026-07-14 Model Response Compatibility

When model logs show a `ValidationError` for `brand_match` or `spec_match` with value `unknown`, run:

```powershell
E:\Python314\python.exe -m pytest tests\test_work_01_saas_approval_json_audit.py -q -k "unknown_optional_booleans or parse_batch_rich_title_model_response"
```

The expected result is two passing tests. For provider timeouts, first validate a smaller batch and then set `TITLE_MATCH_MODEL_BATCH_SIZE=10` and `TITLE_MATCH_MODEL_BATCH_CONCURRENCY=1` for the current process. Do not downgrade failed calls to manual review.
### 招商局优先级失败保护

运行监控表入口时，若任一商城外部任务因优先级 SELECT、UPDATE 或影响行数校验失败而返回非零，父流程必须立刻终止。验收：后续商城不再出现新的 `add` 请求；已启用任务不得被自动停用；不得生成“已提优”或继续导出结果的结论。

已完成任务复用验收：`query_task_status` 返回“本次任务执行完毕”时，入口只能调用导出，不得再次 `enable_task` 或运行优先级 SQL。

优先级 UPDATE 返回 0 的验收：立即按同一任务三片段查询状态分布。若全部为 `task_status=02`，或状态行已清理但招商局进度 API 确认全部完成，应输出 `COMPLETED_UNCONFIRMED`，继续导出并在最终报告标为“任务已完成、未确认提优”；若仍有 `00/01` 或无法确认已完成，子进程必须非零退出，父流程立即停止后续商城。

### Zhaoshang zero-price Excel highlighting

Run the focused regression test after changing Zhaoshang result parsing or workbook highlighting:

```powershell
python -m pytest tests\test_work_01_saas_approval_json_audit.py::TestZhaoshangZeroPriceStatus -q
```

Expected result: a returned numeric page price of `0` is normalized to `PRICE_IS_ZERO`. The Excel writer highlights the complete row with the warning fill for that status.

The same focused test also verifies a late Zhaoshang price backfill recalculates `price_changed` and `final_decision`, and applies the abnormal-row fill when the JSON price differs from the collected page price.

## CNNC cURL Header Regression Check

- Preserve the captured `y1AmuISe` header case exactly. Do not use `urllib.request.Request.add_header()` or the `headers=` constructor for captured CNNC headers: both normalize it to `Y1amuise`.
- Run `python -m pytest tests\test_work_01_saas_approval_json_audit.py -k "detail_request_preserves_cnnc_y1_header_case" -q`.
- For a browser-valid cURL returning `rspCode=3333` only in Python, inspect header-name preservation before refreshing credentials.

## Zhaoshang add cURL Safety

- `crawlTaskManage/add` is a write endpoint. Never send an add cURL as a connectivity or token health check: a successful response can create a real crawler task.
- Validate add cURL syntax and required headers locally with `parse_curl_command()` only. The first real HTTP call to `/add` must be the intended current audit task.
- For read-only connectivity checks, use the captured `exportTask` cURL against its existing task payload; do not alter task state or create a test task unless the user explicitly requests it.

## 招商局断点复用验收

1. 在当前批次 `_zs_tmp_priority` 中保留一个已导出的 `zs_result_<mall>.xlsx`。
2. 用相同 `--timestamp` 重跑 `scripts/process_monitor_no_push_zhaoshang.py`。
3. 日志应出现 `[招商局] 复用已导出结果`，且该商城不再调用 `add` 或 `enable_task`。
4. 如子进程输出 `[优先级调整] WARNING`，父流程必须记入优先级失败清单，不得报告已提优。

直接推送失败文件占用验收：若固定输出文件被 Excel/WPS 占用，但现有文件可读且总行数等于本次失败记录数加表头，入口应复用该文件并继续后续回填；行数不一致或文件无法读取时必须保留 `PermissionError` 并暂停，不得用旧文件冒充本批次。
