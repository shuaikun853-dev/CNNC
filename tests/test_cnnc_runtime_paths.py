# -*- coding: utf-8 -*-
"""scripts/cnnc_runtime.py 路径治理测试：C 盘可控写入迁移 E 盘 runtime/temp。

被测契约（scripts/cnnc_runtime.py，全项目唯一运行时路径 resolver）：

- Case A  runtime_root() 默认 = PROJECT_ROOT/"runtime" 且目录存在
          （CI 安全：与 PROJECT_ROOT 做相对比较，不硬断盘符）
- Case B  temp_root() = runtime_root()/"temp"；cnnc_auth_browser 的
          login/harvest/smoke 三个 cmd 的 tempfile.mkdtemp 全部显式
          dir=str(temp_root())（AST 源码级断言，兼容既有
          "tempfile.mkdtemp" in source 断言）
- Case C  cnnc_auth_refresh 的 CURL_REFRESH_RESULT_PATH /
          LOGIN_RECOVERY_RESULT_PATH 位于 PROJECT_ROOT 下（相对断言）
- Case D  cnnc_auth_refresh 的 AUTH_MAINTENANCE_LOCK_PATH 位于
          PROJECT_ROOT 下（相对断言）
- Case E  增量审计 runtime_paths() 默认根
          （runtime/incremental_price_audit）位于 PROJECT_ROOT 下
- Case F  force_process_tree_e_temp 幂等设置 TEMP/TMP/TMPDIR =
          str(temp_root())，且不碰其他键；绝不触碰注册表/系统全局
- Case H  （skipif 非 win32，盘符仅 Windows 有意义）
          CNNC_RUNTIME_ROOT 指向非 E: 盘 → fail-fast RuntimeError 且
          不创建目录；E: 盘路径 → 原样返回并创建；空字符串 → 回落默认

引用覆盖（不复制实现，行为语义由下列既有用例守护；本迁移对其零改动）：
- Case I  GET /api/status 只读
          → tests/test_dashboard_api.py 的 status 只读用例
- Case J  manual --refresh-curl force 3/3
          → tests/test_cnnc_curl_refresh.py
- Case K  daily guard Case A/B
          → tests/test_cnnc_daily_curl_guard.py
- Case L  single-flight 互斥（login-and-recover vs refresh-curl 双向）
          → tests/test_dashboard_api.py 的互斥用例
- Case M  login 恢复链（login→harvest→exchange→verify→publish）
          → tests/test_cnnc_auth_recover.py
- Case N  浏览器登录/harvest 行为与源码级断言
          → tests/test_cnnc_auth_browser_harvest.py、
            tests/test_cnnc_auth_browser_login_autofill.py、
            tests/test_cnnc_login_autofill.py
"""

from __future__ import annotations

import ast
import inspect
import os
from pathlib import Path

import pytest

from scripts import cnnc_auth_refresh
from scripts import cnnc_runtime


def _mkdtemp_calls(source: str) -> list[ast.Call]:
    """提取模块源码中所有 tempfile.mkdtemp(...) 调用节点。"""
    tree = ast.parse(source)
    calls = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "mkdtemp"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "tempfile"
        ):
            calls.append(node)
    return calls


# --------------------------------------------------------------------- Case A


def test_case_a_default_runtime_root_is_repo_runtime_dir(monkeypatch):
    monkeypatch.delenv("CNNC_RUNTIME_ROOT", raising=False)
    root = cnnc_runtime.runtime_root()
    # 相对断言（CI 安全）：与 PROJECT_ROOT 同源推导，不硬断盘符
    assert root == cnnc_runtime.PROJECT_ROOT / "runtime"
    assert root.is_dir()


def test_case_a_project_root_is_repo_root():
    assert cnnc_runtime.PROJECT_ROOT == Path(cnnc_runtime.__file__).resolve().parents[1]
    assert (cnnc_runtime.PROJECT_ROOT / "scripts" / "cnnc_runtime.py").is_file()


# --------------------------------------------------------------------- Case B


def test_case_b_temp_root_is_runtime_temp_subdir():
    temp_root = cnnc_runtime.temp_root()
    assert temp_root == cnnc_runtime.runtime_root() / "temp"
    assert temp_root.is_dir()


def test_case_b_all_browser_mkdtemp_calls_pin_temp_root_dir():
    source = Path(
        __import__("scripts.cnnc_auth_browser", fromlist=["__file__"]).__file__
    ).read_text(encoding="utf-8")
    calls = _mkdtemp_calls(source)
    assert len(calls) == 3, "cnnc_auth_browser 应恰有 3 处 mkdtemp（login/harvest/smoke）"
    for call in calls:
        dir_kwargs = [kw for kw in call.keywords if kw.arg == "dir"]
        assert dir_kwargs, (
            "line %d: tempfile.mkdtemp 缺 dir=str(temp_root())，会落系统 %%TEMP%%（C:）"
            % call.lineno
        )
        assert ast.unparse(dir_kwargs[0].value) == "str(temp_root())", (
            "line %d: mkdtemp 的 dir 必须是 str(temp_root())" % call.lineno
        )


