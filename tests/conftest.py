"""pytest 全局配置。

让所有测试默认禁用 audit_saas_excel_file.run_main 中的"爬取模板自动导出"，
避免测试用例污染真实的 ./爬取模板/<日期>/ 目录。

测试如果**需要**验证模板导出行为，请显式传 excluded_output_dir=tmp_path 并
unset 这个环境变量，或者直接调用 export_excluded_templates() 函数。
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _suppress_excluded_export(monkeypatch):
    """默认禁用 run_main 的爬取模板自动导出。

    个别测试如需启用，可在用例内 monkeypatch.delenv("AUDIT_SUPPRESS_EXCLUDED_EXPORT", raising=False)。
    """
    monkeypatch.setenv("AUDIT_SUPPRESS_EXCLUDED_EXPORT", "1")
    yield


@pytest.fixture(autouse=True)
def _hermetic_daily_curl_guard(monkeypatch, tmp_path):
    """让每日 cURL freshness guard 在测试中默认 hermetic（零真实 IO/网络）。

    incremental tick/finalize 在引擎前会调用
    cnnc_auth_refresh.ensure_daily_curl_fresh，其默认工件/维护锁路径在真实
    data/，不新鲜时会触发真实网络刷新。这里用透传包装兜底：调用方未显式
    注入 runner/工件路径/锁路径时（即生产 tick 挂钩路径），强制替换为
    stub runner + tmp 路径（stub 抛 AssertionError → guard 捕获后报
    DAILY_CURL_REFRESH_ERROR，恰好同时验证"guard 绝不上抛"语义）；
    单元/集成用例自己显式注入的 seam 一律原样透传，不影响被测函数本身。
    """
    from scripts import cnnc_auth_refresh

    real_ensure = cnnc_auth_refresh.ensure_daily_curl_fresh

    def _no_real_refresh(*_args, **_kwargs):
        raise AssertionError("tests must not trigger a real cURL refresh")

    def _hermetic_ensure(*args, **kwargs):
        kwargs.setdefault("runner", _no_real_refresh)
        kwargs.setdefault("result_path", tmp_path / "guard_curl_refresh_result.json")
        kwargs.setdefault("lock_path", tmp_path / "guard_maintenance.lock")
        return real_ensure(*args, **kwargs)

    monkeypatch.setattr(
        cnnc_auth_refresh, "ensure_daily_curl_fresh", _hermetic_ensure
    )
    yield
