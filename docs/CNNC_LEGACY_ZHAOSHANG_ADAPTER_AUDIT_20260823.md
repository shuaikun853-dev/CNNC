# CNNC Legacy Zhaoshang Adapter Reliability Audit

- 日期：2026-08-23
- 审计对象：`app/services/legacy_zhaoshang_adapter.py`（HEAD `77043e6`，2147 行，63 函数）及其直接依赖/调用方（外部工具 `E:\.A2026\招商局自动建任务\external_tools\crawl_task_tool\auto_crawl_task.py`、`zhaoshang_priority_boost.py`、`scripts/auth_manager.py`、`scripts/zhaoshang_mall_mapping.py`、`scripts/curl_utils.py`、runtime 的 `legacy_zhaoshang_bridge` 与 recovery 各入口）
- 审计性质：只读。未修改任何生产代码/测试，未 commit，未调用生产 API。
- 基线更正（委托方指示，已采纳）：**"5 行 finalize → 629 行"事件（2026-08-20）不是系统误判 Bug**——爬虫停更期间操作者知情后人工决定按当时数据收尾，爬虫恢复后数据才增长。本报告及历史证据均不计入 freeze/finalize Bug。
- 历史压缩说明：该文件 git 史仅 5 个 commit（df96f56 08-12 巨型 checkpoint 创建 → 7248bce 08-18 checkpoint(+1289/−90，含 08-13~08-17 全部演化) → 08-19 三连修 7da7c04/85d9e63/5cf8ccf）。08-13/08-14 事故的修复被压缩在 7248bce 内，本报告通过 `git diff df96f56 7248bce` 的删除行逐行核验了修复前的代码形态。

---

## 1. Executive Conclusion

- **历史真实 Bug（ORIGIN 在本模块）**：3 起 CONFIRMED（Z-1 08-13 整表验收门过严、Z-2 08-14 all-or-nothing 部分导出卡死、Z-4 08-19 probe 用错 batchNumber + count 缺失崩）+ 1 起 PARTIAL（Z-3 08-19 zero-rows：主因外部远端，adapter 侧 window 刚性 + bare 类型名诊断为共同起源）。
- **Hotspot Audit "4 起 origin + 3 起风暴共同起源"复核结论**：PARTIALLY CONFIRMED——3 起完全成立、1 起部分成立；所谓"3 起风暴共同起源"中只有 Z-3 真有 adapter 侧起源代码，B-17（wait-export MANUAL）与 B-18（export no-price 终态缺失）的 origin 在 runtime 状态机/recovery 缺口，adapter 是修复落点而非起源。
- **当前 HEAD 77043e6 确认潜伏 Bug：2 个**（L-2a/L-2b：submit 把"从未启动的发送"与"启动后未知"混同/放走 auth 异常，均 fail-closed 方向、无双发风险，但分类语义错误且零测试覆盖；L-1：`Sequence` 注解未导入，annotation-only 无运行时影响）。另有 1 处死代码（~188 行重复分类器）与 5 项边界设计风险。
- **双发（DOUBLE SEND）可能性：未发现任何路径**（FACT）。三层防护闭合：外部工具 `/add` 单次 + ADD_INTENT 先落 + 已有 ADD_ 事件拒再建；adapter submit 以 CREATED+ENABLED 证据终判、超时→SUBMIT_UNKNOWN 永不重发；runtime claim-before-send（post_attempted 先烧）。
- **最值得下一步做的不是拆文件**：是给 probe 的畸形响应分支补 golden fixtures、类型化四个任务标识符、收敛 diagnostic 字符串的双重职责。结构拆分应最后做（复用 Phase 1A playbook）。
- **策略推荐（第 17 节问题）**：OPTION 1——先修 2 个确认小 Bug（test-first），再做 behavior-preserving hardening，拆分最后。禁止因 2147 行直接重构。

---

## 2. Module Responsibility Map

| 职责 | 代码位置 | 说明 |
|---|---|---|
| A. 模板生成/发布 | `_write_or_verify_template` :489-533, `_verify_existing_template` :474-487, `prepare_job` :535-584 | 临时文件+fsync+os.replace（PermissionError 重试×5）+发布后回读校验；payload_sha256 嵌入 workbook description 作为 manifest |
| B. 外部工具加载 | `_load_module` :821-833；`_default_reconciler` :1051-1078（不注册 sys.modules） | importlib spec 加载；4 个固定名注册 sys.modules（见 §12） |
| C. 环境/子进程构造 | `_production_runner` :789-814, `_create_args` :760-772, `_harvest_args` :774-787, `_temporary_environment` :337-349, `internal_subprocess_env`（curl_utils:175-180） | refresh-before-spawn；代理剥离；TITLE_MATCH_MODEL_* 剥离；cwd=工具目录 |
| D. Submit | `submit` :934-975 | create-only 子进程 + 证据终判（见 §5） |
| E. Reconcile unknown | `reconcile_unknown` :1080-1165 + `_remote_takeover_conflict` :1001-1036, `_remote_is_enabled` :1038-1049, `_one_reconciled_remote` :983-999 | 只读查询 + 精确接管（ADD_INTENT 单标记 → 写 CREATED/ENABLED recovered_from=SUBMIT_UNKNOWN_EXACT_LOOKUP） |
| F. Priority | `prioritize` :1167-1225 + `_production_priority_runner` :835-932 | 幂等证据重放 + 外部 MySQL boost（lock→UPDATE→commit→readback） |
| G. Harvest/export | `harvest` :2119-2147, `harvest_existing` :1668-1715 | wait-export-only 子进程；已存在文件零远端调用 |
| H. 既有导出验收 | `_validate_result_workbook` :1470-1666 | provenance 必需（执行编号/批次号/商城标识）+ 外批过滤 + 严格全量 Counter 相等 |
| I. Partial exact harvest | `classify_existing_export_partial` :1923-2117 | 执行编号前缀 taskNumber- 校验 + code/url 匹配 + ambiguous/exact_absent 分类 |
| J. Export-no-price probe | `probe_export_no_price` :1326-1468 | 只读四连查（name→identity→status→progress→export） |
| K. URL 规范化/匹配 | `_matches_legacy_export_url` :1235-1284 | 精确相等 + 3 条白名单重写（见 §10） |
| L. Task identity | `CrawlRequest.identity` :209-211, `exact_export_remote_identity` :1286-1307, `exact_priority_remote_identity` :1309-1324 | (mall_key, mall_source_id, sku_id, exact_url, backend_version)；task_name 生成在 runtime（fingerprint 14 位后缀，日内稳定跨日滚动） |
| M. 批次证据 | `_read_batch_events` :586-603, `_priority_evidence` :604-638, `_submission_evidence` :640-666, `_submission_marker_time` :668-695, `_submission_intent_marker` :698-727 | BATCH_STATE.md 追加式 JSONL 事件 = 全部终判依据 |
| N. 认证刷新 | 委托 `AuthManager.export_environment("zhaoshang", refresh=True)` :795, :1063, :1359 | refresh 失败→异常先于任何发送（§5） |
| O. 重试/错误分类 | :85-156（retryable/not-materialized 分类） | frozenset 常量 + 消息前缀锚定 |
| P. 远端结果解析 | 委托 `zhaoshang_result_adapter.read_zhaoshang_result_xlsx(_by_identity)` :1630-1650 | 列头别名集 :73-84 |

