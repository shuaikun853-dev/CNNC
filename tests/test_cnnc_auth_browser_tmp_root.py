# -*- coding: utf-8 -*-
"""scripts/cnnc_auth_browser.py 一次性临时 Profile 落盘位置与陈旧清扫测试（Case 22）。

背景（2026-09-15 生产事故）：harvest 子进程被调用方 30 秒预算硬杀
（TerminateProcess）后 finally rmtree 不会执行，tempfile.mkdtemp 出的一次性
Chrome Profile（每个约 78MB）滞留原地；原先落在系统 %TEMP%（C 盘），146 个
残留约 5GB 压满 C 盘，计划任务 PowerShell 包装脚本 Add-Type 编译临时文件失败、
整点审计 tick 静默崩溃。

修复契约：三处 cmd_* 的一次性临时 Profile 统一经 _make_browser_profile_dir
落到仓库 E 盘 runtime/browser_tmp（.gitignore 已忽略 runtime/），创建前清扫
超过 STALE_BROWSER_TMP_MAX_AGE_SECONDS 的 cnnc_login_/cnnc_harvest_ 陈旧残留
自愈；E 盘不可用时回退系统 TEMP，认证链路不因辅助目录中断。

全部离线：只动 tmp_path 下的目录，不启动 Chrome、不访问网络。
"""

from __future__ import annotations

import inspect
import os
import time
from pathlib import Path

import pytest

import scripts.cnnc_auth_browser as browser


# ------------------------------------------------- 常量契约


def test_case22_constants_point_into_repo_runtime_and_cover_windows():
    """BROWSER_TMP_ROOT 在仓库 runtime/ 下；6 小时阈值远超 login/harvest 窗口。"""
    assert browser.BROWSER_TMP_ROOT == browser.ROOT / "runtime" / "browser_tmp"
    assert browser.STALE_BROWSER_TMP_MAX_AGE_SECONDS == 6 * 3600
    assert browser.STALE_BROWSER_TMP_MAX_AGE_SECONDS > browser.LOGIN_TIMEOUT_SEC
    assert browser.STALE_BROWSER_TMP_MAX_AGE_SECONDS > browser.HARVEST_TIMEOUT_SEC


# ------------------------------------------------- 落盘位置与陈旧清扫


def test_case22_profile_dir_created_under_browser_tmp_root(monkeypatch, tmp_path):
    """辅助函数返回的一次性 Profile 目录必须位于 BROWSER_TMP_ROOT 之下。"""
    root = tmp_path / "browser_tmp"
    monkeypatch.setattr(browser, "BROWSER_TMP_ROOT", root)

    profile = browser._make_browser_profile_dir("cnnc_harvest_")

    assert profile.parent == root
    assert root.is_dir()  # mkdir(parents=True) 已建根目录
    assert profile.is_dir()  # mkdtemp(dir=...) 真实创建了 Profile 目录
    assert profile.name.startswith("cnnc_harvest_")


def test_case22_stale_profile_dirs_swept_and_fresh_kept(monkeypatch, tmp_path):
    """超过 6 小时的 cnnc_harvest_/cnnc_login_ 残留被清扫，新鲜与无关目录保留。"""
    root = tmp_path / "browser_tmp"
    monkeypatch.setattr(browser, "BROWSER_TMP_ROOT", root)
    stale = root / "cnnc_harvest_old"
    stale.mkdir(parents=True)
    # mtime 倒档 7 小时（> 6 小时阈值），模拟进程被硬杀后的过夜残留
    os.utime(stale, (time.time() - 7 * 3600,) * 2)
    fresh = root / "cnnc_harvest_fresh"
    fresh.mkdir()
    foreign = root / "not-a-browser-profile"
    foreign.mkdir()

    profile = browser._make_browser_profile_dir("cnnc_harvest_")

    assert not stale.exists()  # 陈旧残留被自愈清扫
    assert fresh.exists()  # 新鲜目录不受影响
    assert foreign.exists()  # 非浏览器 Profile 前缀一律不动
    assert profile.is_dir()


def test_case22_sweep_failure_does_not_break_profile_creation(monkeypatch, tmp_path):
    """清扫吞一切异常：rmtree 抛错也绝不打断，仍照常返回新 Profile 目录。"""
    root = tmp_path / "browser_tmp"
    root.mkdir()
    old = root / "cnnc_login_stale"
    old.mkdir()
    os.utime(old, (time.time() - 7 * 3600,) * 2)
    monkeypatch.setattr(browser, "BROWSER_TMP_ROOT", root)

    def boom_rmtree(*_args, **_kwargs):
        raise OSError("simulated sweep failure")

    monkeypatch.setattr(browser.shutil, "rmtree", boom_rmtree)

    profile = browser._make_browser_profile_dir("cnnc_login_")

    assert profile.parent == root
    assert profile.is_dir()


# ------------------------------------------------- E 盘不可用回退


def test_case22_mkdir_failure_falls_back_to_system_temp(monkeypatch, tmp_path, capsys):
    """BROWSER_TMP_ROOT 创建失败（E 盘不可用）时回退系统 TEMP 并打 WARN。"""
    monkeypatch.setattr(browser, "BROWSER_TMP_ROOT", tmp_path / "browser_tmp")
    seen = {}

    def fake_mkdtemp(*_args, **kwargs):
        seen.update(kwargs)
        return str(tmp_path / "fallback-profile")  # 不真实落盘

    monkeypatch.setattr(browser.tempfile, "mkdtemp", fake_mkdtemp)

    def boom_mkdir(*_args, **_kwargs):
        raise OSError("simulated E: unavailable")

    monkeypatch.setattr(Path, "mkdir", boom_mkdir)

    profile = browser._make_browser_profile_dir("cnnc_login_")

    assert Path(profile) == tmp_path / "fallback-profile"
    assert "dir" not in seen  # 回退路径不带 dir=（用系统 TEMP）
    assert seen.get("prefix") == "cnnc_login_"
    assert "WARN" in capsys.readouterr().err


# ------------------------------------------------- 源码级断言


def test_case22_cmd_sources_route_profiles_through_helper():
    """源码级：login/harvest/smoke 三处不再有裸 tempfile.mkdtemp，统一走辅助函数。"""
    for func in (browser.cmd_login, browser.cmd_harvest, browser.cmd_smoke):
        source = inspect.getsource(func)
        assert "tempfile.mkdtemp" not in source
        assert "_make_browser_profile_dir(" in source


def test_case22_helper_source_sweeps_stale_and_creates_with_dir():
    """源码级：辅助函数内聚 mkdir / 陈旧清扫 / 带 dir= 的 mkdtemp 三步。"""
    source = inspect.getsource(browser._make_browser_profile_dir)
    assert "mkdir(parents=True, exist_ok=True)" in source
    assert "STALE_BROWSER_TMP_MAX_AGE_SECONDS" in source
    assert "mkdtemp(prefix=prefix, dir=str(BROWSER_TMP_ROOT))" in source
