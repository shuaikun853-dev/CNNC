# 第二阶段设计文档：个人本地版只读数据库查询方案

## 1. 背景与目标

### 1.1 第一阶段已完成

- scope_guard：限制中核范围（tenant_id=188605, commodity_pool_id=222）
- safety_guard：白名单表、写库拦截、task_execute 条件、注释/多语句/OUTFILE 拦截
- sql_selector：参数类型+值安全校验、SQL 渲染、残留占位符/双重引号检测
- knowledge_loader：写库 SQL 脱敏（4 种代码块格式 + HTML 实体）
- 高风险写库意图检测 + 脱敏提示注入
- 244 测试全部通过，P0/P1 封版

### 1.2 第二阶段目标

在**个人本地使用**场景下，增加只读数据库查询能力：

1. 识别诊断场景
2. 选择 SQL 模板
3. 校验参数
4. 渲染 SQL
5. 通过 safety_guard + execution_sql_policy 双重校验
6. 在 21 项执行条件全部满足时，使用只读账号执行 SELECT/WITH
7. 返回查询结果（受行数/列数/字节数限制，敏感字段过滤）
8. 根据查询结果生成诊断结论和人工处理建议

### 1.3 核心原则

- SQL 只能来自 YAML 模板渲染，禁止 raw_sql，禁止模型生成 SQL
- 默认不连接数据库，默认不执行 SQL
- db_readonly_verified=false 时**绝对禁止执行**
- 不编造数据，不自动改库，不自动重推

---

## 2. 个人本地版边界

| 维度 | 边界 |
|---|---|
| 使用场景 | 仅个人本地开发/调试 |
| 网络绑定 | 默认 127.0.0.1，不对外网开放 |
| 用户模型 | 单用户，无 user/role/permission 体系 |
| 访问控制 | LOCAL_EXEC_TOKEN 必须（执行查询时），DB_EXECUTION_CONFIRM 二次确认 |
| 数据库 | 只读账号，个人本地只读副本 |
| 审计 | 简单 JSON 日志，不做防篡改 |
| 限流 | 不做用户级限流，做查询资源限制 |
| 多人使用 | 不支持，需升级为企业版 |

---

## 3. 非目标与禁止项

### 3.1 不做

- 企业级多用户权限管理
- 用户认证体系（OAuth/LDAP/JWT）
- 防篡改审计日志
- 用户级限流
- 自动重推任务
- 自动修改价格指数
- 自动删除数据
- 模型自由生成 SQL 后执行
- 跨租户查询
- 对外网开放服务

### 3.2 硬性禁止

| 禁止项 | 防护层 |
|---|---|
| 用户提交 raw_sql | API 不接受 raw_sql 字段 |
| 模型生成 SQL 后执行 | 不接入 LLM 生成 SQL |
| UPDATE/DELETE/INSERT 等 | safety_guard |
| FOR UPDATE / LOCK 等 | execution_sql_policy |
| SLEEP / BENCHMARK / GET_LOCK | execution_sql_policy |
| INTO OUTFILE / DUMPFILE / LOAD DATA | safety_guard + execution_sql_policy |
| 多语句 | safety_guard |
| 跨租户查询 | scope_guard + safety_guard |
| db_readonly_verified=false 时执行 | 执行条件矩阵 |
| 对外网开放 | LOCAL_ONLY + host 绑定检查 |

---

## 4. 总体架构

```
用户问题 + params + execute + dry_run + token
  │
  ▼
[1] scope_guard ── 越界 → 400 OUT_OF_SCOPE
  │
  ▼
[2] planner → scenario
  │
  ▼
[3] sql_selector → sql_templates（参数校验 + SQL 渲染）
  │
  ▼
[4] safety_guard ── 不通过 → blocked=true
  │
  ▼
[5] 执行条件矩阵（21 项检查）
  │  任一不满足 → effective_execute=false
  │  → 降级为 dry_run，返回 execution_reason
  │
  ▼
  effective_execute=true
  → execution_sql_policy 额外检查
  → readonly_client.execute_readonly
  → 超时/行数/字节数限制
  → 敏感字段过滤
  → query_results
  │
  ▼
[6] result_analyzer → structured_metrics + diagnosis_conclusion
  │
  ▼
[7] audit_logger（JSON 日志，不含敏感信息）
  │
  ▼
[8] DiagnoseResponse
```