调用链（Caller → API → 内部职责 → 副作用 → 证据 → 错误映射 → 恢复去向）：

```
tick 工作流 submit 阶段(workflows:640-690, gate→claim_remote_submission 烧 post_attempted)
  → bridge submit(runtime:1209) → adapter.submit → create-only 子进程[/add 单次]
  → 副作用:远端建任务;本地 ADD_INTENT/CREATED/ENABLED 事件(工具写)
  → 证据:CREATED==ENABLED 单一身份(+可选 priority 证据)
  → 映射:SUBMITTED / SUBMIT_UNKNOWN / NOT_ATTEMPTED
  → SUBMIT_UNKNOWN → reconcile_unknown(精确查询/接管) → FOUND→PRIORITY_PENDING / NOT_FOUND→保持 / MANUAL
PRIORITY_PENDING(tick 后续或 recovery/priority.py) → adapter.prioritize → MySQL UPDATE(-1)
  → 证据:PRIORITY_UPDATED(affected_rows>0)/ALREADY_HIGHEST/PRIORITY_UNCONFIRMED(retryable|remote_stall)
RUNNING/EXPORTED(harvest sweep 或 recovery/harvest.py,wait_export.py) → adapter.harvest
  → wait-export-only 子进程[export/download;exportTask 工具内重试≤5]
  → 证据:EXPORTED 事件+result workbook;本地验证(provenance/partial)
MANUAL_REQUIRED(精确恢复) → recovery/no_result.py → adapter.probe_export_no_price(只读四连查)
```

---

## 3. Historical Bug Ledger（本模块 ORIGIN 复核）

| ID | 日期 | Commit | Symptom | Root cause（改前代码实证） | 改动函数 | 回归测试 | 生产影响 | 证据强度 |
|---|---|---|---|---|---|---|---|---|
| Z-1 | 08-13 | (修复压缩于 7248bce) | 远端已完成但本地 harvest 大面积 `REMOTE_EXISTING_HARVEST_MANUAL`（jd 240 行等）、FinalizeBlocked、36 manual | df96f56 版 `_validate_result_workbook`（diff 删除行 875-1101）：无 provenance 列要求，**全表行与期望身份 Counter 严格相等**——任何外批行/部分行/多一行 → 整工作簿 invalid → MANUAL | 重写为 provenance 必需 + 外批过滤 + `exact_export_remote_identity` 证据 + `harvest_existing` 分流 | `test_harvest_filters_foreign_batch_before_exact_identity_validation` 等 10+ | 全天 finalize 阻塞（2026-08-13 BATCH_STATE.md:151,238,297） | HIGH（diff+BATCH_STATE+docs 三源） |
| Z-2 | 08-14 | (同上) | 部分导出（缺行/SKU 列不一致/AF0620700 fanout 歧义）→ 任务卡 MANUAL_REQUIRED | 同一 all-or-nothing 门：部分合法导出无出口 | 新增 `classify_existing_export_partial`（执行编号前缀、ambiguous/exact_absent） | `test_partial_exact_harvest_*`（含 273/272、58/55/3 真实事故形状）、`test_recovery_fanout_gate` | 任务卡死/manual 残留（spec 2026-08-14 + BATCH_STATE:207-211） | HIGH |
| Z-3 | 08-19 | 7da7c04 | 远端 subtask 未物化 → zero-rows 停滞（job71/72），且本地无法区分零行 vs 校验失败 | ① priority 窗口上界硬编码 marker+15min（物化晚于 15min 永久不可达）；② 外部抛 bare `PriorityUpdateZeroRowsError` 类型名，无法分类 | `window_end = max(marker+15m, now+30s)` :866-869；`remote_not_materialized_diagnostic` :141-156（消息前缀锚定） | `test_production_priority_runner_uses_exact_identity_and_persists_verified_event`（断言 window_end 拉伸）、`test_late_materialized_subtask_...`、`test_remote_stall_classification.py` 全套 | 08-19 风暴核心之一（主因外部远端；adapter 侧为共同起源） | HIGH（adapter 侧）/归属 **PARTIAL** |
| Z-4 | 08-19 | 85d9e63 引入（22:46）→ 5cf8ccf 修复（22:52） | 新 probe 查进度 404/无效 + 无价格应答缺 count 字段 → 解析崩 | ① 进度端点键是任务记录的 batchNumber 而非 RW-taskNumber（新代码用错标识符）；② 无价格精确应答可省略 count | `batch_number = _text(task.get("batchNumber")) or resolved_task_name` :1399；count None → 0 仅当 data 空 :1446-1454 | `test_production_probe_queries_progress_by_task_batch_number`（断言 params 精确）、`test_production_probe_accepts_missing_count_only_with_empty_data` | 当日恢复通道受阻 6 分钟即修 | HIGH |

