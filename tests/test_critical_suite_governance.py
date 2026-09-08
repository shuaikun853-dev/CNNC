"""Critical Safety Suite 治理守卫:验证单一权威测试面没有漂移。

Membership 唯一权威 = 测试文件内的文件级 ``pytestmark = pytest.mark.critical_safety``
(marker 在 pytest.ini 注册并全局 --strict-markers;本地与 GitHub 同用
``python -m pytest tests -m critical_safety``)。本文件不复制 membership 清单,
只做闭合世界审计:未分类新文件、marker/排除重叠、陈旧排除条目、关键能力哨兵缺失、
收集数跌破单调性基线,任何一条都会让 Critical Gate 变红。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.critical_safety

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"

# 2026-09-07 迁移时实测 -m critical_safety 收集数(旧 50 文件命令为 1387 收集
# /1386 通过/1 deselect;修复 deselect 根因后该测试回归,再新增 8 个 gate 文件)。
# 合法缩小 gate 必须显式调低本值并在 commit 中说明理由。
FLOOR_CRITICAL_COLLECTED = 1512  # 由迁移实测回填

MARKER_PATTERN = re.compile(
    r"^pytestmark\s*=\s*\[?\s*pytest\.mark\.critical_safety\b", re.MULTILINE
)

# 闭合世界补集:不进 gate 的文件必须在此登记理由(新增未登记文件 → 守卫红)。
EXCLUSIONS: dict[str, str] = {
    # HERMETIC_UNGATED: 纯 hermetic 但属 2026-09-04 审计范围外的业务流/诊断套件;
    # 本地全量照跑;晋升 gate 只需给文件加一行 pytestmark。
    "test_approve_saas_audit_excel.py": "HERMETIC_UNGATED",
    "test_cnnc_auth_browser_harvest.py": "HERMETIC_UNGATED",
    "test_cnnc_detail_smoke.py": "HERMETIC_UNGATED",
    "test_diagnose_integration.py": "HERMETIC_UNGATED",
    "test_human_approval_guard.py": "HERMETIC_UNGATED",
    "test_incremental_audit_operations.py": "HERMETIC_UNGATED",
    "test_incremental_audit_runtime.py": "HERMETIC_UNGATED",
    "test_knowledge_safety.py": "HERMETIC_UNGATED",
    "test_model_parallel_batches.py": "HERMETIC_UNGATED",
    "test_monitor_stb_fallback.py": "HERMETIC_UNGATED",
    "test_process_monitor_zs_result_mapping.py": "HERMETIC_UNGATED",
    "test_push_saas_commodity_price.py": "HERMETIC_UNGATED",
    "test_r35_workflow_alignment.py": "HERMETIC_UNGATED",
    "test_reject_saas_audit_excel.py": "HERMETIC_UNGATED",
    "test_saas_audit_core.py": "HERMETIC_UNGATED",
    "test_saas_flow_diagnostic.py": "HERMETIC_UNGATED",
    "test_workflow_api.py": "HERMETIC_UNGATED",
    "test_workflow_knowledge_adapter.py": "HERMETIC_UNGATED",
    "test_work_01_rules.py": "HERMETIC_UNGATED",
    "test_work_01_saas_approval_json_audit.py": "HERMETIC_UNGATED",
    "test_work_02_price_feedback_framework.py": "HERMETIC_UNGATED",
    "test_work_02_rules.py": "HERMETIC_UNGATED",
    "test_zhaoshang_env_concurrency.py": "HERMETIC_UNGATED",
    "test_zhaoshang_task_phases.py": "HERMETIC_UNGATED",
    # PRODUCTION_MACHINE_ONLY: 依赖生产机真实外部工具或公司机夹具;
    # CI 桩下执行等于 FAKE_GREEN(禁止),历史夹具类在 CI 恒 skip 无信号价值。
    "test_incremental_history_benchmark.py": "PRODUCTION_MACHINE_ONLY",
    "test_incremental_price_audit_history.py": "PRODUCTION_MACHINE_ONLY",
    "test_store_migration_upgrade.py": "PRODUCTION_MACHINE_ONLY",
    "test_zhaoshang_priority_boost.py": "PRODUCTION_MACHINE_ONLY",
    "test_zhaoshang_priority_core.py": "PRODUCTION_MACHINE_ONLY",
    "test_zhaoshang_resume_modes.py": "PRODUCTION_MACHINE_ONLY",
    "test_zhaoshang_single_mall_templates.py": "PRODUCTION_MACHINE_ONLY",
    # NON_CRITICAL_TOOLING: 文档预算/报表工具/辅助脚本/关键词路由。
    "test_context_documents.py": "NON_CRITICAL_TOOLING",
    "test_export_current_monitor_status.py": "NON_CRITICAL_TOOLING",
    "test_query_saas_multi_code.py": "NON_CRITICAL_TOOLING",
    "test_workflow_router.py": "NON_CRITICAL_TOOLING",
}

# 关键能力哨兵:每个生产能力至少一个 gated regression 文件(缺失 → 守卫红)。
CAPABILITY_SENTINELS: dict[str, str] = {
    "Customer Price": "test_customer_price_semantics.py",
    "DailyAuditStore": "test_daily_audit_store.py",
    "Incremental Workflow": "test_incremental_price_audit.py",
    "Finalize": "test_incremental_price_audit_finalize.py",
    "Recovery": "test_recovery_facade_contract.py",
    "Zhaoshang Legacy Adapter": "test_legacy_zhaoshang_adapter.py",
    "Zhaoshang Progress Probe": "test_zhaoshang_progress_probe.py",
    "Zhaoshang Batch Adapter": "test_batch_zhaoshang_adapter.py",
    "Manual Re-export": "test_manual_reexport.py",
    "Manual Rebuild": "test_manual_rebuild.py",
    "Production Identity": "test_production_identity.py",
    "Production Readiness": "test_production_readiness.py",
    "Auth": "test_auth_manager.py",
    "Dashboard API": "test_dashboard_api.py",
    "Dashboard Actions": "test_dashboard_actions_api.py",
    "Scheduler Contract": "test_scheduler_frequency_simulation.py",
    "Safety Guards": "test_safety_guard.py",
}


def _all_test_files() -> set[str]:
    return {path.name for path in TESTS_DIR.glob("test_*.py")}


def _marked_files() -> set[str]:
    marked = set()
    for path in TESTS_DIR.glob("test_*.py"):
        if MARKER_PATTERN.search(path.read_text(encoding="utf-8")):
            marked.add(path.name)
    return marked


def test_closed_world_every_test_file_is_classified():
    unclassified = _all_test_files() - _marked_files() - set(EXCLUSIONS)
    assert not unclassified, (
        "未分类测试文件——进 gate 就加一行 pytestmark = pytest.mark.critical_safety,"
        "不进就在 EXCLUSIONS 登记理由: " + ", ".join(sorted(unclassified))
    )


def test_no_file_is_both_marked_and_excluded():
    overlap = _marked_files() & set(EXCLUSIONS)
    assert not overlap, "同一文件既标记 critical_safety 又被排除: " + ", ".join(sorted(overlap))


def test_exclusions_reference_existing_files():
    existing = _all_test_files()
    stale = sorted(name for name in EXCLUSIONS if name not in existing)
    assert not stale, "EXCLUSIONS 指向不存在的文件(rename/删除后请同步): " + ", ".join(stale)


def test_exclusion_reasons_are_known():
    allowed = {"HERMETIC_UNGATED", "PRODUCTION_MACHINE_ONLY", "NON_CRITICAL_TOOLING"}
    unknown = {name: reason for name, reason in EXCLUSIONS.items() if reason not in allowed}
    assert not unknown, f"未知理由码(允许: {sorted(allowed)}): {unknown}"


def test_capability_sentinels_are_marked():
    marked = _marked_files()
    missing = {cap: name for cap, name in CAPABILITY_SENTINELS.items() if name not in marked}
    assert not missing, f"关键能力缺少 gated regression 文件: {missing}"


def test_workflow_bootstraps_governance_guard_before_critical_suite():
    workflow_path = REPO_ROOT / ".github" / "workflows" / "critical-safety-tests.yml"
    text = workflow_path.read_text(encoding="utf-8")
    # 每个步骤块以步骤名开头,直到下一个 "- name:" 行。
    blocks = re.split(r"(?m)^\s*- name: ", text)
    steps = [(block.splitlines()[0], block) for block in blocks[1:]]

    bootstrap_blocks = [
        block for name, block in steps if name.startswith("Run governance bootstrap")
    ]
    assert bootstrap_blocks, (
        "契约:workflow 必须先经 'Run governance bootstrap' 步骤直接执行本守卫文件"
        "——本守卫寄生在 membership 机制内部(自身带 marker),被整删时"
        "审计者与被审计者一起消失、收集数基线仍过,必须有外部引用兜底"
    )
    assert "python -m pytest tests/test_critical_suite_governance.py" in bootstrap_blocks[0], (
        "契约:bootstrap 步骤必须直接文件选择执行守卫"
        "(python -m pytest tests/test_critical_suite_governance.py)"
    )
    assert "-m critical_safety" not in bootstrap_blocks[0], (
        "契约:bootstrap 不得经 -m critical_safety 过滤——守卫文件被整删时"
        "marker 选择器收集到空集,bootstrap 会被掏空失去 self-guard 作用"
    )

    suite_blocks = [
        block for name, block in steps if name.startswith("Run critical safety tests")
    ]
    assert suite_blocks, "契约:workflow 必须保留 'Run critical safety tests' 主测试步骤"
    assert "python -m pytest tests -m critical_safety" in suite_blocks[0], (
        "契约:主测试步骤必须保持单一 marker selector"
        "(python -m pytest tests -m critical_safety),membership 权威不得漂移回 workflow"
    )

    names = [name for name, _block in steps]
    bootstrap_index = next(
        i for i, name in enumerate(names) if name.startswith("Run governance bootstrap")
    )
    suite_index = next(
        i for i, name in enumerate(names) if name.startswith("Run critical safety tests")
    )
    assert bootstrap_index < suite_index, (
        "契约:治理 bootstrap 必须先于 critical suite 执行"
        "(守卫自身红了就不必再跑二十多分钟的主套件)"
    )


def test_priority_boost_module_does_not_skip_integration_mirror():
    boost_source = (TESTS_DIR / "test_zhaoshang_priority_boost.py").read_text(
        encoding="utf-8"
    )
    assert "allow_module_level" not in boost_source, (
        "契约:priority boost 禁止模块级 skip——外部工具缺失只允许跳过 production 参数,"
        "repo 内 integration 镜像(纯 stdlib、hermetic)必须在 CI 真跑"
    )
    assert re.search(
        r"params\s*=\s*\[\s*INTEGRATION_HELPER,\s*EXTERNAL_HELPER,\s*\]", boost_source
    ), "契约:fixture 必须保持 integration/production 双参数形态"
    assert re.search(
        r"path\s*==\s*EXTERNAL_HELPER\s+and\s+not\s+path\.is_file\(\)", boost_source
    ), "契约:production 参数必须且只能在参数级 skip(仅当外部文件缺失时)"
    assert "test_zhaoshang_priority_boost.py" in _marked_files(), (
        "契约:priority boost 文件必须带 critical_safety membership 行(pytestmark)"
    )


def test_marker_selector_collects_at_least_floor():
    argv = [
        sys.executable, "-m", "pytest", "tests",
        "-m", "critical_safety", "--collect-only", "-q", "--strict-markers",
    ]
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        argv, cwd=str(REPO_ROOT), env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, (
        "critical_safety 选择器收集失败(未知 marker/收集错误/空集都会非零退出):\n"
        + proc.stdout[-2000:] + proc.stderr[-2000:]
    )
    # pytest 9 尾行形如 "1518/2456 tests collected (938 deselected)":
    # 斜杠左侧才是 -m 选中数;无斜杠(零 deselect)时总数即选中数。
    selected = re.findall(r"(\d+)/\d+ tests? collected", proc.stdout)
    if not selected:
        selected = re.findall(r"(\d+) tests? collected", proc.stdout)
    assert selected, proc.stdout[-2000:]
    collected = int(selected[-1])
    assert collected >= FLOOR_CRITICAL_COLLECTED, (
        f"critical_safety 收集数 {collected} 低于单调性基线 {FLOOR_CRITICAL_COLLECTED};"
        "合法缩小 gate 必须显式调低 FLOOR_CRITICAL_COLLECTED 并说明理由"
    )
