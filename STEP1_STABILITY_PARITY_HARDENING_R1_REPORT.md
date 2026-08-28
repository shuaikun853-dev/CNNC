# STEP_1 Production Stability & Production-Parity Hardening — Round 1 开发报告

> 开发日期:2026-08-28(晚)
> 开发分支:`fix/step1-stability-parity-hardening-r1-20260828`(独立 worktree `E:\.A中核运维\cnnc-step1-hardening-r1`)
> 输入:两份已完成的独立审计(`STEP1_STABILITY_AUDIT_20260827.md`、`STEP1_PRODUCTION_PARITY_TEST_AUDIT_CURRENT.md`)
> 本报告为开发交付物,merge/deploy 决策留待 ChatGPT GitHub diff review 与用户。

---

## 1. Version

```text
BASE_BRANCH=origin/prod/closeout-20260818
BASE_SHA=adba73435504746a146354c450ee04e3fecbcb72
REFERENCE_SHA=adba73435504746a146354c450ee04e3fecbcb72
DEV_BRANCH=fix/step1-stability-parity-hardening-r1-20260828
NEW_HEAD=67097b8(代码提交;本报告 commit 紧随其后)
LATEST_PRODUCTION_SHA=adba73435504746a146354c450ee04e3fecbcb72(commit 前重 fetch 复核)
PRODUCTION_MOVED_DURING_DEVELOPMENT=NO
MERGED=NO
DEPLOYED=NO
WORKTREE=E:\.A中核运维\cnnc-step1-hardening-r1(HEAD==BASE_SHA 起,全程干净起步)
```

Git Gate:任务开始时 `BASE_SHA == REFERENCE_SHA`(adba734)→ 通过;commit 前复核生产分支未移动。

提交结构(6 个逻辑 commit):

| commit | 内容 |
|---|---|
| d0da12b | fix(zhaoshang): identity parser 乱码修复(F-P1-1) |
| b2a06f7 | fix(harvest): partial exact NO_RESULT 活性证据门禁(F-P1-2) |
| a273410 | test(parity): 真实 PowerShell 5.1 + SQLite 边界回归线(TEST-B1/B2) |
| 2f66e6c | feat(doctor): scheduler 漂移检测 + source completeness 诊断(DIAG-B3/D) |
| 3be176d | test(parity): legacy parser 契约 + probe 故障分类(PHASE C/E) |
| 67097b8 | test: hermetic 干净 checkout 核心套件(PHASE F) |

---

## 2. Findings resolution matrix

| Finding | 原结论 | 本轮结果 |
|---|---|---|
| F-P1-1 identity parser 乱码 | OPEN | **FIXED**(d0da12b;BASE 复验仍在 → 4 处字面量对齐正确实现;业务分类 parity 测试 7 用例) |
| F-P1-2 partial exact false NO_RESULT | OPEN | **FIXED**(b2a06f7;WORKBOOK_ABSENT≠NO_RESULT_PROVEN 门禁,P1-P6+LIMIT 全测) |
| F-P1-3 source completeness | OPEN | **DIAGNOSTIC**(指标+status 节;hard gate=DEFERRED_BUSINESS_INVARIANT_NOT_PROVEN;NO_ARBITRARY_THRESHOLD_INTRODUCED=YES) |
| F-P2-8 shared probe outage → 批量 MANUAL | OPEN | **TESTED**(20 用例钉死当前逐-job 分类;F-P2-8=TESTED_AND_DESIGNED_NOT_IMPLEMENTED,下一轮设计见 §10) |
| PowerShell parity(TG1-3/TG2-1/FC-1) | TG | **HARDENED**(L0→L2:真实 powershell.exe 5.1 全链 + 13 形状矩阵 + 精确事故回放) |
| SQLite real contention(TG1-1/FC-3/F-P2-9) | TG | **HARDENED**(5 个真实锁竞争用例:busy 结局/分类/单写者/WAL 读者/杀进程回滚) |
| Scheduler live drift(TG1-2/FC-4) | TG | **HARDENED**(只读 drift checker + doctor 集成 + 生产实况 MATCH=true 证据;hermetic 7 漂移形状用例) |
| Legacy fake divergence(TG2-3/TG1-4 部分) | TG | **HARDENED**(两处 fake 逐字镜像真实正则;11 条 corpus 经真实工具 L3 回放 fake==real) |
| test hermeticity(TG3-2/FC-5) | TG | **HARDENED**(5 失败全对账=gitignored data;全部 hermetic 化;干净 checkout FAILED=0;ops 机交叉验证) |

