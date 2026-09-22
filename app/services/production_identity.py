# app/services/production_identity.py
"""STEP_1 Dashboard 生产运行版本(Production Deployment Identity)只读投影。

深模块契约:对外只有 snapshot() 一个接口,返回五块证据——
startup(进程启动身份)/ local(本地 git 状态)/ remote(GitHub 生产分支 HEAD)/
scheduler(计划任务注册状态)/ comparison(三者关系结论 + 中文 alerts)。

安全边界:
- 只执行本文件内预定义的只读 git 查询(rev-parse / branch --show-current /
  status --porcelain=v1 / ls-remote / merge-base --is-ancestor),不接受任何
  来自请求的参数;
- 计划任务查询走 scripts/query_scheduled_task_json.ps1(只读
  Get-ScheduledTask,无任何注册/反注册写操作);
- 所有 subprocess 带超时、TTL 缓存、线程安全;任何失败都降级为对应块的
  status=UNKNOWN,snapshot() 绝不抛异常;
- 绝不写仓库/DB/调度器/网络。
"""
from __future__ import annotations

import json
from copy import deepcopy
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

from app.services.runtime_fingerprint import verify_manifest as verify_runtime_manifest

SHANGHAI = ZoneInfo("Asia/Shanghai")

DEFAULT_PROD_BRANCH = "prod/closeout-20260818"
DEFAULT_SCHEDULER_TASK_NAMES = (
    "CNNC-PriceAudit-Incremental",   # 增量审计(powershell 包裹)
    "CNNC-Dashboard-ReadOnly",       # 本 Dashboard(登录时启动)
)
PROD_BRANCH_ENV = "CNNC_IDENTITY_PROD_BRANCH"

_MAX_ERROR_CODE_CHARS = 200   # remote error_code 截断上限
_MAX_DIRTY_ENTRIES = 10       # dirty.entries 展示上限(计数仍为全量)


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def _now_iso() -> str:
    """Asia/Shanghai 当前时刻,格式 YYYY-MM-DDTHH:MM:SS+08:00。"""
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def _make_default_command_runner(cwd: str):
    """默认 runner:capture + utf-8 容错 + 超时 + Windows 无窗口;
    TimeoutExpired / OSError(含 FileNotFoundError)一律吞成 rc=-1,绝不抛。"""

    def runner(argv, timeout=None):
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            proc = subprocess.run(
                [str(part) for part in argv],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=timeout, cwd=cwd, **kwargs)
            return CommandResult(proc.returncode, proc.stdout or "", proc.stderr or "")
        except (subprocess.TimeoutExpired, OSError) as exc:
            return CommandResult(-1, "", f"<{type(exc).__name__}>")

    return runner


def _is_artifact_path(path: str) -> bool:
    value = path.replace(chr(92), '/')
    parsed = PurePosixPath(value)
    suffix = parsed.suffix.lower()
    if suffix in {'.py','.pyw','.ps1','.bat','.cmd','.exe','.dll','.pyd','.sh','.js'}:
        return False
    if '__pycache__' in parsed.parts or suffix in {'.pyc','.pyo'}:
        return True
    if value.startswith(('_run/', 'runtime/', '.pytest')):
        return True
    return value.startswith('data/') and suffix == '.bak'



def _classify_dirty(paths: list[str]) -> dict:
    artifact = sum(1 for path in paths if _is_artifact_path(path))
    if not paths:
        classification = "CLEAN"
    elif artifact == len(paths):
        classification = "ALL_ARTIFACTS"
    else:
        classification = "HAS_CODE_CHANGES"
    return {
        "total": len(paths),
        "artifact": artifact,
        "code": len(paths) - artifact,
        "classification": classification,
        "entries": list(paths[:_MAX_DIRTY_ENTRIES]),
    }