---

## 5. 执行条件矩阵

只有**同时满足以下 21 项条件**，才允许真正执行数据库查询：

| # | 条件 | 检查位置 | 失败结果 |
|---|---|---|---|
| 1 | LOCAL_ONLY=true | db_config | effective_execute=false |
| 2 | 服务绑定 127.0.0.1 或 localhost | 启动检查 + /health | degraded + **禁止执行** |
| 3 | execute=true（请求参数） | DiagnoseRequest | effective_execute=false |
| 4 | dry_run=false（请求参数） | DiagnoseRequest | effective_execute=false |
| 5 | db_enabled=true | db_config | effective_execute=false |
| 6 | db_configured=true | db_config | effective_execute=false |
| 7 | db_connected=true | readonly_client | effective_execute=false |
| 8 | db_readonly_verified=true | readonly_client | **绝对禁止执行** |
| 9 | LOCAL_EXEC_TOKEN 已配置且匹配 | 请求 header + db_config | effective_execute=false 或 403 |
| 10 | SQL 来自已登记 YAML 模板 | sql_selector 来源校验 | effective_execute=false |
| 11 | SQL 不是用户 raw_sql | API 无 raw_sql 字段 | N/A |
| 12 | SQL 不是模型生成 SQL | 无 LLM 接入 | N/A |
| 13 | safe=true | sql_selector 输出 | effective_execute=false |
| 14 | blocked=false | sql_selector 输出 | effective_execute=false |
| 15 | pending_params=false | sql_selector 输出 | effective_execute=false |
| 16 | missing_params=[] | sql_selector 输出 | effective_execute=false |
| 17 | invalid_params=[] | sql_selector 输出 | effective_execute=false |
| 18 | SQL 字段非空 | sql_selector 输出 | effective_execute=false |
| 19 | safety_guard.validate_sql 再次通过 | query_executor 二次校验 | effective_execute=false |
| 20 | execution_sql_policy 通过 | query_executor 执行级检查 | effective_execute=false |
| 21 | SQL 条数/长度/超时/行数未超限 | query_executor 资源检查 | effective_execute=false |

### 5.1 条件优先级

- dry_run=true 优先于 execute=true（条件 4 > 条件 3）
- db_readonly_verified=false 绝对禁止，不是仅告警
- token 错误 → 403（在有效请求前拦截）
- 其余不满足 → effective_execute=false, effective_dry_run=true, 返回 execution_reason

### 5.2 execution_reason 示例

| 场景 | execution_reason |
|---|---|
| dry_run=true | "请求指定 dry_run=true，不执行查询" |
| execute=false | "请求未指定 execute=true" |
| db_enabled=false | "数据库未启用（CNNC_DB_ENABLED=false）" |
| db_readonly_verified=false | "只读权限未验证，禁止执行" |
| db_connected=false | "数据库未连接" |
| token 不匹配 | 403 |
| LOCAL_EXEC_TOKEN 未配置 | "LOCAL_EXEC_TOKEN 未配置，禁止执行真实查询" |
| DB_EXECUTION_CONFIRM=false | "执行确认未开启" |
| bind_host 非本地 | "服务未绑定本地地址，禁止执行查询" |
| safety_guard 阻断 | "SQL 安全检查未通过" |
| execution_sql_policy 阻断 | "执行级 SQL 策略未通过: {原因}" |
| pending_params=true | "SQL 模板缺少必要参数" |
| 超限 | "SQL 条数/长度超过限制" |

## 6. 本地访问控制

### 6.1 LOCAL_ONLY 模式

- LOCAL_ONLY=true（默认开启）
- uvicorn 启动时 --host 127.0.0.1
- /health 检查 bind_host，非 127.0.0.1/localhost 则 status=degraded，issue: not_local_bind
- **bind_host 非 127.0.0.1/localhost 时，所有 execute=true 请求强制 effective_execute=false，不允许真实执行数据库查询**

### 6.2 LOCAL_EXEC_TOKEN