**非本模块起源的关联事件（复核后排除）**：
- B-17 job54 wait-export MANUAL 卡死：origin = runtime 有界等待上限 + 无恢复路径（状态机缺口）；adapter 的 wait-export 语义只是边界条件。e6fb943 修复在 recovery。
- B-18 export no-price 无终态：origin = 状态机缺口；adapter 在修复中获得新能力 probe（FIX location，非 origin）。
- B-07（06-26 任务名秒级碰撞）：scripts 时代（本文件 08-12 才诞生）；当前指纹命名在 runtime 且有 `test_legacy_task_name_is_stable_within_day_but_rolls_over_across_days` 钉死。

**Hotspot 结论复核表**：

| Hotspot 声称 | 复核 |
|---|---|
| "验收门过严 ×2" | **CONFIRMED**（Z-1、Z-2，改前代码逐行实证） |
| "probe 使用错误 batchNumber" | **CONFIRMED**（Z-4①，5cf8ccf diff + 现测试断言） |
| "count 缺失" | **CONFIRMED**（Z-4②，同 commit） |
| "4 起明确 origin" | **PARTIALLY CONFIRMED**：3 全 + 1 部分（Z-3） |
| "08-19 另有 3 起共同起源" | **PARTIALLY CONFIRMED**：仅 Z-3 有 adapter 侧起源代码；B-17/18 非 adapter |

---

## 4. 08-19 Incident Cluster（adapter 视角）

时间线：7248bce(08-18) → eae86fb 白天 harvest(08-19 11:01) → **7da7c04 18:28**（Z-3 修复 + stall_breaker + zero-rows recovery）→ b3f2bd0 20:00（recovery 门 fanout 误判，非本模块）→ e6fb943 21:01（wait-export recovery，非本模块）→ **85d9e63 22:46**（新增 probe）→ **5cf8ccf 22:52**（Z-4，修自己 6 分钟前引入的代码）→ 06e2c6a(08-20)。

FACT：本模块在风暴中承担的是**检测与分类职责**（诊断分类 → stall_breaker 证据 → 恢复入口活探测）。风暴 6 个 fix 中本模块被 3 个触及（7da7c04 分类+窗口、85d9e63 新增 probe、5cf8ccf 修 probe）。Z-4 的"出生 6 分钟即修"是高压新增能力代码无 golden fixture 的直接后果——与 B-16/B-20（recovery 同类）同构。

---

## 5. Submit Safety

- `/add` 何时真正发送：仅 `submit()` :941 经 `_create_args`（--create-only）spawn 外部工具子进程；工具内单次 POST（timeout=180，tool:434-448），POST 前写 ADD_INTENT 事件并预查 `check_existing_task`，已有 ADD_ 事件即拒绝再建（tool:120-139, 1000-1003）。FACT。
- durable intent 先于 send：是——三层：工具内 ADD_INTENT（先于 POST）；runtime `claim_remote_submission` 先烧 post_attempted（workflows:661, store:1108-1116）；adapter 侧 `_submission_intent_marker` :698-727 要求接管时恰有一个 ADD_INTENT。FACT。
- timeout 后是否可能重发：否。工具超时→异常→非零退出；adapter → SUBMIT_UNKNOWN（:971-972）；重发决策权在 reconcile/恢复，`test_submit_timeout_is_unknown_and_never_retried`、`test_submit_connection_reset_then_reconcile_retry_never_posts_twice` 钉死。FACT。
- submitter/工具内部 retry：adapter 无重试循环；工具 /add 单次、enable 单次、priority UPDATE 单发（rowcount 严格校验）；唯 exportTask 工具内重试≤5（读性质，见 §8）。FACT。
- HTTP/业务错误码分类：工具内 `success is False or rspCode not in (None,"0000")` → RuntimeError（非零退出）；唯一业务码豁免 = enable 的 5555"已启用"幂等分支（tool:600-602，刻意窄豁免）。legacy 路径的 401/429/503 fail-closed 无库内测试（batch 后端有）——见 §13 缺口。FACT。
- 发送成功但响应丢失：→ SUBMIT_UNKNOWN → reconcile 精确查询（taskName+mall）→ 唯一匹配且 enabled 且身份一致 → 接管 FOUND；多匹配/冲突 → MANUAL。`test_exact_remote_lookup_recovers_post_success_response_timeout`。FACT。
- **DOUBLE SEND POSSIBILITY：未发现**（结论见 §1）。FACT。
- 缺陷（潜伏，详见 §14）：**从未启动的发送被归类为"未知"**——L-2a（OSError 含工具缺失 FileNotFoundError :790-791 → "completion is unknown" :942-943 → SUBMIT_UNKNOWN）与 L-2b（auth refresh 异常 AuthError/RefreshTokenExpiredError 不在捕获集 → 直接逃逸出 submit → bridge → workflow 兜底烧 claim 记 SUBMIT_UNKNOWN）。对比：harvest 正确区分 OSError→"could not be started" MANUAL（:2138-2139）——submit 的不对称是疏漏证据。均 fail-closed（无双发），但语义错 + 运维重（人为制造需对账的 UNKNOWN）。