def _porcelain_paths(stdout: str) -> list[str]:
    """porcelain v1:每行去前 2 个状态字符取路径;rename 的 'old -> new'
    原样保留,空行跳过。"""
    paths: list[str] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        raw = line[2:]  # 去前 2 状态字符
        if raw.startswith(" "):
            raw = raw[1:]  # 去状态区后的固定空格
        if raw:
            paths.append(raw)
    return paths


def build_comparison(startup: dict, local: dict, remote: dict, is_ancestor=None) -> dict:
    """纯函数 seam(便于测试):startup/local/remote 三块 → comparison 结论。

    version_status 只由 SHA 关系决定(工作区 dirty 仅进 alerts,不污染版本
    结论);CODE_CHANGED_AFTER_PROCESS_START 优先级最高。is_ancestor(a, b)
    表示 "a 是 b 的祖先"(git merge-base --is-ancestor a b rc==0)。
    """
    startup_sha = startup.get("git_sha")
    local_sha = local.get("git_sha")
    remote_sha = remote.get("git_sha")

    if startup_sha and local_sha:
        process_vs_local = (
            "MATCH" if startup_sha == local_sha
            else "CODE_CHANGED_AFTER_PROCESS_START")
    else:
        process_vs_local = "UNKNOWN"

    if remote.get("status") != "OK":
        local_vs_remote = "REMOTE_UNKNOWN"
    elif not local_sha:
        local_vs_remote = "UNKNOWN"
    elif local_sha == remote_sha:
        local_vs_remote = "MATCH"
    else:
        if is_ancestor is not None and is_ancestor(local_sha, remote_sha):
            local_vs_remote = "BEHIND"
        elif is_ancestor is not None and is_ancestor(remote_sha, local_sha):
            local_vs_remote = "AHEAD"
        else:
            # Local != Remote 且无法可靠判定祖先方向(merge-base rc128/分叉)
            local_vs_remote = "REMOTE_DIFFERS"

    version_status = (
        "CODE_CHANGED_AFTER_PROCESS_START"
        if process_vs_local == "CODE_CHANGED_AFTER_PROCESS_START"
        else "UNKNOWN" if process_vs_local == "UNKNOWN" else local_vs_remote)

    alerts: list[str] = []
    if process_vs_local == "CODE_CHANGED_AFTER_PROCESS_START":
        alerts.append("代码已更新但进程未重启")
    if (local.get("dirty") or {}).get("classification") == "HAS_CODE_CHANGES":
        alerts.append("工作区存在代码改动")
    if local_vs_remote == "REMOTE_UNKNOWN":
        alerts.append("GitHub 生产分支 HEAD 暂不可达")
    if local_vs_remote == "REMOTE_DIFFERS":
        alerts.append("本地与 GitHub 生产分支不一致(方向未知)")
    return {
        "process_vs_local": process_vs_local,
        "local_vs_remote": local_vs_remote,
        "version_status": version_status,
        "version_scope": "repository_git_only",
        "alerts": alerts,
    }


