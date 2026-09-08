# TESTING.md - 验证指南

> 增量 tick、SQLite checkpoint、finalize/materializer 与历史 identity 回归命令见 [第一步增量调度手册](docs/runbooks/STEP_1_INCREMENTAL_SCHEDULER.md)。

> 自动化测试与生产验收分开。测试不能调用真实 `/add`、审批、推送、SQL 或本地爬虫；只有用户明确要求执行某一步时，才按对应 runbook 做生产验收。

## 1. 环境

- Python 3.11+；当前机器使用 Python 3.14。
- 安装：`python -m pip install -r requirements.txt`
- 项目内主要依赖：FastAPI、Pydantic、PyYAML、pytest、httpx、openpyxl。
- 敏感输入位于被忽略的 `data/*.curl`、`data/*.env`、`data/*.json` 或当前进程环境变量；不得打印其值。
- 运行前设置：`$env:PYTHONIOENCODING='utf-8'`。

## 2. 自动化测试

### 收集和全量

```powershell
cd E:\.A中核运维\cnnc-ops-agent
$env:PYTHONIOENCODING='utf-8'
python -m pytest --collect-only -q
python -m pytest tests -q
```

不要在文档中长期写死测试总数；以收集命令实时输出为准。`StarletteDeprecationWarning` 和本机 requests 依赖兼容警告目前不等于测试失败，但应在升级依赖时单独处理。

### Critical Safety Suite（CI 同款 selector）
- gate 命令 `E:\Python314\python.exe -m pytest tests -m critical_safety -q`；membership 唯一权威 = 测试文件内一行 `pytestmark = pytest.mark.critical_safety`（marker 在 `pytest.ini` 注册，`--strict-markers` 全局生效），本地与 GitHub workflow 用同一 selector，Workflow 不再维护文件清单。新增关键 regression 加该行即进 gate；不能进 CI 的测试必须在 `tests/test_critical_suite_governance.py` 的 `EXCLUSIONS` 登记理由（`PRODUCTION_MACHINE_ONLY`/`NON_CRITICAL_TOOLING`/`HERMETIC_UNGATED`）；守卫拦截未分类新文件、陈旧排除条目、marker 与排除重叠、关键能力哨兵缺失、收集数跌破单调性基线；守卫自身的存在性/可执行性由 workflow 的 "Run governance bootstrap" 步骤直接执行守卫文件保证（self-guard：守卫被整删时该步骤必红）。`PRODUCTION_MACHINE_ONLY` = 依赖生产机真实外部工具（E:\.A2026）或公司机夹具；CI 存在性桩下执行等于 FAKE_GREEN，只在本机全量运行；例外 `test_zhaoshang_priority_boost.py` 已晋升 gated——repo 内 integration 镜像参数在 CI 必测，production 参数仅在外部工具存在时真跑、缺失时参数级 skip。

### 按改动范围

```powershell
# 第一步审计、模型、Excel、cURL 和爬价回填
python -m pytest tests\test_work_01_saas_approval_json_audit.py -q

# SaaS / 招商局 Token 初始化、Rotation、原子保存、并发锁和只读 401 单次重试
python -m pytest tests\test_auth_manager.py -q

# 第一步审计后受控驳回/通过（仅测试替身，不调真实写接口）
python -m pytest tests\test_reject_saas_audit_excel.py tests\test_approve_saas_audit_excel.py -q

# SaaS 文档 ID 受控价格补推（仅测试替身，不调真实写接口）
python -m pytest tests\test_push_saas_commodity_price.py -q --basetemp=.pytest_tmp_push_controlled

# 第二步状态判定和 Excel
python -m pytest tests\test_export_current_monitor_status.py -q

# 第三步授权开关、分阶段并行、恢复模式、回填和任务结果映射
python -m pytest tests\test_monitor_stb_fallback.py tests\test_process_monitor_zs_result_mapping.py tests\test_zhaoshang_task_phases.py tests\test_zhaoshang_resume_modes.py -q

# 招商局当前任务优先级保护
python -m pytest tests\test_zhaoshang_priority_boost.py tests\test_zhaoshang_resume_modes.py -q

# 旧诊断和工作流安全边界
python -m pytest tests\test_safety_guard.py tests\test_scope_guard.py tests\test_workflow_scope_guard.py -q

# 上下文文档体积、链接和必备规则
python -m pytest tests\test_context_documents.py -q
```
### 增量价格审计
```powershell
# Scheduler 路由（白天 15 分钟、17:00-17:50 每 5 分钟 tick、17:55 harvest-only、18:00-20:00 每 5 分钟 closeout、20:35 一次性 safety closeout）、snapshot 180 秒有限重试、公平/post harvest、freeze gate、writer lock、只读 status；simulation 文件另证四 trigger 时间线 + IgnoreNew 重叠丢弃/不堆积/单 writer（2/6/9 分钟 tick、12/20/28/32 分钟 closeout）
python -m pytest tests\test_incremental_audit_operations.py -k "scheduler or scheduled" -q; python -m pytest tests\test_scheduler_frequency_simulation.py -q
python -m pytest tests\test_harvest_only.py -q  # 白天 harvest-only：无 discovery/Detail/Model/submit/finalize、本地 NOOP、全量 sweep、幂等
python -m pytest tests\test_incremental_price_audit.py tests\test_incremental_audit_runtime.py -k "existing_remote_drain or drain_only" -q; python -m pytest tests\test_remote_running_recovery.py -q  # RUNNING 停滞判定/一次性精确重启/RECOVERY_EXHAUSTED 终态（全 fake adapter）
python -m pytest tests\test_daily_audit_store.py tests\test_incremental_audit_runtime.py tests\test_incremental_audit_operations.py -k "process_lock or writer_lock or lock_conflict or lock_status or active_writer or materialization_remains" -q
python -m pytest tests\test_daily_audit_store.py tests\test_cnnc_saas_snapshot_adapter.py tests\test_saas_audit_core.py tests\test_title_match_model_retry.py tests\test_incremental_price_audit.py tests\test_incremental_audit_runtime.py tests\test_harvest_only.py `
  tests\test_incremental_audit_operations.py tests\test_incremental_audit_resilience.py tests\test_incremental_audit_recovery.py tests\test_scheduler_frequency_simulation.py tests\test_partial_exact_harvest.py `
  tests\test_incremental_price_audit_finalize.py tests\test_incremental_production_readiness_audit.py tests\test_incremental_audit_materializer.py tests\test_legacy_zhaoshang_adapter.py `
  tests\test_incremental_price_audit_history.py tests\test_incremental_history_benchmark.py -q