**Auth refresh**：refresh-before-send 覆盖全部三个真实写/读路径（子进程 :793-795、reconciler :1062-1066、probe :1357-1362）；refresh 失败→异常先于 spawn，不存在"refresh 失败仍 send"；写路径无 401 自动重试（401-retry-once 仅存在于 AuthManager.request_json 且 read_only=True）。FACT。

---

## 6. Reconcile Safety

- 只读查询按 (task_name, mall_code) 精确匹配（工具 queryListByPage 按 taskName 过滤 + 本地 name+mall 精确单匹配，多匹配 fail-closed）。FACT。
- `_BoundedReadOnlySession` 将所有读请求钳制在单一单调 deadline（:287-325）。FACT。
- 接管条件（全部满足才写证据）：远端唯一匹配 + taskName/mall/subtask-count 无冲突（:1001-1036）+ taskId/taskNumber 完整 + 既有事件身份一致或仅有部分身份一致 + **远端已 enabled**（:1123-1127）+ 恰一个 ADD_INTENT 标记（计数匹配 + 时间戳合法）→ 写 CREATED/ENABLED（recovered_from=SUBMIT_UNKNOWN_EXACT_LOOKUP）→ 回读验证。FACT。
- 任何异常分层：证据冲突→MANUAL；工具不可用→MANUAL；其余一切（读超时/auth 竞态）→NOT_FOUND"not yet conclusive"（保持 durable unknown 直至外层上限）。FACT（:1088-1096）。
- 接管写失败 → NOT_FOUND"could not be written"（:1155-1159），`test_reconcile_unknown_does_not_adopt_when_evidence_write_fails`。

---

## 7. Priority Safety

- 身份精确性：task_name + task_number + mall_code（由 mall_key 映射）+ marker create_time 窗口（−2min 起）；SQL 仅按主键 UPDATE 且 FORCE INDEX；每步身份再校验（外部 boost `_validate_identity_rows`）。adapter 侧再要求 priority 身份 == CREATED/ENABLED 身份（:1183-1184）。FACT。
- 窗口：window_start=marker−2min；window_end=max(marker+15min, now+30s)（Z-3 修复后）。回归测试断言两侧。FACT。
- 分支处理：0 rows/不完整 → REMOTE_NOT_MATERIALIZED（retryable，驱动 stall_breaker）；身份/UPDATE rowcount/readback 校验失败 → 不可重试 MANUAL 方向；already −1 → 幂等 ALREADY_HIGHEST；MySQL 瞬态码（1040/1053/1203/1205/1206/1213/2002/2003/2004/2006/2013/2055）→ retryable，确定性码（1044/1045/1049/1142/1143/1227/1251）→ 不可重试；**类名相同但非 DB-API 类型不算 retryable**（job60 教训，`test_priority_error_class_name_alone_does_not_make_failure_retryable`）。FACT。
- 事务：lock(FOR UPDATE)→UPDATE 分块（rowcount 严格相等）→事务内验证→commit→提交后 readback 验证→rollback；异常即 rollback+raise。连接 read_timeout 由 adapter 钳制 [30,300]（:879-882）。FACT。
- 残余风险：连接在 lookup 循环中空闲可达 ≤120s → wait_timeout → InterfaceError（已按 retryable 分类，job60/2026-08-20 I-11 实证路径）；write_timeout 固定 30s 不可调。

---

## 8. Harvest / Export Safety

- 下载归属：wait-export-only 子进程按 durable task_name 寻址；导出键 = taskNumber+taskId+mallLogo（非 batchNumber）；本地 result workbook + EXPORTED 事件（含 task_id/task_number/output_path）共同构成证据，`exact_export_remote_identity` :1286-1307 要求 EXPORTED 事件身份 == 提交身份且 output_path 精确匹配（normcase resolve）→ 防 old export 误用。FACT。
- batch number/execution number/mall 对应：验收门强制 批次号==task_name（外批行过滤计数）、执行编号在 partial 路径必须前缀 `{task_number}-`（:1991）、商城标识==mall_key（:1521/1997）。FACT。
- sibling SKU 串结果：三重隔离——durable_identity 的 conflicting_skus 检查（:1544-1554）、SKU 候选列等价类唯一性（:1616-1627）、partial 的 code/url 匹配冲突 → ambiguous（:2032-2038）；`test_harvest_preserves_two_skus_sharing_one_url_as_distinct_results` 等 4 测试。FACT。
- 终态分类：price>0→OK；==0→PRICE_IS_ZERO（0 与缺失严格区分）；成功但无价→PRICE_MISSING；否则 FAILED。FACT。
- 已知边界：URL 键 result_map 对"两 SKU 共享一 URL"先天有歧义——设计上以 `result_by_identity` 为准（:242-244 注释明示 legacy 调用方才用 URL map）。FACT（复杂但正确）。
- 外部工具 export 重试 ≤5 次重发 exportTask（tool:828-856）——读性质副作用，可接受；但无独立 deadline，占用子进程单一外层超时预算（边界风险，见 §12/§14 L-6）。

---

## 9. Probe Safety

