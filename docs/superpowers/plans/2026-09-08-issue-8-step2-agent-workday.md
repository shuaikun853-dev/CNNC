# Issue #8 STEP2 User-Triggered Outputs + Agent Workday Context — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 一次显式 STEP2 调用基于同一 authoritative snapshot 产出固定三份 xlsx（监控结果/可直接推送/不可直接推送）+ 机器可读 workday manifest；STEP1 与 STEP3 语义零改动。

**Architecture:** `scripts/export_current_monitor_status.py` 保持唯一业务 CLI 与 pushability 权威（`_status`/`_is_valid_new_price`/`_build_monitor_values`/`_write_workbook` 业务语义冻结）；新增 `app/services/workday_context.py` 通用 workday 目录/manifest 原子助手；脚本新增 staging→校验→atomic replace→hash→manifest 提交流程。旧 `runtime/saas状态查询/` 输出被新 authoritative 路径取代（审计结论：无生产代码读者）。

**Tech Stack:** Python 3.14（`E:\Python314\python.exe`）、openpyxl、pytest、GitHub Actions（critical-safety-tests.yml）。

**Spec:** 用户提供的 Issue #8 完整规格（51 节）+ GitHub Issue #8/#6。基线 `a1baf01a72620e4260e465a02b4e251f7d2c74ce`（origin/prod/closeout-20260818，已核验未移动）。

**Worktree:** `E:\.A中核运维\cnnc-ops-agent-worktrees\issue-8-step2-agent-workday`，分支 `feat/issue-8-step2-agent-workday`。

## Global Constraints

- `_status` / `_is_valid_new_price` / `_build_monitor_values` / `_write_workbook` 的业务语义与 5-Sheet 结构、`MONITOR_HEADERS`/`CNNC_KEYS` 顺序名称一律不改（只允许把 `_write_workbook` 内的样式循环原样抽成 `_format_sheet` 共享助手）。
- 现有 `tests/test_export_current_monitor_status.py` 必须原样全绿（一个字符都不改该文件）。
- 三份输出文件名固定：`STEP2_监控结果.xlsx` / `STEP2_可直接推送.xlsx` / `STEP2_不可直接推送.xlsx`，禁止 timestamp 进文件名；位置固定 `workdays/YYYY-MM-DD/03_step2/`。
- split 两表是 authoritative records 的纯投影（按 `row[17] == "是"/"否"`），禁止二次业务判断、禁止二次 API 请求。
- 不修改 STEP1 任何业务代码；不调用 `process_monitor_no_push_zhaoshang.py`；不生成 zhImport；不碰 #10/#11 代码（customer_prewarm_dispatch.py / customer_reference_routing.py / Staples worker / Shared Crawl Cache / crawl_url_identity.py / Zhaoshang adapter / priority）。
- 所有测试 fake/mock，绝不访问真实 CNNC/SaaS API，绝不写生产 `runtime/`。
- 文档行数预算（`tests/test_context_documents.py`）：TESTING.md ≤240（现为 240，净增必须 ≤0）、README.md ≤180（现 120）、STEP_2 runbook ≤120（现 68）；runbook 必须保留字符串 `sassCheckStatuss=["0"]`、`每批最多 200`、`syncPriceStatus!=5`、`BATCH_STATE.md`。
- Shell 是 cmd.exe；pytest 用 `E:\Python314\python.exe`，全量/长套件加 `--basetemp=.pytest_tmp`。
- 提交信息格式沿用仓库惯例：`feat(step2): ...` / `test(step2): ...` / `docs(step2): ...` / `ci(step2): ...`。禁止 push（由主 Agent 验收后执行）。

## 冻结的 pushability 语义（回归护栏，禁止"修复"）

- `isSync != 1` → 否/采集未成功；`isSyncStandard == 0` → 否/同款匹配中；`!= 1` → 否/同款匹配异常/未成功；`syncPriceStatus == 5` → 否/价格检测校验失败；newPrice 无效（None/空/null/非数字/NaN/Inf/≤0/bool）→ 否/推送价格为空或无效；否则 是/可推送。
- `syncPriceStatus == ""` 或 `== 2` 且 newPrice 有效 → **是**（客户既有语义，已有参数化测试锁定）。
- 中核存在但 SaaS monitor 未返回 → 否/SaaS监控缺失/中核存在但SaaS监控未返回/缺失（索引 20 = "缺失"），必须保留且进入不可直接推送表。
- records 行结构：13 个 CNNC_KEYS + [采集状态, 同款匹配状态, 价格计算状态, newPrice, 是否可推送, 最终分类, 问题说明, SaaS监控状态]；`是否可推送`=row[17]，`SaaS监控状态`=row[20]。

---

### Task 1: `app/services/workday_context.py`（通用 workday/manifest 助手）

**Files:**
- Create: `app/services/workday_context.py`
- Test: `tests/test_workday_context.py`

**Interfaces (Produces):**
- `SCHEMA_VERSION = 1`；`STAGE_DIRS = {"step1": "01_step1", "reference_prewarm": "02_reference_prewarm", "step2": "03_step2", "step3": "04_step3"}`
- `class WorkdayContextError(RuntimeError)`
- `now_in_shanghai() -> datetime`（tz-aware）
- `normalize_business_date(value) -> str`（严格 YYYY-MM-DD 且真实日期，否则 raise）
- `workday_root(business_date, base: Path | None = None) -> Path`（base 缺省=仓库根，即模块 `parents[2]`；返回 `<base>/workdays/YYYY-MM-DD`）
- `ensure_workday_layout(business_date, base=None) -> Path`（建根目录+4 个 stage 目录，不建 manifest）
- `manifest_path(business_date, base=None) -> Path`
- `read_manifest(business_date, base=None) -> dict`（缺失→返回骨架；JSON 坏/schema 不对/business_date 不符/stages 非 dict → `WorkdayContextError`，绝不静默当空）
- `update_stage(business_date, stage, stage_payload: dict, base=None) -> dict`（读-改-原子写：tempfile.mkstemp 同目录 + `os.replace`；stage 节点非 dict → raise；未知 stage → raise；刷新顶层 `updated_at`）
- `resolve_stage_output(business_date, stage, key, base=None) -> Path`（读 `stages[stage].outputs[key]` 相对路径 → workday 根解析；越出 workday 根 → raise）