```
历史 fixture 缺失时，最后一项会 skip；不得用生产接口补齐它。
### Dashboard 测试（只读看板）
`tests\test_dashboard_projection.py`（投影/分级/日历/scheduler 降级/fingerprint）+ `tests\test_dashboard_api.py`（GET-only、400/503、ETag/304、自启脚本与 runbook 的 -Apply 门控）：
`E:\Python314\python.exe -m pytest tests\test_dashboard_projection.py tests\test_dashboard_api.py -q --basetemp=runtime\pytest-tmp\dash`
`tests\test_saas_flow_diagnostic.py`（SaaS 链路诊断：SQL registry 只读、降级/缓存/限流、分支分类、18:00 前增长分叉、summary 隔离；全部 fake connection/tmp SQLite，绝不连真实数据库）一并执行，命令追加该文件名即可。
`tests\test_production_identity.py`（生产部署身份只读投影：fake runner + tmp git 迷你仓库，不碰生产 git/调度器/网络）：
`E:\Python314\python.exe -m pytest tests\test_production_identity.py -q --basetemp=runtime\pytest-tmp\prodidentity`
fixture 原则：只用 `tmp_path` 下的临时 SQLite/`daily_status.json` 或 monkeypatch 替身，不碰生产 `runtime\incremental_price_audit`；不注册真实计划任务，自启用例只读文本、从不执行 `-Apply`。
### 编译检查

```powershell
python -m py_compile `
  scripts\audit_saas_excel_file.py `
  scripts\export_current_monitor_status.py `
  scripts\process_monitor_no_push_zhaoshang.py `
  scripts\reject_saas_audit_excel.py `
  scripts\approve_saas_audit_excel.py `
  scripts\push_saas_commodity_price.py `
  scripts\auth_manager.py `
  scripts\curl_utils.py
```

只有修改外部招商局项目时才编译其脚本；不要为了查看帮助而创建真实任务。

## 3. 文档与交接验证

只改文档时，至少运行：

```powershell
python -m pytest tests\test_context_documents.py -q
git diff --check
git diff -- AGENTS.md CURRENT_STATE.md TESTING.md README.md docs\runbooks tests\test_context_documents.py docs\superpowers

$secretPatterns = @(
  ('x-access-token=' + 'eyJ'),
  ('auth-token=' + 'eyJ'),
  ('Bearer ' + 'eyJ'),
  ('sk-' + '[A-Za-z0-9]{20,}'),
  ('password' + '\s*=\s*[^<]')
)
Select-String -Path AGENTS.md,CURRENT_STATE.md,TESTING.md,README.md,docs\runbooks\*.md -Pattern $secretPatterns