- 历史核实："probe 使用错误 batchNumber" **CONFIRMED**（Z-4①）：22:46 引入的代码用 RW-taskNumber 查进度；22:52 修复。当前实现 :1397-1400 用**任务记录的 batchNumber**（`task.get("batchNumber")`，fallback resolved_task_name），与 run_auto_task 的选择镜像（tool:1030），`test_production_probe_queries_progress_by_task_batch_number` 断言请求 params 精确等于 batchNumber。FACT。
- 当前四连查：check_existing_task(resolved_task_name) → 活身份必须等于 durable (task_id, task_number)（:1385-1388）→ query_task_status(taskId) → query_task_progress(batchNumber) → exportTask POST(taskNumber+taskId+mallLogo)。每个端点键不同——这是协议现实，不是 bug，但正是 Z-4 的温床（§15 C1）。
- 字段混淆残余风险：batchNumber 三处默认等于 task_name（tool:401、tool:1030、adapter:1399 fallback）——当前一致；若未来远端让两者分叉，进度查询将静默错键。HYPOTHESIS（低概率）/DESIGN RISK。
- 响应判定链（:1416-1461）：workbook 嗅探（content-type/disposition/PK 魔数）→ 拒绝；JSON 内嵌下载 URL（url/fileUrl/downloadUrl/exportUrl 或字符串含 .xlsx/download/export）→ 拒绝；count None → 0 仅当 data 空；count 不可解析 → 拒绝。**这些畸形分支零测试覆盖**（§13 缺口 1，B-19 同类）。
- auth：refresh 先于 session；BLADE_AUTH+COOKIE 缺一 → 拒绝（:1363-1370，该分支亦无测试）。

---

## 10. Identity & URL Rules

1. normalization 目的：**仅容错（把导出重写 URL 映射回 durable URL），不参与身份构造**。durable 身份五元组 (mall_key, mall_source_id, sku_id, exact_url, backend_version) 永不规范化。FACT。
2. 过宽匹配：未发现。白名单仅 3 条重写（:1235-1284）：qstbg `/gd/detail.do?goods_id=X&formid=searchform` ↔ `/gd/initParameter.do#title_detail?goods_id=X&formid=form1`（要求双方 query 键唯一且参数集精确）；jd `i-item.jd.com` ↔ `item.jd.com`（同商品号、无 query/fragment）；`/detail/inner` 后缀剥离（其余分量全等）。此外仅精确相等。FACT。
3. 不同商品同身份：不可能经由 normalization 达成——canonical 匹配仅在 SKU 唯一且无跨 SKU 冲突时启用（conflicting_skus 检查 :1544-1554）。FACT。
4. URL 同 SKU 不同 / SKU 同 URL 不同：前者由 SKU 唯一 URL 回退处理；后者两身份并存（result_by_identity 分立，URL map 仅 legacy 用）。均有测试。
5. fallback 精确顺序：精确 (sku,url) ∈ expected → SKU 唯一 URL canonical 匹配 → SKU 回退（该 SKU 恰一 URL）→ URL 回退（该 URL 恰一 SKU）→ 报错。ambiguous 一律 ValueError/ambiguous。FACT。
6. sibling 隔离保护测试：`test_harvest_preserves_two_skus_sharing_one_url_as_distinct_results`、`test_harvest_rejects_sku_candidate_that_conflicts_with_another_exact_url`、`test_harvest_rejects_matching_sku_with_unrelated_export_url`、`test_partial_exact_absence_does_not_borrow_sibling_sku_price`、`test_no_result_recovery_uses_exact_completed_export_without_borrowing_sibling_price`。FACT。

---

## 11. Error Classification

| 分类 | 产生位置 | 载体 | 消费方 |
|---|---|---|---|
| retryable（TimeoutError/ConnectionError/InterfaceError/OperationalError[白名单码]） | `retryable_priority_error_diagnostic` :94-110 | frozenset 字符串 | runtime PRIORITY_PENDING 重试、stall_breaker 排除项、recovery 校验 |
| REMOTE_NOT_MATERIALIZED（NotMaterialized/Incomplete） | `remote_not_materialized_diagnostic` :141-156 | 消息前缀锚定字符串 | stall_breaker 证据、zero-rows recovery 入口 |
| bare legacy `PriorityUpdateZeroRowsError` | 史前持久化 | 类型名字符串 | 仅 zero-rows recovery 活探测后再分类（:133-138） |
| SUBMIT_UNKNOWN / NOT_ATTEMPTED / SUBMITTED | submit :964-975 | 枚举 + 诊断串 | bridge/store 状态机 |
| MANUAL（reconcile 冲突/harvest 无法启动等） | 各处 | 枚举 + 人类可读串 | store error_text |
| wait-export 诊断三兄弟 | harvest :2137/2144/2146 | 精确字符串（"still running"/"did not produce an export"） | recovery/wait_export.py 入口集合（frozenset 常量，06e2c6a 教训） |
| export-no-price 字面量 | recovery/no_result.py:220-221 常量（"本次任务执行完毕"/"未检索到商品价格信息"） | rspDesc 中文字符串 | 终态判定门 |

**DESIGN RISK（FACT，不修改）**：diagnostic string 同时承担人类日志与程序控制流——`error_text` 被持久化、被 recovery 以精确字符串相等做入口门、stall_breaker 以字符串成员做证据、异常分类靠类型名+消息前缀。缓解现状：关键字面量已 frozenset 常量化且被 facade 契约测试钉住 4 个；MySQL 码表已封闭。风险集中在"新恢复通道手工枚举可接受错误集合"这一模式（06e2c6a/B-20 的失败模式在 adapter 侧同样存在）。

---

## 12. External Tool Boundary

