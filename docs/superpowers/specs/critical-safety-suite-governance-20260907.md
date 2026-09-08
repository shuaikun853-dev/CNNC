# Spec: Critical Safety CI Test Surface Governance (2026-09-07)

Branch: `refactor/critical-safety-suite-governance-20260907` (base `origin/prod/closeout-20260818` @ a1baf01).

## Problem

`.github/workflows/critical-safety-tests.yml` 用手工枚举的 50 个测试文件定义 Critical Safety Gate。
新增关键 regression test 时必须记得改 YAML;忘记改则 GitHub 绿灯不代表新测试被执行。
实证 Drift A:a1baf01 合入 production identity(ccfc226)与 zhaoshang progress capability(a1baf01),
两者的 regression 测试(`tests/test_production_identity.py`、`tests/test_zhaoshang_progress_probe.py`)
从未进入 gate;`tests/test_production_readiness.py`、dashboard 操作 API 三件套、调度频率契约、
batch zhaoshang adapter 同样漂移在 gate 之外(均为 hermetic)。

YAML 同时承担"执行入口"+"测试集合定义"两个职责 → 未通过 Deletion Test(删除清单后无第二权威来源)。

## Baseline (characterize before change)

- GitHub run #9 @ a1baf01(2026-09-07):50 files → **1386 passed, 1 deselected, 470.63s**(job 8m47s)。
- 唯一 deselect:`tests/test_remote_running_recovery.py::test_default_dependencies_wire_probe_and_write_gated_restart`,
  根因 = `build_default_dependencies` 构造期无条件读 `PROJECT_DATA_ROOT/*.curl`(app/services/incremental_audit_runtime.py:1879-1885),
  而该测试未 monkeypatch 数据根 → 隐式依赖生产机 gitignored data/。
- 本仓库无 pytest.ini/pyproject/setup.cfg;全部 tests 仅用内置 mark(parametrize/skipif),无 `pytestmark` 使用。

## Inventory (93 files)

- Gate 内 50(全 hermetic,CI 依赖两条 production-shape stub:E:\Python314 junction + auto_crawl_task.py 存在性桩,均不执行桩内容)。
- Gate 外 43:CRITICAL_SAFE 31 + ENV_SHAPED 1 + PRODUCTION_MACHINE_ONLY 7(3 优雅 skip + 4 硬依赖 E:\.A2026 且 CI 会 ERROR) + NON_CRITICAL 4 + EXTERNAL_INTEGRATION 0。

## Selected design

**文件级 pytest marker `critical_safety` = membership 唯一权威**:

1. `pytest.ini`(新建):注册 marker;`addopts = --strict-markers`(仓库现零自定义 mark,安全)。
2. Gate 文件(50 现有 + 8 新增)各加一行 `pytestmark = pytest.mark.critical_safety`。
3. `tests/test_critical_suite_governance.py`(新建 guard):闭合世界(每个 test_*.py ∈ marked ∪ EXCLUSIONS)、
   无重叠、无陈旧条目(rename/删除即红)、能力哨兵(§26 各能力至少一个 gated 文件)、
   子进程真实 collection(`-m critical_safety --collect-only --strict-markers`)且收集数 ≥ 迁移基线(单调性机械化)。
4. Workflow 命令改为 `python -m pytest tests -m critical_safety -q -l --timeout=600 --timeout-method=thread`;
   删除 50 文件清单与 --deselect;production-shape prep 步骤保留(collection 仍需导入全部 93 文件)。
   permissions/timeout/触发契约不变。
5. 本地与 CI 同一 selector(§24)。

### Rejected alternatives