---

## 3. TEST_FAILURE_RECONCILIATION(5 failures 重对账)

基线(adba734 干净 checkout,独立 baseline worktree 实跑):`COLLECTED=1881 PASSED=1867 FAILED=5 SKIPPED=9 DURATION=329.75s`。

| 测试 | 根因(证据) | 决策 |
|---|---|---|
| test_incremental_audit_runtime::…do_not_depend_on_cwd | 干净 checkout 缺 gitignored `data/model.env` → `_load_model_configuration()` RuntimeConfigurationError | HERMETIC(monkeypatch `PROJECT_DATA_ROOT` → tmp 合成 data root;常量关系断言前置;契约不变) |
| test_incremental_audit_runtime::…non_project_cwd_is_offline | `build_default_dependencies` 读 `data/latest_saas_sameAudit_queryAll.curl` → FileNotFoundError | HERMETIC(同上注入) |
| test_remote_running_recovery::test_default_dependencies_wire_probe… | 同一 curl 缺失 | HERMETIC(同上注入) |
| test_work_01…::test_dual_json_mode_auto_exports_excluded_templates | `scripts/export_excluded_malls.py:45` 模块级 `EXCLUDED_MALLS=load_zhaoshang_mall_mappings()` 读 gitignored `商城sourceId映射表.xlsx`;缺失→`[]`→KeyError '京东' | HERMETIC(注入结构等价合成映射;断言原样) |
| test_zhaoshang_single_mall_templates::test_lxwl_templates… | 同上;KeyError '领先未来商城' | HERMETIC(同上;域名优先路由断言原样) |

**对账结论:两版审计归因中"版本 B(全部 gitignored 本地 data 依赖)"正确,"版本 A(2 个为仓外模块漂移)"不成立**——京东/震坤行/lxwl 模板全部来自 gitignored 映射表 xlsx,与 `E:\.A2026` 外部工具无关(实测 ops checkout 真实 xlsx 含 19 条映射,原测试在有 data 的机器通过)。
交叉验证:ops checkout(有真实 data)`5 passed`(修改前后均通过);干净 worktree 修改后 `5 passed`。

### 9+1 个 skip 的分类(最终全套件)

| 数量 | 位置 | 分类 |
|---|---|---|
| 9 | test_incremental_history_benchmark.py:18×3,:91×1;test_incremental_price_audit_history.py:88×5 | **HISTORICAL_CORPUS_MISSING**(EXPECTED;TESTING.md:70 明示不得用生产接口补齐) |
| 1 | test_scheduler_drift.py:325 | **PLATFORM/PARITY(opt-in)**:`CNNC_SCHEDULER_LIVE_DRIFT=1` 才执行的只读 live 对照;hermetic suite 不依赖真实计划任务 |

---

## 4. Fresh full suite(开发结束,干净 worktree)

```text
命令:  python -m pytest tests -q --basetemp=... -p no:cacheprovider -ra(本机 E:\Python314,Windows,PS 5.1)
COLLECTED=1993  PASSED=1983  FAILED=0  SKIPPED=10  XFAILED=0  DURATION=357.78s
```

对比基线:+116 通过(新增回归线),5 failed→**0 failed**。POWERSHELL_VERSION=5.1.19041.6456;REAL_PS_BOUNDARY_TEST=PASS(19 用例 14.22s,真实 powershell.exe 子进程)。

---

## 5. Production parity scorecard(本轮后)

| 边界 | 审计前 | 本轮后 | 提升点 |
|---|---|---|---|
| PowerShell scheduler probe | D(套内全 L0;一次性 L3) | **A-** | 真实 PS 5.1 全链(生产命令+真任务只读)、13 形状矩阵、INCIDENT 回放行为化钉死 [int64];命令/decode seam 暴露为可测单元 |
| SQLite | B | **A-** | 真实 BUSY 结局(5.5s/OperationalError/unknown 分类)、构造器争锁、WAL 读者并发、事务中杀进程回滚——全部入套 |
| Windows Scheduler(任务本体) | C | **B+** | 只读 drift checker(preview 权威 vs Get-ScheduledTask 实况,11 字段)+ doctor 集成 + 生产实况 MATCH=true;hermetic 覆盖 7 种漂移形状;live 级自动检测仍依赖 doctor/opt-in(见 §11 残留) |
| Legacy tool parser | C(替身已分叉) | **B+/A-** | 两处 fake 逐字镜像 `[：:](\d+)条`;11 条 corpus 真实工具 L3 回放 fake==real;canonical signature 契约 pin |
| Remote platform read contract | L1(fake) | **L1(未变,如实)** | 本轮禁止真实远程写 E2E;状态机层新增 probe 故障分类钉死(20 用例);真实平台行为仍待生产旁证 |