- [ ] **Step 1: 写失败测试** `tests/test_workday_context.py`（完整内容）：

```python
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services import workday_context
from app.services.workday_context import WorkdayContextError


def test_workday_layout_creates_root_and_all_stage_dirs(tmp_path):
    root = workday_context.ensure_workday_layout("2026-09-08", tmp_path)
    assert root == tmp_path / "workdays" / "2026-09-08"
    for name in ("01_step1", "02_reference_prewarm", "03_step2", "04_step3"):
        assert (root / name).is_dir()
    assert not (root / "manifest.json").exists()


def test_read_manifest_missing_returns_fresh_skeleton(tmp_path):
    manifest = workday_context.read_manifest("2026-09-08", tmp_path)
    assert manifest["schema_version"] == 1
    assert manifest["business_date"] == "2026-09-08"
    assert set(manifest["stages"]) == set(workday_context.STAGE_DIRS)
    assert all(value == {} for value in manifest["stages"].values())


def test_update_stage_roundtrips_payload_atomically_and_keeps_others(tmp_path):
    workday_context.update_stage(
        "2026-09-08",
        "step2",
        {"status": "RUNNING", "generation": 1},
        tmp_path,
    )
    workday_context.update_stage("2026-09-08", "step2", {"status": "COMPLETED"}, tmp_path)
    manifest = workday_context.read_manifest("2026-09-08", tmp_path)
    assert manifest["stages"]["step2"] == {"status": "COMPLETED", "generation": 1}
    assert manifest["updated_at"]
    assert manifest["stages"]["step1"] == {}
    raw = json.loads((tmp_path / "workdays" / "2026-09-08" / "manifest.json").read_text(encoding="utf-8"))
    assert raw == manifest


@pytest.mark.parametrize("bad_manifest", ["{ not json", "[]", '{"schema_version": 2}', '{"schema_version": 1, "business_date": "2026-09-07", "stages": {}}', '{"schema_version": 1, "business_date": "2026-09-08"}'])
def test_read_manifest_malformed_fails_closed_without_overwriting(tmp_path, bad_manifest):
    path = workday_context.manifest_path("2026-09-08", tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(bad_manifest, encoding="utf-8")
    with pytest.raises(WorkdayContextError):
        workday_context.read_manifest("2026-09-08", tmp_path)
    with pytest.raises(WorkdayContextError):
        workday_context.update_stage("2026-09-08", "step2", {"status": "RUNNING"}, tmp_path)
    assert path.read_text(encoding="utf-8") == bad_manifest


@pytest.mark.parametrize("bad_date", ["2026/09/08", "2026-9-8", "../evil", "2026-02-30", ""])
def test_normalize_business_date_rejects_invalid_dates(bad_date):
    with pytest.raises(WorkdayContextError):
        workday_context.normalize_business_date(bad_date)


def test_normalize_business_date_accepts_real_dates():
    assert workday_context.normalize_business_date("2026-09-08") == "2026-09-08"


def test_update_stage_rejects_unknown_stage(tmp_path):
    with pytest.raises(WorkdayContextError):
        workday_context.update_stage("2026-09-08", "step5", {}, tmp_path)


def test_resolve_stage_output_resolves_relative_path(tmp_path):
    workday_context.update_stage(
        "2026-09-08",
        "step2",
        {"outputs": {"monitor": "03_step2/STEP2_监控结果.xlsx"}},
        tmp_path,
    )
    resolved = workday_context.resolve_stage_output("2026-09-08", "step2", "monitor", tmp_path)
    assert resolved == (tmp_path / "workdays" / "2026-09-08" / "03_step2" / "STEP2_监控结果.xlsx").resolve()


@pytest.mark.parametrize("bad_outputs", [{"monitor": "../escape.xlsx"}, {"other": "03_step2/x.xlsx"}, "not-a-dict"])
def test_resolve_stage_output_rejects_missing_or_escaping_paths(tmp_path, bad_outputs):
    workday_context.update_stage("2026-09-08", "step2", {"outputs": bad_outputs}, tmp_path)
    with pytest.raises(WorkdayContextError):
        workday_context.resolve_stage_output("2026-09-08", "step2", "monitor", tmp_path)


def test_now_in_shanghai_is_timezone_aware():
    now = workday_context.now_in_shanghai()
    assert now.tzinfo is not None and now.utcoffset() is not None
```

注意：`bad_outputs` 传字符串时 `update_stage` 也会写进去（payload 不校验 outputs 形状，校验在 resolve 侧），这是刻意的分层。

- [ ] **Step 2: 跑测试确认失败**

Run: `E:\Python314\python.exe -m pytest tests\test_workday_context.py -q --basetemp=.pytest_tmp`
Expected: FAIL（ModuleNotFoundError: app.services.workday_context）

- [ ] **Step 3: 实现 `app/services/workday_context.py`**（完整内容）：