@pytest.mark.parametrize("func_name", ["cmd_login", "cmd_harvest", "cmd_smoke"])
def test_case_b_cmd_sources_keep_mkdtemp_and_pin_temp_root(func_name):
    import scripts.cnnc_auth_browser as browser

    source = inspect.getsource(getattr(browser, func_name))
    # 兼容既有源码级断言风格（tests/test_cnnc_auth_browser_harvest.py）
    assert "tempfile.mkdtemp" in source
    assert "dir=str(temp_root())" in source


# ----------------------------------------------------------------- Case C / D


def test_case_c_curl_refresh_result_path_under_project_root():
    assert cnnc_auth_refresh.CURL_REFRESH_RESULT_PATH.is_relative_to(
        cnnc_runtime.PROJECT_ROOT
    )


def test_case_c_login_recovery_result_path_under_project_root():
    assert cnnc_auth_refresh.LOGIN_RECOVERY_RESULT_PATH.is_relative_to(
        cnnc_runtime.PROJECT_ROOT
    )


def test_case_d_auth_maintenance_lock_path_under_project_root():
    assert cnnc_auth_refresh.AUTH_MAINTENANCE_LOCK_PATH.is_relative_to(
        cnnc_runtime.PROJECT_ROOT
    )


# --------------------------------------------------------------------- Case E


def test_case_e_incremental_audit_runtime_paths_under_project_root():
    from app.services.incremental_audit_runtime import (
        DEFAULT_RUNTIME_ROOT,
        runtime_paths,
    )

    assert DEFAULT_RUNTIME_ROOT.is_relative_to(cnnc_runtime.PROJECT_ROOT)
    assert DEFAULT_RUNTIME_ROOT == (
        cnnc_runtime.PROJECT_ROOT / "runtime" / "incremental_price_audit"
    )
    paths = runtime_paths("2026-09-10")
    assert paths.run_dir.is_relative_to(cnnc_runtime.PROJECT_ROOT)
    assert paths.state_path.is_relative_to(cnnc_runtime.PROJECT_ROOT)
    assert paths.lock_path.is_relative_to(cnnc_runtime.PROJECT_ROOT)
    assert paths.detail_lock_path.is_relative_to(cnnc_runtime.PROJECT_ROOT)
    assert paths.final_output_path.is_relative_to(cnnc_runtime.PROJECT_ROOT)


# --------------------------------------------------------------------- Case F


def test_case_f_force_process_tree_e_temp_sets_three_keys_and_keeps_others():
    env = {
        "TEMP": r"C:\Temp\before",
        "TMP": r"C:\Temp\before",
        "TMPDIR": "/tmp/before",
        "PATH": "keep-me",
    }
    returned = cnnc_runtime.force_process_tree_e_temp(env)
    expected = str(cnnc_runtime.temp_root())
    assert returned == cnnc_runtime.temp_root()
    assert env["TEMP"] == expected
    assert env["TMP"] == expected
    assert env["TMPDIR"] == expected
    assert env["PATH"] == "keep-me", "必须不碰 TEMP/TMP/TMPDIR 之外的键"


def test_case_f_force_process_tree_e_temp_is_idempotent():
    env = {"TEMP": r"C:\Temp\before"}
    first = cnnc_runtime.force_process_tree_e_temp(env)
    second = cnnc_runtime.force_process_tree_e_temp(env)
    assert first == second
    assert env["TEMP"] == env["TMP"] == env["TMPDIR"] == str(first)


# --------------------------------------------------------------------- Case H

_WIN32_ONLY = pytest.mark.skipif(
    os.name != "nt", reason="盘符校验仅 Windows（win32）有意义；CI Linux 跳过"
)


@_WIN32_ONLY
def test_case_h_non_e_drive_rejected_fail_fast(monkeypatch):
    target = r"C:\tmp\cnnc_runtime_case_h_must_not_exist"
    monkeypatch.setenv("CNNC_RUNTIME_ROOT", target)
    with pytest.raises(RuntimeError, match="CNNC_RUNTIME_ROOT"):
        cnnc_runtime.runtime_root()
    assert not Path(target).exists(), "fail-fast 路径不得在 C: 创建任何目录"


@_WIN32_ONLY
def test_case_h_e_drive_path_accepted(tmp_path, monkeypatch):
    # tmp_path 落在 --basetemp=.pytest_tmp → 仓库内 → E: 盘
    target = tmp_path / "case_h_e_drive_runtime"
    monkeypatch.setenv("CNNC_RUNTIME_ROOT", str(target))
    assert cnnc_runtime.runtime_root() == target
    assert target.is_dir()


@_WIN32_ONLY
def test_case_h_empty_value_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("CNNC_RUNTIME_ROOT", "")
    assert cnnc_runtime.runtime_root() == cnnc_runtime.PROJECT_ROOT / "runtime"