- **Manifest txt**:membership 远离测试本体;Workflow→manifest 需转发层(§10D);rename 检测仍靠脚本;Locality 差。
- **目录结构 tests/critical/**:git mv 50+ 文件,破坏各文件 `parents[1]` REPO_ROOT 惯例与 blame;为重构而重构。
- **默认全收(不标即入)**:fail-open——忘记排除环境依赖测试即 CI 红或更糟;排除清单反而成隐藏权威;违反 §10B。

## Gate membership 变更(单调性:NEW ⊇ OLD)

新增 8 文件(全 hermetic,~120 tests):
`test_production_identity.py`(33,生产治理身份,§17)
`test_production_readiness.py`(10,只读体检规则,§18)
`test_zhaoshang_progress_probe.py`(16,新 Progress capability 探针 seam,§19)
`test_dashboard_actions_api.py`(22)/`test_dashboard_action_runner.py`(11)/`test_dashboard_action_real_subprocess.py`(5)
(Dashboard↔Recovery 操作面写路径 API 层,§26)
`test_scheduler_frequency_simulation.py`(11,IgnoreNew 单写者契约,Windows preview-only)
`test_batch_zhaoshang_adapter.py`(12,生产可选 batch 写后端)

不纳入(guard EXCLUSIONS,显式理由):
- `PRODUCTION_MACHINE_ONLY`×7:history_benchmark/price_audit_history/store_migration_upgrade(夹具缺失优雅 skip,CI 恒 skip 无价值);
  zhaoshang_priority_core/priority_boost/resume_modes/single_mall_templates(exec E:\.A2026 真实外部工具 = 对真实工具的特征化,CI 桩下执行即 FAKE_GREEN,禁止)。
- `NON_CRITICAL_TOOLING`×4:context_documents/export_current_monitor_status/query_saas_multi_code/workflow_router。
- `HERMETIC_UNGATED`×24:三步业务流/旧诊断/知识库等 hermetic 套件(2026-09-04 审计范围决策,非漂移);
  本地全量照跑;未来可加一行 marker 晋升。

## TEST_BUG 修复(独立 commit,生产代码零改动)

1. deselect 根因修复:`test_default_dependencies_wire_probe_and_write_gated_restart` 照
   `tests/test_incremental_audit_runtime.py:120-130` 既有模式 monkeypatch `CnncSaasSnapshotAdapter` 为替身、
   `load_curl_command` 为 stub → 测试 hermetic,--deselect 移除,本地/CI 行为一致。
2. `test_zhaoshang_priority_core.py` 模块级 exec 外部工具 → 缺真工具的机器上 collection 即炸;
   4 个 zhaoshang 外部工具文件加模块级 skip 守卫(存在性 + 符号探针区分"存在性桩"与"真工具"),
   PRODUCTION_MACHINE_ONLY 显式化,不再以 ERROR 假装失败。

## Commit plan

1. `test(ci): make default-dependencies wiring test hermetic`(TEST_BUG,deselect 根因)
2. `test(ci): module-level guards for zhaoshang external-tool tests`(TEST_BUG,PRODUCTION_MACHINE_ONLY 显式化)
3. `test(ci): classify critical safety membership via pytest marker`(behavior-neutral 机械 commit:pytest.ini + 58 行 marker)
4. `ci: switch critical safety workflow to single-source marker selector`(workflow + guard + TESTING.md)

## 验收

- 本地:selector 全绿;OLD 集合 ⊆ NEW 集合(节点级对比脚本);guard 全绿。
- GitHub Runner:push 分支后 CNNC Critical Safety Tests 真跑,COLLECTED/PASSED 与本地一致,deselected 仅为非 gate 集合。
- SAFETY:无 Secret、无生产写、permissions contents:read 不变。

## Runner 实证与二轮修复(2026-09-07 run #15 → #16)

Run #15(18cdd59)真实结果:4 failed / 1514 passed / 4 skipped / 860 deselected @ 24:05。
marker 选择器在 Runner 上与本地分毫不差(1514+4=1518),但暴露两类真问题:

1. **测试侧竞态(TEST_BUG)**:`test_dashboard_action_runner.py` 三个 closeout 用例的
   `_wait_done` 只等内存 handle 状态,而 `_supervise` 写序为"先翻转 status、后写持久化
   finished 记录"(app/services/dashboard_actions.py:414-431,设计内语义,未改)。
   重载 2 核 Runner 上读账本必输竞态。修复 = `_wait_done` 增加 run_dir 参数,
   同时等 finished 记录落地(commit 9b9a73a)。
2. **宿主时区形状**:`production_readiness.check_timezone` 除 FakeRunner 覆盖的 tzutil 外
   还审计真实宿主 UTC 偏移与本地日期(app/services/production_readiness.py:421-432)。
   Runner 默认 UTC → all-green 用例真 WARN。修复 = CI 构造生产形状
   `Set-Timezone "China Standard Time"`(与 E:\ junction 同类,宿主审计真实通过,非 mock;
   commit 1b9eec6)。
3. **运行时长**:1518 用例在冷 Runner 实测 24:05,30 分钟上限余量不足;
   timeout 30→40(有实测证据)+ `--durations=25` 观测(commit 1b9eec6)。

## Reviewer 返修(2026-09-08)

Review 发现两个治理缺口,按 TDD 返修(先特征化 RED,再修复 GREEN;两个特征化
测试先落 commit 907dd06 确认红,修复 commit 01e9eaa 转绿)。

### Finding A:治理守卫自身缺守卫(self-guard 缺失)

根因:守卫完全寄生在 membership 机制内部——自身带 `critical_safety` marker、
由同一 selector 收集。若 `tests/test_critical_suite_governance.py` 被整删:
6 个治理测试消失、闭合世界 glob 也看不到它、`1512 >= 1512` 收集数基线仍过,
而 workflow 对该文件零显式引用 → CI 仍绿(审计者与被审计者一起消失)。

修复:workflow 在 production-shape prep 与主套件之间插入 "Run governance
bootstrap" 步骤,直接文件选择执行守卫(`python -m pytest
tests/test_critical_suite_governance.py`,不经 `-m critical_safety` 过滤)。
契约三 Case:

| Case | 结果 |
|---|---|
| 守卫文件存在且通过 | bootstrap PASS |
| 守卫文件被整删 | bootstrap 步骤 pytest 非零退出(实测 exit 4,file not found)→ job 必红 |
| 守卫文件存在但守卫自身失败 | bootstrap 必红(先于约 24 分钟的主套件,快速失败) |

新增普通 critical 测试仍只需给文件加一行 marker,无需改 workflow——bootstrap
只引用守卫自身,不是第二份 membership 清单(不引用任何测试清单)。
特征化契约由 `test_workflow_bootstraps_governance_guard_before_critical_suite`
钉住(bootstrap 存在、直接文件选择、无 `-m critical_safety`、先于主套件、
主套件 selector 不变)。

### Finding B:整模块 skip 杀死集成覆盖

根因:`tests/test_zhaoshang_priority_boost.py` 用 `allow_module_level=True` 把
"production 参数缺外部文件(E:\.A2026,公司机存在/GitHub Runner 不存在)"上升成
整模块 skip——Runner 上 repo 内 integration 镜像(`zhaoshang_priority_boost_
integration/`,git 跟踪、纯 stdlib、hermetic)一起被跳过,gate 对该能力零覆盖。

修复语义:删除模块级 skip;文件加 `pytestmark = pytest.mark.critical_safety`
晋升 gated;fixture 保持 integration/production 双参数——integration 镜像
缺失仍 assert 硬失败(repo 内文件必在),production 参数仅在外部文件缺失时
参数级 `pytest.skip`(主套件 `-ra` 在 Runner 上直接列出 skip 原因)。
新增能力哨兵 `"Zhaoshang Priority Writer" → test_zhaoshang_priority_boost.py`。
特征化契约由 `test_priority_boost_module_does_not_skip_integration_mirror`
钉住(无 `allow_module_level`、双参数形态、参数级 skip 形态、membership 行)。

### core / resume_modes 维持排除的真实理由

- `test_zhaoshang_priority_core.py`:经 `CnncLegacyZhaoshangAdapter._load_module`
  加载真实外部工件,被测对象就是生产加载面本身——CI 存在性桩下执行 = FAKE_GREEN。
- `test_zhaoshang_resume_modes.py`:真实外部工具 + wrapper exec 模块且要求
  `run_auto_task` 属性,同样只有生产机有真实工件。
- repo 侧适配器行为已由 gated 的 `test_legacy_zhaoshang_adapter.py` 覆盖;
  两者晋升不增加 CI 信号,只会制造 FAKE_GREEN 面 → 保留排除,不晋升。

### floor 与 bootstrap 职责区分

| 机制 | 位置 | 检测什么 | 自身失效方式 | 兜底 |
|---|---|---|---|---|
| FLOOR_CRITICAL_COLLECTED | 守卫测试内 | 套件规模缩水(单调性基线) | 守卫文件被删时随守卫一起消失 | workflow bootstrap |
| governance bootstrap | workflow 步骤 | 守卫文件自身存在性/可执行性 | workflow 文件被删 = 触发契约丢失 | 触发契约(指向 prod 的 PR 必现 Check)+ review |

二者不可互相替代:FLOOR 检测"套件缩水",bootstrap 检测"审计者消失"。
FLOOR 随本轮晋升更新:1512 → **1568**(实测 `1568/2458 collected, 890
deselected`;构成 = 1518 既有 + 2 个特征化测试 + 48 个 boost 用例)。
主套件本地实测:1568 passed / 0 failed @ 402.78s。