```python
"""Agent workday directory and manifest context.

固定按业务日期组织的工作日目录与机器可读 manifest：

    workdays/YYYY-MM-DD/
    ├── manifest.json
    ├── 01_step1/
    ├── 02_reference_prewarm/
    ├── 03_step2/
    └── 04_step3/

manifest 只是 Agent 导航/运行状态，不是任何业务步骤的 blocker；全部写入
通过同目录临时文件 + os.replace 原子完成。manifest 中的路径一律使用相对
workday 根目录的正斜杠相对路径，便于跨机器复用。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SCHEMA_VERSION = 1
WORKDAYS_DIR_NAME = "workdays"
STAGE_DIRS = {
    "step1": "01_step1",
    "reference_prewarm": "02_reference_prewarm",
    "step2": "03_step2",
    "step3": "04_step3",
}
BUSINESS_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SHANGHAI = ZoneInfo("Asia/Shanghai")


class WorkdayContextError(RuntimeError):
    """business_date 非法或 manifest 不可读/不合法时抛出（fail-closed）。"""


def now_in_shanghai() -> datetime:
    return datetime.now(SHANGHAI)


def normalize_business_date(business_date: object) -> str:
    text = str(business_date).strip()
    if not isinstance(business_date, str) or not BUSINESS_DATE_PATTERN.match(text):
        raise WorkdayContextError(f"business_date must be YYYY-MM-DD, got: {business_date!r}")
    try:
        day = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as error:
        raise WorkdayContextError(f"business_date is not a real date: {business_date!r}") from error
    return day.isoformat()


def _default_base() -> Path:
    return Path(__file__).resolve().parents[2]


def workday_root(business_date: str, base: Path | None = None) -> Path:
    root = Path(base) if base is not None else _default_base()
    return root / WORKDAYS_DIR_NAME / normalize_business_date(business_date)


def ensure_workday_layout(business_date: str, base: Path | None = None) -> Path:
    root = workday_root(business_date, base)
    for stage_dir in STAGE_DIRS.values():
        (root / stage_dir).mkdir(parents=True, exist_ok=True)
    return root


def manifest_path(business_date: str, base: Path | None = None) -> Path:
    return workday_root(business_date, base) / "manifest.json"


def _skeleton(business_date: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "business_date": normalize_business_date(business_date),
        "updated_at": now_in_shanghai().isoformat(timespec="seconds"),
        "stages": {stage: {} for stage in STAGE_DIRS},
    }


def _validate_manifest(manifest: object, business_date: str, path: Path) -> None:
    if not isinstance(manifest, dict):
        raise WorkdayContextError(f"manifest at {path} is not a JSON object")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise WorkdayContextError(
            f"manifest at {path} has unsupported schema_version: {manifest.get('schema_version')!r}"
        )
    if manifest.get("business_date") != normalize_business_date(business_date):
        raise WorkdayContextError(
            f"manifest at {path} has business_date {manifest.get('business_date')!r}, expected {business_date!r}"
        )
    stages = manifest.get("stages")
    if not isinstance(stages, dict):
        raise WorkdayContextError(f"manifest at {path} is missing a stages object")


def read_manifest(business_date: str, base: Path | None = None) -> dict:
    path = manifest_path(business_date, base)
    if not path.exists():
        return _skeleton(business_date)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkdayContextError(f"manifest unreadable at {path}: {error}") from error
    _validate_manifest(manifest, business_date, path)
    return manifest


def _write_manifest_atomic(manifest: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".manifest-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def update_stage(business_date: str, stage: str, stage_payload: dict, base: Path | None = None) -> dict:
    if stage not in STAGE_DIRS:
        raise WorkdayContextError(f"unknown stage: {stage!r}")
    manifest = read_manifest(business_date, base)
    node = manifest["stages"].get(stage)
    if not isinstance(node, dict):
        raise WorkdayContextError(f"manifest stage {stage!r} is not an object")
    node.update(stage_payload)
    manifest["updated_at"] = now_in_shanghai().isoformat(timespec="seconds")
    _write_manifest_atomic(manifest, manifest_path(business_date, base))
    return manifest


def resolve_stage_output(business_date: str, stage: str, key: str, base: Path | None = None) -> Path:
    manifest = read_manifest(business_date, base)
    node = manifest["stages"].get(stage)
    outputs = node.get("outputs") if isinstance(node, dict) else None
    if not isinstance(outputs, dict) or key not in outputs:
        raise WorkdayContextError(f"manifest has no output {stage}.{key}")
    relative = outputs[key]
    if not isinstance(relative, str) or not relative:
        raise WorkdayContextError(f"manifest output {stage}.{key} is not a path string")
    root = workday_root(business_date, base).resolve()
    resolved = (root / relative).resolve()
    if root != resolved and root not in resolved.parents:
        raise WorkdayContextError(f"manifest output {stage}.{key} escapes the workday root: {relative!r}")
    return resolved
```

- [ ] **Step 4: 跑测试确认通过**

Run: `E:\Python314\python.exe -m pytest tests\test_workday_context.py -q --basetemp=.pytest_tmp`
Expected: PASS（全部）

- [ ] **Step 5: Commit**

```bash
git add app/services/workday_context.py tests/test_workday_context.py
git commit -m "feat(step2): add workday context manifest helpers"
```

---

### Task 2: STEP2 三表输出 + business_date CLI + 原子发布

