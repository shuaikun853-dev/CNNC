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
- `data/detail_request.curl` — 中核 detail 接口 cURL
- `data/saas_json.json` — SaaS 审批导出
- `data/customer_json.json` — 客户后台导出
- `E:\.A中核运维\自动拉取\.env` — SaaS + 中核认证（监控查询用）

---

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

**运行命令**（完整流程，含模型+爬虫+招商局）：

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
  --crawler-headless `
  --crawler-concurrency 3 `
  --crawler-per-domain-delay 2 `
  --zhaoshang-task `
  --zhaoshang-concurrency 6 `
  --zhaoshang-wait-timeout 7200 `
  --zhaoshang-progress-interval 30
```

**验证点**：
1. 控制台输出 `[model] SET` 表示模型配置加载成功
2. 控制台输出 detail 接口 FOUND / FOUND_SINGLE_REFERENCE 表示客户价获取成功
3. 控制台输出爬虫 OK / FAILED 表示爬取完成
4. `runtime/saas_audit_final.xlsx` 生成，含 summary + record_results 两个 sheet
5. summary sheet 有总记录数 / 建议通过 / 建议驳回 / 超过30% 数量
6. record_results 有中文表头，over_30pct=是 的行整行淡红

**不含招商局（只跑 multi_mall_crawler）**：去掉 `--zhaoshang-task`
**不含爬虫（只跑 detail + 模型）**：去掉 `--enable-crawler` 和 `--zhaoshang-task`
**不含模型**：去掉 `--use-model`

### 验证 2：监控状态查询（中核→SaaS）

```powershell
$env:PYTHONIOENCODING='utf-8'
python "E:\.A中核运维\cnnc_to_saas_monitor.py"
```

**验证点**：
1. 控制台输出 `[中核] 共 N 条`
2. 控制台输出 `[SaaS] 用 N 个 documentId 查监控状态...`
3. 控制台输出 `[完成] 共获取 N 条数据`
4. `runtime/saas状态查询/<时间戳>/中核待处理_SaaS监控状态_<时间戳>.xlsx` 生成
5. Excel 含 4 个 sheet：全量监控结果 / 可推送数据 / 需处理数据 / 状态统计
6. 可推送数据 = isSync=1 AND isSyncStandard=1 AND (syncPriceStatus=1 OR 空)

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
| 爬虫价格回填 | 检查 Excel record_results 的"爬取页面价格"列是否有值 |
| 价格=0 处理 | 确认 page_price_status="价格为0"，final_decision=REJECT_PRICE_CHANGED |
| 多段 JSON 容错 | 用分页导出首尾拼接的 JSON 文件测试，确认 rows 全部合并 |
| cURL 解析 | 用 Windows ^ 续行的 cURL 测试，确认 headers/cookie/data 正确提取 |
| 分页获取 | 用小 limit（如 15）测试，确认自动翻页到取完 |

---

## 六、常见报错和排查方向

### 1. `detail 全部 DETAIL_REQUEST_FAILED`
- **原因**：`data/detail_request.curl` 登录态过期
- **解决**：从中核商品详情页 F12 重新抓 detail 接口 cURL，覆盖 `data/detail_request.curl`

### 2. `模型全部 MODEL_UNAVAILABLE`
- **原因**：`data/model.env` 缺失或 KEY 过期
- **解决**：检查 `data/model.env` 是否存在且 3 个变量都填了；确认 KEY 有效

### 3. `SaaS 接口返回 401/403`
- **原因**：SAAS_COOKIE 过期
- **解决**：从 saasportal.iio.com 浏览器 F12 重新抓 Cookie，更新 `自动拉取/.env` 的 SAAS_COOKIE

### 4. `中核接口返回 count=None rows=0`
- **原因**：y1AmuISe 过期
- **解决**：从 cnncmall.com 浏览器 F12 重新抓 y1AmuISe（URL 和 header 两个值都不同！），更新 `.env`

### 5. `招商局建任务失败`
- **原因**：Blade-Auth token 过期
- **解决**：从招商局后台 F12 重新抓 cURL，更新 `auto_crawl_task.py` 的 `_DEFAULT_BLADE_AUTH` 和 `_DEFAULT_COOKIE`

### 6. `zkh 全部 FAILED`
- **原因**：阿里云 WAF 识别 headless
- **解决**：等 30 分钟重试或人工解验证码。**不要绕过**

### 7. `JSON 解析失败`
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