---

## 6. Exact incident replay

```text
INCIDENT=2026-08-27 UInt32(0x800710E0=2147946720)→ /api/summary 503
AUTOMATED_EXACT_REPLAY=YES
TESTS:
  tests/test_scheduler_probe_real_powershell.py::test_INCIDENT_20260827_UINT32_REPLAY
    (从生产命令文本正则提取 last_result 表达式 → 真实 PS 5.1 执行 [uint32]2147946720 → 断言 JSON 数字 2147946720;[int64]→[int] 回潮即红)
  tests/test_scheduler_probe_real_powershell.py::test_incident_shape_int_cast_still_produces_degraded_not_crash
    (旧 [int] 形状:rc=0、last_result={}、stderr GBK InvalidCast——经生产 decode/projection 降级 -1/未知,不 503)
  tests/test_scheduler_probe_real_powershell.py::test_scheduler_probe_type_matrix(13 参数点全形状)
矩阵实测定论:0/267009/2147946720/-1/[uint32]/[int64][uint32]→JSON 数字;$null/'abc'/''/对象/数组→-1;'2147946720'→2147946720;42.5→42;空输出/坏 JSON→unavailable。任何形状下 summary 不可 503。
```

## 7. SQLite proof(真实观察,非"测试通过")

| Case | 真实观察 |
|---|---|
| S1 写锁被持→生产写 | `sqlite3.OperationalError('database is locked')`,busy 等待 **5.56s**(busy_timeout=5000+开销);`_closeout_failure_class`→**'unknown'**(bounded stop,pin 现状);释放后同一写路径恢复成功,竞争尝试零提交 |
| S2 双连接争 lease | 同异常 5.54s;释放后 acquire 成功;第三获取者(不同 owner,未过期)→ `LeaseUnavailable`(单写者规则,非 BUSY) |
| S3 WAL 读者 | writer 持 IMMEDIATE+未提交 INSERT 时:mode=ro URI 0.002s 成功、`store._reader()` 同;未提交行不可见;不消耗 busy 预算 |
| S4 构造器争锁 | DDL 构造 5.66s 有界失败;释放后 0.010s 重建;integrity_check=ok |
| S5 事务中杀进程 | 子进程 IMMEDIATE+INSERT 后被 kill;下一连接 WAL 恢复、integrity ok、标记行不可见;store 完整重建+租约+生产写成功 |

分类说明:把 SQLITE_BUSY 改为 retryable 是独立生产决策(涉 F-P2-7),本轮按任务原则未改——测试钉死现状,防草率把 fail-closed 改成自动重试。

## 8. Scheduler drift proof(READ_ONLY)

EXPECTED_SPEC(注册脚本 preview 真实执行,-PythonExe=sys.executable):4 trigger(Baseline 09:00/PT15M/PT7H46M、Peak 17:00/PT5M/PT56M、Closeout 18:00/PT5M/PT2H1M、SafetyCloseout 20:35 一次性)、IgnoreNew、PT32M、action=powershell.exe+runner 参数、周一至五(mask 62,实测非猜测)。

LIVE_SPEC(Get-ScheduledTask 只读实查):与上述逐字段一致;`start_when_available=false`、`principal_logon_type=Interactive`、action 指向生产根 `E:\.A中核运维\cnnc-ops-agent` 且带 `-EnableRemoteWrites`。

```text
生产根+enable_remote_writes=True:SCHEDULER_CONFIG_MATCH=true  drift=[]   (11 字段全对齐)
开发 worktree 根(默认无 flag):MATCH=false,drift=[action_arguments_mismatch, working_directory_mismatch]
  ——该两项为部署上下文差异(worktree 路径/无 flag),非真实配置漂移;证明 checker 对 action/working-dir 敏感
```

掩码实测:`[int]$t.Triggers[n].DaysOfWeek`=62=2|4|8|16|32;Export-ScheduledTask XML=Mon-Fri;类型 System.UInt16(非枚举)。doctor 输出:`scheduler_config_drift, ok=<not drift>, required=False, detail="SCHEDULER_CONFIG_MATCH=…"`。**绝不自动修复**(模块内 Register/Set/Unregister/Start/Stop/-Apply 全部禁止,docstring 声明+测试保障)。

