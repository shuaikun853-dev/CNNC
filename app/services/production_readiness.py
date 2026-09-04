"""生产就绪只读检查（READ-ONLY）。

承诺（本模块绝不触碰生产状态）：
- 不注册 / 修改 / 删除计划任务，只对 Task Scheduler 做 Get-ScheduledTask
  等只读查询；
- 不连接任何数据库；
- 不登录、不刷新认证，不读取也不打印 ``data/`` 下任何敏感文件的内容
  或 token 值，只检查存在性 / 大小 / mtime；
- 不发起网络请求，不写任何文件。

退出码约定（由 :mod:`scripts.production_readiness_check` 使用）：
- 0 = 全部检查 PASS（READY）；
- 1 = 存在任一 WARN / FAIL / UNKNOWN（NOT READY）。

外部命令全部通过注入的 ``CommandRunner`` 执行（默认 ``DefaultCommandRunner``），
测试用假 runner 与假时钟完全替代，绝不真实运行 powershell / git / tzutil。
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Protocol

# ---------------------------------------------------------------------------
# 状态常量与期望配置
# ---------------------------------------------------------------------------

STATUS_PASS = "PASS"
STATUS_WARN = "WARN"
STATUS_FAIL = "FAIL"
STATUS_UNKNOWN = "UNKNOWN"

# 严重度排序：PASS < WARN < UNKNOWN < FAIL（worst_status 汇报用）。
_STATUS_SEVERITY = {
    STATUS_PASS: 0,
    STATUS_WARN: 1,
    STATUS_UNKNOWN: 2,
    STATUS_FAIL: 3,
}

EXPECTED_BRANCH_DEFAULT = "prod/closeout-20260818"
EXPECTED_TIMEZONE = "China Standard Time"
EXPECTED_LOCAL_UTC_OFFSET = timedelta(hours=8)

SCHEDULER_TASK_NAME = "CNNC-PriceAudit-Incremental"
DASHBOARD_AUTOSTART_TASK_NAME = "CNNC-Dashboard-ReadOnly"
EXPECTED_TRIGGER_COUNT = 4
EXPECTED_TRIGGER_START_TIMES = frozenset({"09:00", "17:00", "18:00", "20:35"})
EXPECTED_MULTIPLE_INSTANCES = "IgnoreNew"
EXPECTED_EXECUTION_TIME_LIMIT_MINUTES = 32.0
EXPECTED_SCHEDULER_RUNNER_MARKER = "run_incremental_price_audit_scheduled.ps1"
SCHEDULER_TASK_NEVER_RUN_RESULT = 267009

# 只读查询命令（绝不注册 / 删除 / 修改计划任务）。
_SCHEDULER_TASK_QUERY = (
    f"Get-ScheduledTask -TaskName '{SCHEDULER_TASK_NAME}' | ConvertTo-Json -Depth 6"
)
_SCHEDULER_INFO_QUERY = (
    f"(Get-ScheduledTask -TaskName '{SCHEDULER_TASK_NAME}' "
    "| Get-ScheduledTaskInfo) | ConvertTo-Json"
)
_DASHBOARD_EXISTENCE_QUERY = (
    "[bool](Get-ScheduledTask -TaskName "
    f"'{DASHBOARD_AUTOSTART_TASK_NAME}' -ErrorAction SilentlyContinue)"
)

REQUIRED_PYTHON_PACKAGES = (
    "fastapi",
    "uvicorn",
    "pydantic",
    "yaml",
    "pytest",
    "httpx",
    "openpyxl",
    "pymysql",
)

# Windows PowerShell 5.1 的 ConvertTo-Json 会把 CimInstance 序列化成
# {"CimInstanceProperties": [{"Name": ..., "Value": ...}, ...]}；这些表把其
# 中的 int 枚举还原成 Task Scheduler 语义名。
_TASK_STATE_NAMES = {
    0: "Unknown",
    1: "Disabled",
    2: "Queued",
    3: "Ready",
    4: "Running",
}
_MULTIPLE_INSTANCE_POLICIES = {
    0: "Parallel",
    1: "Queue",
    2: "IgnoreNew",
}

# data/ 目录下的认证 / 请求凭据文件（只查存在性，绝不读内容）。
_AUTH_REQUIRED_FILES = (
    "auth_state.json",
    "model.env",
    "detail_request.curl",
    "latest_saas_sameAudit_queryAll.curl",
)
_AUTH_WARN_ONLY_FILES = ("cnnc_auth_ledger.jsonl",)
AUTH_STATE_MAX_AGE = timedelta(hours=24)

# ---------------------------------------------------------------------------
# 结果模型
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    """单项检查结果：名称、状态、一行摘要与补充明细。"""

    name: str
    status: str
    summary: str
    details: list[str] = field(default_factory=list)


@dataclass
class ReadinessReport:
    """整体报告：全部 PASS 才算 READY。"""

    checks: list[CheckResult] = field(default_factory=list)

    @property
    def overall(self) -> str:
        """全 PASS -> READY，否则 NOT READY（无检查项时同样 NOT READY）。"""
        if self.checks and all(check.status == STATUS_PASS for check in self.checks):
            return "READY"
        return "NOT READY"

    @property
    def worst_status(self) -> str:
        """当前最差状态；无检查项时为 UNKNOWN。"""
        if not self.checks:
            return STATUS_UNKNOWN
        return max(
            (check.status for check in self.checks),
            key=lambda status: _STATUS_SEVERITY.get(status, _STATUS_SEVERITY[STATUS_UNKNOWN]),
        )

    def to_dict(self) -> dict:
        """JSON 可序列化结构（供 CLI --json 与未来 Dashboard 端点复用）。"""
        return {
            "overall": self.overall,
            "worst_status": self.worst_status,
            "checks": [
                {
                    "name": check.name,
                    "status": check.status,
                    "summary": check.summary,
                    "details": list(check.details),
                }
                for check in self.checks
            ],
        }

    def exit_code(self) -> int:
        """0 = READY；1 = NOT READY（含 WARN / FAIL / UNKNOWN）。"""
        return 0 if self.overall == "READY" else 1


# ---------------------------------------------------------------------------
# 命令执行抽象（依赖注入，测试全部用假实现）
# ---------------------------------------------------------------------------


class CommandRunner(Protocol):
    """外部只读命令执行协议（兼容 subprocess.CompletedProcess 返回对象）。"""

    def run(self, cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
        ...  # pragma: no cover - 协议声明


class DefaultCommandRunner:
    """subprocess.run 封装：UTF-8 解码、有限超时、Windows 不弹窗。"""

    def run(self, cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
        # CREATE_NO_WINDOW 仅存在于 Windows，其余平台用 0。
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        )
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=creationflags,
        )


@dataclass(frozen=True)
class _CheckContext:
    """单个报告构建周期内共享的执行上下文。"""

    runner: CommandRunner
    wall_clock: Callable[[], datetime]
    timeout_seconds: float
    expected_branch: str

    def local_now_naive(self) -> datetime:
        """把 wall_clock 归一为 naive 本地时间（与文件 mtime 可直接相减）。"""
        now = self.wall_clock()
        if now.tzinfo is not None and now.utcoffset() is not None:
            return now.astimezone().replace(tzinfo=None)
        return now


# ---------------------------------------------------------------------------
# 各项检查
# ---------------------------------------------------------------------------


def check_os(repo_root: Path, ctx: _CheckContext) -> CheckResult:
    """操作系统：生产环境仅支持 Windows。"""
    if sys.platform == "win32":
        return CheckResult("Operating system", STATUS_PASS, "Windows 生产环境", [])
    return CheckResult(
        "Operating system",
        STATUS_WARN,
        "非 Windows：仅设计/测试环境",
        [f"sys.platform={sys.platform}"],
    )


def check_git(repo_root: Path, ctx: _CheckContext) -> CheckResult:
    """git 仓库状态：可用性、分支、工作区脏行（过滤 .pyc 假脏）。"""
    try:
        inside = ctx.runner.run(
            ["git", "-C", str(repo_root), "rev-parse", "--is-inside-work-tree"],
            ctx.timeout_seconds,
        )
        head = ctx.runner.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"], ctx.timeout_seconds
        )
        branch = ctx.runner.run(
            ["git", "-C", str(repo_root), "branch", "--show-current"], ctx.timeout_seconds
        )
        status = ctx.runner.run(
            ["git", "-C", str(repo_root), "status", "--porcelain"], ctx.timeout_seconds
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return CheckResult(
            "Git repository",
            STATUS_UNKNOWN,
            "git 不可用",
            [f"{type(exc).__name__}: {exc}"],
        )
    if any(result.returncode != 0 for result in (inside, head, branch, status)):
        return CheckResult(
            "Git repository",
            STATUS_UNKNOWN,
            "git 查询失败（非零退出码）",
            [
                f"is-inside-work-tree rc={inside.returncode}",
                (inside.stderr or "").strip()[:200],
            ],
        )

    details: list[str] = []
    dirty_lines = [line for line in (status.stdout or "").splitlines() if line.strip()]
    # .pyc 变更是运行稳态（pytest 重新编译），不算真实脏。
    real_dirty = [line for line in dirty_lines if not _porcelain_path(line).endswith(".pyc")]
    pyc_only = bool(dirty_lines) and not real_dirty

    details.append(f"HEAD: {(head.stdout or '').strip()[:12]}")
    if real_dirty:
        details.extend(f"dirty: {line}" for line in real_dirty[:5])
        if len(real_dirty) > 5:
            details.append(f"dirty: ... 共 {len(real_dirty)} 条（仅列前 5 条）")

    branch_name = (branch.stdout or "").strip()
    issues: list[str] = []
    if branch_name != ctx.expected_branch:
        issues.append(
            f"当前分支 {branch_name or '(空)'} 与期望分支 {ctx.expected_branch} 不一致"
        )
    if real_dirty:
        issues.append(f"工作区有 {len(real_dirty)} 条非 .pyc 脏行")
    if issues:
        return CheckResult("Git repository", STATUS_WARN, "；".join(issues), details)

    if pyc_only:
        details.append("仅 .pyc 假脏(运行稳态)")
        return CheckResult(
            "Git repository",
            STATUS_PASS,
            f"分支 {branch_name}，仅 .pyc 假脏(运行稳态)",
            details,
        )
    return CheckResult(
        "Git repository", STATUS_PASS, f"分支 {branch_name}，工作区干净", details
    )


def _porcelain_path(line: str) -> str:
    """从 porcelain 行提取路径（XY path 或 'XY old -> new' 取新路径）。"""
    raw = line[3:] if len(line) > 3 else line
    if " -> " in raw:
        raw = raw.rsplit(" -> ", 1)[1]
    return raw.strip().strip('"')


def _normalized_cim_properties(node: object) -> dict:
    """把 ConvertTo-Json 输出的节点归一成普通 dict。

    Windows PowerShell 5.1 把 CimInstance 的属性序列化进
    ``CimInstanceProperties``（Name/Value 列表），而 PowerShell 7 直接平铺
    属性；这里统一成平铺视图，普通键优先、CIM 键补缺。
    """
    if not isinstance(node, dict):
        return {}
    values: dict = {}
    cim_properties = node.get("CimInstanceProperties")
    if isinstance(cim_properties, list):
        for entry in cim_properties:
            if isinstance(entry, dict) and isinstance(entry.get("Name"), str):
                values[entry["Name"]] = entry.get("Value")
    for key, value in node.items():
        if key not in ("CimClass", "CimInstanceProperties", "CimSystemProperties"):
            values.setdefault(key, value)
    return values


def _multiple_instances_policy(value: object) -> str | None:
    """把 MultipleInstances 的 int 枚举 / 嵌套 Policy / 字符串统一成策略名。"""
    if isinstance(value, int) and not isinstance(value, bool):
        return _MULTIPLE_INSTANCE_POLICIES.get(value)
    if isinstance(value, dict):
        value = value.get("Policy")
    if isinstance(value, str) and value:
        return value
    return None


def _task_state_text(value: object) -> str | None:
    """把任务的 State（int 枚举或字符串）统一成可读文本。"""
    if isinstance(value, int) and not isinstance(value, bool):
        return _TASK_STATE_NAMES.get(value, str(value))
    if isinstance(value, str) and value:
        return value
    return None


def _datetime_text(value: object) -> str | None:
    """把 /Date(epoch_ms)/ 或 ISO 字符串转成本地可读时间文本。"""
    if not isinstance(value, str) or not value:
        return None
    match = re.fullmatch(r"/Date\((-?\d+)\)/", value)
    if match:
        try:
            stamp = datetime.fromtimestamp(int(match.group(1)) / 1000)
            return stamp.strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, OverflowError, ValueError):
            return value
    return value


def check_python(repo_root: Path, ctx: _CheckContext) -> CheckResult:
    """Python 版本与关键依赖包可用性。"""
    version_text = sys.version.split()[0]
    details: list[str] = []
    if sys.version_info < (3, 11):
        return CheckResult(
            "Python runtime",
            STATUS_FAIL,
            f"Python {version_text} 低于 3.11 下限",
            details,
        )
    if sys.version_info[:2] != (3, 14):
        details.append(f"当前 {version_text}；生产为 3.14")
    missing = [
        name for name in REQUIRED_PYTHON_PACKAGES if importlib.util.find_spec(name) is None
    ]
    if missing:
        details.append("缺失包: " + ", ".join(missing))
        return CheckResult(
            "Python runtime",
            STATUS_FAIL,
            f"缺失依赖包: {', '.join(missing)}",
            details,
        )
    summary = f"Python {version_text}，关键依赖齐全"
    return CheckResult("Python runtime", STATUS_PASS, summary, details)


def check_timezone(repo_root: Path, ctx: _CheckContext) -> CheckResult:
    """时区一致性：系统时区、UTC+8 偏移与上海业务日期（绝不修改时区）。"""
    details: list[str] = []
    issues: list[str] = []
    try:
        tz_result = ctx.runner.run(["tzutil", "/g"], ctx.timeout_seconds)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return CheckResult(
            "Timezone", STATUS_UNKNOWN, "tzutil 不可用", [f"{type(exc).__name__}: {exc}"]
        )
    if tz_result.returncode != 0:
        return CheckResult(
            "Timezone",
            STATUS_UNKNOWN,
            "tzutil 查询失败",
            [(tz_result.stderr or "").strip()[:200]],
        )

    tz_name = (tz_result.stdout or "").strip()
    if tz_name != EXPECTED_TIMEZONE:
        issues.append(f"系统时区不是 {EXPECTED_TIMEZONE}")
        details.append(f"Expected: {EXPECTED_TIMEZONE}")
        details.append(f"Actual: {tz_name}")

    local_offset = datetime.now().astimezone().utcoffset()
    if local_offset != EXPECTED_LOCAL_UTC_OFFSET:
        issues.append(f"本地 UTC 偏移 {local_offset} 与 +08:00 不一致")
        details.append(f"Expected offset: +08:00")
        details.append(f"Actual offset: {local_offset}")

    # 上海业务日期（业务权威）与本地钟表日期（Scheduler 触发依据）比对：
    # 两者不一致意味着调度器与 Python 的"业务日"语义分裂。
    from app.services.incremental_audit_runtime import business_date_in_shanghai

    shanghai_date = business_date_in_shanghai()
    local_date = datetime.now().date().isoformat()
    if shanghai_date != local_date:
        issues.append(
            f"上海业务日期 {shanghai_date} 与本地日期 {local_date} 不一致"
        )
        details.append(
            "Scheduler(本地钟表) 与 Python(Asia/Shanghai) 语义分裂风险："
            "业务日期以 Asia/Shanghai 为权威，跨日窗口内调度归属可能与业务归属错位"
        )

    if issues:
        return CheckResult("Timezone", STATUS_WARN, "；".join(issues), details)
    return CheckResult(
        "Timezone",
        STATUS_PASS,
        f"{EXPECTED_TIMEZONE}（UTC+08:00），上海业务日期一致",
        details,
    )


def check_scheduler(repo_root: Path, ctx: _CheckContext) -> CheckResult:
    """计划任务只读校验：存在性 + 配置漂移（绝不注册/删除/修改）。"""
    try:
        task_result = ctx.runner.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _SCHEDULER_TASK_QUERY],
            ctx.timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            "Task scheduler", STATUS_UNKNOWN, "调度器查询超时", ["Get-ScheduledTask 超时"]
        )
    except (FileNotFoundError, OSError) as exc:
        return CheckResult(
            "Task scheduler",
            STATUS_UNKNOWN,
            "powershell 不可用",
            [f"{type(exc).__name__}: {exc}"],
        )

    task_stdout = (task_result.stdout or "").strip()
    task_stderr = (task_result.stderr or "").strip()
    if task_result.returncode != 0 or not task_stdout:
        # PowerShell 对不存在的任务报 "No MSFT_ScheduledTask objects found"。
        if not task_stdout or "MSFT_ScheduledTask" in task_stderr:
            return CheckResult(
                "Task scheduler",
                STATUS_FAIL,
                f"计划任务不存在: {SCHEDULER_TASK_NAME}",
                [task_stderr[:200]] if task_stderr else [],
            )
        return CheckResult(
            "Task scheduler",
            STATUS_UNKNOWN,
            "Get-ScheduledTask 查询失败",
            [f"rc={task_result.returncode}", task_stderr[:200]],
        )

    try:
        payload = json.loads(task_stdout)
    except json.JSONDecodeError as exc:
        return CheckResult(
            "Task scheduler", STATUS_UNKNOWN, "调度器 JSON 解析失败", [str(exc)]
        )
    if isinstance(payload, list):
        if len(payload) != 1:
            return CheckResult(
                "Task scheduler",
                STATUS_FAIL,
                f"匹配到 {len(payload)} 个同名任务，无法唯一校验",
                [],
            )
        payload = payload[0]
    if not isinstance(payload, dict):
        return CheckResult(
            "Task scheduler",
            STATUS_FAIL,
            f"计划任务不存在: {SCHEDULER_TASK_NAME}",
            [],
        )

    details: list[str] = []
    drift: list[str] = []
    missing_fields: list[str] = []
    warn_lines: list[str] = []

    task_properties = _normalized_cim_properties(payload)
    state = _task_state_text(task_properties.get("State"))
    if state:
        details.append(f"State: {state}")
    else:
        missing_fields.append("State")

    # Trigger 数量与 StartBoundary 时刻集合。
    triggers = payload.get("Triggers")
    if not isinstance(triggers, list):
        missing_fields.append("Triggers")
        triggers = []
    if len(triggers) != EXPECTED_TRIGGER_COUNT:
        drift.append(f"trigger 数 {len(triggers)} != {EXPECTED_TRIGGER_COUNT}")
    start_times: set[str] = set()
    for trigger in triggers:
        boundary = _normalized_cim_properties(trigger).get("StartBoundary")
        if not isinstance(boundary, str) or "T" not in boundary:
            missing_fields.append("StartBoundary")
            continue
        start_times.add(boundary.split("T", 1)[1][:5])
    if start_times != EXPECTED_TRIGGER_START_TIMES:
        drift.append(
            f"StartBoundary 时刻 {sorted(start_times)} != "
            f"{sorted(EXPECTED_TRIGGER_START_TIMES)}"
        )

    # 多实例策略与执行时限。
    settings = _normalized_cim_properties(payload.get("Settings"))
    policy = _multiple_instances_policy(settings.get("MultipleInstances"))
    if policy is None:
        missing_fields.append("MultipleInstances")
    elif policy != EXPECTED_MULTIPLE_INSTANCES:
        drift.append(f"MultipleInstances={policy} != {EXPECTED_MULTIPLE_INSTANCES}")
    limit_minutes = _iso8601_duration_to_minutes(settings.get("ExecutionTimeLimit"))
    if limit_minutes is None:
        missing_fields.append("ExecutionTimeLimit")
    elif abs(limit_minutes - EXPECTED_EXECUTION_TIME_LIMIT_MINUTES) > 0.01:
        drift.append(
            f"ExecutionTimeLimit={settings.get('ExecutionTimeLimit')}"
            f"（约 {limit_minutes:g} 分钟）!= PT32M"
        )

    # Action 指向受保护的调度 runner。
    actions = payload.get("Actions")
    if not isinstance(actions, list) or not actions:
        missing_fields.append("Actions")
    else:
        action_parts: list[str] = []
        for action in actions:
            action_properties = _normalized_cim_properties(action)
            action_parts.append(
                f"{action_properties.get('Execute') or ''} "
                f"{action_properties.get('Arguments') or ''}"
            )
        if EXPECTED_SCHEDULER_RUNNER_MARKER not in " ".join(action_parts):
            drift.append(f"Actions 未指向 {EXPECTED_SCHEDULER_RUNNER_MARKER}")

    # 运行信息（LastRunTime / NextRunTime / LastTaskResult）。
    try:
        info_result = ctx.runner.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _SCHEDULER_INFO_QUERY],
            ctx.timeout_seconds,
        )
        info_stdout = (info_result.stdout or "").strip()
        info_payload = (
            json.loads(info_stdout)
            if info_result.returncode == 0 and info_stdout
            else None
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, json.JSONDecodeError):
        info_payload = None
    if isinstance(info_payload, dict):
        last_run = _datetime_text(info_payload.get("LastRunTime"))
        next_run = _datetime_text(info_payload.get("NextRunTime"))
        last_result = info_payload.get("LastTaskResult")
        if last_run:
            details.append(f"LastRunTime: {last_run}")
        if next_run:
            details.append(f"NextRunTime: {next_run}")
        if isinstance(last_result, int):
            if last_result == 0:
                details.append("LastTaskResult: 0（上次运行成功）")
            elif last_result == SCHEDULER_TASK_NEVER_RUN_RESULT:
                details.append(
                    f"LastTaskResult: {last_result}（任务从未运行过；首次部署属预期状态）"
                )
            else:
                warn_lines.append(
                    f"LastTaskResult: {last_result}（0x{last_result & 0xFFFFFFFF:08X}）非 0"
                )
        else:
            details.append("LastTaskResult 不可用，未验证")
    else:
        details.append("Get-ScheduledTaskInfo 不可用：LastRunTime/NextRunTime/LastTaskResult 未验证")

    # 登录自启任务 CNNC-Dashboard-ReadOnly 存在性。
    try:
        dash_result = ctx.runner.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _DASHBOARD_EXISTENCE_QUERY,
            ],
            ctx.timeout_seconds,
        )
        if dash_result.returncode != 0:
            dash_present: bool | None = None
        else:
            dash_present = (dash_result.stdout or "").strip().lower() == "true"
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        dash_present = None
    if dash_present is None:
        details.append(
            f"[未知] {DASHBOARD_AUTOSTART_TASK_NAME} 存在性查询失败，未验证"
        )
    elif not dash_present:
        warn_lines.append(f"登录自启任务 {DASHBOARD_AUTOSTART_TASK_NAME} 不存在")

    if missing_fields:
        return CheckResult(
            "Task scheduler",
            STATUS_UNKNOWN,
            f"调度器字段缺失，无法完整校验: {', '.join(sorted(set(missing_fields)))}",
            [f"拿不到的字段: {', '.join(sorted(set(missing_fields)))}（不假装验证过）"]
            + details
            + drift,
        )
    if drift or warn_lines:
        return CheckResult(
            "Task scheduler",
            STATUS_WARN,
            "；".join(drift + warn_lines),
            [f"配置漂移: {item}" for item in drift]
            + [f"[WARN] {item}" for item in warn_lines]
            + details,
        )
    return CheckResult(
        "Task scheduler",
        STATUS_PASS,
        f"配置已校验（4 triggers 09:00/17:00/18:00/20:35，"
        f"{EXPECTED_MULTIPLE_INSTANCES}，PT32M，"
        f"runner={EXPECTED_SCHEDULER_RUNNER_MARKER}）",
        details,
    )


def _iso8601_duration_to_minutes(value: object) -> float | None:
    """解析 PT#H#M#S 形式的 ISO 8601 时长为分钟；失败返回 None。"""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(
        r"PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?",
        value,
    )
    if not match:
        return None
    hours, minutes, seconds = (float(part) if part else 0.0 for part in match.groups())
    return hours * 60.0 + minutes + seconds / 60.0