1. 加载稳定性：spec_from_file_location + exec；文件缺失/损坏 → 明确异常（fail-closed）。FACT。
2. 固定模块名碰撞：4 个固定名注册 sys.modules（:735-738/:848/:850-853/:1356）——同进程每次调用**重新 exec 并覆盖**条目；模块顶层只读（MALL_CONFIG/URL 常量）故当前无害；若未来出现并发 adapter 调用或模块获得可变全局，即成隐患。reconciler 路径刻意不注册（:1057-1061）——不一致本身说明该机制是权宜。FACT + DESIGN RISK。
3. 多实例/并发：子进程路径天然隔离；进程内 priority/probe 串行调用安全；`_temporary_environment`（:337-349）在窗口期向**进程全局** os.environ 注入 DB 凭据/auth——tick 各阶段当前串行故无泄漏，但这是隐式约束。FACT + DESIGN RISK（L-4）。
4. 环境泄漏：子进程 env = 全量父 env − 代理 + auth + DB − TITLE_MATCH_MODEL_*（:794-800）。DB/auth 变量经测试断言不泄漏回 os.environ；工具 stdout 打印响应体 ≤1500 字符（adapter 捕获后丢弃，若有人将来记日志即泄漏向量）。FACT。
5. 子进程解析可靠性：exit code 可靠；stdout/stderr 内容按设计**不参与判定**（证据全部来自 batch-state 事件 + 文件）；`text=True` 用宿主 cp936 解码 + 工具内 mojibake 字面量 → 子进程 UnicodeEncodeError 风险（fail-closed 非零退出，噪声型）。FACT。
6. timeout 后残留：`subprocess.run` timeout 杀直接子进程；工具无孙进程 → 无孤儿。FACT（与 scheduler 层 Job Object 兜底独立）。
7. Windows 路径/编码：cwd=工具目录（:807）；路径含中文正常工作（生产实证）；模板发布 PermissionError 重试×5（:518-525）针对扫描器占用。FACT。
8. **双副本**：外部 `zhaoshang_priority_boost.py`（08-12）与库内 mirror（08-19）已分叉——mirror 多两个异常子类（NotMaterialized/Incomplete），其余字节等价；adapter 分类同时接受两种形态（类型名集合 + 消息前缀）；双 fixture 测试（integration+production 参数化）是唯一防漂移闸。DESIGN RISK（FACT，§15 C5）。

---

## 13. Test Coverage Map（要点）

**DENSELY COVERED（复杂且被钉死——"复杂但正确"区）**：submit 证据终判/超时永不重发（5 测试）；reconcile 精确接管/冲突 fail-closed（11）；priority 身份/窗口/分类/幂等/操作码（15+，双副本参数化）；harvest provenance/部分导出/sibling 隔离/URL 白名单/别名信任（30+）；probe batchNumber/count（2 个生产级测试）；环境/子进程 deadline/秘密剥离（6）；stall 分类与熔断（~20）。

**缺口（按危险排序）**：

| # | 缺口 | 现状 | 危险级 |
|---|---|---|---|
| G1 | probe 畸形响应分支（workbook 嗅探 :1416-1445、内嵌下载 URL、count 不可解析、auth 两变量缺失 :1363-1370） | **NOT COVERED**——唯一 payload 测试都是良构 JSON；恰是 Z-4 同类形状混淆 | **P1** |
| G2 | submit 的 auth-refresh 异常路径（L-2b） | **NOT COVERED**——只有 deadline 耗尽变体有测试 | **P0**（与 L-2b 绑定） |
| G3 | ADD_INTENT 损坏标记拒绝分支（重复/计数不符/坏时间戳 :706-727） | 无直接测试（仅 happy-path 接管）；写入保证本身在仓库外 | P1 |
| G4 | mall 映射表（SUPPORTED_ZHAOSHANG_MALL_KEYS 21 键含 comix、EXTRA_DOMAIN_ALIASES） | **NOT COVERED**——comix 在零个测试中出现；真实映射表 xlsx 驱动的加载无测试 | P1 |
| G5 | `_production_priority_runner` deadline 耗尽 | NOT COVERED（另两个 runner 有） | P2 |
| G6 | 子进程 `cwd` 与 `--wait-timeout` 数值断言 | WEAK | P2 |
| G7 | legacy 路径 HTTP 401/429/503 fail-closed 对等性 | NOT COVERED（仅 batch 后端有） | P2 |

事故引用测试现状（job17/54/60/71/72、batchNumber、InterfaceError、window 拉伸）全部在库——**历史 bug 无回归测试的仅 Z-1/Z-2 的"改前形态"本身不可测，属正常**；真正"出过事且当前无保护"的是 G1/G2（Z-4 的兄弟分支）。

---

## 14. Latent Bugs（全部未修，仅记录）