**Files:**
- Modify: `scripts/export_current_monitor_status.py`
- Test: `tests/test_step2_outputs.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `ensure_workday_layout` / `update_stage` / `read_manifest` / `workday_root` / `now_in_shanghai` / `WorkdayContextError`；`app.services.incremental_audit_runtime.business_date_in_shanghai`。
- Produces（脚本内新增，供测试引用）：
  - `PUSHABLE_COL = 17`；`STEP2_DIR_NAME = "03_step2"`；`STEP2_OUTPUT_NAMES = {"monitor": "STEP2_监控结果.xlsx", "pushable": "STEP2_可直接推送.xlsx", "non_pushable": "STEP2_不可直接推送.xlsx"}`
  - `_format_sheet(sheet)`（从 `_write_workbook` 原样抽出的样式循环）
  - `_write_projection_workbook(records, output: Path, sheet_name: str) -> None`
  - `_read_sheet_rows(path, sheet_name) -> list[list[object]]`
  - `_validate_split_projection(records, monitor_path, pushable_path, non_pushable_path) -> None`
  - `_begin_step2_run(business_date: str, base=None) -> dict`（写 RUNNING，返回 `{"run_id", "generation", "started_at"}`；run_id 形如 `20260908T193012-123456`，Windows 文件名安全）
  - `_complete_step2_publication(records, business_date, run_info: dict, base=None) -> dict`（staging→写三表→校验→os.replace→sha256→COMPLETED；返回含 `business_date/run_id/generation/outputs/counts/summary`）
  - `_fail_step2_run(business_date, error: BaseException, base=None) -> None`（写 FAILED `error_type`/`error_summary`，二次失败不掩盖原错误）
  - `main(argv)`：`--business-date`（缺省 `business_date_in_shanghai()`）→ `ensure_workday_layout` → RUNNING → 查询构建 records（原逻辑原样）→ `_complete_step2_publication` → 打印 JSON。

**脚本改动要点（精确）：**

1. 顶部 import 增加 `hashlib os shutil sys`；在 `ROOT = Path(__file__).resolve().parents[1]` 之后加：

```python
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.incremental_audit_runtime import business_date_in_shanghai
from app.services.workday_context import (
    WorkdayContextError,
    ensure_workday_layout,
    now_in_shanghai,
    read_manifest,
    update_stage,
    workday_root,
)
```

2. `_write_workbook` 中样式 for 循环替换为 `_format_sheet(sheet)` 调用；新函数逐行复制原样式代码（Font/PatternFill/freeze A2/autofilter/auto width）。
3. 查询→records 的现有主体原样保留（分页、count 校验、documentId 唯一性、SaaS 分批 join 全部不动）。
4. 删除旧输出段（`timestamp = datetime.now()...` 起 5 行），换为发布流程。
5. `build_parser()` 增加 `--business-date`。
6. `main()` 新结构（完整）：

```python
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="查询截至当前的中核 SaaS 监控状态并生成五 Sheet Excel。"
    )
    parser.add_argument(
        "--business-date",
        default=None,
        help="业务日期 YYYY-MM-DD；缺省使用上海时区当前业务日",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    business_date = args.business_date or business_date_in_shanghai()
    run_info = _begin_step2_run(business_date, ROOT)
    try:
        apply_internal_no_proxy_env()
        cnnc = load_curl_command(ROOT / "data/latest_cnnc_query.curl")
        saas = load_curl_command(ROOT / "data/saas_zhuangtai.curl")
        cnnc_body, saas_body = json.loads(cnnc["data"]), json.loads(saas["data"])
        if cnnc_body.get("sassCheckStatuss") != ["0"] or (str(saas_body.get("tenantId")), str(saas_body.get("commodityPoolId"))) != ("188605", "222"):
            raise RuntimeError("租户、商品池或中核待处理筛选不符合第二步范围")
        auth_manager = AuthManager()
        saas = auth_manager.prepare_curl("saas", saas, refresh=True)
        records = _collect_records(cnnc, cnnc_body, saas, saas_body, auth_manager)
        publication = _complete_step2_publication(records, business_date, run_info, ROOT)
    except BaseException as error:
        _fail_step2_run(business_date, error, ROOT)
        raise
    print(json.dumps(publication, ensure_ascii=False))
```

（`_begin_step2_run` 内部 `normalize_business_date` 校验非法日期；查询主体抽为 `_collect_records(cnnc, cnnc_body, saas, saas_body, auth_manager) -> list[list[object]]`，逻辑与现状逐行等价，仅函数化。）

7. 新增发布函数（完整）：

```python
def _begin_step2_run(business_date: str, base=None) -> dict:
    ensure_workday_layout(business_date, base)
    manifest = read_manifest(business_date, base)
    previous = manifest["stages"].get("step2") or {}
    now = now_in_shanghai()
    run_info = {
        "run_id": now.strftime("%Y%m%dT%H%M%S") + f"-{now.microsecond:06d}",
        "generation": int(previous.get("generation") or 0) + 1,
        "started_at": now.isoformat(timespec="seconds"),
    }
    update_stage(
        business_date,
        "step2",
        {"status": "RUNNING", **run_info},
        base,
    )
    return run_info


def _fail_step2_run(business_date: str, error: BaseException, base=None) -> None:
    try:
        update_stage(
            business_date,
            "step2",
            {
                "status": "FAILED",
                "error_type": type(error).__name__,
                "error_summary": str(error)[:500],
            },
            base,
        )
    except Exception:
        pass  # 失败记录失败不得掩盖原始业务错误


def _file_record(relative: str, path: Path) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"path": relative, "sha256": digest.hexdigest(), "size": path.stat().st_size}