- 环境变量 LOCAL_EXEC_TOKEN，**执行真实查询时必须配置**
- dry_run 请求不需要 LOCAL_EXEC_TOKEN
- 只要 execute=true 且要真实执行数据库查询，LOCAL_EXEC_TOKEN 必须已配置且请求携带正确 X-Local-Exec-Token header
- 不匹配 → HTTP 403, error_code=INVALID_TOKEN
- **LOCAL_EXEC_TOKEN 未配置时，所有 execute=true 请求强制 effective_execute=false，effective_dry_run=true**
- execution_reason = "LOCAL_EXEC_TOKEN 未配置，禁止执行真实查询"

### 6.3 DB_EXECUTION_CONFIRM

- 环境变量 DB_EXECUTION_CONFIRM=false（默认）
- 即使其他条件全满足，此开关为 false 时不执行
- 人工显式设为 true 才允许执行
- 物理保险，防止误操作

### 6.4 访问控制流程

```
请求 execute=true
  ├─ LOCAL_EXEC_TOKEN 已配置？
  │   ├─ 否 → effective_execute=false, "LOCAL_EXEC_TOKEN 未配置，禁止执行真实查询"
  │   └─ 是 → 检查 X-Local-Exec-Token header
  │           不匹配 → 403 INVALID_TOKEN
  ├─ DB_EXECUTION_CONFIRM=true？
  │   ├─ 否 → effective_execute=false
  │   └─ 是 → 继续
  ├─ 执行条件矩阵其余检查
  └─ 全部通过 → effective_execute=true
```

### 6.5 生产/多人警告

- /health 返回 local_only=true
- host 非 127.0.0.1 → warning: "非本地绑定，不建议多人/生产使用个人本地版"
- 升级企业版必须：用户认证、RBAC、审计防篡改、限流、加密传输

## 7. 数据库配置与只读验证

### 7.1 配置来源

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| CNNC_DB_ENABLED | false | 数据库总开关 |
| CNNC_DB_HOST | （空） | 只读副本地址 |
| CNNC_DB_PORT | 3306 | 端口 |
| CNNC_DB_NAME | （空） | 库名 |
| CNNC_DB_USER | （空） | 只读账号 |
| CNNC_DB_PASSWORD | （空） | 密码 |
| CNNC_DB_READONLY_VERIFIED | false | 人工确认只读 |
| CNNC_DB_QUERY_TIMEOUT | 10 | 超时秒数 |
| CNNC_DB_MAX_ROWS | 100 | 最大返回行数 |
| CNNC_DB_MAX_QUERIES_PER_REQUEST | 5 | 最大 SQL 条数 |
| CNNC_DB_MAX_SQL_LENGTH | 10000 | SQL 最大长度 |
| LOCAL_ONLY | true | 本地模式 |
| LOCAL_EXEC_TOKEN | （空） | 执行令牌 |
| DB_EXECUTION_CONFIRM | false | 执行确认 |

### 7.2 .env.example

```
CNNC_DB_ENABLED=false
CNNC_DB_HOST=127.0.0.1
CNNC_DB_PORT=3306
CNNC_DB_NAME=saas_onecode_price
CNNC_DB_USER=cnnc_readonly
CNNC_DB_PASSWORD=change_me
CNNC_DB_READONLY_VERIFIED=false
CNNC_DB_QUERY_TIMEOUT=10
CNNC_DB_MAX_ROWS=100
CNNC_DB_MAX_QUERIES_PER_REQUEST=5
CNNC_DB_MAX_SQL_LENGTH=10000
LOCAL_ONLY=true
LOCAL_EXEC_TOKEN=
DB_EXECUTION_CONFIRM=false
```

### 7.3 db_configured 判定

HOST/PORT/NAME/USER 全部非空 → db_configured=true

### 7.4 只读验证

不能只看 SHOW GRANTS 是否含 SELECT，必须检查危险权限。