| ID | 位置 | 触发 | 期望 | 实际 | 证据 | 置信度 |
|---|---|---|---|---|---|---|
| L-1 | `legacy_zhaoshang_adapter.py:1720`（import 块 :20 无 `Sequence`） | 对该函数做 typing.get_type_hints()/静态检查 | 名字可解析 | NameError（**运行时无影响**：:3 `from __future__ import annotations` 使注解不求值） | grep 全文唯一使用点；重构计划 Phase 2.10 已登记 | HIGH（FACT） |
| L-2a | `submit()` :941-943 捕获 `(TimeoutExpired, TimeoutError, OSError)` → "completion is unknown" | 外部工具缺失（:790-791 FileNotFoundError ∈ OSError）或 spawn 失败——**发送从未发生** | NOT_ATTEMPTED（可安全重试） | SUBMIT_UNKNOWN（强制对账流程）；对比 harvest :2138-2139 正确区分 "could not be started" | 代码对照；fail-closed 方向、无双发 | HIGH |
| L-2b | 同上捕获集不含 auth 异常；`_production_runner` :795 `export_environment(refresh=True)` 抛 AuthError/RefreshTokenExpiredError（auth_manager:324-335） | refreshToken 过期/auth 网络故障 | 明确分类（未尝试）而非异常逃逸 | 异常穿透 submit→bridge→workflows:667-670 兜底烧 post_attempted 记 SUBMIT_UNKNOWN；测试 agent 独立同判 | 代码链 + 双源交叉 | HIGH |
| L-3 | `_submission_marker_time` :689-690 把 aware 时间戳归一为 **UTC+8 naive**；`_production_priority_runner` :866-868 与 `datetime.now()`（宿主本地 naive）比较 | 宿主时区 ≠ UTC+8 运行 | 窗口锚定 marker 时刻 | 窗口整体偏移（lookup 错窗/漏行） | 代码；生产机 locale 为中国时区故休眠 | HIGH（耦合是事实）/影响 LOW |
| L-4 | `_load_module` 固定名重复 exec（:822-833）+ `_temporary_environment` 进程级 env 窗口（:337-349, :854-855, :1371） | 同进程并发调用 adapter 方法 | 隔离 | 模块对象被替换/他线程窗口期看见 DB 凭据或 auth | 机制明确；当前 tick 各阶段串行 → 休眠 | HIGH 机制/LOW 概率 |
| L-5 | `_classify_existing_export_partial_legacy` :1734-1921 | —（死代码） | 单一分类器实现 | 零调用方的第二实现并存（约 188 行），未来"改错份"温床 | grep 全库零引用 | HIGH（FACT） |
| L-6 | 外部边界三项：exportTask 重试 ≤5 无独立 deadline（tool:828-856）；batchNumber 三处默认=task_name（tool:401/1030, adapter:1399）；任务名无 14 位后缀时静默重造 marker（tool:913-918，当前被 runtime 指纹命名掩蔽） | 各自条件 | 有界/显式 | 如上 | 外部工具取证 | HIGH |
| L-7 | mall 映射配置零测试（comix 等 21 键）——覆盖缺口而非 bug（见 G4） | 回归改动映射表 | 测试拦截 | 静默通过 | 测试映射 | HIGH（FACT） |

**未发现的（负面结论，FACT）**：undefined name（除 L-1 注解）、task_name/task_number 运行时混用、重复任务创建路径、静默 except 吞错（唯一 `except Exception: pass` :909-910 仅吞事件记录失败且原异常照抛）、off-by-one、可变全局。

---

## 15. Root Cause Clusters

| 簇 | 证据 | 覆盖 Bug/风险 |
|---|---|---|
| **C1 任务标识符语义隐式且分散**——同一"任务"有 4 个标识符（task_name/batchNumber/taskNumber/taskId），每个端点键不同（/add=name+batch；progress=batchNumber；status=taskId；export=taskNumber+taskId+mallLogo；DB=task_num+mall+窗口），且 batchNumber 三处默认=name | 端点键控表（§9/§12）；Z-4 即新代码选错键 | Z-4；L-6②③ |
| **C2 远端协议无类型化**——rspDesc 中文串/可选 count/success+rspCode 双标志/workbook-vs-JSON 嗅探；且 diagnostic 字符串同时当控制流（§11 DESIGN RISK） | Z-4②；probe 未覆盖分支；恢复入口精确串门 | Z-4；B-20 同构风险 |
| **C3 验收门严格度靠事故校准**——远端结果形状无契约文档，门先写严（Z-1/Z-2）后按事故放宽为 provenance+partial | df96f56 删除行 vs 现实现 | Z-1、Z-2 |
| **C4 高压新增能力无 golden fixture**——08-19 当天新增 probe 6 分钟即修 | 85d9e63→5cf8ccf 时间戳；G1 缺口 | Z-4；G1 |
| **C5 边界所有权分裂**——ADD_INTENT 写入保证与 priority 核心活在仓库外；库内 mirror 与外部文件已分叉，仅靠双 fixture 防漂移；测试依赖 E:\A2026 路径存在 | 双副本分叉实证（§12.8）；G3/G7 | 漂移风险 |

INFERENCE：Z-1..Z-4 全部落在 C1/C3/C4——不是四个独立失误，是两个结构性根因（标识符隐式、协议无契约）加一个过程根因（新能力无 fixture）的反复表现。

---

## 16. Risk Ranking（0-100；依据：历史 Bug/副作用风险/爆炸半径/覆盖/状态歧义/协议复杂度/变更频率）

| 职责 | 分数 | 主因 |
|---|---|---|
| Priority（含外部 DB 写） | **65** | 唯一远端 DB 写路径；Z-3 共同起源 + job60 分类史；UNCONFIRMED 三态歧义；窗口/操作码协议复杂度最高。缓解：全库最密测试（双副本参数化） |
| Harvest 验收/Partial 分类 | **60** | 历史起源最多（Z-1/Z-2）；错放行=错价格入正式表（爆炸半径 85）；逻辑复杂度最高（两套 durable_identity + 等价类）。缓解：30+ 回归 |
| Submit | **55** | 副作用风险 95（唯一 /add 出口）但双发防护闭合；L-2a/b 潜伏；覆盖密 |
| Probe | **55** | Z-4 出生地；畸形分支零覆盖（G1）；NO_RESULT 终态判定依赖其证据；变更频率最高（08-19 新生） |
| 证据处理（batch-state 事件） | **55** | 全部终判的地基；ADD_INTENT 损坏分支无直接测试（G3）；文件追加式非事务 |
| 外部子进程构造 | **50** | env 组装/cwd/编码边界；G5/G6 |
| Reconcile | **40** | 只读+本地写；覆盖好 |
| URL 匹配 | **40** | 白名单严格、覆盖好；错配爆炸半径大但概率低 |
| 模板生成 | **25** | 无历史 Bug；原子发布有测试 |
| 结果解析（委托 result_adapter） | ~50（随 harvest） | 列头别名集是唯一柔性点 |