def _complete_step2_publication(records, business_date: str, run_info: dict, base=None) -> dict:
    step2_dir = workday_root(business_date, base) / STEP2_DIR_NAME
    staging_dir = step2_dir / ".staging" / run_info["run_id"]
    staging_dir.mkdir(parents=True, exist_ok=False)
    pushable_rows = [row for row in records if row[PUSHABLE_COL] == "是"]
    non_pushable_rows = [row for row in records if row[PUSHABLE_COL] == "否"]
    staged = {key: staging_dir / name for key, name in STEP2_OUTPUT_NAMES.items()}
    try:
        summary = _write_workbook(records, staged["monitor"])
        _write_projection_workbook(pushable_rows, staged["pushable"], "可直接推送")
        _write_projection_workbook(non_pushable_rows, staged["non_pushable"], "不可直接推送")
        _validate_split_projection(records, staged["monitor"], staged["pushable"], staged["non_pushable"])
        for name in STEP2_OUTPUT_NAMES.values():
            os.replace(staging_dir / name, step2_dir / name)
    except BaseException:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise
    files = {
        key: _file_record(f"{STEP2_DIR_NAME}/{name}", step2_dir / name)
        for key, name in STEP2_OUTPUT_NAMES.items()
    }
    counts = {
        "monitor_total": len(records),
        "pushable": len(pushable_rows),
        "non_pushable": len(non_pushable_rows),
    }
    update_stage(
        business_date,
        "step2",
        {
            "status": "COMPLETED",
            "completed_at": now_in_shanghai().isoformat(timespec="seconds"),
            "outputs": {key: f"{STEP2_DIR_NAME}/{name}" for key, name in STEP2_OUTPUT_NAMES.items()},
            "files": files,
            "counts": counts,
        },
        base,
    )
    shutil.rmtree(staging_dir, ignore_errors=True)
    return {
        "business_date": business_date,
        "run_id": run_info["run_id"],
        "generation": run_info["generation"],
        "outputs": {key: str(step2_dir / name) for key, name in STEP2_OUTPUT_NAMES.items()},
        "counts": counts,
        "summary": summary,
    }
```

（`workday_root` 已在脚本顶部 import；测试通过 `monkeypatch.setattr(monitor_export, "ROOT", tmp_path)` 重定向输出根。）

注意：`_fail_step2_run` 由 `main` 的 except 调用；`_complete_step2_publication` 内部失败向上抛给 main 统一记 FAILED，staging 清理在自身 except 中完成（失败代 staging 不落正式文件——os.replace 前任何失败都不触碰旧代文件；replace 中途失败的混合态由 FAILED manifest 挡住消费，符合规格 §21 manifest=commit point）。

8. `_write_projection_workbook` / `_read_sheet_rows` / `_validate_split_projection`（完整）：

```python
def _write_projection_workbook(records, output: Path, sheet_name: str) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_name
    sheet.append(MONITOR_HEADERS)
    for row in records:
        sheet.append(row)
    _format_sheet(sheet)
    workbook.save(output)


def _read_sheet_rows(path: Path, sheet_name: str) -> list[list[object]]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        return [list(row) for row in workbook[sheet_name].iter_rows(values_only=True)]
    finally:
        workbook.close()


def _validate_split_projection(records, monitor_path: Path, pushable_path: Path, non_pushable_path: Path) -> None:
    expected_monitor = [MONITOR_HEADERS, *records]
    expected_pushable = [MONITOR_HEADERS, *[row for row in records if row[PUSHABLE_COL] == "是"]]
    expected_non_pushable = [MONITOR_HEADERS, *[row for row in records if row[PUSHABLE_COL] == "否"]]
    monitor_rows = _read_sheet_rows(monitor_path, "全量监控结果")
    if monitor_rows != expected_monitor:
        raise RuntimeError(f"monitor workbook rows do not match authoritative records: {monitor_path}")
    pushable_rows = _read_sheet_rows(pushable_path, "可直接推送")
    if pushable_rows != expected_pushable:
        raise RuntimeError(f"pushable projection mismatch: {pushable_path}")
    non_pushable_rows = _read_sheet_rows(non_pushable_path, "不可直接推送")
    if non_pushable_rows != expected_non_pushable:
        raise RuntimeError(f"non-pushable projection mismatch: {non_pushable_path}")
    pushable_ids = {row[0] for row in pushable_rows[1:]}
    non_pushable_ids = {row[0] for row in non_pushable_rows[1:]}
    monitor_ids = {row[0] for row in monitor_rows[1:]}
    if pushable_ids & non_pushable_ids:
        raise RuntimeError("pushable and non-pushable documentIds intersect")
    if pushable_ids | non_pushable_ids != monitor_ids:
        raise RuntimeError("pushable + non-pushable do not cover monitor documentIds")
```

顶部需补 `from openpyxl import Workbook, load_workbook`。

- [ ] **Step 1: 写失败测试** `tests/test_step2_outputs.py` 核心 Case 1-8 + 快车道（完整骨架，fake 全套）：

```python
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from openpyxl import load_workbook

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))
SPEC = importlib.util.spec_from_file_location(
    "export_current_monitor_status_step2",
    SCRIPT_DIR / "export_current_monitor_status.py",
)
assert SPEC and SPEC.loader
monitor_export = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor_export)


def cnnc_row(document_id, name="商品"):
    return {
        "documentId": document_id,
        "commodityName": name,
        "categoryName": "办公用品",
        "fourCategoryId": 4,
        "fourCategoryName": "四級",
        "referencePrice": 10,
        "referenceLink": "https://example.com/item",
        "materialCode": "M-1",
        "materialDesc": "desc",
        "createTime": "2026-09-08 10:00:00",
        "updateTime": "2026-09-08 10:00:00",
        "sassCheckStatus": 0,
        "quasiStandardCommodityCode": "",
    }


def saas_row(document_id, *, is_sync=1, is_standard=1, price_status=1, new_price="19.90"):
    return {
        "commodityId": str(document_id),
        "isSync": is_sync,
        "isSyncStandard": is_standard,
        "syncPriceStatus": price_status,
        "newPrice": new_price,
    }