## 9. Source completeness(F-P1-3)

新增只读指标(discovery evidence → daily counters → status `source_completeness` 节 → dashboard payload 自动可见):

```json
{"saas_count": 10, "customer_count": 2, "source_count_gap": -8,
 "customer_matched": 1, "customer_matched_no_category": 1, "customer_not_found": 8,
 "customer_match_rate": 10.0, "customer_not_found_rate": 80.0}
```

```text
F-P1-3_HARD_GATE=DEFERRED_BUSINESS_INVARIANT_NOT_PROVEN
NO_ARBITRARY_THRESHOLD_INTRODUCED=YES   (硬要求;未设任何 95%/90%/gap>10 之类阈值)
负面断言测试:customer_not_found_rate=80% 时 freeze 照常(门未变),仅记录指标。
```

## 10. PHASE E(F-P2-8)测试结论与下一轮设计(未实现)

现状钉死(20 用例,tests/test_probe_outage_fault_classification.py):

| 异常类 | 实际走向 |
|---|---|
| RefreshTokenExpiredError / pymysql OperationalError(2003) / Timeout / Connection / ConnectionReset / 单任务畸形响应(ValueError/KeyError) | 全部进 per-job 连续 probe 失败计数:单次仍 RUNNING,连续 5 次→MANUAL;两个 job 同源故障各自独立累计 |
| RemoteTaskIdentityConflictError(精确身份冲突) | 首次即 MANUAL、零写、不消耗 probe 计数;兄弟 job 不受影响 |

```text
F-P2-8=TESTED_AND_DESIGNED_NOT_IMPLEMENTED
```

候选下一轮设计(仅设计,满足 bounded+fail-closed+不掩盖真停滞+不无限 RUNNING+不重试远程写才许实现):在 `record_remote_recovery_probe_failure` 前增加 shared/job 二分类(auth/DB/transport 异常类型白名单 → shared;identity/畸形 → job-specific);shared 类失败同时维护一个**全局** outage 窗口(有界,如最近 30 分钟内 ≥N 个不同 job 同类失败才认定);窗口内 shared 失败**不累计** per-job 5 次上限(停滞判定不受影响——progress ACTIVE 证据独立);窗口结束仍失败则恢复逐 job 计数并最终 fail-closed 到 MANUAL;全程零远程写、零自动重启。需独立 review 其"不掩盖单任务真停滞"的反例后再实施。

## 11. Mutation reasoning(PHASE G,纯推理未改生产代码)

| 假想 mutation | 拦截测试 |
|---|---|
| [int64] 改回 [int] | test_INCIDENT_20260827_UINT32_REPLAY(提取表达式真实执行,{} 非数字即红)+ test_scheduler_probe_source_no_int32_cast(文本) |
| 20:35 trigger 从注册脚本 expected spec 删除 | test_scheduler_drift.py L2 preview 用例(4 trigger 含 20:35)+ 既有 operations preview 断言 |
| live scheduler 20:35 被人工删掉 | hermetic:missing_trigger_at_20:35 compare 用例;live 级:doctor scheduler_config_drift 报告(opt-in 测试 CNNC_SCHEDULER_LIVE_DRIFT=1)——**默认套件不轮询 live**,见残留风险 |
| busy_timeout 被删除 | test_sqlite_real_contention S1/S2/S4(elapsed≥4.5s 有界等待断言,删除后立即抛出即红) |
| SQLITE_BUSY 分类改成 permanent | S1 断言 `_closeout_failure_class(real busy exc)=='unknown'` 即红 |
| partial exact 缺行重新直接 NO_RESULT(门禁移除) | P1/P2/P4(期望 RecoveryRejected+零写入)+ probe 调用计数断言 |
| identity parser "成功"再次乱码 | test_zhaoshang_result_adapter_identity_parity(PRICE_MISSING/招商局-价格不可见+双 parser parity 即红) |
| SUBMIT_UNKNOWN 被重新 POST | 既有 test_incremental_audit_resilience.py:450-485("must not blindly POST again") |
| exact identity 变模糊 lookup | 既有 identity conflict 电池(test_remote_running_recovery 身份不匹配→typed conflict;production-readiness L2) |