验证流程：
1. 连接后执行 SHOW GRANTS FOR CURRENT_USER()
2. 允许权限白名单：USAGE、SELECT
3. 检查是否出现以下危险权限：INSERT/UPDATE/DELETE/CREATE/DROP/ALTER/TRUNCATE/REPLACE/EXECUTE/FILE/SUPER/PROCESS/GRANT OPTION/ALL PRIVILEGES
4. 出现任一危险权限 → db_readonly_verified=false
5. 只有白名单权限（USAGE + SELECT）且无危险权限 → db_readonly_verified=true
6. CNNC_DB_READONLY_VERIFIED 环境变量须同时为 true（双重确认）

| SHOW GRANTS | 环境变量 | 最终 db_readonly_verified |
|---|---|---|
| 只有 USAGE + SELECT | true | true |
| 只有 USAGE + SELECT | false | false |
| 含危险权限 | true | false（运行时覆盖） |
| 含危险权限 | false | false |
| 验证失败 | 任意 | false |

db_readonly_verified=false 时绝对禁止执行，不是仅告警。

### 7.5 连接失败处理

| 场景 | db_last_error | 泄漏检查 |
|---|---|---|
| 连接超时 | "数据库连接超时" | 无 IP/密码 |
| 认证失败 | "数据库认证失败" | 无用户名 |
| 库不存在 | "数据库不存在" | 无库名 |
| 网络不可达 | "数据库网络不可达" | 无 IP |
| 权限不足 | "数据库账号权限不足" | 无用户名 |
| 未知异常 | "数据库连接异常" | 无任何信息 |

## 8. SQL 来源校验与执行级 SQL 策略

### 8.1 SQL 来源校验

- SQL 只能由 sql_selector.get_sql_templates() 输出
- query_executor 不接受任意字符串
- API 无 raw_sql 字段
- 无 LLM 接入
- 每条 SQL 关联 template_name

### 8.2 execution_sql_policy

safety_guard 保留不变。执行前增加额外检查：