def check_auth_state(repo_root: Path, ctx: _CheckContext) -> CheckResult:
    """认证 / 凭据文件存在性（只报存在性、大小、mtime，绝不读内容）。"""
    data_dir = repo_root / "data"
    details: list[str] = []
    failures: list[str] = []
    warnings: list[str] = []
    now = ctx.local_now_naive()

    for name in _AUTH_REQUIRED_FILES:
        path = data_dir / name
        if not path.is_file():
            failures.append(f"缺失 {name}")
            continue
        stat = path.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime)
        details.append(f"{name}: 存在，{stat.st_size} 字节，mtime {mtime:%Y-%m-%d %H:%M:%S}")
        if name == "auth_state.json":
            if stat.st_size == 0:
                failures.append("auth_state.json 为空文件")
                continue
            try:
                with path.open("r", encoding="utf-8") as handle:
                    json.load(handle)
            except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
                failures.append(f"auth_state.json 非 JSON（{type(exc).__name__}）")
                continue
            age = now - mtime
            if age > AUTH_STATE_MAX_AGE:
                warnings.append(
                    f"auth_state.json 距今 {age.total_seconds() / 3600:.1f} 小时未刷新（>24h）"
                )

    for name in _AUTH_WARN_ONLY_FILES:
        path = data_dir / name
        if not path.is_file():
            warnings.append(f"缺失 {name}")
        else:
            stat = path.stat()
            details.append(
                f"{name}: 存在，{stat.st_size} 字节，"
                f"mtime {datetime.fromtimestamp(stat.st_mtime):%Y-%m-%d %H:%M:%S}"
            )

    if failures:
        return CheckResult(
            "Auth state", STATUS_FAIL, "；".join(failures), details + failures
        )
    if warnings:
        return CheckResult(
            "Auth state", STATUS_WARN, "；".join(warnings), details + warnings
        )
    return CheckResult("Auth state", STATUS_PASS, "认证/凭据文件齐全且新鲜", details)