REGRESSION_ESCAPE_REMAINS:**仅一条(如实)**——live 计划任务被人工改动的**默认套件内自动**检测仍缺(hermetic 比较逻辑+checker+opt-in live 测试已备;常态化需 ops 侧定期跑 doctor 或后续把 drift 并入 status 轮询,属下一轮决策)。

## 12. Review Gates(PHASE I)

| Gate | 结论 | 证据 |
|---|---|---|
| A silent data loss | **0 新路径** | 新 NO_RESULT 路径仅当 live probe 五重契约全满足;缺证据一律预突变前拒绝(P1/P2/P4);SILENT_FALSE_NO_RESULT_PATH=CLOSED(partial exact 维度) |
| B remote write | **submit/priority/restart 语义零改动** | 唯一新调用 `probe_export_no_price` 为只读(EXPORT_TASK_URL=harvest 同源下载语义,审计已定性);本分支无任何远端写 |
| C false confidence | 新测试全部显式标级(L2/L3 docstring);唯一 monkeypatch=decode seam 注入**真实跨边界字节** | test_scheduler_probe_real_powershell.py 头注 |
| D 环境测试 | 干净 checkout FAILED=0;无生产 secret 依赖红灯 | §4 |
| E production moved | commit 前重 fetch:origin/prod/closeout-20260818==adba734 | §1 |

## 13. 外部副作用证明(PHASE H §42)

```text
REAL_ZHAOSHANG_CREATE_CALLS=0      REAL_ZHAOSHANG_PRIORITY_WRITES=0
REAL_ZHAOSHANG_RESTART_WRITES=0    PRODUCTION_SQLITE_WRITES=0
SCHEDULED_TASK_WRITES=0(全程仅 Get-* + 注册脚本 preview,无 -Apply)
DEPLOYMENTS=0                      GIT_MERGE/REBASE=0
```

ops 生产 checkout 仅被只读使用(PHASE F 交叉验证;PYTHONDONTWRITEBYTECODE+no:cacheprovider,git status 复核零写入)。

## 14. Remaining risks(如实)

1. **真实招商局远程写 parity 仍未证明**(禁写 E2E 是本轮边界;probe/restart/priority 的真实平台行为继续靠生产旁证)。
2. **真实 RUNNING restart 新机制尚无一次实战**(8/25 事故发生在旧代码;审计 J-1 维持)。
3. **shared outage policy 未实现**(F-P2-8;§10 设计待独立 review)。
4. **interactive-only scheduler / StartWhenAvailable=false / 20:36 后无自动路径 / 绝对路径烘焙**(F-P2-6,调度静默失效四联未动,本轮只加了漂移可见性)。
5. **live 漂移的默认套件自动检测缺**(§11;需 ops 定期 doctor 或下轮并入轮询)。
6. 20:35 final opportunity 语义未变(drift checker 能发现 20:35 被删,但不改变"20:35 是当日最后自动机会"的设计)。
7. 生产任务 action 带 `-EnableRemoteWrites` 与 worktree preview 的差异需以生产根+flag 运行 doctor 才 MATCH(§8;参数已支持)。
8. partial exact 全终态幂等重跑会把 job result_json 重写为 `{}`(BASE 既有行为,未授权不改,flash 已存证)。

## 15. 最终 Gate

```text
P0_FOUND_DURING_DEVELOPMENT=NO
NEW_P0_INTRODUCED=NO
SILENT_FALSE_NO_RESULT_PATH=CLOSED(partial exact;其余核心协议未动)
REAL_POWERSHELL_PARITY=PROVEN(生产命令+真任务+13 形状+事故回放,全部真实子进程)
REAL_SQLITE_CONTENTION=COVERED(5 用例真实锁)
LIVE_SCHEDULER_DRIFT_DETECTION=IMPLEMENTED(只读 checker+doctor+opt-in 测试;生产实况 MATCH=true)
CORE_TEST_SUITE=GREEN(干净 checkout 1993 collected / 1983 passed / 0 failed / 10 skipped)
READY_FOR_GITHUB_DIFF_REVIEW=YES
```

## 16. 核心原则自检

- 未以测试数量为目标(+116 是边界防线的结果,不是目的);
- 未削弱 exactly-once / exact identity / fail-closed 任何核心协议(Review Gate B);
- 未引入任何业务阈值(completeness 纯诊断);
- 未把任何 fail-closed 改成自动重试(SQLITE_BUSY 分类、F-P2-8 均钉死现状);
- 唯一实时新增远端行为:一次只读 probe(仅当操作员显式执行 partial-exact 恢复且存在待决 absent 行)。