| 模式 | 正则 | 阻断原因 |
|---|---|---|
| FOR UPDATE | \bFOR\s+UPDATE\b | 锁行 |
| LOCK IN SHARE MODE | \bLOCK\s+IN\s+SHARE\s+MODE\b | 锁行 |
| LOCK TABLES | \bLOCK\s+TABLES\b | 锁表 |
| UNLOCK TABLES | \bUNLOCK\s+TABLES\b | 解锁 |
| SLEEP | \bSLEEP\s*\( | 阻塞 |
| BENCHMARK | \bBENCHMARK\s*\( | 阻塞/DoS |
| GET_LOCK | \bGET_LOCK\s*\( | 锁 |
| RELEASE_LOCK | \bRELEASE_LOCK\s*\( | 锁 |
| IS_FREE_LOCK | \bIS_FREE_LOCK\s*\( | 锁 |
| IS_USED_LOCK | \bIS_USED_LOCK\s*\( | 锁 |
| LOAD_FILE | \bLOAD_FILE\s*\( | 文件读取 |
| INTO OUTFILE | \bINTO\s+OUTFILE\b | 文件写入（safety_guard 已覆盖，执行级再次拒绝） |
| INTO DUMPFILE | \bINTO\s+DUMPFILE\b | 文件写入（safety_guard 已覆盖，执行级再次拒绝） |
| LOAD DATA | \bLOAD\s+DATA\b | 数据导入（safety_guard 已覆盖，执行级再次拒绝） |
| EXPLAIN ANALYZE | \bEXPLAIN\s+ANALYZE\b | 信息泄漏 |
| PROCEDURE ANALYSE | \bPROCEDURE\s+ANALYSE\b | 信息泄漏 |
| 递归 CTE | \bWITH\s+RECURSIVE\b | 资源消耗 |

原则：不替代 safety_guard，是额外防线；解析不可靠时默认拒绝。

### 8.3 检查顺序

sql_selector → safety_guard → execution_sql_policy → readonly_client

## 9. 查询资源限制

| 限制项 | 默认值 | 配置项 |
|---|---|---|
| 单次请求最大 SQL 条数 | 5 | CNNC_DB_MAX_QUERIES_PER_REQUEST |
| 单条 SQL 最大长度 | 10000 字符 | CNNC_DB_MAX_SQL_LENGTH |
| 查询超时 | 10 秒 | CNNC_DB_QUERY_TIMEOUT |
| 最大返回行数 | 100 | CNNC_DB_MAX_ROWS |
| 最大返回列数 | 50 | CNNC_DB_MAX_COLUMNS（可配） |
| 最大响应字节数 | 1MB | CNNC_DB_MAX_RESPONSE_BYTES（可配） |

截断策略：
- 使用 fetchmany(max_rows + 1) 判断是否截断
- 不依赖额外 COUNT 查询
- 不直接拼接 LIMIT，由 YAML 模板声明
- 超时由 asyncio.wait_for 控制
- 截断时 truncated=true, row_count=实际返回行数

## 10. 结果返回与基础脱敏

### 10.1 include_rows 控制

- 请求参数 include_rows=false（默认）
- false → executed_queries 只返回 sql_name/row_count/truncated/elapsed_ms
- true → query_results 返回 columns + rows（受限制）

### 10.2 敏感字段过滤

命中以下规则的字段值替换为 "***"：

| 规则 | 匹配字段名（不区分大小写） |
|---|---|
| 密码 | password, passwd, pwd, secret |
| 令牌 | token, api_key, access_key |
| Cookie | cookie, session_id |
| 手机 | phone, mobile, tel |
| 证件 | id_card, identity, id_number |
| 地址 | address, addr, location |
| 连接串 | connection, conn_str, dsn |
| 凭证 | credential, auth |

未命中规则的字段默认允许返回。

### 10.3 error 字段约束

error 不得包含：SQL 原文、IP、用户名、密码、连接串、本地路径。

### 10.4 result_analyzer 数据使用

- 只使用 structured_metrics/evidence，不直接消费原始 rows
- 如后续接 LLM，只给脱敏 summary + metrics + evidence

## 11. API 契约

### 11.1 POST /diagnose

请求：

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| question | string | 必填 | 用户问题 |
| params | dict | null | SQL 参数 |
| execute | bool | false | 请求执行 |
| dry_run | bool | true | 只预览不执行 |
| include_rows | bool | false | 是否返回行数据 |

Header（execute=true 时必须）：
- X-Local-Exec-Token: {token}

响应增加字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| execute_requested | bool | 用户请求是否 execute=true |
| dry_run_requested | bool | 用户请求是否 dry_run=true |
| effective_execute | bool | 实际是否执行了查询 |
| effective_dry_run | bool | 实际是否为 dry_run |
| execution_reason | string | 未执行原因（空=已执行） |
| db_status | object | 数据库连接状态 |
| executed_queries | list | 精简执行信息 |
| query_results | list | 完整查询结果（include_rows=true） |
| result_summary | string | 结果摘要 |
| structured_metrics | list | 结构化指标 |
| diagnosis_conclusion | string | 诊断结论 |
| manual_action_suggestions | list | 人工处理建议 |

冲突规则：
- dry_run=true 优先级高于 execute=true
- db_enabled=false → 不执行
- db_readonly_verified=false → 不执行
- token 错误 → 403
- DB_EXECUTION_CONFIRM=false → 不执行
- safety_guard/execution_sql_policy 不通过 → 不执行

### 11.2 POST /diagnose/preview

强制：execute=false, dry_run=true, include_rows=false。
只预览 SQL，不执行。

### 11.3 GET /health

增加字段：

| 字段 | 说明 |
|---|---|
| local_only | 是否本地模式 |
| bind_host | 绑定地址（127.0.0.1 或实际） |
| db_configured | 数据库是否已配置 |
| db_enabled | 数据库是否启用 |
| db_connected | 数据库是否连接 |
| db_readonly_verified | 只读权限是否验证 |
| db_last_error | 最近错误（脱敏） |
| execution_enabled | 是否允许执行查询 |
| execution_blockers | 阻止执行的原因列表 |

不泄漏：IP、用户名、密码、连接串、本地路径。

## 12. result_analyzer 设计

### 12.1 输入输出

输入：scenario + query_results(list[QueryResult])
输出：AnalysisResult（structured_metrics + evidence + diagnosis_conclusion + manual_action_suggestions + severity）

### 12.2 各场景指标

| 场景 | 指标 | 阈值 | severity |
|---|---|---|---|
| weekly_check | enabled_rate | <95% | warning |
| weekly_check | collecting_rate(STATUS=0) | >5% | warning |
| weekly_check | task_fail_rate(STATE=3) | >2% | critical |
| single_item_check | STATUS | !=1 | warning |
| single_item_check | collecting_hours | >24h | critical |
| crawler_check | fail_rate | >10% | critical |
| crawler_check | fail_rate | 5-10% | warning |
| task_check | fail_rate | >2% | critical |
| task_check | backlog_count | >100 | critical |
| relation_check | no_relation_rate | >20% | critical |

### 12.3 硬性规则

- 不编造数据
- 空结果说明可能原因
- 不输出写库 SQL
- 不说"已确认一定是"
- 修复建议只含"建议人工核查/人工处理"
- LLM 只用于润色，不生成事实和 SQL

## 13. 审计日志设计

格式：单行 JSON，写入 logs/audit.jsonl

记录字段：request_id, timestamp, scenario, execute_requested, effective_execute, dry_run_requested, effective_dry_run, execution_reason, sql_template_names, safety_guard_result, execution_policy_result, query_elapsed_ms, row_count, truncated, error_code

不记录：数据库密码、连接串、IP、账号、SQL 原文、原始 rows、用户问题全文。

用户问题如需记录：只记 hash 或截断脱敏摘要（前 20 字符 + sha256）。

## 14. 错误码设计

| error_code | HTTP | 说明 |
|---|---|---|
| OUT_OF_SCOPE | 400 | 非中核范围 |
| INVALID_TOKEN | 403 | LOCAL_EXEC_TOKEN 不匹配 |
| INTERNAL_ERROR | 500 | 内部错误 |
| TEMPLATE_ERROR | 500 | 模板加载失败 |
| DB_NOT_ENABLED | 200 | 数据库未启用，降级 dry_run |
| DB_NOT_CONNECTED | 200 | 数据库未连接，降级 dry_run |
| DB_READONLY_NOT_VERIFIED | 200 | 只读权限未验证，禁止执行 |
| DB_EXECUTION_NOT_CONFIRMED | 200 | DB_EXECUTION_CONFIRM=false |
| DB_CONNECT_ERROR | 200 | 连接失败，降级 dry_run |
| DB_QUERY_TIMEOUT | 200 | 查询超时 |
| DB_PERMISSION_DENIED | 200 | 权限不足 |
| SAFETY_GUARD_BLOCKED | 200 | SQL 安全检查未通过 |
| EXECUTION_POLICY_BLOCKED | 200 | 执行级策略未通过 |
| PARAM_VALIDATION_FAILED | 200 | 参数校验失败 |
| QUERY_LIMIT_EXCEEDED | 200 | SQL 条数/长度超限 |
| RESULT_TRUNCATED | 200 | 结果截断（info 级） |

注：200 表示请求本身成功，但查询未执行或部分失败，通过 effective_execute 和 execution_reason 表达。

## 15. 测试方案

### 15.1 执行控制测试

- dry_run=true（默认）不执行数据库
- execute=false 不执行
- execute=true + dry_run=true 不执行（dry_run 优先）
- execute=true + db_enabled=false 不执行
- execute=true + db_readonly_verified=false 绝对不执行
- execute=true + DB_EXECUTION_CONFIRM=false 不执行
- token 不匹配 → 403
- token 匹配 + 其余条件满足 → 执行

### 15.2 安全阻断测试

- 非中核问题 → 400
- safety_guard 不通过 → 不执行
- execution_sql_policy FOR UPDATE → 不执行
- execution_sql_policy SLEEP() → 不执行
- task_execute 无 TENANT_ID → 不执行

### 15.3 资源限制测试

- SQL 条数超 CNNC_DB_MAX_QUERIES_PER_REQUEST → 整个请求不执行，effective_execute=false，effective_dry_run=true，error_code=QUERY_LIMIT_EXCEEDED，execution_reason="SQL 条数超过限制，未执行查询"
- SQL 长度超 10000 → 不执行
- 查询超时 → 结构化返回
- 返回行数超 100 → truncated=true

### 15.4 脱敏测试

- 连接失败不泄漏 IP/密码
- /health 不含密码/连接串
- include_rows=false → 无 rows
- 敏感字段（password 等）→ mask
- error 不含 SQL 原文

### 15.5 result_analyzer 测试

- mock query_results → 正确 diagnosis_conclusion
- 空结果 → 说明可能原因
- 所有建议含"建议人工"

## 16. 分阶段实施计划

### 阶段 2.1：设计文档和接口契约

- 输出：本文档
- 更新 Pydantic 模型（DiagnoseRequest/Response）
- 不写接库代码，不连真实数据库
- 验收：模型定义通过 typecheck

### 阶段 2.2：mock query_executor + mock readonly_client

- 实现 db_config.py（环境变量读取，默认全关）
- 实现 mock readonly_client.py（返回预设数据）
- 实现 query_executor.py（执行条件矩阵 + mock 调用）
- 实现 execution_sql_policy.py
- 验收：dry_run/db_enabled/token/confirm gating 测试全通过

### 阶段 2.3：结果模型、基础字段过滤、mock rows

- 实现 db_result.py（Pydantic 模型）
- 实现敏感字段过滤
- 更新 /diagnose 和 /health
- 验收：include_rows/max_rows/敏感字段测试通过

### 阶段 2.4：result_analyzer 规则分析

- 实现 analyze_results()（5 场景规则）
- 使用 mock query_results
- 验收：result_analyzer 测试通过

### 阶段 2.5：简单 audit_logger

- JSON 日志写入 logs/audit.jsonl
- 不记录敏感信息
- 验收：审计日志测试通过

### 阶段 2.6：Codex 安全复审

- 审查个人本地版安全性
- 修复所有 P0/P1

### 阶段 2.7：真实 readonly_client 骨架

- 默认 CNNC_DB_ENABLED=false
- 不填真实配置
- 连接失败自动关闭
- 只读权限验证

### 阶段 2.8：本地测试库验证

- 使用本地测试库或 mock
- 不连接生产库
- 确认无写库行为

## 17. 未来升级为企业版需补充

| 能力 | 说明 |
|---|---|
| 用户认证 | OAuth2/JWT/LDAP |
| RBAC | 角色+权限管理 |
| 审计防篡改 | 签名日志/外部存储 |
| 限流 | 用户级 QPS/并发控制 |
| 加密传输 | TLS/HTTPS |
| 多租户隔离 | 数据行级隔离 |
| 结果脱敏增强 | 手机号/身份证正则脱敏 |
| 操作审批流 | 写库操作多级审批 |
| LLM 接入 | 仅润色，不生成事实/SQL |
| 容器化部署 | Docker/K8s |
| 监控告警 | Prometheus + Grafana |

## 18. Codex 审查清单

### P0（必须通过）

- [ ] 不允许任何写库 SQL 执行
- [ ] SQL 只能来自 YAML 模板
- [ ] 每条 SQL 执行前 safety_guard + execution_sql_policy 双重校验
- [ ] db_readonly_verified=false 时绝对禁止执行
- [ ] dry_run=true 时绝对不执行
- [ ] db_enabled=false 时绝对不执行
- [ ] DB_EXECUTION_CONFIRM=false 时绝对不执行
- [ ] 不允许用户提交 raw_sql
- [ ] 不允许模型生成 SQL 后执行
- [ ] 不允许自动重推/修改/删除
- [ ] 连接失败不泄漏密码/IP/连接串
- [ ] .env.example 不含真实密码
- [ ] 默认 host=127.0.0.1
- [ ] LOCAL_ONLY=true 默认开启

### P1（应当通过）

- [ ] /health 包含完整数据库状态
- [ ] db_last_error 不含敏感信息
- [ ] execution_reason 说明具体阻断原因
- [ ] 查询超时结构化返回
- [ ] 结果截断提示
- [ ] 敏感字段过滤（password/token 等）
- [ ] error 不含 SQL 原文
- [ ] result_analyzer 不编造数据
- [ ] manual_action_suggestions 只含"建议人工"
- [ ] 审计日志不含密码/IP/SQL 原文/rows
- [ ] execution_sql_policy 禁止锁/阻塞/文件操作
- [ ] 递归 CTE 禁止
- [ ] SQL 条数/长度超限不执行
