# -*- coding: utf-8 -*-
"""closeout 命令每日 cURL freshness guard 集成测试（2026-09-10）。

漏洞：_run_runtime_command_locked 的每日 cURL guard 原先只挂在 tick/finalize
分支内部，而 closeout 在命令分发处提前 return——closeout 日的首次快照抓取
（run_closeout 内部多个 pass 全走 run_incremental_tick）没有任何预检。

契约（全部 hermetic：tmp 路径 + fake orchestrator/fake runner，零真实 IO/
网络/子进程；不触碰真实 data/）：
- Case 1 closeout 进入 orchestrator 前 guard 恰好 1 次，且先于 run_closeout
- Case 2 今日工件 CURL_REFRESH_OK 3/3 → runner 0 次，closeout 照常执行
- Case 3 昨日工件（含 finalize 期证据字段）+ 今日首次命令=closeout →
  daily refresh 恰好 1 次（计数 runner 显式注入）
- Case 4 fake run_closeout 内部多个 incremental pass → guard 仍恰好 1 次
- Case 5 guard 返回 SSO_LOGIN_REQUIRED → closeout 照常执行不 raise
- Case 6 harvest-only / drain-only → guard 0 次（语义保持）
- §10 附加：deps 构建先于 guard，guard 消费 build_default_dependencies 产物
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import app.services.incremental_audit_runtime as runtime_module
from app.services.daily_audit_store import DailyAuditStore
from app.services.incremental_audit_runtime import run_runtime_command, runtime_paths
from app.workflows.incremental_price_audit import (
    DiscoveredState,
    TickDependencies,
    TickSummary,
)
from scripts import cnnc_auth_refresh

TODAY = "2026-09-10"
YESTERDAY = "2026-09-09"
# conftest._hermetic_daily_curl_guard 给 guard 工件/锁路径的 tmp 默认名。
GUARD_RESULT_NAME = "guard_curl_refresh_result.json"


def _deps() -> TickDependencies:
    """最小只读依赖：本文件所有场景都 fake 掉 orchestrator/tick，不真正执行。"""
    return TickDependencies(
        discover=lambda timeout: DiscoveredState(
            observed_at=datetime(2026, 9, 10, 10, 1, tzinfo=UTC),
            headers=["vCommodityId"],
            rows=[],
            source_fingerprints=["source"],
            customer_fingerprints=["customer"],
            evidence={"saas_count": 0, "customer_count": 0},
        ),
    )


def _write_artifact(result_path, *, business_date: str) -> None:
    """按 run_curl_refresh 工件 schema 写 fake 结果工件。

    判定只认 business_date + outcome + 同步数；completed_at/trigger/at_redacted
    等 finalize 期证据字段一律不参与放行（昨日工件即证）。
    """
    payload = {
        "completed_at": "2026-09-10T00:00:00+00:00",
        "business_date": business_date,
        "trigger": "daily-preflight",
        "outcome": "CURL_REFRESH_OK",
        "curl_expected": 3,
        "curl_synced": 3,
        "curl_failed_files": [],
        "at_redacted": "AT-XXXX***",
    }
    Path(result_path).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _fake_closeout(seq):
    """fake run_closeout：记录调用序列，返回一个终止型 summary。"""

    def closeout(store, lock, config, deps, closeout_config, **_kwargs):
        seq.append(("closeout", config.business_date))
        return TickSummary("CLOSEOUT_DEADLINE", (), {}, {}, None), None, None, None

    return closeout


def _counting_guard_probe(seq, runner):
    """把 runtime 挂钩点换成"计数 + 真实 guard 透传"包装。

    conftest 的 hermetic autouse 已把 ensure_daily_curl_fresh 包成透传包装；
    这里在其外再包一层：显式注入计数 runner（setdefault 优先级高于 stub），
    工件/锁路径仍由 hermetic 包装默认到本用例 tmp_path。这样集成用例观测到
    的是真实新鲜判定逻辑 + 真实 runner 调用次数。
    """
    hermetic_ensure = cnnc_auth_refresh.ensure_daily_curl_fresh

    def probe(*args, **kwargs):
        seq.append(("guard", kwargs.get("business_date")))
        kwargs.setdefault("runner", runner)
        return hermetic_ensure(*args, **kwargs)

    return probe


def test_case1_closeout_runs_daily_curl_guard_once_before_orchestrator(
    tmp_path, monkeypatch
):
    """closeout 必须在进入 orchestrator 前恰好跑 1 次每日 cURL guard。"""
    seq = []

    def guard(*_args, **kwargs):
        seq.append(("guard", kwargs.get("business_date")))
        return "DAILY_CURL_ALREADY_FRESH"

    monkeypatch.setattr(
        runtime_module.cnnc_auth_refresh, "ensure_daily_curl_fresh", guard
    )
    monkeypatch.setattr(runtime_module, "run_closeout", _fake_closeout(seq))

    result = run_runtime_command(
        "closeout",
        business_date=TODAY,
        runtime_root=tmp_path / "runtime",
        dependencies=_deps(),
    )

    assert result.tick.outcome == "CLOSEOUT_DEADLINE"
    # 恰好 1 次、先 guard 后 closeout；RED 时 guard_calls==0。
    assert seq == [("guard", TODAY), ("closeout", TODAY)]


def test_case2_today_fresh_artifact_closeout_never_refreshes(tmp_path, monkeypatch):
    """今日工件 CURL_REFRESH_OK 3/3 → runner 0 次，closeout 照常执行。"""
    _write_artifact(
        tmp_path / GUARD_RESULT_NAME, business_date=TODAY
    )  # conftest 把 guard 工件路径默认到这里
    seq = []
    runner_calls = []

    def counting_runner(**kwargs):
        runner_calls.append(dict(kwargs))
        return 0

    monkeypatch.setattr(
        runtime_module.cnnc_auth_refresh,
        "ensure_daily_curl_fresh",
        _counting_guard_probe(seq, counting_runner),
    )
    monkeypatch.setattr(runtime_module, "run_closeout", _fake_closeout(seq))

    result = run_runtime_command(
        "closeout",
        business_date=TODAY,
        runtime_root=tmp_path / "runtime",
        dependencies=_deps(),
    )

    assert result.tick.outcome == "CLOSEOUT_DEADLINE"
    assert runner_calls == []  # 今日已证全同步 → 零刷新动作
    assert seq == [("guard", TODAY), ("closeout", TODAY)]


def test_case3_yesterday_artifact_first_command_closeout_refreshes_once(
    tmp_path, monkeypatch
):
    """昨日工件 + 今日首次命令=closeout → daily refresh 恰好 1 次。"""
    _write_artifact(tmp_path / GUARD_RESULT_NAME, business_date=YESTERDAY)
    seq = []
    runner_calls = []

    def counting_runner(**kwargs):
        runner_calls.append(dict(kwargs))
        _write_artifact(kwargs["result_path"], business_date=TODAY)  # rc=0 全同步
        return 0

    monkeypatch.setattr(
        runtime_module.cnnc_auth_refresh,
        "ensure_daily_curl_fresh",
        _counting_guard_probe(seq, counting_runner),
    )
    monkeypatch.setattr(runtime_module, "run_closeout", _fake_closeout(seq))

    result = run_runtime_command(
        "closeout",
        business_date=TODAY,
        runtime_root=tmp_path / "runtime",
        dependencies=_deps(),
    )

    assert result.tick.outcome == "CLOSEOUT_DEADLINE"
    assert seq == [("guard", TODAY), ("closeout", TODAY)]
    assert len(runner_calls) == 1  # 恰好刷新一次，不重试不叠加
    assert runner_calls[0]["business_date"] == TODAY
    assert runner_calls[0]["trigger"] == "daily-preflight"


def test_case4_closeout_multiple_incremental_passes_still_guard_once(
    tmp_path, monkeypatch
):
    """run_closeout 内部多个 incremental pass → guard 仍恰好 1 次。"""
    seq = []
    tick_calls = []

    def guard(*_args, **kwargs):
        seq.append(("guard", kwargs.get("business_date")))
        return "DAILY_CURL_ALREADY_FRESH"

    def fake_tick(*_args, **_kwargs):
        tick_calls.append(1)
        return TickSummary("PENDING_RETRY", (), {}, {}, None)

    def multi_pass_closeout(store, lock, config, deps, closeout_config, **_kwargs):
        seq.append(("closeout", config.business_date))
        for _ in range(3):  # 模拟 run_closeout 多 pass 全走 run_incremental_tick
            runtime_module.run_incremental_tick(
                store, lock, config, deps, finalize=True
            )
        return TickSummary("CLOSEOUT_DEADLINE", (), {}, {}, None), None, None, None

    monkeypatch.setattr(
        runtime_module.cnnc_auth_refresh, "ensure_daily_curl_fresh", guard
    )
    monkeypatch.setattr(runtime_module, "run_incremental_tick", fake_tick)
    monkeypatch.setattr(runtime_module, "run_closeout", multi_pass_closeout)

    result = run_runtime_command(
        "closeout",
        business_date=TODAY,
        runtime_root=tmp_path / "runtime",
        dependencies=_deps(),
    )

    assert result.tick.outcome == "CLOSEOUT_DEADLINE"
    assert len(tick_calls) == 3  # 多 pass 真实发生
    assert seq == [("guard", TODAY), ("closeout", TODAY)]  # guard 不随 pass 重复


def test_case5_sso_login_required_guard_does_not_block_closeout(
    tmp_path, monkeypatch
):
    """guard 返回 SSO_LOGIN_REQUIRED → closeout 照常执行、绝不 raise。"""
    closeout_calls = []

    def guard(*_args, **_kwargs):
        return "SSO_LOGIN_REQUIRED"

    def closeout(store, lock, config, deps, closeout_config, **_kwargs):
        closeout_calls.append(config.business_date)
        return TickSummary("CLOSEOUT_DEADLINE", (), {}, {}, None), None, None, None

    monkeypatch.setattr(
        runtime_module.cnnc_auth_refresh, "ensure_daily_curl_fresh", guard
    )
    monkeypatch.setattr(runtime_module, "run_closeout", closeout)

    result = run_runtime_command(
        "closeout",
        business_date=TODAY,
        runtime_root=tmp_path / "runtime",
        dependencies=_deps(),
    )

    assert result.tick.outcome == "CLOSEOUT_DEADLINE"
    assert closeout_calls == [TODAY]  # guard 只上报状态，不阻断命令


def test_case6_lifecycle_only_commands_never_run_daily_guard(tmp_path, monkeypatch):
    """harvest-only / drain-only 只收既有远端任务 → guard 0 次（语义保持）。"""
    guard_calls = []

    def guard(*_args, **kwargs):
        guard_calls.append(kwargs.get("business_date"))
        return "DAILY_CURL_ALREADY_FRESH"

    monkeypatch.setattr(
        runtime_module.cnnc_auth_refresh, "ensure_daily_curl_fresh", guard
    )
    monkeypatch.setattr(
        runtime_module,
        "run_harvest_only",
        lambda *_args, **_kwargs: TickSummary("HARVESTED", (), {}, {}, None),
    )
    monkeypatch.setattr(
        runtime_module,
        "run_existing_remote_drain",
        lambda *_args, **_kwargs: TickSummary("DRAINED", (), {}, {}, None),
    )
    root = tmp_path / "runtime"
    paths = runtime_paths(TODAY, root)
    paths.run_dir.mkdir(parents=True)
    DailyAuditStore(paths.state_path).ensure_run(TODAY)

    harvest = run_runtime_command(
        "harvest-only",
        business_date=TODAY,
        runtime_root=root,
        dependencies=_deps(),
        runtime_budget_seconds=300,
    )
    drain = run_runtime_command(
        "drain-only",
        business_date=TODAY,
        runtime_root=root,
        dependencies=_deps(),
        runtime_budget_seconds=300,
    )

    assert harvest.tick.outcome == "HARVESTED"
    assert drain.tick.outcome == "DRAINED"
    assert guard_calls == []  # drain-only/harvest-only 不触发 guard


def test_closeout_guard_consumes_freshly_built_dependencies(tmp_path, monkeypatch):
    """§10 附加：deps 构建先于 guard，guard 消费 build_default_dependencies 产物。"""
    seq = []
    sentinel_provider = object()
    sentinel_deps = _deps()
    sentinel_deps.cnnc_provider = sentinel_provider  # 构建产物携带 provider 属性

    def fake_build_default_dependencies(**_kwargs):
        seq.append("build_default_dependencies")
        return sentinel_deps

    def guard(*_args, **kwargs):
        seq.append(("guard", kwargs.get("provider")))
        return "DAILY_CURL_ALREADY_FRESH"

    monkeypatch.setattr(
        runtime_module, "build_default_dependencies", fake_build_default_dependencies
    )
    monkeypatch.setattr(
        runtime_module.cnnc_auth_refresh, "ensure_daily_curl_fresh", guard
    )
    monkeypatch.setattr(runtime_module, "run_closeout", _fake_closeout(seq))

    result = run_runtime_command(
        "closeout", business_date=TODAY, runtime_root=tmp_path / "runtime"
    )  # 不注入 dependencies → 走 build_default_dependencies

    assert result.tick.outcome == "CLOSEOUT_DEADLINE"
    # guard 收到的 provider 正是构建产物上的属性：deps 先构建、guard 后消费。
    assert seq == [
        "build_default_dependencies",
        ("guard", sentinel_provider),
        ("closeout", TODAY),
    ]