class FakeHarness:
    """一次 main() 端到端所需的全部替身；cnnc_docs 顺序即 authoritative 顺序。"""

    def __init__(self, monkeypatch, tmp_path, cnnc_docs, saas_monitors, extra_cnnc_after_first_pass=None):
        self.tmp_path = tmp_path
        self.calls = []
        self.pass_count = 0
        self.extra = extra_cnnc_after_first_pass

        def fake_load_curl(path):
            name = Path(path).name
            if name == "latest_cnnc_query.curl":
                body = {"sassCheckStatuss": ["0"], "page": 1, "limit": 200}
            else:
                body = {"tenantId": "188605", "commodityPoolId": "222"}
            return {
                "url": f"https://fake.local/{name}",
                "headers": [("Content-Type", "application/json")],
                "cookie": "x-access-token=fake",
                "method": "POST",
                "data": json.dumps(body),
            }

        class FakeAuthManager:
            def prepare_curl(self, system, curl, refresh=False):
                return curl

        def fake_request(config, payload, **kwargs):
            self.calls.append((config["url"], dict(payload)))
            if "commodityId" in payload:
                ids = payload["commodityId"].split()
                rows = [row for row in saas_monitors if str(row["commodityId"]) in ids]
                return {"rspCode": "0000", "count": len(rows), "rows": rows}
            docs = list(cnnc_docs)
            if self.pass_count >= 1 and self.extra:
                docs = docs + self.extra  # 第二轮完整分页才会看到：证明单轮内不复用第二次数据
            self.pass_count += 1
            return {"rspCode": "0000", "count": len(docs), "rows": docs}

        monkeypatch.setattr(monitor_export, "ROOT", tmp_path)
        monkeypatch.setattr(monitor_export, "load_curl_command", fake_load_curl)
        monkeypatch.setattr(monitor_export, "AuthManager", FakeAuthManager)
        monkeypatch.setattr(monitor_export, "_request", fake_request)
        monkeypatch.setattr(monitor_export, "apply_internal_no_proxy_env", lambda: None)

    def run(self, argv):
        monitor_export.main(argv)
        return self.calls
```

测试用例（都写进文件）：

```python
BASIC_DOCS = [cnnc_row("A"), cnnc_row("B"), cnnc_row("C")]
BASIC_SAAS = [
    saas_row("A", is_sync=1, is_standard=1, price_status=1, new_price="10.00"),
    saas_row("B", is_sync=1, is_standard=1, price_status=5, new_price="5.00"),
    # C 无 SaaS monitor → SaaS监控缺失
]


def _step2_dir(tmp_path, business_date="2026-09-08"):
    return tmp_path / "workdays" / business_date / "03_step2"


def _rows(path, sheet):
    book = load_workbook(path, read_only=True, data_only=True)
    try:
        return [list(r) for r in book[sheet].iter_rows(values_only=True)]
    finally:
        book.close()


def test_case1_three_fixed_outputs_and_manifest(monkeypatch, tmp_path):
    harness = FakeHarness(monkeypatch, tmp_path, BASIC_DOCS, BASIC_SAAS)
    harness.run(["--business-date", "2026-09-08"])
    step2 = _step2_dir(tmp_path)
    assert (step2 / "STEP2_监控结果.xlsx").is_file()
    assert (step2 / "STEP2_可直接推送.xlsx").is_file()
    assert (step2 / "STEP2_不可直接推送.xlsx").is_file()
    for stage in ("01_step1", "02_reference_prewarm", "03_step2", "04_step3"):
        assert (tmp_path / "workdays" / "2026-09-08" / stage).is_dir()
    manifest = json.loads((tmp_path / "workdays" / "2026-09-08" / "manifest.json").read_text(encoding="utf-8"))
    step2_node = manifest["stages"]["step2"]
    assert step2_node["status"] == "COMPLETED"
    assert step2_node["counts"] == {"monitor_total": 3, "pushable": 1, "non_pushable": 2}
    assert step2_node["generation"] == 1
    assert step2_node["outputs"]["monitor"] == "03_step2/STEP2_监控结果.xlsx"
    import hashlib
    for key, name in monitor_export.STEP2_OUTPUT_NAMES.items():
        info = step2_node["files"][key]
        assert info["path"] == f"03_step2/{name}"
        assert info["sha256"] == hashlib.sha256((step2 / name).read_bytes()).hexdigest()
        assert info["size"] == (step2 / name).stat().st_size
    assert manifest["stages"]["step3"] == {}