def check_external_tools(repo_root: Path, ctx: _CheckContext) -> CheckResult:
    """招商局外部工具与运行时目录（复用既有常量，不新硬编码路径）。"""
    details: list[str] = []
    failures: list[str] = []
    warnings: list[str] = []

    # 复用 legacy 适配器中的真实常量定义（app/services/legacy_zhaoshang_adapter.py）。
    try:
        from app.services.legacy_zhaoshang_adapter import EXTERNAL_TOOL_PATH
    except Exception as exc:  # 常量定义模块不可导入时不崩溃。
        return CheckResult(
            "External tools",
            STATUS_UNKNOWN,
            "无法导入外部工具路径常量定义",
            [f"{type(exc).__name__}: {exc}"],
        )
    external_tool_path = Path(EXTERNAL_TOOL_PATH)
    if not external_tool_path.is_file():
        failures.append(f"招商局外部工具缺失: {external_tool_path}")
    else:
        details.append(f"外部工具: {external_tool_path}")

    for tool in ("powershell", "git"):
        resolved = shutil.which(tool)
        if resolved is None:
            failures.append(f"{tool} 不在 PATH")
        else:
            details.append(f"{tool}: {resolved}")

    runtime_dir = repo_root / "runtime" / "incremental_price_audit"
    if not runtime_dir.is_dir():
        warnings.append(f"运行时目录缺失（首跑可接受）: {runtime_dir}")
    else:
        details.append(f"运行时目录: {runtime_dir}")

    if failures:
        return CheckResult(
            "External tools", STATUS_FAIL, "；".join(failures), details + failures
        )
    if warnings:
        return CheckResult(
            "External tools", STATUS_WARN, "；".join(warnings), details + warnings
        )
    return CheckResult("External tools", STATUS_PASS, "外部工具与运行时目录就绪", details)