class ProductionIdentityService:
    """生产部署身份只读投影:git/调度器 subprocess、TTL 缓存与比较逻辑全部
    藏在内部,外部只有一个永不抛异常的 snapshot()。"""

    def __init__(self, repo_root=None, *, command_runner=None,
                 scheduler_task_names=None, prod_branch=None,
                 local_ttl=10.0, remote_ttl=60.0, scheduler_ttl=60.0,
                 git_timeout=5.0, remote_timeout=10.0, scheduler_timeout=15.0):
        self._repo_root = (Path(repo_root) if repo_root is not None
                           else Path(__file__).resolve().parents[2])
        self._runner = (command_runner if command_runner is not None
                        else _make_default_command_runner(str(self._repo_root)))
        self._scheduler_task_names = (tuple(scheduler_task_names)
                                      if scheduler_task_names is not None
                                      else DEFAULT_SCHEDULER_TASK_NAMES)
        self._prod_branch = (prod_branch
                             or os.environ.get(PROD_BRANCH_ENV)
                             or DEFAULT_PROD_BRANCH)
        self._local_ttl = float(local_ttl)
        self._remote_ttl = float(remote_ttl)
        self._scheduler_ttl = float(scheduler_ttl)
        self._git_timeout = float(git_timeout)
        self._remote_timeout = float(remote_timeout)
        self._scheduler_timeout = float(scheduler_timeout)
        self._local_cache: tuple[float, dict] | None = None
        self._remote_cache: tuple[float, dict] | None = None
        self._scheduler_cache: tuple[float, dict] | None = None
        self._local_lock = threading.Lock()
        self._remote_lock = threading.Lock()
        self._scheduler_lock = threading.Lock()
        self._runtime_identity_lock = threading.Lock()
        self._runtime_identity_cache: tuple[float, dict] | None = None
        # startup 身份在服务初始化时一次性捕获(进程启动快照语义)
        self._startup = self._capture_startup()

    # ---- 底层命令封装 ----

    def _git(self, *args, timeout: float | None = None) -> CommandResult:
        argv = ["git", "-C", str(self._repo_root), *args]
        return self._runner(argv, timeout=self._git_timeout if timeout is None else timeout)

    def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return self._git("merge-base", "--is-ancestor", ancestor,
                         descendant).returncode == 0

    # ---- startup(进程启动身份,__init__ 一次性捕获)----

    def _process_identity(self) -> dict:
        started_at = _now_iso()
        source = "service_init"
        try:
            import psutil  # 可选依赖;缺失/失败退回服务初始化时刻
            started_at = datetime.fromtimestamp(
                psutil.Process().create_time(), SHANGHAI).isoformat(timespec="seconds")
            source = "psutil"
        except Exception:
            pass
        return {"pid": os.getpid(), "started_at": started_at,
                "started_at_source": source}

    def _capture_startup(self) -> dict:
        block = {"status": "UNKNOWN", "git_sha": None, "git_branch": None,
                 "captured_at": _now_iso(),
                 "python": {"version": sys.version.split()[0],
                            "executable": sys.executable,
                            "virtualenv": sys.prefix != sys.base_prefix},
                 "process": self._process_identity()}
        try:
            res = self._git("rev-parse", "HEAD")
            if res.returncode == 0 and res.stdout.strip():
                block["status"] = "OK"
                block["git_sha"] = res.stdout.strip()
            branch = self._git("branch", "--show-current")
            if branch.returncode == 0 and branch.stdout.strip():
                block["git_branch"] = branch.stdout.strip()
        except Exception:
            pass
        return block

    # ---- local(本地 git 状态,TTL 缓存)----

    def _refresh_local(self) -> dict:
        block = {"status": "UNKNOWN", "git_sha": None, "git_branch": None,
                 "detached_head": False, "working_tree": "UNKNOWN",
                 "dirty": {**_classify_dirty([]), "classification": "UNKNOWN"}}
        try:
            res = self._git("rev-parse", "HEAD")
            if res.returncode == 0 and res.stdout.strip():
                block["status"] = "OK"
                block["git_sha"] = res.stdout.strip()
            branch = self._git("branch", "--show-current")
            if branch.returncode == 0:
                name = branch.stdout.strip()
                if name:
                    block["git_branch"] = name
                elif block["status"] == "OK":
                    # branch --show-current 为空串 → detached HEAD
                    block["detached_head"] = True
            status = self._git("status", "--porcelain=v1")
            if status.returncode == 0:
                paths = _porcelain_paths(status.stdout)
                block["working_tree"] = "DIRTY" if paths else "CLEAN"
                block["dirty"] = _classify_dirty(paths)
        except Exception:
            pass
        return block

    def _local_snapshot(self) -> dict:
        with self._local_lock:
            if (self._local_cache is not None
                    and time.monotonic() - self._local_cache[0] <= self._local_ttl):
                return deepcopy(self._local_cache[1])
            value = self._refresh_local()
            self._local_cache = (time.monotonic(), deepcopy(value))
            return deepcopy(value)

    # ---- remote(GitHub 生产分支 HEAD,TTL 缓存)----

    def _refresh_remote(self) -> dict:
        block = {"status": "UNKNOWN", "git_sha": None,
                 "branch": self._prod_branch, "checked_at": _now_iso(),
                 "error_code": None}
        try:
            res = self._runner(
                ["git", "-C", str(self._repo_root), "ls-remote", "origin",
                 f"refs/heads/{self._prod_branch}"],
                timeout=self._remote_timeout)
            if res.returncode == 0:
                lines = [line for line in res.stdout.splitlines() if line.strip()]
                fields = lines[0].split() if len(lines) == 1 else []
                if (len(fields) == 2 and fields[1] == f"refs/heads/{self._prod_branch}"
                        and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", fields[0])):
                    block["status"] = "OK"
                    block["git_sha"] = fields[0]
                else:
                    block["error_code"] = "REMOTE_REF_NOT_FOUND" if not lines else "REMOTE_REF_INVALID"
            else:
                # Git stderr can embed credentials from a configured remote URL.
                # Return only a fixed diagnostic, never raw process output.
                safe_transport_errors = {"<FileNotFoundError>", "<TimeoutExpired>", "<PermissionError>"}
                block["error_code"] = (res.stderr if res.returncode == -1 and res.stderr in safe_transport_errors
                                       else f"GIT_REMOTE_EXIT_{int(res.returncode)}")
        except Exception as exc:
            block["error_code"] = f"<{type(exc).__name__}>"
        return block

    def _remote_snapshot(self) -> dict:
        with self._remote_lock:
            if (self._remote_cache is not None
                    and time.monotonic() - self._remote_cache[0] <= self._remote_ttl):
                return deepcopy(self._remote_cache[1])
            value = self._refresh_remote()
            self._remote_cache = (time.monotonic(), deepcopy(value))
            return deepcopy(value)

    # ---- scheduler(计划任务注册状态,TTL 缓存;ps1 只读查询)----

    def _refresh_scheduler(self) -> dict:
        block = {"status": "UNKNOWN", "checked_at": _now_iso(), "tasks": []}
        ps1 = self._repo_root / "scripts" / "query_scheduled_task_json.ps1"
        all_queries_ok = True
        for name in self._scheduler_task_names:
            entry = {"name": name, "registered": False, "state": None,
                     "last_run_time": None, "next_run_time": None,
                     "last_task_result": None, "action": None}
            try:
                res = self._runner(
                    ["powershell.exe", "-NoProfile", "-NonInteractive",
                     "-ExecutionPolicy", "Bypass", "-File", str(ps1),
                     "-TaskName", name],
                    timeout=self._scheduler_timeout)
                data = None
                if res.returncode == 0:
                    text = res.stdout.strip()
                    # "null" 或 JSON null → 任务未注册(registered False)
                    data = None if text == "null" else json.loads(text)
                else:
                    all_queries_ok = False
                if isinstance(data, dict):
                    if ("error" in data or data.get("task_name") != name
                            or not isinstance(data.get("state"), str)
                            or not isinstance(data.get("action"), str)
                            or type(data.get("last_task_result")) is not int):
                        entry["state"] = "UNKNOWN"   # error payload = 查询失败 → 整块降级
                        all_queries_ok = False
                    else:
                        entry.update({
                            "registered": True,
                            "state": data.get("state"),
                            "last_run_time": data.get("last_run_time"),
                            "next_run_time": data.get("next_run_time"),
                            "last_task_result": data.get("last_task_result"),
                            "action": data.get("action"),
                            "definition_sha256": data.get("definition_sha256"),
                        })
                elif data is not None:
                    all_queries_ok = False
            except Exception:
                all_queries_ok = False
            block["tasks"].append(entry)
        block["status"] = "OK" if all_queries_ok else "UNKNOWN"
        return block

    def _scheduler_snapshot(self) -> dict:
        with self._scheduler_lock:
            if (self._scheduler_cache is not None
                    and time.monotonic() - self._scheduler_cache[0] <= self._scheduler_ttl):
                return deepcopy(self._scheduler_cache[1])
            value = self._refresh_scheduler()
            self._scheduler_cache = (time.monotonic(), deepcopy(value))
            return deepcopy(value)

    def _runtime_identity_snapshot(self, scheduler: dict) -> dict:
        """Additional read-only evidence; never turns Git MATCH into full approval."""
        with self._runtime_identity_lock:
            if self._runtime_identity_cache is not None:
                recorded_at, cached = self._runtime_identity_cache
                if time.monotonic() - recorded_at <= self._local_ttl:
                    return deepcopy(cached)
            manifest_path = self._repo_root / "data" / "production_manifest.json"
            try:
                if not manifest_path.is_file():
                    value = {"status": "UNCONFIGURED", "issues": ["RUNTIME_MANIFEST_NOT_CONFIGURED"]}
                else:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    definitions = {task["name"]: task.get("definition_sha256")
                                   for task in scheduler.get("tasks", [])
                                   if task.get("name")}
                    value = verify_runtime_manifest(self._repo_root, manifest,
                                                    scheduler_definitions=definitions)
            except (OSError, ValueError):
                value = {"status": "UNKNOWN", "issues": ["RUNTIME_MANIFEST_READ_FAILED"]}
            except Exception:
                value = {"status": "UNKNOWN", "issues": ["RUNTIME_IDENTITY_VERIFICATION_FAILED"]}
            self._runtime_identity_cache = (time.monotonic(), value)
            return deepcopy(value)

    # ---- 对外唯一接口:绝不抛异常 ----

    def snapshot(self) -> dict:
        try:
            startup = deepcopy(self._startup)
            local = self._local_snapshot()
            remote = self._remote_snapshot()
            scheduler = self._scheduler_snapshot()
            comparison = build_comparison(
                startup, local, remote, is_ancestor=self._is_ancestor)
            return {"generated_at": _now_iso(), "startup": startup,
                    "local": local, "remote": remote,
                    "scheduler": scheduler, "comparison": comparison,
                    "runtime_identity": self._runtime_identity_snapshot(scheduler)}
        except Exception:
            # 契约兜底:任何意外都返回结构完整的全 UNKNOWN 快照
            startup = deepcopy(self._startup) if isinstance(self._startup, dict) else {
                "status": "UNKNOWN", "git_sha": None, "git_branch": None,
                "captured_at": _now_iso(),
                "python": {"version": sys.version.split()[0],
                           "executable": sys.executable, "virtualenv": False},
                "process": {"pid": os.getpid(), "started_at": _now_iso(),
                            "started_at_source": "service_init"}}
            degraded = {
                "local": {"status": "UNKNOWN", "git_sha": None, "git_branch": None,
                          "detached_head": False, "working_tree": "UNKNOWN",
                          "dirty": {**_classify_dirty([]), "classification": "UNKNOWN"}},
                "remote": {"status": "UNKNOWN", "git_sha": None,
                           "branch": self._prod_branch, "checked_at": _now_iso(),
                           "error_code": "SNAPSHOT_FAILED"},
                "scheduler": {"status": "UNKNOWN", "checked_at": _now_iso(),
                              "tasks": []},
            }
            return {"generated_at": _now_iso(), "startup": startup,
                    **degraded,
                    "runtime_identity": {"status": "UNKNOWN", "issues": ["SNAPSHOT_FAILED"]},
                    "comparison": build_comparison(startup, degraded["local"],
                                                   degraded["remote"])}


# ---- 进程级单例(懒创建 + 双检锁);_reset_default_service 供测试隔离 ----

_default_service: ProductionIdentityService | None = None
_default_service_lock = threading.Lock()


def get_production_identity_service() -> ProductionIdentityService:
    global _default_service
    if _default_service is None:
        with _default_service_lock:
            if _default_service is None:
                _default_service = ProductionIdentityService()
    return _default_service


def _reset_default_service() -> None:
    global _default_service
    with _default_service_lock:
        _default_service = None