```

其余用例（同文件落全代码）：Case2/3 split 行与 authoritative 逐字段相等（直接对 `_rows(pushable_path, "可直接推送")` 与手工构造的期望全行比对，注意 records 13 CNNC 列 + 8 监控列的期望值）；Case4 C 的 documentId 在不可直接推送表；Case5 交集空；Case6 并集=全量；Case7 顺序（docs 顺序 A是/B否/D是/C否 → pushable=[A,D] 顺序、non_pushable=[B,C]）；Case8+单快照：`extra_cnnc_after_first_pass=[cnnc_row("X")]` 时输出仍 3 行且 `len(harness.calls) == 1(cnnc) + 2(saas 批? 3 ids=1 批)` → 3 ids 一批 → calls == 2；business date 均为 2026-09-08；`--business-date` 显式值覆盖 monkeypatched `business_date_in_shanghai`（返回 "2026-09-09"）；缺省日期用 helper（返回 "2026-09-07" → 目录 2026-09-07）；重跑两次（第二次 docs 多一行）→ generation=2、run_id 不同、三文件内容为第二代、manifest hash 与当前文件一致、`.staging` 下无残留；失败语义（成功一轮后 monkeypatch `monitor_export._write_projection_workbook` raise RuntimeError 再跑）→ status=FAILED、error_type 记录、三文件仍为第一代内容；查询失败（fake `_request` raise）→ FAILED 且无正式输出；malformed manifest（先写坏 JSON 再跑）→ 抛 WorkdayContextError、manifest 原文未变、03_step2 无 xlsx；STEP3 隔离（monkeypatch `subprocess.run`/`subprocess.Popen` 为 AssertionError 桩，跑完整 STEP2 → 通过即证明零 subprocess/STEP3；再断言 workdays 树内无 "zhImport" 文件名、step3 stage 保持 `{}`）；五 Sheet 调和（监控工作簿 5 sheet 名不变、`可推送数据` 行 == pushable 文件行、按 documentId `需处理数据`+`SaaS监控缺失` 并集 == non-pushable 文件行且无重复）。

- [ ] **Step 2: 跑新测试确认失败**（脚本还没有新函数/CLI）
Run: `E:\Python314\python.exe -m pytest tests\test_step2_outputs.py -q --basetemp=.pytest_tmp` → FAIL
- [ ] **Step 3: 按上述精确改动实现脚本**
- [ ] **Step 4: 新旧测试都通过**

Run: `E:\Python314\python.exe -m pytest tests\test_step2_outputs.py tests\test_export_current_monitor_status.py tests\test_workday_context.py -q --basetemp=.pytest_tmp`
Expected: PASS（现有 test_export_current_monitor_status.py 未改动且全绿）
- [ ] **Step 5: Commit**

```bash
git add scripts/export_current_monitor_status.py tests/test_step2_outputs.py
git commit -m "feat(step2): authoritative three-output publication with workday manifest"
```

---

### Task 3: 显式触发守卫测试（无自动 caller + STEP3 隔离源级锁）

**Files:**
- Test: `tests/test_step2_explicit_gate.py`（新建）

**Interfaces:** Consumes: 仓库源码树。Produces: 永久回归守卫。

- [ ] **Step 1: 写测试（完整）**

```python
"""STEP2 只能显式触发：仓库代码不得出现自动调用方；STEP2 不得拉起 STEP3。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STEP2_MODULE = "export_current_monitor_status"
STEP3_MODULE = "process_monitor_no_push_zhaoshang"


def _python_sources():
    for folder in (ROOT / "app", ROOT / "scripts"):
        for path in folder.rglob("*.py"):
            if path.name in {f"{STEP2_MODULE}.py", f"{STEP3_MODULE}.py"}:
                continue
            yield path


def test_no_automatic_step2_callers_in_repo_code():
    offenders = [
        str(path.relative_to(ROOT))
        for path in _python_sources()
        if STEP2_MODULE in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"AUTO_STEP2_CALLERS must stay NONE, found: {offenders}"


def test_step2_script_never_references_step3():
    source = (ROOT / "scripts" / f"{STEP2_MODULE}.py").read_text(encoding="utf-8")
    assert STEP3_MODULE not in source
    assert "zhImport" not in source
```

- [ ] **Step 2: 跑测试** Run: `E:\Python314\python.exe -m pytest tests\test_step2_explicit_gate.py -q --basetemp=.pytest_tmp` → PASS（审计已确认现状无 caller；此测试把结论固化为守卫）
- [ ] **Step 3: Commit** `git add tests\test_step2_explicit_gate.py && git commit -m "test(step2): pin explicit-invocation gate with no-auto-caller guard"`

---

### Task 4: 文档（workflow 文档 + runbook + README + TESTING）

**Files:**
- Create: `docs/workflows/STEP2_AGENT_WORKFLOW.md`
- Modify: `docs/runbooks/STEP_2_MONITOR_STATUS.md`、`README.md`（第 10 行输出列）、`TESTING.md`（§2 第二步命令行、§5 第二步验收）

**约束：** TESTING.md 净增行 ≤ 0（现 240/240）；runbook ≤120 行且保留 `sassCheckStatuss=["0"]`、`每批最多 200`、`syncPriceStatus!=5`、`BATCH_STATE.md`；README ≤180。

- [ ] **Step 1: 新建 `docs/workflows/STEP2_AGENT_WORKFLOW.md`**（长期规则，不写当日运行态）：

```markdown
# STEP2 Agent Workflow（用户触发）

## 顺序（人工闸门）

STEP1 → 人工审核 → 人工收口 → STOP → 用户明确要求执行 STEP2 → STEP2 三表 → STOP → 用户再次明确要求才进入 STEP3。

- STEP1 的审核/收口/closeout/finalize/调度 tick/Dashboard 一律不得自动触发 STEP2。
- STEP2 产出后立即停止：不自动生成 zhImport、不自动启动 STEP3（`scripts/process_monitor_no_push_zhaoshang.py`）、不自动处理客户 referenceLink、不创建招商局任务、不运行史泰博。

## STEP2 输入

- `data/latest_cnnc_query.curl`（中核待处理列表，`sassCheckStatuss=["0"]`）与 `data/saas_zhuangtai.curl`（租户 188605 / 商品池 222 监控查询），经 `data/auth_state.json` 运行时认证。完整只读分页，一次查询构成唯一 authoritative snapshot。

## STEP2 输出（固定）

`python scripts/export_current_monitor_status.py --business-date 2026-09-08`（缺省用上海时区当前业务日）一次生成：

- `workdays/2026-09-08/03_step2/STEP2_监控结果.xlsx`（权威五 Sheet：全量监控结果/可推送数据/需处理数据/SaaS监控缺失/状态统计）
- `workdays/2026-09-08/03_step2/STEP2_可直接推送.xlsx`（单 Sheet `可直接推送`）
- `workdays/2026-09-08/03_step2/STEP2_不可直接推送.xlsx`（单 Sheet `不可直接推送`，含 SaaS监控缺失行）
- `workdays/2026-09-08/manifest.json`

三份文件来自同一内存 records snapshot；split 两表只是按 `是否可推送` 列的纯投影，恒等式 `monitor_total = pushable + non_pushable`，documentId 交集为空、并集等于全量。同日重跑为原子整代替换（generation+1）。

## Pushability authority

冻结于 `scripts/export_current_monitor_status.py` 的 `_status()` / `_is_valid_new_price()`：判定顺序 采集→同款→`syncPriceStatus=5`→newPrice 有效性；`syncPriceStatus` 为空或 2 且 newPrice 有效即为可推送（客户既有语义）。任何票据不得"顺手修复"这些语义。

## Agent 定位

`workdays/YYYY-MM-DD/manifest.json` 是当天导航权威：`stages.step2.status == COMPLETED` 且 `files.*.sha256` 与磁盘文件一致时才可消费三表；`FAILED` 表示该代不可用。禁止按文件名时间戳猜测目录。

## 禁止

STEP1 不自动 STEP2；STEP2 不自动 STEP3；真实 STEP2 是用户显式业务动作，开发/测试一律 fake 数据。
```

- [ ] **Step 2: 更新 runbook/README/TESTING**（要点，非全文重写）：
  - runbook：`固定命令` 改为带 `--business-date`；`输出` 改为 workdays 三文件 + manifest；第 10 条 BATCH_STATE 说明改为"输出目录=workdays/<业务日>/03_step2/"；新增一条"运行后核对 manifest step2=COMPLETED 与三表恒等式"。保留全部必留字符串与既有验收恒等式。
  - README 第 10 行输出列改为：`workdays/<YYYY-MM-DD>/03_step2/ 三份 STEP2_*.xlsx + workdays/<YYYY-MM-DD>/manifest.json`。
  - TESTING.md：§2 第二步命令行扩为 `python -m pytest tests\test_export_current_monitor_status.py tests\test_step2_outputs.py tests\test_step2_explicit_gate.py tests\test_workday_context.py -q`（单行替换原两行之一，不增行）；§5 命令 `python scripts\export_current_monitor_status.py` 后补 `--business-date <YYYY-MM-DD>` 说明；§5 验收列表并入三表投影/manifest/原子重跑要点（合并旧 5、6 条腾出行数）；编译检查块追加 `app\services\workday_context.py`（+1 行必须由前述合并补偿）。
- [ ] **Step 3: 验证文档预算** Run: `E:\Python314\python.exe -m pytest tests\test_context_documents.py -q --basetemp=.pytest_tmp` → PASS；再逐文件数行确认 ≤ 预算。
- [ ] **Step 4: Commit** `git add docs README.md TESTING.md && git commit -m "docs(step2): workflow doc and runbook for user-triggered three-output step2"`

---

### Task 5: CI gate + 全量门禁

**Files:**
- Modify: `.github/workflows/critical-safety-tests.yml`

- [ ] **Step 1: 把 4 个测试文件加入 Run critical safety tests 清单**（紧随 `tests/test_customer_price_semantics.py` 之前插入，并同步更新注释中的文件数 50→54 与收集/通过数描述保持"清单=CI_SAFE"的表述真实）：

```text
          tests/test_export_current_monitor_status.py
          tests/test_step2_outputs.py
          tests/test_step2_explicit_gate.py
          tests/test_workday_context.py
```

- [ ] **Step 2: 定向门禁**

```bash
E:\Python314\python.exe -m pytest tests\test_export_current_monitor_status.py tests\test_step2_outputs.py tests\test_step2_explicit_gate.py tests\test_workday_context.py tests\test_context_documents.py -q --basetemp=.pytest_tmp
```

- [ ] **Step 3: STEP1 关键回归**（证明 STEP1 unchanged）

```bash
E:\Python314\python.exe -m pytest tests\test_incremental_price_audit.py tests\test_incremental_audit_runtime.py tests\test_closeout_before_1830.py tests\test_closeout_transient_recovery.py tests\test_incremental_price_audit_finalize.py tests\test_dashboard_api.py -q --basetemp=.pytest_tmp --timeout=600 --timeout-method=thread
```

- [ ] **Step 4: 静态检查**

```bash
git diff --check
E:\Python314\python.exe -m py_compile scripts\export_current_monitor_status.py app\services\workday_context.py
```

- [ ] **Step 5: 全量套件**

```bash
E:\Python314\python.exe -m pytest tests -q --basetemp=.pytest_tmp --timeout=600 --timeout-method=thread
```

若有失败：在干净 `a1baf01` worktree 用同一命令对照，输出 BRANCH_FAILURES / BASELINE_FAILURES / NEW_REAL_REGRESSIONS，禁止写"known failures"了事。

- [ ] **Step 6: Commit** `git add .github\workflows\critical-safety-tests.yml && git commit -m "ci(step2): add step2 output and explicit-gate tests to critical safety suite"`

---

## Self-Review 结论（已核对）

- 规格 §0-§51 逐节映射：§4-§6 冻结语义→Global Constraints+现有测试不动；§7-§12 单快照三表调和→Task 2；§8/§15/§32 固定名/位置/Sheet 名→Task 2；§13 business_date→Task 2 CLI；§14 workday 布局→Task 1；§16 legacy 取代（无代码读者，审计证据在规格报告中）→Task 4 文档；§17/§18/§20/§21 manifest/原子/重跑→Task 1+2；§19 失败语义→Task 2（RUNNING 先行，查询失败也记 FAILED）；§22-§26/§37-§38 显式闸门与 STEP3 隔离→Task 3+Task 2 测试；§27/§28 workflow 文档→Task 4；§29/§30 workday helper/相对路径→Task 1；§31 样式复用→`_format_sheet`；§33 五 Sheet 调和→Task 2 测试；§34-§40 TDD 清单→Task 1-3 测试；§41 全 fake→Global Constraints；§42 不碰 #10/#11→Global Constraints；§43-§45 门禁→Task 5；§47 CI→Task 5；§48 文档预算→Task 4；§49 禁止事项→Global Constraints+DO_NOT。
- 类型/命名一致性：`workday_root(business_date, base)`；`STEP2_OUTPUT_NAMES` keys = monitor/pushable/non_pushable 与 manifest outputs/files/counts 键一致；`PUSHABLE_COL=17` 与现有 records 结构一致。
- 无占位符；所有新增代码在计划中给出全文或逐行等价指令。