Get-ChildItem runtime -Recurse -Filter BATCH_STATE.md | Select-String -Pattern $secretPatterns
```

验收：

1. `AGENTS.md` 仍是短总规则，没有重新复制三步全文或运行历史。
2. `CURRENT_STATE.md` 只描述并行发现和交接协议，不保存唯一当前批次。
3. 每个活跃批次只更新自身运行目录的 `BATCH_STATE.md`；多个步骤及同一步多个批次可以同时为 `ACTIVE`。
4. 其他批次状态不构成当前批次的启动门；约 20/30 轮只提醒和准备交接，不拒绝继续执行。
5. 三个 runbook 的输入、授权门、暂停条件和输出与当前代码一致。
6. `README.md` 只承担项目概览和快速入口。
7. 没有真实密钥、Cookie、Token、账号、密码或连接串。

## 4. 第一步验收

权威流程：`docs/runbooks/STEP_1_PRICE_AUDIT.md`。

### 运行前

- 本轮批次目录为新目录，目录内 `BATCH_STATE.md` 为 `ACTIVE / STEP_1`；其他批次状态不影响本轮。
- 五份 cURL 均由 `scripts/curl_utils.py` 本地解析；没有通过 `/add` 探测认证。
- `data/auth_state.json` 不存在时由 `scripts/auth_manager.py` 一次性从 cURL 初始化 SaaS/招商局两组 Token；存在后不再从 cURL 读取运行时 Token。
- refresh 同时返回并原子保存新的 accessToken、refreshToken；日志不含完整 Token。招商局 `/add` 只在调用前刷新，任何异常都不自动重试创建。
- SaaS 与中核快照都满足 `实际行数 == count`，并已保存 `fetch_summary.json`。
- 模型参数使用稳定值：批大小 10、并发 1。
- 已通过既有受控加载器从 `data/ZHAOSHANG_DB.env` 向当前进程加载完整 `ZHAOSHANG_DB_*`，但日志不显示值。
- 初始命令没有 `--enable-crawler` 或 `--auto-stbchina-fallback`，除非用户已明确授权。

### 结果

- 最终 Excel 有 `summary`、`record_results`，记录数等于 SaaS 快照总数。
- detail 状态区分成功、单参考价、无参考价、请求失败。
- 模型没有把 HTTP/超时/解析错误伪装成“人工复核”。
- 每个招商局任务都有 taskId/taskNumber、链接数、导出状态和提优结论。
- 招商局单商城模板必须有商品编码；URL 可空，仅 URL 无商品编码必须拒绝；有 URL 时商城分组以域名为准。
- “已提优”都有 `affected_rows>0`；已按精确主键范围确认全为 `-1` 的幂等状态单独统计且不冒充本次提优。`task_status=00/01/02` 都不影响 priority 更新资格，完成任务也必须回读确认全部精确有效行均为 `-1`。
- 同一 taskNumber 可对应多个商城；测试必须证明以配置 mall code、taskNumber、marker create_time 窗口定位子任务，并且 UPDATE 只含目标 `task_id` 主键。
- 页面价 0 使用 `PRICE_IS_ZERO`/“价格为0”并整行高亮。
- 任意后补价格都重新计算 `page_vs_customer_check`、`price_changed`、`over_30pct`、`final_decision`。
- 缺价时先有清单和授权门，未授权不启动本地脚本。
- 驳回和通过都不是第一步默认动作；只有指定最终 Excel、颜色范围和对应动作后的二次授权才能使用受控入口。两者都必须有候选数、清单 SHA-256、写前待审核对、逐批 `rspCode=0000` 证据和写后目标无遗留；驳回固定使用 `data/sass-bohui.curl`，`--fill-colors yellow-only --require-customer-price` 必须只选 F 列黄色且表头“客户价”非空的行；通过固定使用 `data/sass-tongguo.curl` 并排除 F 列红/黄色行，`--require-customer-price` 必须再排除表头“客户价”的空值和空白字符串，数值 0 视为非空。

重点回归：

```powershell
python -m pytest tests\test_work_01_saas_approval_json_audit.py -q -k "Zhaoshang or zero_price or model or curl or fallback"
python -m pytest tests\test_zhaoshang_priority_boost.py -q
```

## 5. 第二步验收

权威流程：`docs/runbooks/STEP_2_MONITOR_STATUS.md`。

先验证帮助不会发网络请求：

```powershell
python scripts\export_current_monitor_status.py --help
python -m pytest tests\test_export_current_monitor_status.py -q
```

生产运行只有用户要求第二步时才执行：

```powershell
python scripts\export_current_monitor_status.py
```

验收：

1. 中核实际行数等于接口 `count`；SaaS 每批最多 200 个编码且每批成功。
2. `saas_matched + saas_missing = cnnc_total`。
3. `pushable + needs_processing + saas_missing = cnnc_total`。
4. 五个固定 Sheet 存在，全量表保留原始 `newPrice`。
5. 可推送条件严格为 `isSync=1`、`isSyncStandard=1`、`syncPriceStatus!=5`、`newPrice` 为有限正数。
6. `syncPriceStatus=5` 优先归为价格检测校验失败；其他状态继续校验 `newPrice`。
7. SaaS 只读分批明确返回 401 时只刷新并重试一次；第二次失败或任一分批异常时没有生成“全量”输出。

## 6. 第三步验收

权威流程：`docs/runbooks/STEP_3_NO_PUSH_PROCESSING.md`。

### 生产结果

- 只处理指定监控表中 `是否可推送=否`；没有混入“是”。
- 每个否记录有 detail 状态，`>=2` 与 `0/1` 分流可以按记录对账。
- 基础三文件和待确认清单写入同一新批次目录，不覆盖输入或历史文件。
- 招商局先顺序完成全部 add、启用和提优，再以 `--zs-wait-concurrency` 有界并发等待/导出；并发 worker 没有写操作。
- 每商城都有精确 task_name、taskNumber、mall code 和提优证据；断点重跑复用同批次已有任务或有效导出，不重复 add、启用或提优。
- `BATCH_STATE.md` 在 `/add` 前已有 `ADD_INTENT`，并逐次记录 taskId、启用、提优和导出；已有 add 事件但查无任务时 fail-closed。
- 导出使用临时 XLSX 原子替换；复用前表头有效且输入/结果行数一致，重复商品编码不造成误判。
- 没有价格的行不填商城标题。
- `--skip-zhaoshang` 模式没有 add、启用、SQL 或脚本日志。
- `--auto-stbchina-fallback` 只处理仍缺价的 `stbchina.cn` URL，且必须有本批次授权。
- `--detail-reference-fallback` 只补同 `documentId + 完整 URL` 且价格、标题同时为空的行，来源标记 `中核detail参考回填`。

## 7. SaaS 受控价格补推验收
权威流程：`docs/runbooks/SAAS_CONTROLLED_PUSH.md`。
- ID 文件只接受 1 至 500 个不重复纯数字文档 ID；预览不得刷新认证或调用推送，并须将每个 ID 唯一映射到关系 `id`。
- 预览锁定 `push_candidate_count` 和包含状态、关系 `id`、人工跳过动作的 `execution_plan_sha256`；执行参数、保存的 manifest 和刷新认证后的复查必须完全一致。
- 已手工发送或结果不确定的关系 `id` 必须标记 `SKIP_MANUAL_ATTEMPT`；每项写请求无自动重试，任一失败停止后续项目。
- 成功项逐条 fsync；已有事件或汇总文件时拒绝整批重跑。只有汇总 `COMPLETED` 且确认数等于候选数才能报告成功。
## 8. 常见阻塞

| 症状 | 先检查 | 处理边界 |
|---|---|---|
| `rspCode=3333 / 请重新登录` | `data/auth_state.json` 是否完整、refresh 是否失败、代理是否已清除 | refreshToken 失效则暂停并要求人工登录；不得回退旧 cURL |
| detail 全失败 | cURL 请求体是 `documentId+fourCategoryId` 或受支持的 standard detail 形式 | 更新登录态后重跑；不用列表价冒充 |
| 模型持续失败 | 三个模型变量、无代理 opener、批大小 10/并发 1 | 保持重试或暂停；不降级成业务“人工复核” |
| `/add` 失败 | 调用前 refresh 证据、payload、模板字段 | 输出完整脱敏错误并暂停；请求发出后绝不自动重试 `/add` |
| 优先级 0 行 | 精确 task_name、taskNumber、mall code、marker 窗口、payload 有效子任务数、全量 `weight_type` 分布 | 轮询数量必须等于 payload 数；预查已全为 -1 则记录幂等；否则 0 行或回读不全为 -1 都暂停，不按 `task_status` 跳过且不放宽 SQL |
| 导出无价格 | 任务完成状态、export 返回、结果表头和 URL/SKUID 映射 | 生成缺价清单；未经授权不跑脚本 |
| Excel `PermissionError` | Excel/WPS 是否占用 | 关闭后重跑或使用新文件；不覆盖旧批次 |
| 403/405/验证码 | 商城登录/风控状态 | 立即停止，不规避 |

## 9. FastAPI 旧服务
仅修改旧诊断或 workflow API 时使用：

```powershell
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
python -m pytest tests\test_diagnose_integration.py tests\test_workflow_api.py -q
```

健康检查：`GET http://127.0.0.1:8000/health`。旧 `/diagnose` 只返回模板，不执行 SQL。

## 10. 生产就绪只读检查

一行命令：`python scripts\production_readiness_check.py --json`（退出码 0=READY、1=NOT READY、2=意外错误）；只读不注册任务/不连库/不刷新认证/不发网络请求，详见 [docs/runbooks/PRODUCTION_READINESS.md](docs/runbooks/PRODUCTION_READINESS.md)，测试 `tests\test_production_readiness.py`。