---

## 17. FIX NOW（已确认 Bug；本轮不修）

1. **L-2b + L-2a：submit 的"从未启动"分类**
   - Bug：auth refresh 异常逃逸 submit（L-2b）；OSError 与 TimeoutExpired 混同 → 未发送也记 SUBMIT_UNKNOWN（L-2a）。
   - 证据：:941-943 捕获集；:790-791/:795 先置异常；harvest :2138-2139 的正确对照；workflows:661-670 兜底烧 claim。回归：G2（auth_manager monkeypatch 为 raise → 断言结果分类、无子进程、无 /add）。
   - 最小修复方向：在 submit 内区分 pre-send 失败（auth/工具缺失/spawn OSError）与 post-send 未知（TimeoutExpired）→ 前者返回 NOT_ATTEMPTED 或带显式诊断的新分类。**注意**：bridge（runtime:1212-1213）当前把非 SUBMITTED 一律 RuntimeConfigurationError、workflows 兜底仍会烧 claim 记 UNKNOWN——完整修复需 adapter+bridge+workflows 协同设计（pre-send 异常应释放 claim 而非记 UNKNOWN），这是小项目不是一行改动。
   - 风险：改变提交失败分类会影响 store 状态机语义——必须 test-first 且逐层验证。
2. **L-1：`Sequence` import**（一行，随上项或 Phase 2.10 一起，test-first 意义有限但成本为零）。

## 18. HARDEN NEXT（无确认 Bug，但同类事故温床）

1. **G1 golden fixtures**：probe 的 workbook/attachment/PK/内嵌 URL/字符串 data/count 不可解析/auth 缺失 各喂一个真实形状 payload（Z-4 同类防线）。P1。
2. **类型化标识符**：TaskName/BatchNumber/TaskNumber/TaskId 值对象 + 单一模块拥有"端点→键"映射，batchNumber 推导只出现一次（攻 C1）。P1。
3. **诊断结构化**：diagnostic 以 enum/dataclass 产生，字符串仅作序列化；恢复入口集合由单一表驱动（攻 C2；recovery 侧 06e2c6a 教训同源）。P1。
4. **G4 mall 映射表 pin 测试**（21 键 + 别名 + comix + 真实 xlsx 加载）。P1。
5. **G3 ADD_INTENT 损坏标记三分支直接测试**。P1。
6. **G5/G6**：priority runner deadline 耗尽测试；cwd/--wait-timeout 断言补齐。P2。
7. **L-3 时区锚定**：marker 与窗口统一显式 ZoneInfo("Asia/Shanghai")，消除宿主 TZ 耦合。P2。
8. **双副本收敛**：将带子类的 mirror 版本部署到外部路径（或外部生成自 mirror），保留双 fixture 作为漂移闸（攻 C5）。P2。

## 19. REFACTOR LATER（纯可维护性）

1. 删除死代码 `_classify_existing_export_partial_legacy`（:1734-1921，~188 行；先加"无引用"守护）。
2. 按职责拆文件（submit/priority/harvest+validation/probe/identity），复用 Phase 1A playbook：先钉 facade 契约测试，MOVE-only，行为修复单独 commit。
3. `_load_module` 改为向模块注入配置对象而非污染 os.environ/sys.modules（消除 L-4）。
4. harvest 双 durable_identity 重复（严格路径与 partial 路径各一份）在删死代码后评估提取。

（以上不与 A/B 混淆：拆文件不减少事故率——C1-C5 才是事故源。）

---

## 20. Recommended Next Step（十七节问题）

**OPTION 1 —— 先修确定性 Bug，再（behavior-preserving）加固，最后才重构。**

理由（FACT 链）：当前无活跃生产 Bug（最新全绿+唯一 baseline failure 与本模块无关）；确认的 2 个潜伏 Bug 都小且 test-first 可修，但 L-2 的完整修复横跨 adapter/bridge/workflows 三层语义，必须先行明确设计；历史证明本模块事故来自"新能力无 fixture + 标识符隐式"（C1/C4）而非文件长度——纯结构拆分（OPTION 3）不降低复发率；OPTION 4（不动）浪费 G1/G4 这类低成本高价值防线。次序：L-1+L-2（小项目）→ §18 的 1/2/3/4/5 → §19。

---

## 21. Unknowns

- UNKNOWN：外部工具 `auto_crawl_task.py`（08-12 版）与招商平台真实契约的完整枚举——本审计只能覆盖 adapter 实际触达的函数与已观察到的响应形状；probe 未覆盖分支对应哪些**真实发生过的**平台响应，仓库内无证据。
- UNKNOWN：`E:\A2026` 路径依赖的测试（resume_modes/priority_boost production fixture）在无该路径机器上的 skip 行为是否被 CI 等价物覆盖——本机存在该路径，未验证他机。
- UNKNOWN：2026-08-12 checkpoint（df96f56）之前该逻辑的工作树演化细节（被压缩）——Z-1/Z-2 的引入时刻只能定为"df96f56 时已存在"。
- UNKNOWN：外部 `zhaoshang_priority_boost.py` 的部署/变更流程归属（谁拥有 E:\A2026 目录）——影响 §18.8 的收敛方案可行性。
- HYPOTHESIS：batchNumber 与 task_name 未来分叉概率（L-6②）——取决于平台侧，仓库内无先例。

---

## 附：审计产物

- 本文档：`docs/CNNC_LEGACY_ZHAOSHANG_ADAPTER_AUDIT_20260823.md`（唯一写入文件，未 commit）
- 中间证据：`E:\.A中核运维\_run\bugspot_audit\adapter_checkpoint_diff.txt`（df96f56→7248bce 全量 diff）及既有分析产物