# ---------------------------------------------------------------------------
# 报告构建
# ---------------------------------------------------------------------------

_CHECK_ORDER: tuple[tuple[str, Callable[[Path, _CheckContext], CheckResult]], ...] = (
    ("Operating system", check_os),
    ("Git repository", check_git),
    ("Python runtime", check_python),
    ("Timezone", check_timezone),
    ("Task scheduler", check_scheduler),
    ("Auth state", check_auth_state),
    ("External tools", check_external_tools),
)


def build_readiness_report(
    repo_root: Path,
    *,
    expected_branch: str = EXPECTED_BRANCH_DEFAULT,
    runner: CommandRunner | None = None,
    wall_clock: Callable[[], datetime] | None = None,
    timeout_seconds: float = 10.0,
) -> ReadinessReport:
    """构建整体就绪报告；任一检查内部异常收敛为该检查的 UNKNOWN。"""
    ctx = _CheckContext(
        runner=runner if runner is not None else DefaultCommandRunner(),
        wall_clock=wall_clock if wall_clock is not None else datetime.now,
        timeout_seconds=timeout_seconds,
        expected_branch=expected_branch,
    )
    root = Path(repo_root)
    checks: list[CheckResult] = []
    for name, check in _CHECK_ORDER:
        try:
            checks.append(check(root, ctx))
        except Exception as exc:  # 单项异常不拖垮整体报告。
            checks.append(
                CheckResult(
                    name,
                    STATUS_UNKNOWN,
                    f"检查内部异常: {type(exc).__name__}",
                    [f"{type(exc).__name__}: {exc}"],
                )
            )
    return ReadinessReport(checks=checks)
