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
