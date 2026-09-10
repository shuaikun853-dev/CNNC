# -*- coding: utf-8 -*-
"""CNNC 运维项目运行时路径单一事实源：C 盘可控写入迁移 E 盘。

背景:
    生产机仓库部署在 E:，而系统 %TEMP% 在 C:。此前项目级临时写入
    （Chrome 一次性 Profile 的 mkdtemp、selftest 的 TemporaryDirectory、
    pytest tmp_path、以及 Chrome/Playwright driver/.NET host 等子进程
    继承的系统 TEMP）默认全部落 C:，属于不受控磁盘占用。

    本模块是全项目唯一的运行时根 resolver，把这些写入统一收敛到
    <repo>/runtime/temp（runtime/ 已被 .gitignore 忽略，复用既有
    runtime/ 目录语义，不另起第二套根）。

语义（刻意保持最小）:
    - PROJECT_ROOT    仓库根（scripts/ 的上一级）
    - runtime_root(env=None)
                      运行时根：默认 <repo>/runtime；可用环境变量
                      CNNC_RUNTIME_ROOT 覆盖。Windows 上覆盖值若指向
                      非 E: 盘则 fail-fast RuntimeError（防止配置漂移后
                      静默写回 C:）。返回前确保目录存在。
    - runtime_subdir(name)
                      运行时根下具名子目录（自动创建）
    - temp_root()     所有项目级临时文件的家 = <runtime_root>/temp
    - force_process_tree_e_temp(env=None)
                      幂等把 TEMP/TMP/TMPDIR 指向 temp_root()，让当前
                      进程随后创建的所有子进程（Chrome、Playwright
                      driver、.NET host、cURL、DETACHED 登录进程）继承
                      E: 临时目录。只改传入/当前进程环境，绝不触碰
                      注册表或系统全局环境变量。

约束:
    本模块禁止 import 仓库内任何其他模块：它被 cnnc_auth_core /
    cnnc_auth_refresh / cnnc_auth_browser（底层）与 app.main（顶层）
    同时引用，保持零依赖以杜绝循环 import。
"""

from __future__ import annotations

import os
from pathlib import Path

# Windows 上允许的唯一盘符：项目生产部署盘为 E:。其他盘（含 UNC 路径）
# 一律 fail-fast 拒绝，防止 CNNC_RUNTIME_ROOT 配置漂移后静默污染 C:。
_ALLOWED_DRIVE = "E:"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT_ENV_KEY = "CNNC_RUNTIME_ROOT"
_PROCESS_TREE_TEMP_KEYS = ("TEMP", "TMP", "TMPDIR")


def runtime_root(env=None) -> Path:
    """返回项目运行时根（返回前 mkdir(parents=True, exist_ok=True)）。

    env 缺省读 os.environ；CNNC_RUNTIME_ROOT 值为空/缺失时回落默认
    <repo>/runtime。非空覆盖值在 Windows 上若解析出盘符且非 E:，
    raise RuntimeError（错误信息说明约束），绝不在非法盘创建目录。
    """
    if env is None:
        env = os.environ
    raw = (env.get(RUNTIME_ROOT_ENV_KEY) or "").strip()
    if raw:
        root = Path(raw)
        drive = root.resolve().drive if os.name == "nt" else ""
        if drive and drive.upper() != _ALLOWED_DRIVE:
            raise RuntimeError(
                "CNNC_RUNTIME_ROOT=%r 不得指向非 %s 盘（实际解析盘符 %s）。"
                "项目可控写入只允许落 %s:（生产部署盘）；请改指 %s 下路径，"
                "或清空该环境变量以回落默认 <repo>/runtime。"
                % (raw, _ALLOWED_DRIVE, drive, _ALLOWED_DRIVE, _ALLOWED_DRIVE)
            )
    else:
        root = PROJECT_ROOT / "runtime"
    root.mkdir(parents=True, exist_ok=True)
    return root


def runtime_subdir(name: str) -> Path:
    """运行时根下的具名子目录（自动创建），用于 temp/browser 等受控写入点。"""
    path = runtime_root() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def temp_root() -> Path:
    """所有项目级临时文件的家：<runtime_root>/temp。"""
    return runtime_subdir("temp")


def force_process_tree_e_temp(env=None) -> Path:
    """幂等把 TEMP/TMP/TMPDIR 指向 temp_root()，返回该路径。

    只修改传入的环境映射（缺省当前进程 os.environ）；子进程经
    subprocess/CreateProcess 继承后，其临时文件全部落 E:。绝不触碰
    注册表或系统级全局环境变量。重复调用结果一致（幂等）。
    """
    if env is None:
        env = os.environ
    target = str(temp_root())
    for key in _PROCESS_TREE_TEMP_KEYS:
        env[key] = target
    return Path(target)
